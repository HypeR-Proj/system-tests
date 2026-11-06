#!/bin/sh
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
# Run with the Native shell: sh /bin/storage-trace.sh [RUN_ID] [DURATION_MS]

# Mirror only short diagnostic lines; the trace body goes directly to its file.
# pipefail preserves a workload/perf failure when the logger itself succeeds.
# HypeR Native sh supports pipefail; this script does not target POSIX-only sh.
# shellcheck disable=SC3040
set -o pipefail
log_output() {
    while IFS= read -r line; do
        printf '%s\n' "$line" >> "$1" || return 1
        printf '%s\n' "$line" || return 1
    done
}

run=${1:-301}
duration=${2:-3000}
case "$run" in ''|*[!0-9]*|0) echo 'RUN_ID must be a positive integer'; exit 2 ;; esac
case "$duration" in ''|*[!0-9]*|0) echo 'DURATION_MS must be a positive integer'; exit 2 ;; esac
if [ "$#" -gt 2 ]; then
    echo 'usage: sh /bin/storage-trace.sh [RUN_ID] [DURATION_MS]'
    exit 2
fi
if [ "$duration" -gt 300000 ]; then
    echo 'DURATION_MS must not exceed 300000'
    exit 2
fi
stage=/tmp/storage-trace-$run
destination=/tmp/firmware/traces/run-$run
mkdir "$stage" || exit 1
echo 'Preparing a 1 GiB test file; keep other workloads stopped.'
if ! /bin/storage-qual write-volume /data/storage-qual.bin "$run" 2>&1 | log_output "$stage/prepare.log"; then
    echo "Preparation failed; see $stage/prepare.log"
    exit 1
fi
if [ ! -d /tmp/firmware ]; then
    mkdir /tmp/firmware || exit 1
fi
mount /tmp/firmware || exit 1
if [ ! -d /tmp/firmware/traces ]; then
    if ! mkdir /tmp/firmware/traces; then
        umount /tmp/firmware
        exit 1
    fi
fi
if ! mkdir "$destination"; then
    echo 'Run directory exists or could not be created.'
    umount /tmp/firmware
    exit 1
fi
echo 'Recording overwrite I/O. Export starts after the full workload finishes.'
/bin/perf trace record --snapshot --raw-events --duration-ms "$duration" --categories scheduler,ipc,io \
    --cpus all --buffer-records 65536 --poll-ms 10 --max-events 262144 \
    --fail-on-loss --output "$destination/storage.pftrace.partial" \
    -- /bin/storage-qual overwrite-volume /data/storage-qual.bin "$run" \
    2>&1 | log_output "$stage/capture.log"
captured=$?
echo "Capture status: $captured. Saving capture logs."
if cp "$stage/prepare.log" "$destination/prepare.log" && \
    cp "$stage/capture.log" "$destination/capture.log"; then
    if [ "$captured" -eq 0 ]; then
        if ! mv "$destination/storage.pftrace.partial" "$destination/storage.pftrace"; then
            echo 'Trace publication failed; partial file and logs retained.'
            umount /tmp/firmware
            exit 1
        fi
    fi
    umount /tmp/firmware || exit 1
else
    echo "Log export failed. RAM logs retained at $stage"
    umount /tmp/firmware
    exit 1
fi
echo "TRACE FILES EXPORTED: firmware FAT /traces/run-$run/"
if [ "$captured" -ne 0 ]; then
    echo 'Capture was not successful; inspect storage.pftrace.partial with capture.log.'
fi
if [ "$captured" -eq 0 ]; then
    /bin/perf trace clear || exit 1
fi
umount /data || exit 1
echo "EXPORT COMPLETE: firmware FAT /traces/run-$run/"
echo 'Writable volumes unmounted. Power off the Pi before removing the card.'
if [ "$captured" -ne 0 ]; then
    echo 'Capture reported failure or loss; include capture.log with the trace.'
fi
exit "$captured"
