#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise CI dispatch without compiling or booting; retain acceptance coverage."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SuiteTests(unittest.TestCase):
    def run_suite(self, suite, **environment):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / 'commands.jsonl'
            for name in ('make', 'python3', 'cp'):
                program = directory / name
                program.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
with open(os.environ['COMMAND_LOG'], 'a') as log:
    command = [os.path.basename(sys.argv[0]), *sys.argv[1:]]
    if 'project.py' in sys.argv:
        command += [os.environ.get('KERNEL_CI_SUITE'), os.environ.get('SDK_CI_ARCH')]
    log.write(json.dumps(command) + '\\n')
if 'scripts/fetch-io-vm.py' in sys.argv:
    print('/fixture/io-vm')
''')
                program.chmod(0o755)
            env = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ['PATH'],
                       COMMAND_LOG=str(log), **environment)
            env['HYPER_BUILD_VIEW'] = '1'
            env.pop('CI_NATIVE_FIXTURE', None)
            if 'CI_NATIVE_FIXTURE' in environment:
                env['CI_NATIVE_FIXTURE'] = environment['CI_NATIVE_FIXTURE']
            env.pop('IO_VM_PACKAGE', None)
            if 'IO_VM_PACKAGE' in environment:
                env['IO_VM_PACKAGE'] = environment['IO_VM_PACKAGE']
            subprocess.run(['sh', 'tests/ci/run.sh', suite], cwd=ROOT, env=env, check=True)
            return [json.loads(line) for line in log.read_text().splitlines()]

    def test_gicv3_aggregate_retains_all_runtime_contracts(self):
        commands = self.run_suite('native-gicv3')
        targets = {arg for call in commands if call[0] == 'make'
                   for arg in call[1:] if arg.startswith('test-')}
        self.assertEqual(targets, {
            'test-native', 'test-console', 'test-terminal', 'test-apps', 'test-shell', 'test-services',
            'test-fleet-config', 'test-storage-failure', 'test-filesystem-failure',
            'test-runtime-crash', 'test-vm-smoke', 'test-editor', 'test-less',
            'test-service-failure', 'test-process-retirement', 'test-lifecycle-stress',
            'test-boot-profiles',
        })

    def test_board_shards_keep_minimum_stacks_and_instrumentation(self):
        calls = [call for call in self.run_suite('board-storage') if call[0] == 'make']
        self.assertEqual({call[1] for call in calls}, {
            'test-board-storage', 'test-boot-system', 'test-alpine-rootfs',
            'test-board-business', 'test-board-broker', 'test-userspace-device',
            'test-product-config',
        })
        for call in calls:
            if call[1] == 'test-product-config':
                self.assertFalse(any(arg.startswith(('STACK_AUDIT=', 'CARGO_FEATURES='))
                                     for arg in call))
                continue
            self.assertIn('STACK_AUDIT=1', call)
            self.assertIn('STACK_MINIMUM_REMAINING=4096', call)
            self.assertIn('CARGO_FEATURES=--features kernel-test,kernel-stack-audit', call)
            self.assertIn('IO_VM_PACKAGE=/fixture/io-vm', call)
            config = next(arg.split('=', 1)[1] for arg in call if arg.startswith('CONFIG_FILE='))
            self.assertIn('CONFIG_KERNEL_STACK_SIZE_KB=16', Path(config).read_text().splitlines())

    def test_image_consumer_checks_artifact_and_skips_only_build_prerequisites(self):
        calls = self.run_suite('native-console', CI_NATIVE_FIXTURE='/fixture/native', CI_ARCH='riscv64')
        self.assertEqual(calls[0][1:4], ['-B', 'tests/ci/native-fixture.py', 'check'])
        make, = [call for call in calls if call[0] == 'make']
        self.assertEqual(make[1:6], ['-o', 'image', '-o', 'native-initramfs', 'test-console'])
        self.assertIn('ARCH=riscv64', make)
        self.assertIn('KERNEL_IMAGE=/fixture/native/hyper.img', make)
        self.assertIn('NATIVE_INITRAMFS=/fixture/native/initramfs.cpio', make)

    def test_local_probe_builds_its_own_images(self):
        calls = self.run_suite('native-shell')
        self.assertEqual(calls, [['make', 'test-shell', 'ARCH=aarch64']])

    def test_smp_consumers_share_images_but_retain_host_topology(self):
        for suite, cpus, machine in (
                ('native-smp-gicv3', 4, None),
                ('native-smp-gicv2-guest', 4, 'virt,virtualization=on,gic-version=2'),
                ('native-smp-overcommit', 1, None)):
            with self.subTest(suite=suite):
                calls = self.run_suite(suite, CI_NATIVE_FIXTURE='/fixture/smp')
                self.assertIn('check', calls[0])
                self.assertEqual(calls[0][calls[0].index('--arch') + 1], 'aarch64')
                make, = [call for call in calls if call[0] == 'make']
                self.assertEqual(make[1:6], ['-o', 'image', '-o', 'guest-smp-initramfs', 'test-guest-smp'])
                self.assertIn('KERNEL_IMAGE=/fixture/smp/hyper.img', make)
                self.assertIn('NATIVE_SMP_INITRAMFS=/fixture/smp/initramfs-smp.cpio', make)
                self.assertIn(f'QEMU_CPUS={cpus}', make)
                if machine:
                    self.assertIn(f'QEMU_MACHINE={machine}', make)

    def test_shared_producer_builds_both_aarch64_archives_in_one_make(self):
        for arch in ('aarch64', 'riscv64'):
            calls = self.run_suite('native-fixture', CI_ARCH=arch)
            make, = [call for call in calls if call[0] == 'make']
            self.assertIn('image', make)
            self.assertIn('native-initramfs', make)
            self.assertEqual('guest-smp-initramfs' in make, arch == 'aarch64')
            self.assertIn(f'ARCH={arch}', make)
            self.assertIn('pack', calls[-1])

    def test_registered_shards_preserve_aggregate_runtime_coverage(self):
        registry = {data['id']: data for path in (ROOT / 'tests/cases').glob('*.json')
                    for data in [json.loads(path.read_bytes())]}
        groups = {
            'native-gicv2': ['native-gicv2-boot', 'native-gicv2-console', 'native-gicv2-runtime-crash'],
            'native-smp-stack': ['native-smp-gicv3', 'native-smp-gicv2-guest',
                                 'native-smp-gicv2-retirement', 'native-smp-overcommit',
                                 'native-power-retirement', 'native-stack-audit'],
            'native-services': ['native-service-lifecycle', 'native-service-failure', 'native-boot-profiles'],
            'riscv64-vm': ['riscv64-fleet', 'riscv64-vm-smoke', 'riscv64-runtime-crash', 'native-stack-audit'],
        }
        def runtime_calls(calls):
            return sorted((target, next(arg for arg in call if arg.startswith('ARCH=')),
                           next((arg for arg in call if arg.startswith('QEMU_CPUS=')), 'default'))
                          for call in calls if call[0] == 'make'
                          for target in call if target.startswith('test-'))
        for aggregate, shards in groups.items():
            with self.subTest(aggregate=aggregate):
                self.assertNotIn(aggregate, registry)
                arch = 'riscv64' if aggregate.startswith('riscv64') else 'aarch64'
                calls = []
                for name in shards:
                    case = registry[name]
                    self.assertIn(arch, case['architectures'])
                    calls.extend(self.run_suite(case['command'][-1], CI_ARCH=arch,
                                                **case.get('environment', {})))
                self.assertEqual(runtime_calls(calls), runtime_calls(self.run_suite(aggregate)))
        for name in ('native-gicv2-boot', 'native-gicv2-console'):
            self.assertEqual(registry[name]['fixture'], 'native')
        for name in ('native-smp-gicv3', 'native-smp-gicv2-guest', 'native-smp-overcommit'):
            self.assertEqual(registry[name]['fixture'], 'native')
        for name in groups['native-gicv2']:
            self.assertEqual(registry[name]['environment']['QEMU_MACHINE'],
                             'virt,virtualization=on,gic-version=2,dtb-randomness=on')
        for name in ('native-smp-gicv2-retirement', 'native-power-retirement', 'native-stack-audit'):
            self.assertNotIn('fixture', registry[name])

    def test_selected_appliance_is_used_without_registry_lookup(self):
        for suite in ('io-vm', 'io-network', 'io-network-mmio', 'board-storage'):
            with self.subTest(suite=suite):
                calls = self.run_suite(suite, IO_VM_PACKAGE='/candidate/io package')
                self.assertFalse(any('scripts/fetch-io-vm.py' in call for call in calls))
                make = [call for call in calls if call[0] == 'make']
                self.assertTrue(make)
                for call in make:
                    self.assertIn('IO_VM_PACKAGE=/candidate/io package', call)

    def test_gicv3_trap_probe_explicitly_enables_master_test_gate(self):
        calls = [call for call in self.run_suite('native-vm') if call[0] == 'make']
        feature_builds = [call for call in calls
                          if any(arg.startswith('CARGO_FEATURES=') for arg in call)]
        self.assertEqual(len(feature_builds), 1)
        self.assertIn('CARGO_FEATURES=--features kernel-test,kernel-vgic-tc-test',
                      feature_builds[0])

    def test_old_workflows_forward_module_suites_to_their_owners(self):
        forwarding = {
            'quality': [('manifest', None, None), ('kernel', 'quality', None)],
            'scripts': [('ci', None, None), ('build', None, None),
                        ('terminal-service', None, None), ('kernel', 'scripts', None)],
            'kernel-configurations': [('kernel', 'configurations', None)],
            'native-sdk': [('toolchain', None, 'aarch64')],
            'riscv64-sdk': [('toolchain', None, 'riscv64')],
        }
        for suite in ('aarch64-build', 'aarch64-production', 'aarch64-self-test',
                      'aarch64-compact', 'aarch64-qemu', 'riscv64-qemu', 'x86_64-build'):
            forwarding[suite] = [('kernel', suite, None)]
        registered = {json.loads(path.read_bytes())['id'] for path in (ROOT / 'tests/cases').glob('*.json')}
        for suite, owners in forwarding.items():
            with self.subTest(suite=suite):
                self.assertNotIn(suite, registered)
                self.assertEqual(self.run_suite(suite), [
                    ['python3', 'project.py', 'test', owner, kernel, sdk]
                    for owner, kernel, sdk in owners])

    def test_riscv_runtime_does_not_repeat_sdk_or_host_tests(self):
        calls = [call for call in self.run_suite('riscv64-runtime') if call[0] == 'make']
        self.assertTrue(any('test-native' in call for call in calls))
        self.assertTrue(any('test-vm-smoke' in call for call in calls))
        for call in calls:
            self.assertFalse({'sdk-check', 'sdk-test', 'app-check', 'app-test', 'app-sdk-test'} & set(call))

    def test_old_riscv_aggregate_retains_sdk_and_runtime_coverage(self):
        calls = self.run_suite('riscv64-native')
        self.assertEqual(calls[0], ['python3', 'project.py', 'test', 'toolchain', None, 'riscv64'])
        self.assertEqual(calls[1:], self.run_suite('riscv64-runtime'))

    def test_minimal_product_boot_has_no_kernel_test_features(self):
        calls = [call for call in self.run_suite('board-product-config') if call[0] == 'make']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 'test-product-config')
        self.assertIn('IO_VM_PACKAGE=/fixture/io-vm', calls[0])
        self.assertFalse(any('kernel-test' in arg or 'STACK_AUDIT=' in arg for arg in calls[0]))

    def test_mmio_compatibility_has_its_own_network_lifecycle_suite(self):
        for suite, target in [('io-network', 'test-board-network'),
                              ('io-network-mmio', 'test-board-mmio')]:
            calls = [call for call in self.run_suite(suite) if call[0] == 'make']
            self.assertEqual(len(calls), 1)
            self.assertIn(target, calls[0])
            self.assertIn('IO_VM_PACKAGE=/fixture/io-vm', calls[0])

    def test_stress_consumes_fixture_on_one_and_four_cpus(self):
        calls = self.run_suite('native-stress', CI_NATIVE_FIXTURE='/fixture/native', CI_ARCH='riscv64')
        make = [call for call in calls if call[0] == 'make']
        self.assertEqual(len(make), 2)
        for call, cpus in zip(make, (1, 4)):
            self.assertEqual(call[1:6], ['-o', 'image', '-o', 'native-initramfs', 'test-lifecycle-stress'])
            self.assertIn(f'QEMU_CPUS={cpus}', call)
            self.assertIn('ARCH=riscv64', call)

    def test_gicv2_smp_keeps_topology_and_retirement_coverage(self):
        calls = [call for call in self.run_suite('native-smp-gicv2') if call[0] == 'make']
        self.assertEqual(len(calls), 2)
        self.assertIn('test-guest-smp', calls[0])
        self.assertIn('test-runtime-crash', calls[1])
        self.assertIn('QEMU_CPUS=4', calls[1])
        self.assertTrue(any(arg.endswith('/vms-smp-4.json') for arg in calls[1]))
        self.assertNotIn('-o', calls[1])


if __name__ == '__main__':
    unittest.main()
