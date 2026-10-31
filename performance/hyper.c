/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "platform.h"
#include <hyper/startup.h>
#include <hyper/std.h>
#include <hyper/syscall.h>
#include <string.h>

static void output_number(uint64_t value)
{
	char digits[20];
	size_t first = sizeof(digits);
	do {
		digits[--first] = '0' + value % 10;
		value /= 10;
	} while (value);
	bench_output(digits + first, sizeof(digits) - first);
}

_Noreturn void bench_fail(const char *operation, int64_t error)
{
	bench_output("BENCH-FAIL: ", 12);
	bench_output(operation, strlen(operation));
	bench_output(" status=", 8);
	if (error < 0)
		bench_output("-", 1);
	output_number(error < 0 ? 0 - (uint64_t)error : (uint64_t)error);
	bench_output("\n", 1);
	hyper_process_exit(1);
}

uint64_t bench_clock(void)
{
	return __hyper_std_clock();
}

void bench_output(const char *text, size_t length)
{
	while (length) {
		size_t actual = 0;
		if (__hyper_std_write(1, text, length, &actual) || !actual)
			hyper_process_exit(2);
		text += actual;
		length -= actual;
	}
}

uint64_t bench_open(const char *path, int mode)
{
	uint64_t file;
	/* SDK std bridge: READ, READ | WRITE, or READ | WRITE | CREATE | TRUNCATE. */
	uint32_t options = mode == BENCH_CREATE ? 27 : mode == BENCH_READ_WRITE ? 3 : 1;
	int64_t status = __hyper_std_fs_open(path, strlen(path), options, &file);
	if (status)
		bench_fail("open", status);
	return file;
}

void bench_close(uint64_t file)
{
	if (hyper_handle_close(file))
		bench_fail("close", 0);
}

void bench_read(uint64_t file, uint64_t offset, void *buffer, size_t size)
{
	while (size) {
		size_t actual = 0;
		int64_t status = __hyper_std_fs_read(file, offset, buffer, size, &actual);
		if (status || !actual || actual > size) {
			bench_output("BENCH-IO: read offset=", 22);
			output_number(offset);
			bench_output("\n", 1);
			bench_fail("read", status);
		}
		offset += actual;
		buffer = (char *)buffer + actual;
		size -= actual;
	}
}

void bench_write(uint64_t file, uint64_t offset, const void *buffer, size_t size)
{
	while (size) {
		size_t actual = 0;
		uint64_t end = 0;
		int64_t status = __hyper_std_fs_write(file, offset, 0, buffer, size, &actual, &end);
		if (status || !actual || actual > size) {
			bench_output("BENCH-IO: write offset=", 23);
			output_number(offset);
			bench_output("\n", 1);
			bench_fail("write", status);
		}
		offset += actual;
		buffer = (const char *)buffer + actual;
		size -= actual;
	}
}

void bench_sync(uint64_t file)
{
	int64_t status = __hyper_std_fs_sync(file, 1); /* Data and metadata, like fsync. */
	if (status)
		bench_fail("sync", status);
}

void bench_sync_volume(uint64_t file)
{
	/* The current FAT file-sync operation commits the mounted volume. */
	bench_sync(file);
}

void bench_pause(uint64_t nanoseconds)
{
	__hyper_std_sleep(nanoseconds);
}

#ifndef BENCH_CUSTOM_ENTRY
int hyper_main(const hyper_startup_t *startup)
{
	(void)startup;
	return bench_main();
}
#endif
