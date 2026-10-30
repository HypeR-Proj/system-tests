/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

#include <hyper/dlfcn.h>
#include <hyper/startup.h>
#include <hyper/std.h>
#include <hyper/stream.h>
#include <hyper/launch.h>
#include <hyper/syscall.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#define PROBE_VALUE UINT64_C(0x48595045525f444c)

typedef uint64_t (*probe_function_t)(void);

static int startup_heap_ok;
static void initialize_heap_probe(void) __attribute__((constructor));

static void initialize_heap_probe(void)
{
	unsigned char *bytes = calloc(128, 1);
	if (bytes != NULL) {
		startup_heap_ok = 1;
		for (size_t i = 0; i < 128; ++i) {
			if (bytes[i] != 0)
				startup_heap_ok = 0;
		}
		free(bytes);
	}
}

static int write_result(const char *message, size_t length)
{
	while (length) {
		size_t actual = 0;
		if (hyper_runtime_stdio_write(1, message, length, &actual) || !actual)
			return 1;
		message += actual;
		length -= actual;
	}
	return hyper_runtime_stream_fence(1) ? 1 : 0;
}

/* The fixture declares a 512 KiB PT_GNU_STACK. Touch more than the old
 * 256 KiB default, proving that the main executable controls the initial
 * stack even when it starts through an interpreter. */
static int check_initial_stack(void) __attribute__((noinline));

static int check_initial_stack(void)
{
	volatile unsigned char bytes[320 * 1024];
	for (size_t i = 0; i < sizeof(bytes); ++i)
		bytes[i] = (unsigned char)i;
	for (size_t i = 0; i < sizeof(bytes); ++i) {
		if (bytes[i] != (unsigned char)i)
			return 0;
	}
	return 1;
}

static int check_opaque_startup_data(const hyper_startup_t *startup)
{
	const uint32_t purposes[] = {
		HYPER_NATIVE_STARTUP_HANDLE_PURPOSE_TASK_FACTORY,
		HYPER_NATIVE_STARTUP_HANDLE_PURPOSE_TASK_GROUP,
		HYPER_NATIVE_STARTUP_HANDLE_PURPOSE_RESOURCE_DOMAIN,
		HYPER_NATIVE_STARTUP_HANDLE_PURPOSE_ROOT_DIRECTORY,
	};
	hyper_native_handle_t handles[4];
	for (size_t i = 0; i < 4; ++i)
		if (hyper_startup_find_handle(startup, purposes[i], &handles[i]))
			return 0;
	const char path[] = "/bin/echo";
	const uint64_t required[] = {HYPER_NATIVE_RIGHT_READ, HYPER_NATIVE_RIGHT_EXECUTE,
				     HYPER_NATIVE_RIGHT_PROTECT, HYPER_NATIVE_RIGHT_DUPLICATE,
				     HYPER_NATIVE_RIGHT_TRANSFER};
	const uint64_t executable_rights =
		HYPER_NATIVE_RIGHT_READ | HYPER_NATIVE_RIGHT_EXECUTE | HYPER_NATIVE_RIGHT_PROTECT |
		HYPER_NATIVE_RIGHT_DUPLICATE | HYPER_NATIVE_RIGHT_TRANSFER;
	/* Caller-chosen loader must never upgrade a reduced executable capability. */
	for (size_t i = 0; i < sizeof(required) / sizeof(required[0]); ++i) {
		hyper_call_result_t restricted = hyper_directory_open_file(
			handles[3], path, sizeof(path) - 1, executable_rights & ~required[i]);
		if (restricted.status)
			return 0;
		hyper_call_result_t denied = hyper_process_builder_create(
			handles[0], handles[1], handles[2], restricted.value0,
			hyper_runtime_capability(HYPER_LAUNCH_CHILD_LOADER));
		(void)hyper_handle_close(restricted.value0);
		if (denied.status != HYPER_NATIVE_STATUS_ACCESS_DENIED) {
			if (!denied.status)
				(void)hyper_process_builder_abort(denied.value0);
			return 0;
		}
	}
	hyper_call_result_t file =
		hyper_directory_open_file(handles[3], path, sizeof(path) - 1, executable_rights);
	if (file.status)
		return 0;
	/* Loader selection cannot expose readable ELF segments from EXECUTE alone. */
	hyper_call_result_t execute_only_loader = hyper_handle_duplicate(
		hyper_runtime_capability(HYPER_LAUNCH_CHILD_LOADER), HYPER_NATIVE_RIGHT_EXECUTE);
	if (execute_only_loader.status) {
		(void)hyper_handle_close(file.value0);
		return 0;
	}
	hyper_call_result_t denied_loader = hyper_process_builder_create(
		handles[0], handles[1], handles[2], file.value0, execute_only_loader.value0);
	(void)hyper_handle_close(execute_only_loader.value0);
	if (denied_loader.status != HYPER_NATIVE_STATUS_ACCESS_DENIED) {
		if (!denied_loader.status)
			(void)hyper_process_builder_abort(denied_loader.value0);
		(void)hyper_handle_close(file.value0);
		return 0;
	}
	hyper_call_result_t builder =
		hyper_process_builder_create(handles[0], handles[1], handles[2], file.value0,
					     hyper_runtime_capability(HYPER_LAUNCH_CHILD_LOADER));
	(void)hyper_handle_close(file.value0);
	if (builder.status)
		return 0;
	/* The kernel must accept opaque bytes and a subsequent empty replacement,
	 * without parsing either or trying to account a zero-byte allocation. */
	const unsigned char bytes[] = {0xff, 0, 0xfe};
	int ok = !hyper_process_builder_set_data(builder.value0, bytes, sizeof(bytes)) &&
		 !hyper_process_builder_set_data(builder.value0, NULL, 0);
	return !hyper_process_builder_abort(builder.value0) && ok;
}

int hyper_main(const hyper_startup_t *startup)
{
	static const char success[] = "HYPER_DYNAMIC_LINK_OK\n";
	if (!startup_heap_ok || !check_initial_stack() || !check_opaque_startup_data(startup))
		return 1;
	hyper_native_handle_t directory = 0;
	if (hyper_startup_find_handle(startup,
				      HYPER_NATIVE_STARTUP_HANDLE_PURPOSE_DYNAMIC_LIBRARY_DIRECTORY,
				      &directory) != HYPER_NATIVE_STATUS_OK) {
		return 1;
	}
	void *object = hyper_dlopen_at(directory, "libdynamic-probe.so",
				       HYPER_RTLD_NOW | HYPER_RTLD_LOCAL);
	if (object == NULL) {
		return 1;
	}
	void *symbol = hyper_dlsym(object, "hyper_dynamic_probe");
	if (symbol == NULL) {
		(void)hyper_dlclose(object);
		return 1;
	}
	void *heap_symbol = hyper_dlsym(object, "hyper_dynamic_heap_probe");
	if (heap_symbol == NULL) {
		(void)hyper_dlclose(object);
		return 1;
	}
	unsigned char *memory = ((void *(*)(void))heap_symbol)();
	if (memory == NULL || (uintptr_t)memory % 4096 != 0) {
		(void)hyper_dlclose(object);
		return 1;
	}
	for (size_t i = 0; i < 8192; ++i) {
		if (memory[i] != 0x5a) {
			free(memory);
			(void)hyper_dlclose(object);
			return 1;
		}
	}
	free(memory);
	probe_function_t probe = (probe_function_t)symbol;
	int status =
		probe() == PROBE_VALUE ? write_result(success, sizeof(success) - 1) : 1;
	if (hyper_dlclose(object) != HYPER_NATIVE_STATUS_OK) {
		status = 1;
	}
	return status;
}
