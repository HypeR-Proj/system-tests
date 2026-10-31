#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Build a QEMU fixture for exclusive level IRQ delivery to a userspace driver."""

import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--board', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    dtb = args.output / 'host.dtb'
    # Match the normal board launch geometry. QEMU's MMIO virtio IRQ output is
    # a level line; select a level-sensitive GIC input for this fixture instead
    # of QEMU's default edge configuration, exercising the Pi delivery contract.
    subprocess.run([args.qemu, '-machine',
                    f'virt,virtualization=on,gic-version=3,dumpdtb={dtb}',
                    '-cpu', 'max', '-smp', '4', '-m', '512M', '-nodefaults',
                    '-display', 'none', '-global', 'virtio-mmio.force-legacy=false',
                    '-device', 'virtio-scsi-device,id=physicalscsi,iommu_platform=on'],
                   check=True, timeout=30)
    # QEMU allocates the first device from the highest MMIO transport slot.
    # This is an explicit test identity, never production probe fallback.
    nodes = subprocess.check_output(['fdtget', '-l', str(dtb), '/'], text=True).splitlines()
    transports = [node for node in nodes if node.startswith('virtio_mmio@')]
    node = '/' + max(transports, key=lambda node: int(node.split('@')[1], 16))
    irq = subprocess.check_output(['fdtget', '-t', 'x', str(dtb), node, 'interrupts'],
                                  text=True).split()
    if len(irq) != 3 or irq[0] != '0':
        raise ValueError('fixture requires one GIC SPI')
    subprocess.run(['fdtput', '-t', 'x', str(dtb), node, 'interrupts', irq[0], irq[1], '4'],
                   check=True)
    board = json.loads(args.board.read_text())
    board['virtual-machines'] = []
    board['io-vm'].pop('network-device', None)
    board['io-vm'].pop('networks', None)
    board['files'].pop('vm/alpine.itb', None)
    board['io-vm']['io-device'] = {'profile': 'virtio-mmio-scsi', 'path': node}
    (args.output / 'config.json').write_text(json.dumps(board, indent=2) + '\n')


if __name__ == '__main__':
    main()
