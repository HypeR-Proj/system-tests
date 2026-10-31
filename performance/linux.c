/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "platform.h"

/* AArch64 Linux UAPI, confined to this test adapter. No Linux libc or runtime
 * code participates in the common compute workload. */
static int64_t call(uint64_t number, uint64_t a, uint64_t b, uint64_t c, uint64_t d)
{
	register uint64_t x0 __asm__("x0") = a;
	register uint64_t x1 __asm__("x1") = b;
	register uint64_t x2 __asm__("x2") = c;
	register uint64_t x3 __asm__("x3") = d;
	register uint64_t x8 __asm__("x8") = number;
	__asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3), "r"(x8) : "memory", "cc");
	return (int64_t)x0;
}

_Noreturn void bench_fail(const char *operation, int64_t error)
{
	(void)error;
	size_t length = 0;
	while (operation[length])
		length++;
	bench_output("BENCH-FAIL: ", 12);
	bench_output(operation, length);
	bench_output("\n", 1);
	call(93, 1, 0, 0, 0);
	__builtin_unreachable();
}

void bench_output(const char *text, size_t length)
{
	while (length) {
		int64_t actual = call(64, 1, (uintptr_t)text, length, 0);
		if (actual <= 0) {
			call(93, 2, 0, 0, 0);
			__builtin_unreachable();
		}
		text += actual;
		length -= (size_t)actual;
	}
}

uint64_t bench_clock(void)
{
	struct {
		int64_t seconds, nanoseconds;
	} time;

	int64_t status = call(113, 1, (uintptr_t)&time, 0, 0);
	if (status)
		bench_fail("clock_gettime", status);
	return (uint64_t)time.seconds * 1000000000 + (uint64_t)time.nanoseconds;
}

uint64_t bench_open(const char *path, int mode)
{
	/* AT_FDCWD; O_RDWR | O_CREAT | O_TRUNC, O_RDWR, or O_RDONLY. */
	uint64_t flags = mode == BENCH_CREATE ? 578 : mode == BENCH_READ_WRITE ? 2 : 0;
	int64_t file = call(56, (uint64_t)-100, (uintptr_t)path, flags, 0666);
	if (file < 0)
		bench_fail("openat", file);
	return (uint64_t)file;
}

void bench_close(uint64_t file)
{
	int64_t status = call(57, file, 0, 0, 0);
	if (status)
		bench_fail("close", status);
}

void bench_read(uint64_t file, uint64_t offset, void *buffer, size_t size)
{
	while (size) {
		int64_t actual = call(67, file, (uintptr_t)buffer, size, offset);
		if (actual <= 0 || (uint64_t)actual > size)
			bench_fail("pread64", actual);
		offset += actual;
		buffer = (char *)buffer + actual;
		size -= actual;
	}
}

void bench_write(uint64_t file, uint64_t offset, const void *buffer, size_t size)
{
	while (size) {
		int64_t actual = call(68, file, (uintptr_t)buffer, size, offset);
		if (actual <= 0 || (uint64_t)actual > size)
			bench_fail("pwrite64", actual);
		offset += actual;
		buffer = (const char *)buffer + actual;
		size -= actual;
	}
}

void bench_sync(uint64_t file)
{
	int64_t status = call(82, file, 0, 0, 0);
	if (status)
		bench_fail("fsync", status);
}

void bench_sync_volume(uint64_t file)
{
	int64_t status = call(267, file, 0, 0, 0); /* syncfs: this filesystem only. */
	if (status)
		bench_fail("syncfs", status);
}

void bench_pause(uint64_t nanoseconds)
{
	struct {
		uint64_t seconds, nanoseconds;
	} delay = {nanoseconds / 1000000000, nanoseconds % 1000000000};

	int64_t status = call(115, 1, 0, (uintptr_t)&delay, 0); /* clock_nanosleep */
	if (status && status != -4)
		bench_fail("clock_nanosleep", status);
}

#ifndef BENCH_CUSTOM_ENTRY
__attribute__((noreturn)) void _start(void)
{
	call(93, bench_main(), 0, 0, 0);
	__builtin_unreachable();
}
#endif
