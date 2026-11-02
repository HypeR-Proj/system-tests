<!-- SPDX-FileCopyrightText: 2026 roolrz -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Disposable F2FS integration fixture

Run from the synchronized workspace:

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

See [the media library](https://github.com/HypeR-Proj/lib-fs-f2fs/blob/main/README.md)
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
