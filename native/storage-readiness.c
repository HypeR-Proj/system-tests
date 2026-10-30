/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

/* Stand-in for a failing readiness provider. The normal init and service
 * binaries remain unchanged; only the acceptance archive uses this process. */
#include <hyper/startup.h>
#include <hyper/syscall.h>
#include <stdint.h>

#ifndef READINESS_TEST
#error "Select the acceptance scenario at build time"
#endif
#if READINESS_TEST < 0 || READINESS_TEST > 5
#error "Unknown readiness scenario"
#endif

#define READY_PURPOSE UINT32_C(0x80060001)
#define OUTPUT_PURPOSE UINT32_C(0x80030002)

int hyper_main(const hyper_startup_t *startup)
{
	hyper_native_handle_t ready = 0;
	hyper_native_handle_t output = 0;
	static const char marker[] = "READINESS-FIXTURE-RUNNING\n";
	static const char invalid[] = "HYPER-IO-BAD/1\n";
	static const char valid[] = "HYPER-IO-READY/1\n";
	static const char oversized[] =
		"HYPER-IO-READY/1\n"
		"unexpected trailing data beyond the complete readiness receive buffer\n";

	if (READINESS_TEST == 0)
		return 1;
	if (hyper_startup_find_handle(startup, READY_PURPOSE, &ready) != HYPER_NATIVE_STATUS_OK ||
	    hyper_startup_find_handle(startup, OUTPUT_PURPOSE, &output) != HYPER_NATIVE_STATUS_OK)
		return 2;
	if (hyper_byte_channel_write(output, marker, sizeof(marker) - 1) != HYPER_NATIVE_STATUS_OK)
		return 3;
	if (READINESS_TEST == 1 && hyper_handle_close(ready) != HYPER_NATIVE_STATUS_OK)
		return 4;
	if (READINESS_TEST == 2 &&
	    hyper_byte_channel_write(ready, invalid, sizeof(invalid) - 1) != HYPER_NATIVE_STATUS_OK)
		return 5;
	if (READINESS_TEST == 4 &&
	    hyper_byte_channel_write(ready, valid, sizeof(valid) - 1) != HYPER_NATIVE_STATUS_OK)
		return 5;
	if (READINESS_TEST == 5 &&
	    hyper_byte_channel_write(ready, oversized, sizeof(oversized) - 1) != HYPER_NATIVE_STATUS_OK)
		return 5;

	/* Keep the provider alive after EOF, malformed readiness or silence.
	 * Init must retain the live owner for recovery and preserve the Native graph. */
	for (;;) {
		hyper_call_result_t now = hyper_clock_get_monotonic();
		if (now.status != HYPER_NATIVE_STATUS_OK ||
		    now.value0 > UINT64_MAX - UINT64_C(300000000000))
			return 6;
		if (hyper_thread_sleep(now.value0 + UINT64_C(300000000000)) != HYPER_NATIVE_STATUS_OK)
			return 7;
	}
}
