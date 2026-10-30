/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "qualification.h"

__attribute__((noreturn)) void storage_linux_start(const uintptr_t *stack)
{
	int result = storage_main(stack[0], (char *const *)(stack + 1));
	register uint64_t x0 __asm__("x0") = (uint64_t)result;
	register uint64_t x8 __asm__("x8") = 93;
	__asm__ volatile("svc #0" : : "r"(x0), "r"(x8) : "memory", "cc");
	__builtin_unreachable();
}

__attribute__((naked, noreturn)) void _start(void)
{
	/* Preserve the Linux architecture entry stack before any C prologue. */
	__asm__ volatile("mov x0, sp\n\tb storage_linux_start");
}
