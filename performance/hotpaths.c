/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 * Manual Native hot-path measurements; no timed console output.
 */
#include "platform.h"
#include <hyper/heap.h>
#include <hyper/startup.h>
#include <hyper/std.h>
#include <hyper/syscall.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#define REPS 5
static volatile uint64_t sink;
static unsigned char io[128 * 1024] __attribute__((aligned(4096)));

static void output_text(const char *s)
{
	bench_output(s, strlen(s));
}

static void output_number(uint64_t n)
{
	char b[20];
	size_t i = sizeof b;
	do {
		b[--i] = (char)('0' + n % 10);
		n /= 10;
	} while (n);
	bench_output(b + i, sizeof b - i);
}

static unsigned char msg[4096], reply[4096];
static uint64_t cycles = 1;

static void ok(int64_t s)
{
	if (s)
		bench_fail("probe operation", s);
}

static uint64_t value(hyper_call_result_t r)
{
	ok(r.status);
	return r.value0;
}

static void result(const char *n, unsigned s, uint64_t units, uint64_t ns)
{
	output_text("PROBE,");
	output_text(n);
	output_text(",");
	output_number(s);
	output_text(",");
	output_number(units);
	output_text(",");
	output_number(ns);
	output_text("\n");
}

static uint64_t number(const char *s)
{
	uint64_t x = 0;
	if (!*s)
		bench_fail("number", 0);
	for (; *s; s++) {
		if (*s < '0' || *s > '9' || x > (UINT64_MAX - (unsigned)(*s - '0')) / 10)
			bench_fail("number", 0);
		x = x * 10 + *s - '0';
	}
	return x;
}

static uint64_t now(void)
{
	return value(hyper_clock_get_monotonic());
}

static void closeh(uint64_t h)
{
	ok(hyper_handle_close(h));
}

static void wait_channel(uint64_t h, uint32_t signals, uint64_t *deadline)
{
	if (!*deadline)
		*deadline = now() + 10000000000ULL;
	uint64_t observed = value(hyper_object_wait_one(
		h, signals | HYPER_NATIVE_SIGNAL_BYTE_CHANNEL_PEER_CLOSED, *deadline));
	if (observed & HYPER_NATIVE_SIGNAL_BYTE_CHANNEL_PEER_CLOSED)
		bench_fail("peer closed", 0);
}

static uint64_t receive(uint64_t h, void *b, size_t bytes)
{
	uint64_t deadline = 0;
	for (;;) {
		hyper_call_result_t r = hyper_byte_channel_read(h, b, bytes);
		if (!r.status)
			return r.value0;
		if (r.status != HYPER_NATIVE_STATUS_WOULD_BLOCK)
			bench_fail("channel read", r.status);
		wait_channel(h, HYPER_NATIVE_SIGNAL_BYTE_CHANNEL_READABLE, &deadline);
	}
}

static void sendmsg(uint64_t h, const void *b, size_t bytes)
{
	uint64_t deadline = 0;
	for (;;) {
		int64_t status = hyper_byte_channel_write(h, b, bytes);
		if (!status)
			return;
		if (status != HYPER_NATIVE_STATUS_WOULD_BLOCK && status != HYPER_NATIVE_STATUS_BUSY)
			bench_fail("channel write", status);
		wait_channel(h, HYPER_NATIVE_SIGNAL_BYTE_CHANNEL_WRITABLE, &deadline);
	}
}

static void syscalls(void)
{
	uint64_t n = 100000 * cycles, t;
	hyper_call_result_t p = hyper_byte_channel_create();
	ok(p.status);
	for (unsigned s = 0; s < REPS; s++) {
		t = now();
		for (uint64_t i = 0; i < n; i++)
			sink = value(hyper_abi_query());
		result("sys_abi_query", s, n, now() - t);
		t = now();
		for (uint64_t i = 0; i < n; i++)
			sink = value(hyper_clock_get_monotonic());
		result("sys_clock", s, n, now() - t);
		t = now();
		for (uint64_t i = 0; i < n; i++)
			sink = value(hyper_process_get_current_id());
		result("sys_getpid", s, n, now() - t);
		t = now();
		for (uint64_t i = 0; i < n; i++)
			ok(hyper_thread_yield());
		result("yield_single", s, n, now() - t);
	}
	unsigned sizes[] = {32, 256, 1152, 4096};
	for (unsigned j = 0; j < 4; j++)
		for (unsigned s = 0; s < REPS; s++) {
			unsigned size = sizes[j];
			memset(msg, 0xa5, size);
			uint64_t count = 5000 * cycles;
			t = now();
			for (uint64_t i = 0; i < count; i++) {
				sendmsg(p.value0, msg, size);
				if (receive(p.value1, reply, size) != size)
					bench_fail("channel size", 0);
			}
			uint64_t elapsed = now() - t;
			if (memcmp(msg, reply, size))
				bench_fail("channel content", 0);
			const char *names[] = {"channel_local_32", "channel_local_256",
					       "channel_local_1152", "channel_local_4096"};
			result(names[j], s, count, elapsed);
		}
	closeh(p.value0);
	closeh(p.value1);
}

struct peer {
	uint64_t h, n;
};

static void peer(void *arg)
{
	struct peer *p = arg;
	unsigned char b[32];
	ok(hyper_thread_set_current_name("probe-ping-peer", 15));
	for (uint64_t i = 0; i < p->n; i++) {
		if (receive(p->h, b, sizeof b) != sizeof b)
			bench_fail("peer size", 0);
		sendmsg(p->h, b, sizeof b);
	}
}

static _Atomic uint32_t turn;
static uint64_t futex_n;

static void wait_turn(uint32_t desired)
{
	for (;;) {
		uint32_t v = atomic_load_explicit(&turn, memory_order_acquire);
		if (v == desired)
			return;
		if (!hyper_runtime_wait_u32((const uint32_t *)&turn, v, now() + 10000000000ULL))
			bench_fail("atomic wait timeout", 0);
	}
}

static void set_turn(uint32_t v)
{
	atomic_store_explicit(&turn, v, memory_order_release);
	hyper_runtime_wake_u32((const uint32_t *)&turn, 1);
}

static void futex_peer(void *arg)
{
	(void)arg;
	for (uint64_t i = 0; i < futex_n; i++) {
		wait_turn(1);
		set_turn(0);
	}
}

struct job {
	unsigned mode;
	uint64_t n, result, elapsed;
};

static _Atomic uint32_t ready, go;

static void job(void *arg)
{
	struct job *j = arg;
	/* Keep one live block in every participating arena. Measure contention and
	 * block lookup, not repeated teardown of an otherwise empty heap. */
	void *anchor = j->mode == 2 ? malloc(64) : NULL;
	if (j->mode == 2 && !anchor)
		bench_fail("worker anchor", 0);
	atomic_fetch_add(&ready, 1);
	hyper_runtime_wake_u32((const uint32_t *)&ready, 1);
	while (!atomic_load_explicit(&go, memory_order_acquire))
		if (!hyper_runtime_wait_u32((const uint32_t *)&go, 0, now() + 10000000000ULL))
			bench_fail("worker start timeout", 0);
	uint64_t t = now(), x = 0x243f6a8885a308d3ULL;
	if (j->mode == 0) {
		for (uint64_t i = 0; i < j->n; i++) {
			x ^= x << 13;
			x ^= x >> 7;
			x ^= x << 17;
		}
		j->result = x;
	} else if (j->mode == 1) {
		for (uint64_t i = 0; i < j->n; i++)
			ok(hyper_thread_yield());
	} else {
		void *ptrs[64];
		for (uint64_t k = 0; k < j->n; k++) {
			for (unsigned i = 0; i < 64; i++) {
				ptrs[i] = malloc(64);
				if (!ptrs[i])
					bench_fail("malloc worker", 0);
				*(volatile unsigned char *)ptrs[i] = 0x5a;
			}
			for (unsigned i = 0; i < 64; i++)
				free(ptrs[i]);
		}
	}
	j->elapsed = now() - t;
	free(anchor);
}

static void parallel(unsigned mode)
{
	unsigned workers[] = {1, 2, 4, 8};
	const char *compute[] = {"compute_t1", "compute_t2", "compute_t4", "compute_t8"};
	const char *yields[] = {"yield_t1", "yield_t2", "yield_t4", "yield_t8"};
	const char *allocs[] = {"alloc64_t1", "alloc64_t2", "alloc64_t4", "alloc64_t8"};
	for (unsigned k = 0; k < 4; k++)
		for (unsigned s = 0; s < REPS; s++) {
			struct job jobs[8] = {0};
			uintptr_t tokens[8];
			atomic_store(&ready, 0);
			atomic_store(&go, 0);
			uint64_t count = (mode == 0 ? 20000000 : mode == 1 ? 10000 : 400) * cycles;
			for (unsigned i = 0; i < workers[k]; i++) {
				jobs[i].mode = mode;
				jobs[i].n = count;
				ok(__hyper_std_thread_spawn(65536, job, &jobs[i], &tokens[i]));
			}
			while (atomic_load(&ready) != workers[k]) {
				uint32_t old = atomic_load(&ready);
				if (old != workers[k])
					hyper_runtime_wait_u32((const uint32_t *)&ready, old,
							       now() + 10000000000ULL);
			}
			uint64_t t = now();
			atomic_store_explicit(&go, 1, memory_order_release);
			hyper_runtime_wake_u32((const uint32_t *)&go, UINT32_MAX);
			for (unsigned i = 0; i < workers[k]; i++)
				ok(__hyper_std_thread_join(tokens[i]));
			uint64_t elapsed = now() - t;
			if (mode == 0) {
				for (unsigned i = 1; i < workers[k]; i++)
					if (jobs[i].result != jobs[0].result)
						bench_fail("compute consistency", 0);
				sink = jobs[0].result;
			}
			result(mode == 0   ? compute[k]
			       : mode == 1 ? yields[k]
					   : allocs[k],
			       s, count * workers[k] * (mode == 2 ? 64 : 1), elapsed);
		}
}

static void scheduler(void)
{
	for (unsigned s = 0; s < REPS; s++) {
		uint64_t n = 3000 * cycles;
		hyper_call_result_t p = hyper_byte_channel_create();
		ok(p.status);
		struct peer par = {p.value1, n};
		uintptr_t thread;
		ok(__hyper_std_thread_spawn(65536, peer, &par, &thread));
		memset(msg, 0xa5, 32);
		uint64_t t = now();
		for (uint64_t i = 0; i < n; i++) {
			sendmsg(p.value0, msg, 32);
			if (receive(p.value0, reply, 32) != 32)
				bench_fail("reply size", 0);
		}
		uint64_t elapsed = now() - t;
		ok(__hyper_std_thread_join(thread));
		if (memcmp(msg, reply, 32))
			bench_fail("reply content", 0);
		result("channel_thread_rtt_32", s, n, elapsed);
		closeh(p.value0);
		closeh(p.value1);
		atomic_store(&turn, 0);
		futex_n = n;
		ok(__hyper_std_thread_spawn(65536, futex_peer, 0, &thread));
		t = now();
		for (uint64_t i = 0; i < n; i++) {
			set_turn(1);
			wait_turn(0);
		}
		elapsed = now() - t;
		ok(__hyper_std_thread_join(thread));
		result("atomic_thread_rtt", s, n, elapsed);
	}
	parallel(0);
	parallel(1);
}

static void memory(void)
{
	unsigned sizes[] = {64, 256, 4096, 65536};
	const char *names[] = {"alloc_free_64", "alloc_free_256", "alloc_free_4096",
			       "alloc_free_65536"};
	/* One retained small allocation prevents measuring only empty-heap teardown. */
	void *anchor = malloc(64);
	if (!anchor)
		bench_fail("anchor", 0);
	for (unsigned j = 0; j < 4; j++)
		for (unsigned s = 0; s < REPS; s++) {
			uint64_t count = (j < 2 ? 2000 : 200) * cycles;
			void *p[64];
			uint64_t t = now();
			for (uint64_t k = 0; k < count; k++) {
				for (unsigned i = 0; i < 64; i++) {
					p[i] = malloc(sizes[j]);
					if (!p[i])
						bench_fail("malloc", 0);
					*(volatile unsigned char *)p[i] = 1;
				}
				for (unsigned i = 0; i < 64; i++)
					free(p[i]);
			}
			result(names[j], s, count * 64, now() - t);
		}
	for (unsigned s = 0; s < REPS; s++) {
		size_t size = 32 * 1024 * 1024;
		uint64_t t = now();
		void *p = malloc(size);
		if (!p)
			bench_fail("malloc32m", 0);
		uint64_t allocated = now();
		for (size_t i = 0; i < size; i += 4096)
			*((volatile unsigned char *)p + i) = 0x5a;
		uint64_t touched = now();
		for (size_t i = 0; i < size; i += 4096) {
			if (*((volatile unsigned char *)p + i) != 0x5a)
				bench_fail("page touch", 0);
		}
		uint64_t hot = now();
		free(p);
		uint64_t end = now();
		result("heap32m_alloc", s, size, allocated - t);
		result("heap32m_touch", s, size, touched - allocated);
		result("heap32m_hot_read", s, size, hot - touched);
		result("heap32m_free", s, size, end - hot);
	}
	unsigned vmos[] = {4096, 65536};
	const char *vn[] = {"vmo_create_close_4k", "vmo_create_close_64k"};
	for (unsigned j = 0; j < 2; j++)
		for (unsigned s = 0; s < REPS; s++) {
			uint64_t count = 1000 * cycles, t = now();
			for (uint64_t i = 0; i < count; i++) {
				uint64_t h = value(hyper_vmo_create(vmos[j]));
				closeh(h);
			}
			result(vn[j], s, count, now() - t);
		}
	free(anchor);
	parallel(2);
}

static void allocmap(void)
{
	void *anchor = malloc(64);
	if (!anchor)
		bench_fail("anchor", 0);
	const size_t sizes[] = {0, 1024 * 1024, 32 * 1024 * 1024, 64 * 1024 * 1024};
	const char *names[] = {"map_churn_hold0", "map_churn_hold1m", "map_churn_hold32m",
			       "map_churn_hold64m"};
	for (unsigned j = 0; j < 4; j++) {
		void *held = sizes[j] ? malloc(sizes[j]) : 0;
		if (sizes[j] && !held)
			bench_fail("held malloc", 0);
		if (held) {
			for (size_t off = 0; off < sizes[j]; off += 4096)
				*((volatile unsigned char *)held + off) = 0x33;
		}
		for (unsigned s = 0; s < REPS; s++) {
			uint64_t count = 50 * cycles, t = now();
			for (uint64_t k = 0; k < count; k++) {
				void *ptr = malloc(65536);
				if (!ptr)
					bench_fail("churn malloc", 0);
				*(volatile unsigned char *)ptr = 0x77;
				free(ptr);
			}
			result(names[j], s, count, now() - t);
		}
		free(held);
	}
	free(anchor);
}

/* Raw finite distributions avoid turning CPU PMU samples into wall latency.
 * Clock calls are part of the cost; report an empty timed pair as a control. */
#define LATENCY_SAMPLES 512

static void latency(const char *path, uint64_t bytes)
{
	uint64_t samples[LATENCY_SAMPLES];
	uint64_t file = path ? bench_open(path, BENCH_READ_ONLY) : 0;
	for (unsigned i = 0; i < LATENCY_SAMPLES; i++) {
		uint64_t t = now();
		samples[i] = now() - t;
	}
	for (unsigned i = 0; i < LATENCY_SAMPLES; i++)
		result("clock_pair_ns", i, 1, samples[i]);
	if (path) {
		if (bytes < sizeof io || bytes % 4096)
			bench_fail("read-latency requires page-aligned file size >= 128 KiB", 0);
		uint32_t seed = 12345;
		for (unsigned i = 0; i < LATENCY_SAMPLES; i++) {
			seed = seed * 1664525u + 1013904223u;
			uint64_t offset = (seed % (bytes / 4096)) * 4096, t = now();
			bench_read(file, offset, io, 4096);
			samples[i] = now() - t;
		}
		for (unsigned i = 0; i < LATENCY_SAMPLES; i++)
			result("read_4k_latency_ns", i, 1, samples[i]);
		bench_close(file);
	} else {
		atomic_store(&turn, 0);
		futex_n = LATENCY_SAMPLES;
		uintptr_t thread;
		ok(__hyper_std_thread_spawn(65536, futex_peer, 0, &thread));
		for (unsigned i = 0; i < LATENCY_SAMPLES; i++) {
			uint64_t t = now();
			set_turn(1);
			wait_turn(0);
			samples[i] = now() - t;
		}
		ok(__hyper_std_thread_join(thread));
		for (unsigned i = 0; i < LATENCY_SAMPLES; i++)
			result("atomic_rtt_latency_ns", i, 1, samples[i]);
	}
}

int hyper_main(const hyper_startup_t *s)
{
	if (s->argument_count < 2 || s->argument_count > 4)
		bench_fail("usage: hotpaths MODE [SCALE] | read-latency PATH BYTES", 0);
	const char *mode = s->arguments[1];
	if (!strcmp(mode, "read-latency")) {
		if (s->argument_count != 4)
			bench_fail("read-latency PATH BYTES", 0);
		latency(s->arguments[2], number(s->arguments[3]));
		return 0;
	}
	if (s->argument_count > 3)
		bench_fail("MODE [SCALE]", 0);
	if (s->argument_count == 3)
		cycles = number(s->arguments[2]);
	if (cycles < 1 || cycles > 20)
		bench_fail("scale must be 1..20", 0);
	output_text("PROBE_BEGIN,");
	output_text(mode);
	output_text("\n");
	if (!strcmp(mode, "syscall"))
		syscalls();
	else if (!strcmp(mode, "scheduler"))
		scheduler();
	else if (!strcmp(mode, "memory"))
		memory();
	else if (!strcmp(mode, "allocmap"))
		allocmap();
	else if (!strcmp(mode, "alloc-parallel"))
		parallel(2);
	else if (!strcmp(mode, "latency"))
		latency(NULL, 0);
	else
		bench_fail("unknown mode", 0);
	output_text("PROBE_END\n");
	return 0;
}
