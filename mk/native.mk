# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

# Native image composition and acceptance fixtures.
.PHONY: fit-pack guest-itb native-initramfs test-native test-apps test-console \
	test-runtime-crash test-vm-smoke test-io-vm guest-smp-initramfs test-guest-smp

fit-pack:
	CARGO_TARGET_DIR="$(FIT_PACK_TARGET)" $(CARGO) build \
		--manifest-path "tools/fit-pack/Cargo.toml" --release

guest-itb: fit-pack
	@test "$(NATIVE_TEST_VM)" = 1 || { echo "guest images are not implemented for $(ARCH)" >&2; exit 2; }
	$(MAKE) -C "$(KERNEL_DIRECTORY)" guest-assets ARCH="$(ARCH)"
	"$(FIT_PACK)" "$(NATIVE_GUEST_ITB)" "$(NATIVE_GUEST_ARCH)" \
		"$(KERNEL_DIRECTORY)/target/guest/$(ARCH)/Image" \
		"$(NATIVE_GUEST_LOAD)" "$(NATIVE_GUEST_LOAD)" \
		"$(KERNEL_DIRECTORY)/target/guest/$(ARCH)/initramfs.cpio.gz"

# Isolated acceptance environment: a test-only init copies the formal payload
# into a distinct RAMFS mount, then invokes the unmodified stage-1 supervisor.
# Product board images always use independent system storage instead.
.PHONY: native-fixture-init
native-fixture-init: app-fetch
	CARGO_TARGET_DIR="$(APP_BOOT_CARGO_OUTPUT)" HYPER_LINK_MODE=static HYPER_ARCH="$(NATIVE_ARCH)" \
		HYPER_SYSROOT="$(SDK_OUTPUT)" HYPER_RUST_STD=1 \
		HYPER_CLANG="$(CLANG)" HYPER_LD="$(HYPER_LD)" \
		"$(SDK_OUTPUT)/bin/hyper-cargo" build --manifest-path bootstrap/stage1/Cargo.toml \
		-p hyper-bootstrap-stage1-init --bin hyper-native-test-init --release --locked --offline

native-initramfs: APP_BUILD_PROFILE = $(NATIVE_IMAGE_PROFILE)
native-initramfs: app app-boot native-fixture-init $(if $(filter development,$(NATIVE_IMAGE_PROFILE)),app-fixtures) $(NEWC_PACK) $(NATIVE_GUEST_PREREQUISITES)
	python3 -B scripts/pack-native-system.py \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--output "$(NATIVE_INITRAMFS).deployment" --initramfs "$(NATIVE_INITRAMFS)" \
		--deployment "$(APP_DEPLOYMENT)" --profile "$(NATIVE_IMAGE_PROFILE)" \
		$(SYSTEM_CONFIG_ARGS) \
		--apps "$(APP_OUTPUT)" --boot-apps "$(APP_BOOT_OUTPUT)" --sdk "$(SDK_OUTPUT)" \
		--std "$(NATIVE_STD_TEST_OUTPUT)" --arch "$(NATIVE_ARCH)" \
		--boot-components $(APP_BOOT_COMPONENTS) \
		--services "$(NATIVE_SERVICE_MANIFEST)" \
		--replace "init=$(APP_BOOT_CARGO_OUTPUT)/$(NATIVE_RUST_TARGET)/release/hyper-native-test-init" \
		--replace "bin/ps=$(NATIVE_PS_IMAGE)" \
		--replace "svc/vm-manager=$(NATIVE_VM_MANAGER)" \
		--replace "svc/vm-runtime=$(NATIVE_VM_RUNTIME)" \
		$(if $(strip $(NATIVE_ENTRY_MANIFEST)),--entries-from "$(NATIVE_ENTRY_MANIFEST)") \
		$(NATIVE_GUEST_ENTRY) \
		$(if $(strip $(NATIVE_VM_CONFIG)),0644 system:etc/hyper/vms.json "$(NATIVE_VM_CONFIG)") \
		$(NATIVE_EXTRA_ENTRIES)

	python3 -B tests/qemu/prepare-native-fixture.py \
		--composition "$(NATIVE_INITRAMFS).deployment" --output "$(NATIVE_INITRAMFS)" \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)"

NATIVE_QEMU_ENV = QEMU_MACHINE="$(QEMU_MACHINE)" QEMU_CPU="$(QEMU_CPU)" \
	QEMU_CPUS="$(QEMU_CPUS)" QEMU_MEMORY="$(QEMU_MEMORY)" \
	QEMU_BOOTARGS="$(QEMU_BOOTARGS)" HYPER_TEST_VM="$(NATIVE_TEST_VM)"

test-native: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-native-init.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(NATIVE_INITRAMFS)" \
		"$(QEMU_CPU)" "$(QEMU_CPUS)" "$(QEMU_MEMORY)" "$(QEMU_BOOTARGS)"

# GICv2 host and Linux guest coverage with the standard Native service profile.
.PHONY: test-native-gicv2
test-native-gicv2:
	@test "$(ARCH)" = aarch64 || { echo "GICv2 requires ARCH=aarch64" >&2; exit 2; }
	$(MAKE) test-native ARCH=aarch64 QEMU_CPU=max \
		QEMU_MACHINE=virt,virtualization=on,gic-version=2,dtb-randomness=on \
		NATIVE_INITRAMFS="$(APP_OUTPUT)/initramfs-gicv2.cpio"

# Keep instrumentation and the inspector-authorized workload in a dedicated
# fixture. The normal ps binary and production initramfs are unchanged.
.PHONY: stack-initramfs test-stack
stack-initramfs:
	mkdir -p "$(STACK_OUTPUT)"
	$(MAKE) native-initramfs NATIVE_INITRAMFS="$(STACK_OUTPUT)/initramfs.cpio" \
		NATIVE_PS_IMAGE="$(NATIVE_STD_TEST_OUTPUT)/std-dynamic"

test-stack: stack-initramfs
	$(MAKE) image CARGO_FEATURES="--features kernel-test,kernel-stack-audit"
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-stack.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(STACK_OUTPUT)/initramfs.cpio" \
		"$(STACK_OUTPUT)/qemu.log" --minimum-remaining "$(STACK_MINIMUM_REMAINING)" \
		--maximum-used "$(STACK_MAXIMUM_USED)"

.PHONY: test-boot-profiles
.PHONY: native-profile-probe
native-profile-probe: app-fetch
	CARGO_TARGET_DIR="$(APP_BOOT_CARGO_OUTPUT)" HYPER_LINK_MODE=static HYPER_ARCH="$(NATIVE_ARCH)" \
		HYPER_SYSROOT="$(SDK_OUTPUT)" HYPER_RUST_STD=1 \
		HYPER_CLANG="$(CLANG)" HYPER_LD="$(HYPER_LD)" \
		"$(SDK_OUTPUT)/bin/hyper-cargo" build --manifest-path bootstrap/stage1/Cargo.toml \
		-p hyper-bootstrap-stage1-init --bin hyper-profile-storage-probe --release --locked --offline

test-boot-profiles: NATIVE_SERVICE_MANIFEST = $(CURDIR)/bootstrap/stage1/init/config/services-console-only.json
test-boot-profiles: NATIVE_VM_CONFIG =
test-boot-profiles: image native-initramfs native-profile-probe
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-boot-profiles.py \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" \
		--entries "$(NATIVE_INITRAMFS).deployment/fixture.entries.json" \
		--probe "$(APP_BOOT_CARGO_OUTPUT)/$(NATIVE_RUST_TARGET)/release/hyper-profile-storage-probe" \
		--output "$(APP_OUTPUT)/boot-profiles" --packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)"

test-apps: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-apps.py "$(QEMU)" "$(KERNEL_IMAGE)" \
		"$(NATIVE_INITRAMFS)" "$(APP_OUTPUT)/apps.log"

.PHONY: test-clock
# The no-RTC case intentionally tests Native std without starting a guest VM.
test-clock: NATIVE_SERVICE_MANIFEST = $(CURDIR)/bootstrap/stage1/init/config/services-console-only.json
test-clock: NATIVE_VM_CONFIG =
test-clock: image native-initramfs
	@test "$(ARCH)" = aarch64 || { echo "clock fixture requires aarch64" >&2; exit 2; }
	python3 tests/qemu/verify-clock.py "$(QEMU)" "$(KERNEL_IMAGE)" \
		"$(NATIVE_INITRAMFS)" "$(APP_OUTPUT)/clock"

test-console: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-console.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(NATIVE_INITRAMFS)" "$(APP_OUTPUT)/console.log"

.PHONY: test-terminal
test-terminal: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-terminal.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(NATIVE_INITRAMFS)" "$(APP_OUTPUT)/terminal.log"

.PHONY: test-shell
test-shell: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-shell.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(NATIVE_INITRAMFS)" "$(APP_OUTPUT)/shell.log"

.PHONY: test-editor
test-editor: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-editor.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(NATIVE_INITRAMFS)" "$(APP_OUTPUT)/editor.log"

.PHONY: test-less
test-less: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-less.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(NATIVE_INITRAMFS)" "$(APP_OUTPUT)/less.log"

# Correctness workloads only; hardware throughput benchmarks remain manual.
STRESS_SEED ?= 175
STRESS_ROUNDS ?= 12
STRESS_LOG ?= $(APP_OUTPUT)/lifecycle-stress-$(QEMU_CPUS).log
.PHONY: test-lifecycle-stress
test-lifecycle-stress: image native-initramfs
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-lifecycle-stress.py \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" --initramfs "$(NATIVE_INITRAMFS)" \
		--log "$(STRESS_LOG)" --seed "$(STRESS_SEED)" --rounds "$(STRESS_ROUNDS)"

# Native authority fixture boots as /init, independently of product services.
test-vm-smoke: image app-fetch $(NEWC_PACK)
	@test "$(ARCH)" = riscv64 -o "$(ARCH)" = aarch64 || { echo "VM smoke fixture requires aarch64 or riscv64" >&2; exit 2; }
	CARGO_TARGET_DIR="$(APP_BOOT_CARGO_OUTPUT)" HYPER_LINK_MODE=static HYPER_ARCH="$(NATIVE_ARCH)" \
		HYPER_SYSROOT="$(SDK_OUTPUT)" HYPER_RUST_STD=1 \
		HYPER_CLANG="$(CLANG)" HYPER_LD="$(HYPER_LD)" \
		"$(SDK_OUTPUT)/bin/hyper-cargo" build --manifest-path app/Cargo.toml \
		-p hyper-vm-smoke --release --locked --offline
	mkdir -p "$(APP_OUTPUT)"
	python3 -B scripts/pack-native-initramfs.py \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--output "$(APP_OUTPUT)/vm-smoke.cpio" \
		0755 init "$(APP_BOOT_CARGO_OUTPUT)/$(NATIVE_RUST_TARGET)/release/hyper-vm-smoke" \
		symlink lib lib64 \
		0755 lib64/ld-hyper-$(NATIVE_ARCH).so "$(NATIVE_LOADER)" \
		0755 lib64/userspace-loader-hyper-$(NATIVE_ARCH) "$(NATIVE_USERSPACE_LOADER)" \
		0755 $(NATIVE_LIBRARY_DIRECTORY)/libsystem.so "$(NATIVE_RUNTIME_LIBRARY)"
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-vm-smoke.py \
		"$(QEMU)" "$(KERNEL_IMAGE)" "$(APP_OUTPUT)/vm-smoke.cpio" \
		"$(APP_OUTPUT)/vm-smoke-$(QEMU_CPUS).log"

.PHONY: test-filesystem-failure
# A media-independent real process/VMO/RPC test; no backend fault modes are
# compiled or installed in the normal system image.
test-filesystem-failure: image app-fetch $(NEWC_PACK)
	CARGO_TARGET_DIR="$(APP_BOOT_CARGO_OUTPUT)" HYPER_LINK_MODE=static HYPER_ARCH="$(NATIVE_ARCH)" \
		HYPER_SYSROOT="$(SDK_OUTPUT)" HYPER_RUST_STD=1 \
		HYPER_CLANG="$(CLANG)" HYPER_LD="$(HYPER_LD)" \
		"$(SDK_OUTPUT)/bin/hyper-cargo" build --manifest-path app/Cargo.toml \
		-p hyper-fs-failure --release --locked --offline
	mkdir -p "$(APP_OUTPUT)"
	python3 -B scripts/pack-native-initramfs.py \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--output "$(APP_OUTPUT)/filesystem-failure.cpio" \
		--library-dir "$(SDK_OUTPUT)/lib" --library-dir "$(SDK_OUTPUT)/lib64" \
		0755 init "$(APP_BOOT_CARGO_OUTPUT)/$(NATIVE_RUST_TARGET)/release/hyper-fs-failure" \
		symlink lib lib64 \
		0755 lib64/ld-hyper-$(NATIVE_ARCH).so "$(NATIVE_LOADER)" \
		0755 lib64/userspace-loader-hyper-$(NATIVE_ARCH) "$(NATIVE_USERSPACE_LOADER)" \
		0755 $(NATIVE_LIBRARY_DIRECTORY)/libsystem.so "$(NATIVE_RUNTIME_LIBRARY)"
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-filesystem-failure.py \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" \
		--initramfs "$(APP_OUTPUT)/filesystem-failure.cpio" \
		--log "$(APP_OUTPUT)/filesystem-failure.log"

# Cross-VM fixture consumes the external appliance without patching its rootfs.
test-io-vm: image app-fetch fit-pack $(NEWC_PACK)
	@test "$(ARCH)" = aarch64 || { echo "I/O VM acceptance requires aarch64" >&2; exit 2; }
	@package="$(IO_VM_PACKAGE)"; \
	if test -z "$$package"; then \
		package=$$(python3 -B scripts/fetch-io-vm.py --platform qemu \
			--reference "$(IO_VM_REFERENCE)" --oras "$(IO_VM_ORAS)") || exit $$?; \
	fi; \
	python3 -B tests/qemu/verify-io-vm.py prepare --package "$$package" \
		--fit-pack "$(FIT_PACK)" --output "$(APP_OUTPUT)/io-vm" --test "$(IO_VM_TEST)"
	HYPER_ARCH="$(NATIVE_ARCH)" HYPER_SYSROOT="$(SDK_OUTPUT)" \
		"$(SDK_OUTPUT)/bin/hyper-cargo" fetch --manifest-path tests/native/io-smoke/Cargo.toml --locked
	CARGO_TARGET_DIR="$(APP_BOOT_CARGO_OUTPUT)" HYPER_LINK_MODE=static HYPER_ARCH="$(NATIVE_ARCH)" \
		HYPER_SYSROOT="$(SDK_OUTPUT)" HYPER_RUST_STD=1 \
		HYPER_CLANG="$(CLANG)" HYPER_LD="$(HYPER_LD)" \
		"$(SDK_OUTPUT)/bin/hyper-cargo" build --manifest-path tests/native/io-smoke/Cargo.toml \
		--release --locked --offline
	python3 -B scripts/pack-native-initramfs.py \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--output "$(APP_OUTPUT)/io-vm.cpio" \
		--library-dir "$(APP_BOOT_CARGO_OUTPUT)/$(NATIVE_RUST_TARGET)/release/deps" \
		--library-dir "$(SDK_OUTPUT)/lib" --library-dir "$(SDK_OUTPUT)/lib64" \
		0755 init "$(APP_BOOT_CARGO_OUTPUT)/$(NATIVE_RUST_TARGET)/release/hyper-io-smoke" \
		symlink lib lib64 \
		0755 lib64/ld-hyper-$(NATIVE_ARCH).so "$(NATIVE_LOADER)" \
		0755 lib64/userspace-loader-hyper-$(NATIVE_ARCH) "$(NATIVE_USERSPACE_LOADER)" \
		0755 $(NATIVE_LIBRARY_DIRECTORY)/libsystem.so "$(NATIVE_RUNTIME_LIBRARY)" \
		0644 vm/io.itb "$(APP_OUTPUT)/io-vm/io.itb" \
		0644 etc/hyper/io-vms.json "$(APP_OUTPUT)/io-vm/io-vms.json" \
		0644 vm/business.itb "$(APP_OUTPUT)/io-vm/business.itb"
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-io-vm.py run \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" --initramfs "$(APP_OUTPUT)/io-vm.cpio" \
		--log "$(APP_OUTPUT)/io-vm-$(IO_VM_TEST)-$(QEMU_CPUS).log" --test "$(IO_VM_TEST)"

# Explicit fixture target; ordinary app builds never enable this feature.
test-runtime-crash: image native-initramfs
	@test "$(NATIVE_TEST_VM)" = 1 || { echo "VM runtime acceptance is not implemented for $(ARCH)" >&2; exit 2; }
	python3 -B tests/qemu/vm_fixtures.py "$(NATIVE_VM_CONFIG)" "$(APP_OUTPUT)/fleet-fixtures"
	$(MAKE) -o app-fetch app APP_OUTPUT="$(APP_OUTPUT)/runtime-crash-apps" \
		APP_BUILD_PROFILE="$(NATIVE_IMAGE_PROFILE)" \
		APP_FEATURES=hyper-vm-runtime/test-runtime-crash
	$(MAKE) -o app native-initramfs \
		APP_OUTPUT="$(APP_OUTPUT)/runtime-crash-apps" \
		NATIVE_VM_CONFIG="$(APP_OUTPUT)/fleet-fixtures/victim-first.json" \
		NATIVE_INITRAMFS="$(APP_OUTPUT)/runtime-crash.cpio"
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-runtime-crash.py "$(QEMU)" "$(KERNEL_IMAGE)" \
		"$(APP_OUTPUT)/runtime-crash.cpio" "$(APP_OUTPUT)/runtime-crash.log"
	$(MAKE) -o app native-initramfs \
		APP_OUTPUT="$(APP_OUTPUT)/runtime-crash-apps" \
		NATIVE_VM_CONFIG="$(APP_OUTPUT)/fleet-fixtures/survivor-first.json" \
		NATIVE_INITRAMFS="$(APP_OUTPUT)/runtime-crash-reordered.cpio"
	$(NATIVE_QEMU_ENV) python3 tests/qemu/verify-runtime-crash.py "$(QEMU)" "$(KERNEL_IMAGE)" \
		"$(APP_OUTPUT)/runtime-crash-reordered.cpio" "$(APP_OUTPUT)/runtime-crash-reordered.log" \
		--isolation-only

.PHONY: test-storage-failure
test-storage-failure: APP_BUILD_PROFILE = development
test-storage-failure: image app app-boot native-fixture-init fit-pack $(NEWC_PACK)
	@test "$(ARCH)" = aarch64 || { echo "storage recovery acceptance requires aarch64" >&2; exit 2; }
	@package="$(IO_VM_PACKAGE)"; \
	if test -z "$$package"; then \
		package=$$(python3 -B scripts/fetch-io-vm.py --platform qemu \
			--reference "$(IO_VM_REFERENCE)" --oras "$(IO_VM_ORAS)") || exit $$?; \
	fi; \
	python3 -B scripts/io-vm-images.py --package "$$package" \
		--board "$(BOARD_CONFIG)" --fit-pack "$(FIT_PACK)" \
		--output "$(APP_OUTPUT)/storage-failure/io.itb"
	HYPER_ARCH="$(NATIVE_ARCH)" HYPER_SYSROOT="$(SDK_OUTPUT)" \
		HYPER_CLANG="$(CLANG)" HYPER_LD="$(HYPER_LD)" \
		python3 -B tests/qemu/verify-storage-failure.py prepare \
		--apps "$(APP_OUTPUT)" --boot-apps "$(APP_BOOT_OUTPUT)" --sdk "$(SDK_OUTPUT)" \
		--fixture-init "$(APP_BOOT_CARGO_OUTPUT)/$(NATIVE_RUST_TARGET)/release/hyper-native-test-init" \
		--io-image "$(APP_OUTPUT)/storage-failure/io.itb" \
		--boot-components $(APP_BOOT_COMPONENTS) \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--output "$(APP_OUTPUT)/storage-failure"
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-storage-failure.py run \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" \
		--output "$(APP_OUTPUT)/storage-failure"

# Definition admission is a bootstrap result, independent of autostart outcomes.
.PHONY: test-fleet-config
test-fleet-config: APP_BUILD_PROFILE = $(NATIVE_IMAGE_PROFILE)
test-fleet-config: image app
	@test "$(NATIVE_TEST_VM)" = 1 || { echo "VM fleet acceptance is not implemented for $(ARCH)" >&2; exit 2; }
	python3 -B tests/qemu/vm_fixtures.py "$(NATIVE_VM_CONFIG)" "$(APP_OUTPUT)/fleet-fixtures"
	@for case in empty no-autostart malformed missing-image start-failure; do \
		$(MAKE) -o app native-initramfs \
			NATIVE_VM_CONFIG="$(APP_OUTPUT)/fleet-fixtures/$$case.json" \
			NATIVE_INITRAMFS="$(APP_OUTPUT)/fleet-$$case.cpio" || exit $$?; \
		$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-fleet-config.py \
			"$(QEMU)" "$(KERNEL_IMAGE)" "$(APP_OUTPUT)/fleet-$$case.cpio" \
			"$(APP_OUTPUT)/fleet-$$case.log" "$$case" || exit $$?; \
	done

# Test-only binaries: no power fault injection enters ordinary app artifacts.
.PHONY: test-power-crash power-crash-case
test-power-crash: image app
	@test "$(ARCH)" = aarch64 || { echo "power crash acceptance requires AArch64" >&2; exit 2; }
	@for state in dormant pending powered-off; do \
		$(MAKE) -o image -o app power-crash-case POWER_CRASH_STATE=$$state || exit $$?; \
	done

power-crash-case:
	@case "$(POWER_CRASH_STATE)" in dormant|pending|powered-off) ;; *) exit 2 ;; esac
	HYPER_TEST_POWER_CRASH="$(POWER_CRASH_STATE)" \
		$(MAKE) -o app-fetch app APP_OUTPUT="$(APP_OUTPUT)/power-crash-$(POWER_CRASH_STATE)-apps" \
		APP_BUILD_PROFILE="$(NATIVE_IMAGE_PROFILE)" \
		APP_FEATURES=hyper-vm-runtime/test-power-crash
	$(MAKE) -o app native-initramfs ARCH=aarch64 \
		APP_OUTPUT="$(APP_OUTPUT)/power-crash-$(POWER_CRASH_STATE)-apps" \
		NATIVE_VM_CONFIG="$(CURDIR)/bootstrap/stage1/init/tests/config/vms-power-crash.json" \
		NATIVE_GUEST_ITB="$(KERNEL_DIRECTORY)/target/guest/aarch64/alpine-smp.itb" \
		NATIVE_INITRAMFS="$(APP_OUTPUT)/power-crash-$(POWER_CRASH_STATE).cpio"
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-power-crash.py "$(QEMU)" "$(KERNEL_IMAGE)" \
		"$(APP_OUTPUT)/power-crash-$(POWER_CRASH_STATE).cpio" \
		"$(APP_OUTPUT)/power-crash-$(POWER_CRASH_STATE).log" "$(POWER_CRASH_STATE)"

# Select SMP policy from JSON; reuse the same guest payload bundle.
# The same archive exercises both hardware GIC backends and host overcommit.
guest-smp-initramfs: APP_BUILD_PROFILE = $(NATIVE_IMAGE_PROFILE)
guest-smp-initramfs: app $(NEWC_PACK)
	@test "$(ARCH)" = aarch64 || { echo "guest SMP acceptance requires AArch64" >&2; exit 2; }
	$(MAKE) -o app native-initramfs ARCH=aarch64 \
		NATIVE_VM_CONFIG="$(NATIVE_SMP_VM_CONFIG)" \
		NATIVE_GUEST_ITB="$(KERNEL_DIRECTORY)/target/guest/aarch64/alpine-smp.itb" \
		NATIVE_INITRAMFS="$(NATIVE_SMP_INITRAMFS)"

test-guest-smp: image guest-smp-initramfs
	$(NATIVE_QEMU_ENV) GUEST_CPUS="$$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["virtual-machines"][0]["configuration"]["vcpus"])' "$(NATIVE_SMP_VM_CONFIG)")" \
		python3 tests/qemu/verify-guest-smp.py "$(QEMU)" "$(KERNEL_IMAGE)" \
		"$(NATIVE_SMP_INITRAMFS)" "$(APP_OUTPUT)/guest-smp.log"

.PHONY: test-services
test-services: image native-initramfs
	@fixture=$$(mktemp -d "$(APP_OUTPUT)/services.XXXXXX") && \
	python3 -B tests/qemu/verify-services.py prepare \
		--composition "$(NATIVE_INITRAMFS).deployment" --output "$$fixture/prepared" \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" && \
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-services.py run \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" \
		--initramfs "$$fixture/prepared/services.cpio" --log "$$fixture/services.log" && \
	python3 -B tests/qemu/verify-services.py prepare-rejected-console \
		--composition "$(NATIVE_INITRAMFS).deployment" --output "$$fixture/rejected-console" \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" && \
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-services.py run-rejected-console \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" \
		--initramfs "$$fixture/rejected-console/services.cpio" --log "$$fixture/rejected-console.log"

.PHONY: test-service-failure
.PHONY: test-process-retirement
test-process-retirement: image native-initramfs
	@fixture=$$(mktemp -d "$(APP_OUTPUT)/retirement.XXXXXX") && \
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-process-retirement.py \
		--composition "$(NATIVE_INITRAMFS).deployment" --output "$$fixture/prepared" \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)"

test-service-failure: image
	$(MAKE) native-initramfs ARCH="$(ARCH)" \
		APP_OUTPUT="$(APP_OUTPUT)/service-failure-apps" \
		APP_FEATURES=hyper-component-manager/test-service-failure,hyper-session-manager/test-service-failure \
		NATIVE_INITRAMFS="$(APP_OUTPUT)/service-failure.cpio"
	@fixture=$$(mktemp -d "$(APP_OUTPUT)/service-failure.XXXXXX") && \
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-service-failure.py \
		--composition "$(APP_OUTPUT)/service-failure.cpio.deployment" --output "$$fixture" \
		--packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)"

# Separate real-storage fault fixtures keep the normal disk and Native image intact.
.PHONY: test-service-provider-failure
test-service-provider-failure: image
	@test "$(ARCH)" = aarch64 || { echo "provider failure tests require aarch64" >&2; exit 2; }
	@mkdir -p "$(APP_OUTPUT)"
	@fixture=$$(mktemp -d "$(APP_OUTPUT)/service-provider-failure.XXXXXX") && \
	$(MAKE) board-initramfs board-guest-images BOARD=qemu \
		BOARD_CONFIG="$(CURDIR)/boards/qemu.json" BOARD_OUTPUT="$$fixture" \
		APP_OUTPUT="$(APP_OUTPUT)/service-failure-apps" \
		APP_FEATURES=hyper-component-manager/test-service-failure,hyper-session-manager/test-service-failure && \
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-service-failure.py \
		--composition "$$fixture" --output "$$fixture/failures" \
		--board "$$fixture/deployment.json" --apps "$(APP_OUTPUT)/service-failure-apps" \
		--mkfs-erofs "$(EROFS_MKFS)" --packer "$(NEWC_PACK)" --strip "$(LLVM_STRIP)" \
		--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)"

# Production instrumentation is opt-in and never requires kernel-test.
PMU_LOG ?= $(APP_OUTPUT)/pmu-$(QEMU_CPUS).log
.PHONY: test-pmu
test-pmu: export KERNEL_FEATURES = rtc runtime-symbols crash-console pmu-profiling
test-pmu: image native-initramfs
	@test "$(ARCH)" = aarch64 || { echo "PMUv3 acceptance requires aarch64" >&2; exit 2; }
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/verify-pmu.py "$(QEMU)" "$(KERNEL_IMAGE)" \
		"$(NATIVE_INITRAMFS)" "$(PMU_LOG)" \
		--kernel "$(KERNEL_OUTPUT)/hyper" --nm "$(LLVM_NM)"

# Every trace body leaves through the filesystem backend. The UART carries
# command receipts only; each run owns its disk and extracts after QEMU exits.
TRACE_LOG ?= $(APP_OUTPUT)/trace-$(QEMU_CPUS).log
.PHONY: test-trace test-trace-remote
test-trace test-trace-remote: export KERNEL_FEATURES = rtc runtime-symbols crash-console kernel-trace
test-trace test-trace-remote: APP_EXTRA_BINS += hyper-perf hyper-test-report hyper-lifecycle-stress
test-trace test-trace-remote: image app
	@test "$(ARCH)" = aarch64 || { echo "disk-backed trace acceptance requires aarch64" >&2; exit 2; }
	mkdir -p "$(BOARD_TEST_OUTPUT)" "$(dir $(TRACE_LOG))"
	@fixture=$$(mktemp -d "$(BOARD_TEST_OUTPUT)/trace.XXXXXX") && \
	python3 -B tests/qemu/trace_artifacts.py --board-output "$$fixture/board.json" && \
	$(MAKE) -o image -o app board-image BOARD=qemu \
		BOARD_CONFIG="$$fixture/board.json" BOARD_OUTPUT="$$fixture" \
		BOARD_IMAGE="$$fixture/disk.img" \
		BOARD_EXTRA_ENTRIES='0755 bin/perf "$(APP_OUTPUT)/perf" 0755 bin/test-report "$(APP_OUTPUT)/test-report" 0755 bin/lifecycle-stress "$(APP_OUTPUT)/lifecycle-stress"' && \
	$(NATIVE_QEMU_ENV) python3 -B tests/qemu/$(if $(filter test-trace-remote,$@),verify-trace-remote.py,verify-trace.py) \
		$(if $(filter test-trace-remote,$@),--qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" --initramfs "$$fixture/bootstrap.cpio" --log "$(TRACE_LOG)","$(QEMU)" "$(KERNEL_IMAGE)" "$$fixture/bootstrap.cpio" "$(TRACE_LOG)") \
		--disk "$$fixture/disk.img" --board "$$fixture/deployment.json"

# Functional benchmark acceptance only: TCG latency is never a CI threshold.
SCHED_BENCH_LOG ?= $(BOARD_TEST_OUTPUT)/scheduler-$(QEMU_CPUS).log
SCHED_BENCH_DATA_MIB ?= 512
.PHONY: test-scheduler-bench
test-scheduler-bench: APP_EXTRA_BINS += hyper-sched-bench hyper-test-report
test-scheduler-bench: image app
	@test "$(ARCH)" = aarch64 || { echo "scheduler benchmark fixture requires aarch64" >&2; exit 2; }
	mkdir -p "$(BOARD_TEST_OUTPUT)" "$(dir $(SCHED_BENCH_LOG))"
	@fixture=$$(mktemp -d "$(BOARD_TEST_OUTPUT)/scheduler.XXXXXX") && \
	python3 -B tests/qemu/trace_artifacts.py --board-output "$$fixture/board.json" --data-mib "$(SCHED_BENCH_DATA_MIB)" && \
	$(MAKE) -o image -o app board-image BOARD=qemu \
		BOARD_CONFIG="$$fixture/board.json" BOARD_OUTPUT="$$fixture" \
		BOARD_IMAGE="$$fixture/disk.img" \
		BOARD_EXTRA_ENTRIES='0755 bin/sched-bench "$(APP_OUTPUT)/sched-bench" 0755 bin/test-report "$(APP_OUTPUT)/test-report"' && \
	$(NATIVE_QEMU_ENV) python3 -B tests/performance/scheduler/run.py \
		--functional --qemu "$(QEMU)" --image "$(KERNEL_IMAGE)" \
		--initramfs "$$fixture/bootstrap.cpio" --disk "$$fixture/disk.img" \
		--board "$$fixture/deployment.json" --cpus "$(QEMU_CPUS)" \
		--output "$$fixture/results" --log "$(SCHED_BENCH_LOG)"
