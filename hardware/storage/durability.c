/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "qualification.h"

static uint64_t buffer[QUAL_IO_BYTES / 8] __attribute__((aligned(4096)));

void qual_durable(uint64_t file, uint64_t run)
{
	/* Refuse to reuse a prior power-cut run. Preparation and its full-volume
	 * sync must finish before this command, with a fresh external run identity. */
	for (uint64_t offset = 0; offset < QUAL_BYTES; offset += QUAL_IO_BYTES) {
		bench_read(file, offset, buffer, sizeof(buffer));
		if (!qual_matches(buffer, sizeof(buffer), offset, run, 0))
			bench_fail("durability baseline; run prepare first", 0);
	}
	for (uint64_t start = 0; start < QUAL_BYTES; start += QUAL_MIB) {
		qual_record("ISSUE", run, start / QUAL_MIB + 1);
		for (uint64_t offset = start; offset < start + QUAL_MIB; offset += QUAL_IO_BYTES) {
			qual_fill(buffer, sizeof(buffer), offset, run, 1);
			bench_write(file, offset, buffer, sizeof(buffer));
		}
		bench_sync_volume(file);
		/* This ACK is evidence only after an independent host has received
		 * its complete line. No result stored on the tested card is an oracle. */
		qual_record("ACK", run, start / QUAL_MIB + 1);
		bench_pause(UINT64_C(100000000));
	}
	qual_record("COMPLETE", run, QUAL_BYTES);
}

void qual_recover(uint64_t file, uint64_t run, uint64_t acknowledged)
{
	uint64_t old = 0, changed = 0, torn = 0;
	for (uint64_t offset = 0; offset < QUAL_BYTES; offset += sizeof(buffer)) {
		bench_read(file, offset, buffer, sizeof(buffer));
		int current = qual_matches(buffer, sizeof(buffer), offset, run, 1);
		if (offset < acknowledged * QUAL_MIB && !current) {
			qual_record("LOST_ACK", run, offset);
			bench_fail("acknowledged data lost after power cut", 0);
		}
		if (offset >= acknowledged * QUAL_MIB) {
			if (current)
				changed++;
			else if (qual_matches(buffer, sizeof(buffer), offset, run, 0))
				old++;
			else
				torn++;
		}
	}
	qual_record("UNACK_OLD_CHUNKS", run, old);
	qual_record("UNACK_NEW_CHUNKS", run, changed);
	qual_record("UNACK_TORN_CHUNKS", run, torn);
	qual_record("ACK_PREFIX_VERIFIED", run, acknowledged);
}
