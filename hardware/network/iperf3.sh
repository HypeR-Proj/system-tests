#!/bin/sh
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

# Keep test-only package files outside Alpine's managed system libraries.
export LD_LIBRARY_PATH=/opt/hyper-network/usr/lib
exec /opt/hyper-network/usr/bin/iperf3 "$@"
