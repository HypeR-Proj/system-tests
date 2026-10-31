<!-- SPDX-FileCopyrightText: 2026 roolrz -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# HypeR system test cases

This repository owns full-system acceptance cases, Native test applications,
QEMU drivers, performance checks and immutable fixture recipes. Reusable harness
code belongs to [test-framework](https://github.com/HypeR-Proj/test-framework).
GitHub topic coordination belongs to [ci](https://github.com/HypeR-Proj/ci).

```sh
python3 test-framework/runner.py list
python3 test-framework/runner.py run native-apps-aarch64
make test MODULE=system-tests
```

`cases/*.json` declares an id, architectures, an argument-array command, optional
variants/environment, timeout and fixture name. `fixtures.json` declares fixture
builders. Add a case here; the manually started system workflow derives its job
matrix from this registry, so adding an ordinary case needs no CI workflow edit.
See [native-apps](cases/native-apps.json) for a minimal declaration.

RISC-V ordinary Native cases and VM acceptance have separate jobs. Lifecycle
stress runs remain 12 rounds each for one and four CPUs, in independent stress
shards; reducing serial runtime does not reduce stress coverage. Native images
are built once per architecture and shared where the case accepts that fixture.
Feature-specific kernel tests still build their own selected configuration.

`self-test.py` runs case-driver regression tests without booting a whole system.
Those tests belong to this repository's automatic module check. Runtime cases
run only after an explicit system-verification request. Execution logs and
per-case elapsed times are retained by the framework under OUT/results.

The case entry point is [ci/run.sh](ci/run.sh). See [suite ownership](ci/README.md)
for the responsibilities of the case families and `profiling/` for Perfetto validation helpers.
