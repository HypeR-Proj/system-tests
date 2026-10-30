/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#ifndef BENCH_PLATFORM_H
#define BENCH_PLATFORM_H
#include <stddef.h>
#include <stdint.h>

uint64_t bench_clock(void);
void bench_output(const char *text, size_t length);
_Noreturn void bench_fail(const char *operation, int64_t error);
uint64_t bench_open(const char *path, int mode);
void bench_close(uint64_t file);
void bench_read(uint64_t file, uint64_t offset, void *buffer, size_t size);
void bench_write(uint64_t file, uint64_t offset, const void *buffer, size_t size);
void bench_sync(uint64_t file);

/* Creating a payload opens it read/write and truncates any previous contents. */
enum bench_open_mode { BENCH_READ_ONLY, BENCH_CREATE, BENCH_READ_WRITE };

void bench_sync_volume(uint64_t file);
void bench_pause(uint64_t nanoseconds);
int bench_main(void);
#endif
