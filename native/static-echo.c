/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

#include <hyper/startup.h>
#include <hyper/std.h>
#include <hyper/stream.h>
#include <string.h>

static int write_all(const char *bytes, size_t length)
{
	while (length) {
		size_t actual = 0;
		if (hyper_runtime_stdio_write(1, bytes, length, &actual) || !actual)
			return 1;
		bytes += actual;
		length -= actual;
	}
	return 0;
}

/* Keep the static executable contract independent of ordinary tools' Rust
 * dependencies. The Rust static/std path has its own std-test-static fixture. */
int hyper_main(const hyper_startup_t *startup)
{
	for (size_t i = 1; i < startup->argument_count; ++i) {
		if (i > 1 && write_all(" ", 1))
			return 1;
		const char *argument = startup->arguments[i];
		if (write_all(argument, strlen(argument)))
			return 1;
	}
	return write_all("\n", 1) || hyper_runtime_stream_fence(1) ? 1 : 0;
}
