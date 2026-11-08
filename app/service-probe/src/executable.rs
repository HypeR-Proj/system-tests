// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Explicit component authority publishes immutable, executable copies.

use hyper_os::{
    Error, Status,
    handle::Rights,
    memory::{
        ExecutableAuthority, ExecutableMapping, PAGE_SIZE, SharedAtomicU32Mapping, WritableVmo,
    },
    startup,
};

type Result<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;

pub(super) fn code(value: u32) -> Vec<u8> {
    #[cfg(target_arch = "aarch64")]
    let words = [0x5280_0000 | (value << 5), 0xd65f_03c0]; // mov w0, #value; ret
    #[cfg(target_arch = "riscv64")]
    let words = [(value << 20) | 0x0000_0513, 0x0000_8067]; // li a0, value; ret
    #[cfg(any(target_arch = "aarch64", target_arch = "riscv64"))]
    {
        words.into_iter().flat_map(u32::to_le_bytes).collect()
    }
    #[cfg(target_arch = "x86_64")]
    {
        let mut bytes = vec![0xb8]; // mov eax, value; ret
        bytes.extend_from_slice(&value.to_le_bytes());
        bytes.push(0xc3);
        bytes
    }
}

pub(super) fn execute(mapping: &ExecutableMapping) -> u32 {
    // SAFETY: the mapping contains the two reviewed, architecture-specific
    // return-constant instructions above. It stays alive throughout the call;
    // publication performs instruction-cache synchronization and maps RX only.
    unsafe {
        let entry: unsafe extern "C" fn() -> u32 = std::mem::transmute(mapping.base_address());
        entry()
    }
}

fn denied<T>(result: hyper_os::Result<T>) -> Result {
    if matches!(result, Err(Error::Status(Status::ACCESS_DENIED))) {
        Ok(())
    } else {
        Err("executable operation did not reject missing rights".into())
    }
}

pub fn verify() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    let authority = startup.take_optional(startup::EXECUTABLE_AUTHORITY)?;
    if std::env::args().nth(2).as_deref() == Some("denied") {
        if authority.is_some() {
            return Err("unprivileged sibling inherited executable authority".into());
        }
        println!("EXECUTABLE_AUTHORITY_DENIED_OK");
        return Ok(());
    }
    let authority =
        ExecutableAuthority::from_handle(authority.ok_or("missing explicit authority")?);
    let root = startup.borrow(startup::ROOT_VMAR)?;
    let source = WritableVmo::create(PAGE_SIZE)?;
    source.write_all_at(0, &code(42))?;
    let read_only = source.as_handle_ref().duplicate(Rights::READ)?;
    let first = authority.publish(read_only.as_handle_ref())?;
    if first.as_handle_ref().info()?.rights.contains(Rights::WRITE) {
        return Err("published executable retained WRITE".into());
    }
    // SAFETY: this test owns root and never externally modifies the private
    // mapping. execute() calls only the known return-constant instructions.
    let first_mapping = unsafe { first.map(root) }?;
    if execute(&first_mapping) != 42 {
        return Err("published code did not execute".into());
    }
    source.write_all_at(0, &code(43))?;
    let second = authority.publish(source.as_handle_ref())?;
    // SAFETY: same private, retained RX owner contract as first_mapping.
    let second_mapping = unsafe { second.map(root) }?;
    if execute(&first_mapping) != 42 || execute(&second_mapping) != 43 {
        return Err("source mutation changed an already published image".into());
    }
    let weak =
        ExecutableAuthority::from_handle(authority.as_handle_ref().duplicate(Rights::INSPECT)?);
    denied(weak.publish(source.as_handle_ref()))?;
    let write_only = source.as_handle_ref().duplicate(Rights::WRITE)?;
    denied(authority.publish(write_only.as_handle_ref()))?;
    // SAFETY: the kernel must reject WRITE on this immutable executable before
    // any mapping exists; no access through an AtomicU32 alias is performed.
    denied(unsafe { SharedAtomicU32Mapping::map(root, first.as_handle_ref(), PAGE_SIZE) })?;
    // SAFETY: all aliases of the ordinary source stay unused during this
    // temporary AtomicU32 mapping; no external VMAR edit occurs.
    let active_write =
        unsafe { SharedAtomicU32Mapping::map(root, source.as_handle_ref(), PAGE_SIZE) }?;
    if !matches!(
        authority.publish(source.as_handle_ref()),
        Err(Error::Status(Status::BUSY))
    ) {
        return Err("publication accepted an active writable source mapping".into());
    }
    drop(active_write);
    let _retry = authority.publish(source.as_handle_ref())?;
    crate::executable_cross_cpu::verify(&startup, &authority)?;
    println!("EXECUTABLE_PUBLICATION_ALLOWED_OK");
    Ok(())
}
