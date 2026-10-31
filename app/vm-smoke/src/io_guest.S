// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0
.section .rodata.hyper_io_guest,"a",@progbits
.balign 2048
.global hyper_io_guest_start
.global hyper_io_guest_end
hyper_io_guest_start:
    mov x19, x0                         // 0 frontend, 1 backend
    mov x18, x1                         // GIC version
    mov x20, #0x0a000000                // frontend transport
    mov x21, #0x0a010000                // backend mailbox
    mov x22, #0x0a020000                // backend notification
    mov x23, #0x0a030000                // Native checkpoints
    mov x26, #0x401f0000                // zeroed private IRQ observations
    adr x0, io_vectors
    msr vbar_el1, x0
    isb
    mov x0, #0x08000000
    cmp x18, #2
    b.eq 1f
    mov w1, #2                         // EnableGrp1NS
    str w1, [x0]
    mov x1, #1
    msr icc_sre_el1, x1
    isb
    mov x1, #255
    msr icc_pmr_el1, x1
    mov x1, #0
    msr icc_ctlr_el1, x1                // EOImode=0
    mov x1, #1
    msr icc_igrpen1_el1, x1
    b 2f
1:
    mov w1, #1
    str w1, [x0]                       // GICv2 Group0 distributor
    mov w1, #0x0101
    movk w1, #0x0101, lsl #16
    str w1, [x0, #0x828]               // SPIs40..43 -> vCPU0
    mov x2, #0x08010000
    mov w1, #255
    str w1, [x2, #4]                   // GICC_PMR
    mov w1, #1
    str w1, [x2]                       // GICC_CTLR
2:
    mov w1, #0x700                     // IRQ40,41,42
    str w1, [x0, #0x104]               // GICD_ISENABLER1
    dsb sy
    isb
    msr daifclr, #2
    cbnz x19, io_backend

io_frontend:
    mov w0, #0xf0
    str w0, [x23]                      // Native enables binding before resume
    mov w0, #2
    str w0, [x20, #0x50]
3:
    msr daifset, #2
    ldr w0, [x26]
    cbnz w0, 4f
    wfi
    msr daifclr, #2
    b 3b
4:
    msr daifclr, #2
    ldr w0, [x26, #4]
    cmp w0, #1
    b.ne io_failure
    mov w0, #0xf2
    str w0, [x23, #8]                  // Native retires backend while parked
5:
    msr daifset, #2
    ldr w0, [x26]
    cmp w0, #2
    b.hs 6f
    wfi
    msr daifclr, #2
    b 5b
6:
    msr daifclr, #2
    ldr w0, [x26, #4]
    cmp w0, #2
    b.ne io_failure
    ldr w0, [x20, #0x60]               // Hot reads remain valid after peer exit
    cbnz w0, io_failure
    mov w0, #2
    str w0, [x20, #0x50]               // Closed binding ignores new kicks
    mov w0, #0xf3
    str w0, [x23, #12]
    b io_idle

io_backend:
    mov w0, #0xb0
    str w0, [x23]
    mov w0, #1
    str w0, [x21, #0x0c]               // Mailbox RX IRQ, durable preexisting data
7:
    msr daifset, #2
    ldr w0, [x26, #8]
    cbnz w0, 8f
    wfi
    msr daifclr, #2
    b 7b
8:
    msr daifclr, #2
    ldr w0, [x21, #0x10]
    cmp w0, #4
    b.ne io_failure
    ldr w0, [x21, #0x100]
    mov w1, #0x6970
    movk w1, #0x676e, lsl #16           // "ping"
    cmp w0, w1
    b.ne io_failure
    ldr x0, [x21, #0x18]
    str x0, [x21, #0x20]
    str x0, [x21, #0x20]               // Stale consume must set ERROR
    ldr w0, [x21, #8]
    tbz w0, #3, io_failure
    mov w0, #8
    str w0, [x21, #8]
    mov w0, #0x6f70
    movk w0, #0x676e, lsl #16           // "pong"
    str w0, [x21, #0x200]
    mov w0, #4
    str w0, [x21, #0x28]
    mov w0, #1
    str w0, [x21, #0x2c]
    mov w0, #0xb1
    str w0, [x23, #4]
9:
    msr daifset, #2
    ldr w0, [x26, #12]
    cbnz w0, 10f
    wfi
    msr daifclr, #2
    b 9b
10:
    msr daifclr, #2
    cmp w0, #4
    b.ne io_failure
    ldr w0, [x22, #8]
    lsl x0, x0, #32
    orr x0, x0, #1
    str x0, [x22, #0x18]
    mov w0, #0xb2
    str w0, [x23, #8]
    b io_idle

io_irq:
    stp x0, x1, [sp, #-48]!
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    cmp x18, #2
    b.eq 11f
    mrs x0, icc_iar1_el1
    b 12f
11:
    mov x1, #0x08010000
    ldr w0, [x1, #12]
12:
    and w2, w0, #0x3ff
    cmp w2, #40
    b.eq 13f
    cmp w2, #41
    b.eq 14f
    cmp w2, #42
    b.eq 15f
    cmp w2, #1023
    b.eq 17f
    b io_failure
13:
    ldr w1, [x20, #0x60]
    str w1, [x20, #0x64]
    str w1, [x26, #4]
    ldr w1, [x26]
    add w1, w1, #1
    str w1, [x26]
    b 16f
14:
    str wzr, [x21, #0x0c]              // Mask persistent RX until main consumes
    mov w1, #1
    str w1, [x26, #8]
    b 16f
15:
    ldr w1, [x22, #0x10]               // Atomic consume deasserts kick line
    str w1, [x26, #12]
16:
    dsb sy
    cmp x18, #2
    b.eq 18f
    msr icc_eoir1_el1, x0
    b 17f
18:
    mov x1, #0x08010000
    str w0, [x1, #16]
17:
    ldp x4, x5, [sp, #32]
    ldp x2, x3, [sp, #16]
    ldp x0, x1, [sp], #48
    eret

io_failure:
    mov w0, #0xff
    str w0, [x23, #0x40]
io_idle:
    wfi
    b io_idle

.balign 2048
io_vectors:
.set io_vector_index, 0
.rept 16
    .balign 128
    .if (io_vector_index % 4) == 1
        b io_irq
    .else
        b io_failure
    .endif
    .set io_vector_index, io_vector_index + 1
.endr
.balign 128
hyper_io_guest_end:
