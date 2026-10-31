/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

/* Replacement manager for supervision acceptance archives only. It uses the
 * production provisioning wire contract and never creates a VM or I/O client. */
#include <hyper/startup.h>
#include <hyper/syscall.h>
#include <stdint.h>

#ifndef VM_PROVISION_TEST
#error "Select the provisioning scenario at build time"
#endif
#if VM_PROVISION_TEST < 0 || VM_PROVISION_TEST > 4
#error "Unknown provisioning scenario"
#endif

#define PROVISIONING_PURPOSE UINT32_C(0x80050004)
#define OUTPUT_PURPOSE UINT32_C(0x80030002)

static int pause_seconds(uint64_t seconds)
{
	hyper_call_result_t now = hyper_clock_get_monotonic();
	uint64_t duration = seconds * UINT64_C(1000000000);
	if (now.status != HYPER_NATIVE_STATUS_OK || now.value0 > UINT64_MAX - duration)
		return 1;
	return hyper_thread_sleep(now.value0 + duration) != HYPER_NATIVE_STATUS_OK;
}

int hyper_main(const hyper_startup_t *startup)
{
	hyper_native_handle_t provisioning = 0, output = 0;
	static const char running[] = "VM-PROVISION-FIXTURE-RUNNING\n";
	static const char closed[] = "VM-PROVISION-FIXTURE-CLOSED-REPLY\n";
	static const char invalid[] = "VM-PROVISION-FIXTURE-INVALID-REPLY\n";
	static const char configured[] = "VM-PROVISION-FIXTURE-CONFIGURED\n";
	static const char exiting[] = "VM-PROVISION-FIXTURE-EXIT\n";
	/* hyper_service::vm::ProvisionRequest / ProvisionResult, format version 1. */
	static const uint8_t request[] = {'H', 'V', 'M', 1, 1, 1, 0, 0};
	static const uint8_t reply[] = {'H', 'V', 'M', 1, 6, 1, 0, 0};
	static const uint8_t malformed[8] = {0};
	uint8_t bytes[8] = {0};
	hyper_native_capability_receive_slot_t slots[2] = {
		{.rights = HYPER_NATIVE_RIGHT_READ, .expected_kind = HYPER_NATIVE_OBJECT_FILE},
		{.rights = HYPER_NATIVE_RIGHT_WRITE,
		 .expected_kind = HYPER_NATIVE_OBJECT_BYTE_CHANNEL},
	};

	if (hyper_startup_find_handle(startup, OUTPUT_PURPOSE, &output) != HYPER_NATIVE_STATUS_OK)
		return 1;
	if (hyper_byte_channel_write(output, running, sizeof(running) - 1) != HYPER_NATIVE_STATUS_OK)
		return 2;
	if (VM_PROVISION_TEST == 0) {
		(void)hyper_byte_channel_write(output, exiting, sizeof(exiting) - 1);
		return 1;
	}
	if (hyper_startup_find_handle(startup, PROVISIONING_PURPOSE, &provisioning) !=
	    HYPER_NATIVE_STATUS_OK)
		return 3;
	hyper_call_result_t received = hyper_capability_channel_receive(
		provisioning, HYPER_NATIVE_DEADLINE_INFINITE, bytes, sizeof(bytes), slots, 2);
	if (received.status != HYPER_NATIVE_STATUS_OK || received.value0 != sizeof(request) ||
	    received.value1 != 2)
		return 4;
	for (size_t i = 0; i < sizeof(request); i++) {
		if (bytes[i] != request[i])
			return 5;
	}
	if (hyper_handle_close(slots[0].handle) != HYPER_NATIVE_STATUS_OK)
		return 6;
	if (VM_PROVISION_TEST == 1) {
		if (hyper_byte_channel_write(output, closed, sizeof(closed) - 1) !=
			    HYPER_NATIVE_STATUS_OK ||
		    hyper_handle_close(slots[1].handle) != HYPER_NATIVE_STATUS_OK)
			return 7;
	} else if (VM_PROVISION_TEST == 2) {
		if (hyper_byte_channel_write(output, invalid, sizeof(invalid) - 1) !=
			    HYPER_NATIVE_STATUS_OK ||
		    hyper_byte_channel_write(slots[1].handle, malformed, sizeof(malformed)) !=
			    HYPER_NATIVE_STATUS_OK)
			return 8;
	} else {
		if (hyper_byte_channel_write(slots[1].handle, reply, sizeof(reply)) !=
			    HYPER_NATIVE_STATUS_OK ||
		    hyper_handle_close(slots[1].handle) != HYPER_NATIVE_STATUS_OK ||
		    hyper_byte_channel_write(output, configured, sizeof(configured) - 1) !=
			    HYPER_NATIVE_STATUS_OK)
			return 9;
		if (VM_PROVISION_TEST == 4) {
			hyper_native_handle_t root = 0;
			static const char trigger[] = "/tmp/critical-exit";
			if (hyper_startup_find_handle(startup,
				HYPER_NATIVE_STARTUP_HANDLE_PURPOSE_ROOT_DIRECTORY, &root))
				return 12;
			/* Only the test's already-running formal shell creates this
			 * file. This separates steady-state policy from boot recovery. */
			for (;;) {
				hyper_call_result_t file = hyper_directory_open_file(root,
					trigger, sizeof(trigger) - 1, HYPER_NATIVE_RIGHT_READ);
				if (file.status == HYPER_NATIVE_STATUS_OK) {
					(void)hyper_handle_close(file.value0);
					(void)hyper_byte_channel_write(output, exiting, sizeof(exiting) - 1);
					return 1;
				}
				if (file.status != HYPER_NATIVE_STATUS_NOT_FOUND || pause_seconds(1))
					return 13;
			}
		}
		if (pause_seconds(5))
			return 10;
		(void)hyper_byte_channel_write(output, exiting, sizeof(exiting) - 1);
		return 0;
	}
	/* EOF or invalid data must be handled while this process is still alive.
	 * The real init decides to stop this optional manager; core must survive. */
	for (;;) {
		if (pause_seconds(300))
			return 11;
	}
}
