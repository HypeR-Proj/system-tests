# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

# Position-independent guest fixture. Every address reference stays inside
# this copied blob; no host address or linker relocation escapes into it.
.pushsection .rodata.hyper_vm_smoke_guest,"a",@progbits
.balign 16
.option push
.option arch, +f,+d
.option norelax
.option rvc
.global hyper_vm_smoke_guest_start
.global hyper_vm_smoke_guest_end
hyper_vm_smoke_guest_start:
    li t0, 0x1234
    bne a2, t0, .Lfailure
    li t0, 0x5678
    bne a3, t0, .Lfailure
    beqz a0, .Lbusy
    li t0, 1
    beq a0, t0, .Ltimer
    li t0, 2
    beq a0, t0, .Linput
    li t0, 3
    beq a0, t0, .Lfault
    li t0, 4
    beq a0, t0, .Lprivilege
    j .Lfailure

.macro emit character
    li t5, 0x10000000
    li t6, \character
    sb t6, 0(t5)
.endm

.Lbusy:
    emit 66 # B
.Lspin:
    j .Lspin

.Ltimer:
    li s0, 0x1122334455667788
    li s2, 0x3ff123456789abcd
    fmv.d.x fs0, s2
    mv s3, a1
    li t0, 32
    csrs sie, t0
    rdtime a0
    add a0, a0, s3
    li a6, 0
    li a7, 0x54494d45
    ecall
    bnez a0, .Lfailure
.Ltimer_wait:
    wfi
    csrr t0, sip
    andi t0, t0, 32
    beqz t0, .Ltimer_wait
    li t0, 0x1122334455667788
    bne s0, t0, .Lfailure
    fmv.x.d t0, fs0
    bne t0, s2, .Lfailure
    emit 84 # T: SBI timer, GPR and FP survived the blocked run
    rdtime t0
    add t0, t0, s3
    csrw 0x14d, t0 # direct Sstc STIMECMP
.Ldirect_wait:
    wfi
    csrr t0, sip
    andi t0, t0, 32
    beqz t0, .Ldirect_wait
    emit 68 # D
    li t0, 2
    csrs sip, t0
    csrc sip, t0
    li t5, 0x10000000
    lbu t6, 5(t5) # MMIO exit must not restore old VSSIP
    csrr t0, sip
    andi t0, t0, 2
    bnez t0, .Lfailure
    emit 83 # S
    li t0, -1
    csrw 0x14d, t0
    j .Lspin

.Linput:
    li a4, 0x0c000028 # UART source 10 priority
    li a5, 1
    c.sw a5, 0(a4)
    c.lw a5, 0(a4)
    li t1, 1
    bne a5, t1, .Lfailure
    li t0, 0x0c002080 # supervisor context enable
    li t1, 1024
    sw t1, 0(t0)
    li t0, 0x0c201000 # supervisor threshold
    sw zero, 0(t0)
    li t0, 0x10000000
    li t1, 0xc1 # enable FIFO, receive trigger 14
    sb t1, 2(t0)
    li t1, 1
    sb t1, 1(t0) # receiver data available
    li t1, 512
    csrs sie, t1
    emit 82 # R: parent can inject one paced byte
.Linput_wait:
    wfi
    csrr t1, sip
    andi t1, t1, 512
    beqz t1, .Linput_wait
    li t0, 0x0c201004
    lw s0, 0(t0)
    li t1, 10
    bne s0, t1, .Lfailure
    li t0, 0x10000000
    lbu s1, 0(t0)
    li t1, 90
    bne s1, t1, .Lfailure
    sb s1, 0(t0)
    li t0, 0x0c201004
    sw s0, 0(t0)
    csrr t1, sip
    andi t1, t1, 512
    bnez t1, .Lfailure
    emit 73 # I
    j .Lspin

.Lfault:
    emit 88 # X: started before the fatal guest memory access
    li t0, 0x20000000
    ld t0, 0(t0)
    j .Lfailure

.Lprivilege:
    lla t0, .Lvu_trap
    csrw stvec, t0
    li s0, 0
    li s2, 0x3ff123456789abcd
    fmv.d.x fs0, s2
    li t0, 256 # SPP=VU
    csrc sstatus, t0
    lla t0, .Lvu_entry
    csrw sepc, t0
    sret
.Lvu_entry:
    li s1, 5000000
.Lvu_preempt:
    addi s1, s1, -1
    bnez s1, .Lvu_preempt
    fmv.x.d t0, fs0
    bne t0, s2, .Lfailure
    ecall
    csrr t0, hstatus # must trap as an illegal VU CSR access
    j .Lfailure
.balign 4
.Lvu_trap:
    csrr t0, scause
    bnez s0, .Lvu_illegal
    li t1, 8
    bne t0, t1, .Lfailure
    addi s0, s0, 1
    csrr t0, sepc
    addi t0, t0, 4
    csrw sepc, t0
    sret
.Lvu_illegal:
    li t1, 2
    bne t0, t1, .Lfailure
    emit 85 # U: both synchronous traps reached VS
    j .Lspin

.Lfailure:
    emit 70 # F
    j .Lspin
hyper_vm_smoke_guest_end:
.option pop
.popsection
