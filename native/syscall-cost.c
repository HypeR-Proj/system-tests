/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

/* Opt-in Native syscall/IPC comparison; no emulator timing pass threshold. */
#include <hyper/startup.h>
#include <hyper/invocation.h>
#include <hyper/std.h>
#include <hyper/stream.h>
#include <hyper/syscall.h>
#include <stdint.h>
#include <string.h>

#define ITERATIONS UINT64_C(4096)
#define REPETITIONS 9

static int output(const char *bytes, size_t size)
{
	while (size) {
		size_t written = 0;
		if (hyper_runtime_stdio_write(1, bytes, size, &written) || !written ||
		    written > size)
			return 1;
		bytes += written;
		size -= written;
	}
	return 0;
}

static int failure(const char *message)
{
	(void)output(message, strlen(message));
	(void)hyper_runtime_stream_fence(1);
	return 1;
}

static size_t decimal(char *destination, uint64_t number)
{
	char reverse[20];
	size_t size = 0;
	do {
		reverse[size++] = (char)('0' + number % 10);
		number /= 10;
	} while (number);
	for (size_t index = 0; index != size; ++index)
		destination[index] = reverse[size - index - 1];
	return size;
}

static int report(const char *stage, unsigned int repetition, uint64_t elapsed)
{
	char line[160];
	const char *prefix = "SYSCALL-COST stage=";
	size_t used = strlen(prefix);
	memcpy(line, prefix, used);
	size_t size = strlen(stage);
	memcpy(line + used, stage, size);
	used += size;
	memcpy(line + used, " repetition=", 12);
	used += 12;
	used += decimal(line + used, repetition);
	memcpy(line + used, " iterations=", 12);
	used += 12;
	used += decimal(line + used, ITERATIONS);
	memcpy(line + used, " elapsed_ns=", 12);
	used += 12;
	used += decimal(line + used, elapsed);
	line[used++] = '\n';
	return output(line, used);
}

static int query(uint64_t iterations, hyper_native_handle_t unused)
{
	(void)unused;
	for (uint64_t index = 0; index != iterations; ++index) {
		hyper_call_result_t result = hyper_abi_query();
		if (result.status || result.value0 != HYPER_NATIVE_ABI_REVISION ||
		    result.value1 != HYPER_NATIVE_FEATURE_CORE)
			return 1;
	}
	return 0;
}

static int event(uint64_t iterations, hyper_native_handle_t handle)
{
	for (uint64_t index = 0; index != iterations; ++index) {
		hyper_call_result_t signal = hyper_native_invoke6(
			handle, HYPER_NATIVE_OP_EVENT_SIGNAL, 0, 1, 0, 0, 0, 0);
		if (signal.status)
			return 1;
		hyper_call_result_t wait = hyper_object_wait_one(handle, 1, UINT64_MAX);
		if (wait.status || wait.value0 != 1)
			return 1;
	}
	return 0;
}

static hyper_native_handle_t channel_peer;

static int channel(uint64_t iterations, hyper_native_handle_t handle)
{
	for (uint64_t index = 0; index != iterations; ++index) {
		uint64_t received = UINT64_MAX;
		if (hyper_byte_channel_write(handle, &index, sizeof(index)))
			return 1;
		hyper_call_result_t read =
			hyper_byte_channel_read(channel_peer, &received, sizeof(received));
		if (read.status || read.value0 != sizeof(received) || received != index)
			return 1;
	}
	return 0;
}

static int measure(const char *name, int (*operation)(uint64_t, hyper_native_handle_t),
		   hyper_native_handle_t handle)
{
	if (operation(256, handle))
		return 1;
	for (unsigned int repetition = 0; repetition != REPETITIONS; ++repetition) {
		hyper_call_result_t before = hyper_clock_get_monotonic();
		if (before.status || operation(ITERATIONS, handle))
			return 1;
		hyper_call_result_t after = hyper_clock_get_monotonic();
		if (after.status || after.value0 < before.value0 ||
		    report(name, repetition, after.value0 - before.value0))
			return 1;
	}
	return 0;
}

int hyper_main(const hyper_startup_t *startup)
{
	(void)startup;
	hyper_call_result_t created = hyper_native_invoke6(
		hyper_native_object_factory(), HYPER_NATIVE_OP_EVENT_CREATE, 0, 0, 0, 0, 0, 0);
	if (created.status)
		return failure("SYSCALL-COST FAIL stage=event-create\n");
	hyper_call_result_t endpoints = hyper_byte_channel_create();
	if (endpoints.status) {
		(void)hyper_handle_close(created.value0);
		return failure("SYSCALL-COST FAIL stage=channel-create\n");
	}
	channel_peer = endpoints.value1;
	int failed = measure("abi-query", query, 0) ||
		     measure("ready-event", event, created.value0) ||
		     measure("channel-roundtrip", channel, endpoints.value0);
	(void)hyper_handle_close(endpoints.value0);
	(void)hyper_handle_close(endpoints.value1);
	(void)hyper_handle_close(created.value0);
	static const char passed[] = "SYSCALL-COST PASS\n";
	if (failed)
		return failure("SYSCALL-COST FAIL stage=workload\n");
	return output(passed, sizeof(passed) - 1) || hyper_runtime_stream_fence(1) ? 1 : 0;
}
