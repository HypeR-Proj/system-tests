/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */
#include "qualification.h"
#include <hyper/startup.h>

int hyper_main(const hyper_startup_t *startup)
{
	return storage_main(startup->argument_count, startup->arguments);
}
