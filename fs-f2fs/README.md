<!-- SPDX-FileCopyrightText: 2026 roolrz -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Disposable F2FS integration fixture

Run from the synchronized workspace:

```sh
export OUT="$PWD/out-f2fs"
python3 tests/fs-f2fs/run.py
```

The entry prepares the generated execution view, builds the host formatter from
the checked-in service sources and builds the current Native SDK/kernel/worker/probe,
downloads the published I/O VM package, and runs seven QEMU boots against a new
128 MiB disposable data volume. It never builds I/O VM Linux. `native-f2fs` is
also discoverable by the system-test runner through `cases/native-f2fs.json`.

All generated files, including configuration, remain below `OUT`. The default
fixture is `$OUT/fixtures/f2fs`; an existing fixture is refused. To run again,
pass `--output "$OUT/fixtures/f2fs-second"`. The host needs the normal HypeR build
dependencies, Clang/LLVM with C++20 support, Python 3.11 or newer, `oras`, QEMU
AArch64 and the EROFS formatter. Fuchsia sources are part of the service
checkout; no source archive cache is required. The worker and probe are declared Cargo modules; no
source-side `app/Cargo.toml` or private C++ runtime is created.

The formatter and fixture builder can also be used through the execution view:

```sh
python3 project.py exec -- python3 tests/fs-f2fs/prepare.py --help
python3 project.py exec -- python3 tests/qemu/verify-f2fs.py --help
```

See [the F2FS service](https://github.com/HypeR-Proj/fs-f2fs/blob/main/README.md)
for independent module build commands. The complete entry writes its kernel,
SDK and test binaries from the same current checkout; mixing old artifacts can
produce misleading ABI failures.

Preparation refuses an existing output directory and validates both GPT copies.
The ordinary board packer formats and seeds F2FS. The fixture clones that disk,
installs test instrumentation into the cloned EROFS system volume, and preserves
the production data extent and truthful `deployment.json` filesystem declaration.
It never modifies the source board or bypasses the production formatter.

The acceptance test writes and synchronizes 4 MiB through ordinary Native file
APIs, renames it, creates 80 directory entries, enumerates them, unmounts, stops
QEMU, and starts a new instance. The second instance verifies every byte,
checks truncate/regrow zeroing and nested directory creation, removes the test tree and unmounts again.
Another write/verify pair kills QEMU without unmount after an acknowledged sync
and checks checkpointed data after restart. Each boot also reads the production-
seeded `vms.json`. A final instance writes and verifies 12 MiB, crossing the 8 MiB background
writeback threshold before explicit sync.
The smoke application only accesses the mounted service through ordinary file
APIs and does not depend on the media core. RAM-core and host fault-injection
tests belong to the service module gate. A failed system test retains its logs
and disposable disk for inspection.

A final streaming pair writes 48 MiB, restarts QEMU, and reads it twice under the
ordinary worker quota. The cold first read cannot be satisfied from the previous
boot's kernel cache, guarding against clean copies accumulating in the F2FS heap.
