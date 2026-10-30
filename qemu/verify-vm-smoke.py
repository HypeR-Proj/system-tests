#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Boot the Native VM lifecycle fixture and retain its bounded acceptance log."""

import os
import re
from pathlib import Path
import selectors
import subprocess
import sys
import time


def main():
    qemu, image, initramfs, logfile = sys.argv[1:]
    command = [
        qemu, "-machine", os.environ.get("QEMU_MACHINE", "virt"),
        "-cpu", os.environ.get("QEMU_CPU", "rv64"),
        "-smp", os.environ.get("QEMU_CPUS", "4"),
        "-m", os.environ.get("QEMU_MEMORY", "1G"),
        "-nodefaults", "-display", "none", "-serial", "stdio",
        "-monitor", "none", "-no-reboot", "-kernel", image, "-initrd", initramfs,
        "-append", os.environ.get("QEMU_BOOTARGS", "earlycon=uart8250,mmio,0x10000000"),
    ]
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    timeout = float(os.environ.get("QEMU_VM_SMOKE_TIMEOUT_SECONDS", "180"))
    output = bytearray()
    with open(logfile, "wb") as log, selectors.DefaultSelector() as selector:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for key, _ in selector.select(0.2):
                    data = os.read(key.fd, 65536)
                    if not data:
                        raise RuntimeError("QEMU closed output before fixture completion")
                    log.write(data)
                    log.flush()
                    output.extend(data.replace(b"\r", b""))
                if re.search(rb"VM-SMOKE: FAIL[^\n]*\n", output) or any(marker in output for marker in (
                    b"HypeR crash monitor", b"HypeR: fatal",
                    b"kernel panic", b"kernel startup failed",
                )):
                    raise RuntimeError("VM lifecycle fixture reported a failure")
                if b"VM-SMOKE: PASS\n" in output:
                    print(f"Native VM lifecycle acceptance passed: {logfile}")
                    return
                if process.poll() is not None:
                    raise RuntimeError(f"QEMU exited before completion: {process.returncode}")
            raise TimeoutError(f"VM lifecycle fixture exceeded {timeout:g} seconds")
        except Exception:
            sys.stderr.write(output[-16384:].decode(errors="replace"))
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()


if __name__ == "__main__":
    main()
