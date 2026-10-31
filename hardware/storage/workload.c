/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "qualification.h"

static uint64_t buffer[QUAL_IO_BYTES / 8] __attribute__((aligned(4096)));

static int equal(const char *a, const char *b)
{
	while (*a && *a == *b) {
		a++;
		b++;
	}
	return *a == *b;
}

static uint64_t decimal(const char *text)
{
	uint64_t value = 0;
	if (!*text)
		bench_fail("empty integer", 0);
	for (; *text; text++) {
		if (*text < '0' || *text > '9' || value > (UINT64_MAX - (*text - '0')) / 10)
			bench_fail("invalid integer", 0);
		value = value * 10 + *text - '0';
	}
	return value;
}

static int payload_path(const char *path)
{
	const char *name = path;
	for (const char *cursor = path; *cursor; cursor++)
		if (*cursor == '/')
			name = cursor + 1;
	/* Catch accidental filenames; callers must still avoid symlinks/devices. */
	return equal(name, "storage-qual.bin");
}

static void verify(uint64_t file, uint64_t run, unsigned epoch)
{
	for (uint64_t offset = 0; offset < QUAL_BYTES; offset += sizeof(buffer)) {
		bench_read(file, offset, buffer, sizeof(buffer));
		if (!qual_matches(buffer, sizeof(buffer), offset, run, epoch)) {
			qual_record("MISMATCH", run, offset);
			bench_fail("payload verification", 0);
		}
	}
	qual_record("VERIFIED", run, QUAL_BYTES);
}

static void write_payload(uint64_t file, uint64_t run, unsigned epoch)
{
	uint64_t start = bench_clock();
	for (uint64_t offset = 0; offset < QUAL_BYTES; offset += sizeof(buffer)) {
		qual_fill(buffer, sizeof(buffer), offset, run, epoch);
		bench_write(file, offset, buffer, sizeof(buffer));
	}
	uint64_t written = bench_clock();
	bench_sync_volume(file);
	uint64_t synced = bench_clock();
	qual_record("WRITE_NS", run, written - start);
	qual_record("SYNC_NS", run, synced - written);
	qual_record("TOTAL_NS", run, synced - start);
	verify(file, run, epoch);
}

static void sync_writes(uint64_t file, uint64_t run)
{
	uint64_t latency[128];
	uint32_t random = 54321;
	/* This command preserves the epoch-1 pattern, allowing a separate full
	 * cold readback. It measures synchronization latency, not crash atomicity. */
	verify(file, run, 1);
	for (unsigned i = 0; i < 128; i++) {
		random = random * 1664525u + 1013904223u;
		uint64_t offset = (random % (QUAL_BYTES / 4096)) * 4096;
		qual_fill(buffer, 4096, offset, run, 1);
		uint64_t start = bench_clock();
		bench_write(file, offset, buffer, 4096);
		bench_sync_volume(file);
		latency[i] = bench_clock() - start;
	}
	for (unsigned i = 0; i < 128; i++)
		qual_record("SYNC4K_NS", run, latency[i]);
}

int storage_main(size_t argc, char *const *argv)
{
	if (argc != 4 && argc != 5) {
		qual_text(
			"usage: storage-qual {write|overwrite|check|sync4k|prepare|durable|recover|stress} PATH RUN_ID [ACK_MIB]\n");
		return 2;
	}
	const char *mode = argv[1];
	if (!payload_path(argv[2]))
		bench_fail("payload basename must be storage-qual.bin", 0);
	int recover = equal(mode, "recover");
	int stress = equal(mode, "stress");
	int create = equal(mode, "write") || equal(mode, "prepare") || stress;
	int readonly = equal(mode, "check") || recover;
	if ((!create && !readonly && !equal(mode, "overwrite") && !equal(mode, "sync4k") &&
	     !equal(mode, "durable")) ||
	    (argc == 5) != recover)
		bench_fail("invalid command", 0);
	uint64_t run = decimal(argv[3]);
	uint64_t acknowledged = recover ? decimal(argv[4]) : 0;
	if (!run || (recover && (!acknowledged || acknowledged > QUAL_BYTES / QUAL_MIB)))
		bench_fail("run ID / acknowledged extent", 0);
	qual_text("STORAGE,BEGIN,");
	qual_number(run);
	qual_text(",");
	qual_text(mode);
	qual_text("\n");
	qual_record("PAYLOAD_BYTES", run, QUAL_BYTES);
	int open_mode = BENCH_READ_WRITE;
	if (create)
		open_mode = BENCH_CREATE;
	else if (readonly)
		open_mode = BENCH_READ_ONLY;
	uint64_t file = bench_open(argv[2], open_mode);
	if (equal(mode, "overwrite")) {
		/* Require an already allocated 1 GiB extent before measuring. */
		bench_read(file, QUAL_BYTES - sizeof(buffer), buffer, sizeof(buffer));
	}
	if (stress) {
		qual_text(
			"STORAGE,STRESS: continuous I/O on a fixed 1 GiB extent; no performance result\n");
		for (;;) {
			write_payload(file, run, 1);
			if (run == UINT64_MAX)
				bench_fail("run identity exhausted", 0);
			run++;
		}
	} else if (create || equal(mode, "overwrite")) {
		write_payload(file, run, equal(mode, "prepare") ? 0 : 1);
		if (equal(mode, "prepare"))
			qual_record("PREPARED", run, QUAL_BYTES);
	} else if (equal(mode, "check")) {
		verify(file, run, 1);
	} else if (equal(mode, "sync4k")) {
		sync_writes(file, run);
	} else if (recover) {
		qual_recover(file, run, acknowledged);
	} else {
		qual_durable(file, run);
	}
	bench_close(file);
	qual_record("END", run, 0);
	return 0;
}
