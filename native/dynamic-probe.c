/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PROBE_VALUE UINT64_C(0x48595045525f444c)

static uint64_t probe_state;

static void initialize_probe(void) __attribute__((constructor));

static void initialize_probe(void)
{
	probe_state = PROBE_VALUE;
}

__attribute__((visibility("default"))) uint64_t hyper_dynamic_probe(void)
{
	return probe_state;
}

/* Allocation and release cross a DSO boundary but share one libsystem heap. */
__attribute__((visibility("default"))) void *hyper_dynamic_heap_probe(void)
{
	void *memory = aligned_alloc(4096, 8192);
	if (memory != NULL) {
		memset(memory, 0x5a, 8192);
	}
	return memory;
}
