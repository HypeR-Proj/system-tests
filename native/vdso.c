/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include <hyper/vdso.h>
#include <hyper/invocation.h>
#include <hyper/std.h>
#include <hyper/stream.h>
#include <hyper/elf.h>
#include <hyper/thread.h>
#include <string.h>

static hyper_call_result_t slow(uint64_t operation)
{
	return hyper_native_invoke6(hyper_native_system_info(), operation, 0, 0, 0, 0, 0, 0);
}

static int ordered(hyper_call_result_t a, hyper_call_result_t b)
{
	return (int64_t)a.value0 < (int64_t)b.value0 ||
	       (a.value0 == b.value0 && a.value1 <= b.value1);
}

static int clocks(void)
{
	uint64_t previous = 0;
	for (unsigned i = 0; i < 1024; ++i) {
		hyper_call_result_t before = slow(HYPER_NATIVE_OP_CLOCK_GET_MONOTONIC);
		hyper_call_result_t fast = hyper_vdso_monotonic();
		hyper_call_result_t after = slow(HYPER_NATIVE_OP_CLOCK_GET_MONOTONIC);
		if (before.status || after.status || before.value0 < previous)
			return 3;
		previous = after.value0;
#if defined(__aarch64__)
		if (fast.status || fast.value0 < before.value0 || fast.value0 > after.value0)
			return 4;
#else
		if (fast.status != HYPER_NATIVE_STATUS_NOT_SUPPORTED)
			return 5;
#endif
		before = slow(HYPER_NATIVE_OP_CLOCK_GET_REALTIME);
		fast = hyper_clock_get_realtime();
		after = slow(HYPER_NATIVE_OP_CLOCK_GET_REALTIME);
		if (before.status || fast.status || after.status || !ordered(before, fast) ||
		    !ordered(fast, after))
			return 6;
		if (i % 16 == 0 && hyper_thread_yield())
			return 11;
	}
	return 0;
}

static void clock_worker(void *result)
{
	*(int *)result = clocks();
}

int main(int argc, char **argv)
{
	if (argc == 2) {
		int expected = !strcmp(argv[1], "active");
		if ((!expected && strcmp(argv[1], "idle")) ||
		    !!hyper_trace_ipc_active() != expected)
			return 10;
	}
	const hyper_startup_t *startup = hyper_runtime_startup();
	uintptr_t base = 0;
	if (!startup)
		return 1;
	for (size_t i = 0; i < startup->auxiliary_count; ++i)
		if (startup->auxiliary[i].key == HYPER_NATIVE_VDSO_AUXV_BASE)
			base = startup->auxiliary[i].value;
	if (!base || ((const Elf64_Ehdr *)base)->type != 3)
		return 2;
	uintptr_t threads[4] = {0};
	int results[4] = {0}, failure = 0;
	for (unsigned i = 0; i < 4; ++i) {
		if (hyper_runtime_thread_spawn(65536, clock_worker, &results[i], &threads[i])) {
			failure = 12;
			break;
		}
	}
	for (unsigned i = 0; i < 4; ++i) {
		if (threads[i] && hyper_runtime_thread_join(threads[i]))
			failure = 13;
		if (results[i])
			failure = results[i];
	}
	if (failure)
		return failure;
	/* Runtime fallback keeps the same clock domain when discovery is absent. */
	hyper_startup_t absent = {0};
	hyper_vdso_initialize(&absent);
	if (!hyper_trace_ipc_active() ||
	    hyper_vdso_monotonic().status != HYPER_NATIVE_STATUS_NOT_SUPPORTED)
		return 7;
	if (hyper_clock_get_monotonic().status || hyper_clock_get_realtime().status)
		return 8;
	hyper_vdso_initialize(startup);
	const char message[] = "VDSO_CLOCK_FALLBACK_OK\n";
	size_t written = 0;
	if (hyper_runtime_stdio_write(1, message, sizeof(message) - 1, &written) ||
	    written != sizeof(message) - 1 || hyper_runtime_stream_fence(1))
		return 9;
	return 0;
}
