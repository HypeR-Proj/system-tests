// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0
.section .rodata.hyper_mmio_guest,"a",@progbits
.balign 16
.global hyper_mmio_guest_start
.global hyper_mmio_guest_end
hyper_mmio_guest_start:
    mov x18, x0 // GIC version supplied by Native platform discovery
    mov x20, #0x0a000000
    mov x22, #0x08000000
    mov x27, #0x08010000
    msr daifset, #2
    // Fill the registered 64 KiB transport and the 32-byte SBSA TX FIFO.
    // The Native runtime never consumes it; the MMIO rendezvous closes the
    // final serial handle. Observe the GIC first: a UART read would itself
    // pump TX and hide a missing close notification.
    mov x9, #0x09000000
    mov w10, #0x20
    str w10, [x9, #0x38] // TX interrupt enable
    mov w11, #0x10000
    add w11, w11, #32
    mov w12, #'x'
serial_fill:
    str w12, [x9]
    subs w11, w11, #1
    b.ne serial_fill
    ldr w10, [x9, #0x18]
    tbz w10, #5, 3f // TXFF must reflect backpressure
    ldr w10, [x22, #0x204]
    tbnz w10, #1, 3f // TX IRQ must be clear while full
    mov w10, #0x5342
    str w10, [x20, #24]
    ldr w10, [x22, #0x204]
    tbz w10, #1, 3f // Closing the sink must assert TX IRQ
    ldr w10, [x9, #0x18]
    tbz w10, #7, 3f // TXFE after close
    str wzr, [x9, #0x38]
    mov w10, #0x20
    str w10, [x9, #0x44]
    bl test_dir_overflow
    mov w21, #32
1:
    // SPI 63 is unused by this fixture. Exercise real distributor traps and
    // the kernel-owned deferred transaction before userspace-owned MMIO.
    // The interrupt stays disabled, so no guest IRQ handler is needed.
    mov x22, #0x08000000
    mov w23, #0x80000000
    str w23, [x22, #0x304] // ISACTIVER1: set active
    ldr w24, [x22, #0x384] // ICACTIVER1: same readback
    tst w24, w23
    b.eq 3f
    str wzr, [x22, #0x384] // zero write must not clear
    ldr w24, [x22, #0x304]
    tst w24, w23
    b.eq 3f
    str w23, [x22, #0x204] // ISPENDR1: retain pending while active
    str w23, [x22, #0x384] // ICACTIVER1: clear active only
    ldr w24, [x22, #0x304]
    tst w24, w23
    b.ne 3f
    ldr w24, [x22, #0x204]
    tst w24, w23
    b.eq 3f
    str w23, [x22, #0x284] // ICPENDR1: leave the source clean
    mov w1, #0x1234
    str w1, [x20]
    ldrsb x2, [x20, #4]
    str x2, [x20, #8]
    ldr w3, [x20, #16]
    cmp w3, w1
    b.ne 3f
    subs w21, w21, #1
    b.ne 1b
    mov w1, #0x55
    str w1, [x20, #32]
2:
    wfi
    b 2b
3:
    mov w1, #0xff
    str w1, [x20, #32]
    b 2b

// Hardware LRs contain no APR priority-stack entry for software-set active
// interrupts. Fill all 32 SPIs (more than GICv3's maximum 16 LRs and Pi 5's 4)
// and deactivate in reverse order, including sources absent from the LR bank.
test_dir_overflow:
    cmp x18, #2
    b.eq dir_v2_initial
    mov x0, #1
    msr icc_sre_el1, x0
    isb
    mrs x8, icc_pmr_el1
    mov x0, #0x80
    msr icc_pmr_el1, x0
    isb
    mrs x1, icc_pmr_el1
    cmp x1, x0
    b.ne 3b
    msr icc_pmr_el1, x8
    msr icc_ctlr_el1, xzr
    isb
    mrs x1, icc_ctlr_el1
    tst x1, #3
    b.ne 3b
    b dir_fill
dir_v2_initial:
    mov w0, #1
    str w0, [x27] // GICC_CTLR, EOImode=0
    dsb sy
    ldr w1, [x27]
    tst w1, #0x200
    b.ne 3b
dir_fill:
    mov w25, #-1
    str w25, [x22, #0x304]
    ldr w1, [x22, #0x304]
    cmp w1, w25
    b.ne 3b
    mov w0, #63
    cmp x18, #2
    b.eq dir_v2_mode_zero
    // RPR remains idle: ISACTIVER does not acknowledge an interrupt.
    mrs x1, icc_rpr_el1
    cmp x1, #255
    b.ne 3b
    msr icc_dir_el1, x0
    isb
    b dir_check_mode_zero
dir_v2_mode_zero:
    str w0, [x27, #0x1000]
    dsb sy
dir_check_mode_zero:
    // Our defined handling of architecturally unpredictable mode-zero DIR
    // ignores it, preserving both resident and nonresident active state.
    ldr w1, [x22, #0x304]
    cmp w1, w25
    b.ne 3b
    cmp x18, #2
    b.eq dir_v2_split
    mov x0, #2
    msr icc_ctlr_el1, x0
    isb
    mrs x1, icc_ctlr_el1
    and x1, x1, #3
    cmp x1, #2
    b.ne 3b
    b dir_split_ready
dir_v2_split:
    mov w0, #0x201
    str w0, [x27]
    dsb sy
    ldr w1, [x27]
    tst w1, #0x200
    b.eq 3b
dir_split_ready:
    mov w0, #0x80000000
    str w0, [x22, #0x204] // pending must survive DIR for SPI63
    mov w26, #63
dir_clear_loop:
    cmp x18, #2
    b.eq dir_v2_clear
    msr icc_dir_el1, x26
    isb
    b dir_check_clear
dir_v2_clear:
    str w26, [x27, #0x1000]
    dsb sy
dir_check_clear:
    sub w0, w26, #32
    mov w1, #1
    lsl w1, w1, w0
    bic w25, w25, w1
    ldr w1, [x22, #0x304]
    cmp w1, w25
    b.ne 3b
    sub w26, w26, #1
    cmp w26, #32
    b.hs dir_clear_loop
    ldr w1, [x22, #0x204]
    tst w1, #0x80000000
    b.eq 3b
    mov w0, #0x80000000
    str w0, [x22, #0x284]
    ret
hyper_mmio_guest_end:
