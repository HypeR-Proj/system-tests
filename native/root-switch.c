/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

/* A readiness provider holds capabilities across the real init handoff. */
#include <hyper/startup.h>
#include <hyper/syscall.h>
#include <stdint.h>

#define READY_PURPOSE UINT32_C(0x80060001)
#define OUTPUT_PURPOSE UINT32_C(0x80030002)
#define DIRECTORY_RIGHTS (HYPER_NATIVE_RIGHT_READ | HYPER_NATIVE_RIGHT_INSPECT)

static int same(const hyper_native_directory_info_t *a, const hyper_native_directory_info_t *b)
{
	return a->filesystem_id == b->filesystem_id && a->mount_id == b->mount_id &&
	       a->node_id == b->node_id;
}

static int pause_ms(uint64_t milliseconds)
{
	hyper_call_result_t now = hyper_clock_get_monotonic();
	uint64_t duration = milliseconds * UINT64_C(1000000);
	if (now.status || now.value0 > UINT64_MAX - duration)
		return 1;
	return hyper_thread_sleep(now.value0 + duration) != HYPER_NATIVE_STATUS_OK;
}

int hyper_main(const hyper_startup_t *startup)
{
	hyper_native_handle_t root = 0, ready = 0, output = 0;
	hyper_native_directory_info_t before = {0}, after = {0}, retained = {0};
	static const char readiness[] = "HYPER-IO-READY/1\n";
	static const char passed[] = "ROOT-SWITCH: live capabilities preserved\n";
	static const char failed[] = "ROOT-SWITCH: FAIL\n";
	if (hyper_startup_find_handle(startup, HYPER_NATIVE_STARTUP_HANDLE_PURPOSE_ROOT_DIRECTORY,
				      &root) ||
	    hyper_startup_find_handle(startup, READY_PURPOSE, &ready) ||
	    hyper_startup_find_handle(startup, OUTPUT_PURPOSE, &output))
		return 1;
	if (hyper_directory_get_info(root, &before, sizeof(hyper_native_directory_info_t)).status)
		goto fail;
	hyper_call_result_t fixed = hyper_directory_open_directory(root, "/", 1, DIRECTORY_RIGHTS);
	hyper_call_result_t scoped = hyper_directory_scope_create(root, root, DIRECTORY_RIGHTS);
	if (fixed.status || scoped.status)
		goto fail;
	/* Existing files remain valid; a root switch must not retire the ramdisk. */
	hyper_call_result_t file =
		hyper_directory_open_file(root, "/etc/hyper/board.json", 21,
					  HYPER_NATIVE_RIGHT_READ | HYPER_NATIVE_RIGHT_INSPECT);
	hyper_native_file_info_t file_before = {0}, file_after = {0};
	if (file.status ||
	    hyper_file_get_info(file.value0, &file_before, sizeof(hyper_native_file_info_t)).status)
		goto fail;
	/* Ordinary core services must not acquire namespace administration. */
	if (hyper_filesystem_mount_ramfs(root, "/forbidden", 10).status !=
	    HYPER_NATIVE_STATUS_ACCESS_DENIED)
		goto fail;
	if (hyper_byte_channel_write(ready, readiness, sizeof(readiness) - 1))
		goto fail;
	for (unsigned int i = 0; i != 6000; ++i) {
		if (hyper_directory_get_info(root, &after, sizeof(hyper_native_directory_info_t))
			    .status)
			goto fail;
		if (!same(&before, &after))
			break;
		if (pause_ms(10))
			goto fail;
	}
	if (same(&before, &after) || after.filesystem_id == before.filesystem_id ||
	    hyper_directory_get_info(scoped.value0, &retained,
				     sizeof(hyper_native_directory_info_t))
		    .status ||
	    !same(&after, &retained) ||
	    hyper_directory_get_info(fixed.value0, &retained, sizeof(hyper_native_directory_info_t))
		    .status ||
	    !same(&before, &retained))
		goto fail;
	hyper_call_result_t old =
		hyper_directory_open_directory(root, "/boot", 5, DIRECTORY_RIGHTS);
	if (old.status ||
	    hyper_directory_get_info(old.value0, &retained, sizeof(hyper_native_directory_info_t))
		    .status ||
	    !same(&before, &retained) ||
	    hyper_file_get_info(file.value0, &file_after, sizeof(hyper_native_file_info_t))
		    .status ||
	    file_before.filesystem_id != file_after.filesystem_id ||
	    file_before.mount_id != file_after.mount_id ||
	    file_before.node_id != file_after.node_id || file_before.size != file_after.size)
		goto fail;
	(void)hyper_handle_close(old.value0);
	(void)hyper_handle_close(file.value0);
	(void)hyper_handle_close(scoped.value0);
	(void)hyper_handle_close(fixed.value0);
	(void)hyper_byte_channel_write(output, passed, sizeof(passed) - 1);
	/* Remain the live readiness owner while the shell tests recovery. */
	for (;;)
		if (pause_ms(60000))
			return 2;
fail:
	(void)hyper_byte_channel_write(output, failed, sizeof(failed) - 1);
	return 1;
}
