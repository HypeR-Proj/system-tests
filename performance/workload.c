/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "platform.h"

#define MIB (1024u * 1024u)
#define MEMORY_BYTES (8u * MIB)
#define FILE_BYTES (16u * MIB)
#define HOT_FILE_BYTES (256u * 1024u)
#define IO_BYTES (128u * 1024u)
#define RANDOM_OPS 512u
#define SYNC_OPS 64u
#define SAMPLES 3u
#define MEMORY_PASSES 256u

static uint64_t source[MEMORY_BYTES / sizeof(uint64_t)] __attribute__((aligned(4096)));
static uint64_t destination[MEMORY_BYTES / sizeof(uint64_t)] __attribute__((aligned(4096)));
static unsigned char io_buffer[IO_BYTES] __attribute__((aligned(4096)));
static volatile uint64_t observed;

static size_t length(const char *text)
{
	size_t size = 0;
	while (text[size])
		size++;
	return size;
}

static void print(const char *text)
{
	bench_output(text, length(text));
}

static void number(uint64_t value)
{
	char buffer[24];
	size_t cursor = sizeof(buffer);
	do {
		buffer[--cursor] = '0' + value % 10;
		value /= 10;
	} while (value);
	bench_output(buffer + cursor, sizeof(buffer) - cursor);
}

static void result(const char *name, unsigned sample, uint64_t count, uint64_t elapsed)
{
	if (!elapsed)
		bench_fail("zero elapsed time", 0);
	print("BENCH,");
	print(name);
	print(",");
	number(sample);
	print(",");
	number(count);
	print(",");
	number(elapsed);
	print("\n");
}

/* Separate calls and compiler barriers retain every pass without volatile
 * loads/stores in the timed loops. The same object is linked into both ELFs. */
__attribute__((noinline)) static void copy_memory(void)
{
	for (size_t i = 0; i < MEMORY_BYTES / sizeof(uint64_t); i++)
		destination[i] = source[i];
	__asm__ volatile("" ::: "memory");
}

__attribute__((noinline)) static uint64_t read_memory(void)
{
	uint64_t sum = 0;
	for (size_t i = 0; i < MEMORY_BYTES / sizeof(uint64_t); i++)
		sum += source[i];
	__asm__ volatile("" ::: "memory");
	return sum;
}

static void memory(void)
{
	uint64_t expected = 0;
	for (size_t i = 0; i < MEMORY_BYTES / sizeof(uint64_t); i++) {
		source[i] = i ^ UINT64_C(0x1234567812345678);
		expected += source[i];
	}
	copy_memory();
	observed = read_memory();
	for (unsigned sample = 0; sample < SAMPLES; sample++) {
		uint64_t start = bench_clock();
		for (unsigned pass = 0; pass < MEMORY_PASSES; pass++)
			copy_memory();
		uint64_t elapsed = bench_clock() - start;
		for (size_t i = 0; i < MEMORY_BYTES / sizeof(uint64_t); i++)
			if (destination[i] != source[i])
				bench_fail("memory copy verification", i);
		result("memory_copy", sample, (uint64_t)MEMORY_BYTES * MEMORY_PASSES, elapsed);
		start = bench_clock();
		uint64_t sum = 0;
		for (unsigned pass = 0; pass < MEMORY_PASSES; pass++)
			sum += read_memory();
		elapsed = bench_clock() - start;
		observed = sum;
		if (sum != expected * MEMORY_PASSES)
			bench_fail("memory read verification", 0);
		result("memory_read", sample, (uint64_t)MEMORY_BYTES * MEMORY_PASSES, elapsed);
	}
}

static void verify_buffer(uint64_t offset, size_t size)
{
	for (size_t i = 0; i < size; i++)
		if (io_buffer[i] != (unsigned char)(((offset + i) * 17 + 31) % 251))
			bench_fail("file data verification", offset + i);
}

static void read_file(uint64_t file)
{
	for (uint64_t offset = 0; offset < FILE_BYTES; offset += IO_BYTES)
		bench_read(file, offset, io_buffer, IO_BYTES);
}

static void storage(void)
{
	uint64_t file = bench_open("/data/seed.bin", BENCH_READ_ONLY);
	for (unsigned sample = 0; sample < SAMPLES; sample++) {
		uint64_t start = bench_clock();
		read_file(file);
		uint64_t elapsed = bench_clock() - start;
		verify_buffer(FILE_BYTES - IO_BYTES, IO_BYTES);
		result(sample ? "file_read_repeat" : "file_read_first", sample, FILE_BYTES,
		       elapsed);
	}
	/* Repeatable LCG, aligned 4 KiB, QD1; verification is outside timing. */
	for (unsigned sample = 0; sample < SAMPLES; sample++) {
		uint32_t random = 12345;
		uint64_t offset = 0;
		uint64_t start = bench_clock();
		for (unsigned i = 0; i < RANDOM_OPS; i++) {
			random = random * 1664525u + 1013904223u;
			offset = (uint64_t)(random % (FILE_BYTES / 4096)) * 4096;
			bench_read(file, offset, io_buffer, 4096);
		}
		uint64_t elapsed = bench_clock() - start;
		verify_buffer(offset, 4096);
		result("file_random_read_4k", sample, RANDOM_OPS, elapsed);
	}
	/* Full seed validation after read measurements. */
	for (uint64_t offset = 0; offset < FILE_BYTES; offset += IO_BYTES) {
		bench_read(file, offset, io_buffer, IO_BYTES);
		verify_buffer(offset, IO_BYTES);
	}
	bench_close(file);
	for (size_t i = 0; i < IO_BYTES; i++)
		io_buffer[i] = (unsigned char)(i * 13 + 7);
	file = bench_open("/data/write.bin", BENCH_CREATE);
	for (unsigned sample = 0; sample < SAMPLES; sample++) {
		uint64_t start = bench_clock();
		for (uint64_t offset = 0; offset < FILE_BYTES; offset += IO_BYTES)
			bench_write(file, offset, io_buffer, IO_BYTES);
		bench_sync(file);
		result(sample ? "file_overwrite_sync" : "file_grow_sync", sample, FILE_BYTES,
		       bench_clock() - start);
	}
	for (unsigned sample = 0; sample < SAMPLES; sample++) {
		uint32_t random = 54321;
		uint64_t start = bench_clock();
		for (unsigned i = 0; i < SYNC_OPS; i++) {
			random = random * 1664525u + 1013904223u;
			uint64_t offset = (uint64_t)(random % (FILE_BYTES / 4096)) * 4096;
			bench_write(file, offset, io_buffer, 4096);
			bench_sync(file);
		}
		result("file_random_write_sync_4k", sample, SYNC_OPS, bench_clock() - start);
	}
	for (uint64_t offset = 0; offset < FILE_BYTES; offset += IO_BYTES) {
		bench_read(file, offset, io_buffer, IO_BYTES);
		for (size_t i = 0; i < IO_BYTES; i++)
			if (io_buffer[i] != (unsigned char)(i * 13 + 7))
				bench_fail("write readback verification", offset + i);
	}
	bench_close(file);
}

static void hot_random_read(void)
{
	/* Reuse the already resident memory-copy destination to verify every
	 * returned byte after timing, without changing the earlier workloads. */
	_Static_assert(RANDOM_OPS * 4096u <= MEMORY_BYTES, "random read results fit");
	unsigned char *reads = (unsigned char *)destination;
	uint64_t file = bench_open("/data/seed.bin", BENCH_READ_ONLY);
	for (unsigned sample = 0; sample < SAMPLES; sample++) {
		/* Warm the entire 64-page working set before each timed sample. */
		for (uint64_t offset = 0; offset < HOT_FILE_BYTES; offset += IO_BYTES) {
			bench_read(file, offset, io_buffer, IO_BYTES);
			verify_buffer(offset, IO_BYTES);
		}
		uint32_t random = 12345;
		uint64_t start = bench_clock();
		for (unsigned i = 0; i < RANDOM_OPS; i++) {
			random = random * 1664525u + 1013904223u;
			uint64_t offset = (uint64_t)(random % (HOT_FILE_BYTES / 4096)) * 4096;
			bench_read(file, offset, reads + i * 4096u, 4096);
		}
		uint64_t elapsed = bench_clock() - start;
		random = 12345;
		for (unsigned i = 0; i < RANDOM_OPS; i++) {
			random = random * 1664525u + 1013904223u;
			uint64_t offset = (uint64_t)(random % (HOT_FILE_BYTES / 4096)) * 4096;
			for (size_t byte = 0; byte < 4096; byte++)
				if (reads[i * 4096u + byte] !=
				    (unsigned char)(((offset + byte) * 17 + 31) % 251))
					bench_fail("hot random read verification", offset + byte);
		}
		result("file_random_read_hot_4k", sample, RANDOM_OPS, elapsed);
	}
	bench_close(file);
}

int bench_main(void)
{
	print("BENCH-BEGIN\n");
	memory();
	storage();
	hot_random_read();
	print("BENCH-PASS\n");
	return 0;
}
