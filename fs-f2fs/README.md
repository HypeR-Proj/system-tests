<!-- SPDX-FileCopyrightText: 2026 roolrz -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Disposable F2FS integration fixture

For the multi-repository working branch, run from the synchronized workspace:

```sh
export OUT="$PWD/out-f2fs"
python3 tests/fs-f2fs/run.py
```

The entry prepares the generated execution view, fetches pinned sources, builds
and tests the host media core, builds the current Native SDK/kernel/worker/probe,
downloads the published I/O VM package, and runs three QEMU boots against a new
128 MiB disposable data volume. It never builds I/O VM Linux. `native-f2fs` is
also discoverable by the system-test runner through `cases/native-f2fs.json`.

All generated files, including configuration, remain below `OUT`. The default
fixture is `$OUT/fixtures/f2fs`; an existing fixture is refused. To run again,
pass `--output "$OUT/fixtures/f2fs-second"`. The host needs the normal HypeR build
dependencies, Clang/LLVM with C++20 support, Python 3.11 or newer, `oras`, QEMU
AArch64 and the FAT/EROFS formatters. `HYPER_F2FS_SOURCE_CACHE` can reuse a verified
source archive cache. The worker and probe are declared Cargo modules; no
source-side `app/Cargo.toml` or private C++ runtime is created.

The formatter and fixture builder can also be used through the execution view:

```sh
python3 project.py exec -- python3 tests/fs-f2fs/prepare.py --help
python3 project.py exec -- python3 tests/qemu/verify-f2fs.py --help
```

See [the media library](https://github.com/HypeR-Proj/lib-fs-f2fs/blob/feat/fuchsia-f2fs/README.md)
for independent module build commands. The complete entry writes its kernel,
SDK and test binaries from the same current checkout; mixing old artifacts can
produce misleading ABI failures.

Preparation refuses an existing output directory, validates both GPT copies,
clones the source, writes freshly formatted F2FS only to the clone's data extent,
and refreshes the clone's EROFS system payload. It never modifies the source
board. `deployment.json` retains source partition geometry, including its original
FAT formatter declaration; `fixture.json` records the F2FS override. Do not use
the ordinary full-board formatter on this fixture to preserve F2FS contents.

The acceptance test writes and synchronizes 4 MiB through ordinary Native file
APIs, renames it, creates 80 directory entries, enumerates them, unmounts, stops
QEMU, and starts a new instance. The second instance verifies every byte,
checks truncate/regrow zeroing and nested directory creation, removes the test tree and unmounts again.
A third instance writes and verifies 12 MiB, crossing the 8 MiB background
writeback threshold before explicit sync.
`/bin/f2fs-smoke ram` independently tests the C++/Rust runtime against an owned
RAM device; it never formats a block capability. A failed test retains its logs
and disposable disk for inspection.

## Modular workspace validation (2026-10-31)

After rebasing onto the modular workspace, host persistence, geometry/extension
rejection and write/flush failure tests passed. The AArch64 I/O VM test passed
all three phases with a freshly built kernel and public SDK. Write still used
23 queue submissions / 4,943,872 bytes, and writeback used 41 submissions /
12,701,696 bytes; both reached four outstanding writes. Configuration, caches,
binaries and disposable media were kept under one explicit `OUT` directory.
These counts verify the integration; they are not a hardware performance result.

## Multi-repository working-branch result (2026-10-30)

`run.py` passed all three AArch64 QEMU phases against the published I/O VM
package with the public libc++, musl and libsystem SDK. The original private
C++ runtime is not present in this checkout. The fresh write phase used 23 queue
writes / 4,943,872 bytes, six flushes and four writes outstanding; readback,
truncate/regrow, recursive removal and explicit unmount passed after restarting
QEMU. The 12 MiB writeback phase used 41 writes / 12,701,696 bytes, six flushes
and four writes outstanding. Host persistence and injected write/flush failure
tests also passed. These are queue counts, not a throughput claim.

## Original monorepo integration result (2026-10-29)

AArch64 QEMU with the resident I/O VM passed both phases above, including a full
QEMU restart and recursive removal through Rust std. The RAM-core probe and a 12 MiB writeback-threshold workload also
passed on Native. The latter submitted 41 writes (12,701,696 bytes including
metadata), with four writes outstanding at peak. RISC-V cross-compilation passed. Host tests passed malformed geometry/extension rejection and
persistent write/flush failure propagation, including refusing a later sync
without remount after the device recovers.

With `io-statistics`, the write phase including final unmount reported:

| Measurement | Result |
| --- | ---: |
| Data file payload | 4,194,304 bytes |
| Additional small files | 80 |
| Native queue writes | 23 on a fresh image; 24 on a reused image |
| Bytes submitted for writing, including metadata | 4,943,872 |
| Native queue flushes | 6 |
| Peak outstanding writes | 4 |

These are submissions at the Native block queue boundary, not physical device
commands or an end-to-end throughput benchmark. They demonstrate aggregation and
multiple outstanding writes across the I/O VM path. The host test additionally
observed a 1 MiB merged operation before the Native adapter's 512 KiB split.
The verify/delete/unmount phase used 12 writes, 73,728 written bytes and six
flushes. No FAT performance comparison or hardware latency claim is implied.

Integration also exercised and fixed the public Rust std recursive-removal
path: revalidate directory identity while an opened handle pins it, then release
the traversal handle before conditional removal on remote volumes.
