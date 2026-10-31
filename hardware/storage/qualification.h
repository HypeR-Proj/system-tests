/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#ifndef STORAGE_QUALIFICATION_H
#define STORAGE_QUALIFICATION_H
#include "platform.h"

#define QUAL_MIB UINT64_C(1048576)
#ifndef QUAL_BYTES
#define QUAL_BYTES (1024 * QUAL_MIB)
#endif
_Static_assert(QUAL_BYTES >= QUAL_MIB && QUAL_BYTES % QUAL_MIB == 0, "whole MiB payload");
#define QUAL_IO_BYTES (128 * 1024u)

void qual_text(const char *text);
void qual_number(uint64_t value);
void qual_record(const char *kind, uint64_t run, uint64_t value);
void qual_fill(void *buffer, size_t size, uint64_t offset, uint64_t run, unsigned epoch);
int qual_matches(const void *buffer, size_t size, uint64_t offset, uint64_t run, unsigned epoch);
void qual_durable(uint64_t file, uint64_t run);
void qual_recover(uint64_t file, uint64_t run, uint64_t acknowledged);
int storage_main(size_t argc, char *const *argv);
#endif
