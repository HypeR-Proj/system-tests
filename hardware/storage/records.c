/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "qualification.h"

void qual_text(const char *text)
{
	size_t size = 0;
	while (text[size])
		size++;
	bench_output(text, size);
}

void qual_number(uint64_t value)
{
	char bytes[24];
	size_t at = sizeof(bytes);
	do {
		bytes[--at] = '0' + value % 10;
		value /= 10;
	} while (value);
	bench_output(bytes + at, sizeof(bytes) - at);
}

void qual_record(const char *kind, uint64_t run, uint64_t value)
{
	qual_text("STORAGE,");
	qual_text(kind);
	qual_text(",");
	qual_number(run);
	qual_text(",");
	qual_number(value);
	qual_text("\n");
}

/* Every word depends on its file offset, run identity and durability epoch.
 * This detects stale, torn and misdirected writes, without a second 1 GiB
 * resident copy. The identical object is linked into both OS executables. */
static uint64_t word(uint64_t offset, uint64_t run, unsigned epoch)
{
	uint64_t value = (offset / 8) ^ (run * UINT64_C(0x9e3779b97f4a7c15)) ^
			 (epoch ? UINT64_C(0xd1b54a32d192ed03) : 0);
	value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
	return value ^ (value >> 31);
}

void qual_fill(void *buffer, size_t size, uint64_t offset, uint64_t run, unsigned epoch)
{
	uint64_t *words = buffer;
	for (size_t i = 0; i < size / 8; i++)
		words[i] = word(offset + i * 8, run, epoch);
}

int qual_matches(const void *buffer, size_t size, uint64_t offset, uint64_t run, unsigned epoch)
{
	const uint64_t *words = buffer;
	for (size_t i = 0; i < size / 8; i++)
		if (words[i] != word(offset + i * 8, run, epoch))
			return 0;
	return 1;
}
