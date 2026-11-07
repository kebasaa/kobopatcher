"""Structure-aware ELF patches for 32-bit little-endian ARM shared libraries.

A port of kobopatch's patchlib/elf.go. Everything is read from the current
buffer with elffile and the raw headers, and all writes go through write_at
(and thus the hook), so the same safety guarantees as ReplaceBytes apply (the
original bytes are checked).

The functions take the Patcher as their first argument (the Patcher has thin
methods delegating to them). Where kobopatch returns an error, a PatchError
with the same message is raised; if kobopatch also returns values together
with the error, they are available as the exception's ``result`` attribute
(e.g. the partial list of changes of elf_lazy_binding, or the offset and the
original bytes of stub_function).
"""

import os
import struct
from dataclasses import dataclass

from ..compat import go_compat
from ..errors import GoPanic
from ..gofmt import hexbytes_spaced, quote
from . import asm
from .elffile import (
    DF_1_NOW,
    DF_BIND_NOW,
    DT_BIND_NOW,
    DT_FINI,
    DT_FLAGS,
    DT_FLAGS_1,
    DT_GNU_HASH,
    DT_HASH,
    DT_INIT,
    DT_INIT_ARRAY,
    DT_INIT_ARRAYSZ,
    DT_JMPREL,
    DT_NEEDED,
    DT_NULL,
    DT_PLTGOT,
    DT_PLTREL,
    DT_PLTRELSZ,
    DT_REL,
    DT_RELA,
    DT_RELSZ,
    DT_SONAME,
    DT_STRSZ,
    DT_STRTAB,
    DT_SYMTAB,
    DT_VERNEED,
    DT_VERNEEDNUM,
    DT_VERSYM,
    ELFCLASS32,
    ELFDATA2LSB,
    ELFMAG,
    EM_ARM,
    PF_W,
    PF_X,
    PT_DYNAMIC,
    PT_GNU_RELRO,
    PT_LOAD,
    PT_NULL,
    R_ARM_JUMP_SLOT,
    SHN_UNDEF,
    STT_FUNC,
    ElfFile,
    class_name,
    data_name,
    dt_name,
    machine_name,
    pt_name,
    stt_name,
)
from .patcher import PatchError

_M32 = 0xFFFFFFFF

# Thumb/ARM instruction encodings used by stub_function and nop_call.
THUMB_BX_LR = b"\x70\x47"  # bx lr
THUMB_NOP = b"\x00\xbf"  # nop (T1)
ARM_BX_LR = b"\x1e\xff\x2f\xe1"


def _err(msg, result=None):
    e = PatchError(msg)
    e.result = result
    return e


def _gostrs(lst) -> str:
    """%v of a []string."""
    return "[" + " ".join(lst) + "]"


def _goerr(e) -> str:
    """%v of an error (nil included)."""
    return "<nil>" if e is None else str(e)


def _i32(v: int) -> int:
    v &= _M32
    return v - (1 << 32) if v & 0x80000000 else v


def _slice(buf, off: int, n: int, what: str) -> bytes:
    """buf[off:off+n], which panics in Go if it is past the end."""
    if off + n > len(buf):
        if go_compat():
            raise GoPanic("runtime error: slice bounds out of range [:%d] with capacity %d" % (off + n, len(buf)))
        raise PatchError("%s: 0x%X (+%d) is past the end of the file" % (what, off, n))
    return bytes(buf[off : off + n])


@dataclass
class ElfStub:
    sym: str
    data: bytes


@dataclass
class ElfNop:
    off: int
    data: bytes


@dataclass
class ElfDynEnt:
    off: int  # file offset of the entry
    tag: int
    val: int


class ElfInfo:
    """A snapshot of the parts of an ELF file the elf_* functions need."""

    def __init__(self, f, phoff, phent, progs, dyn, dents):
        self.f, self.phoff, self.phent, self.progs, self.dyn, self.dents = f, phoff, phent, progs, dyn, dents

    def phdr_off(self, i: int) -> int:
        """The file offset of the i-th program header."""
        return (self.phoff + i * self.phent) & _M32

    def dynamic(self, tag: int):
        """The entries with the tag (excluding the terminating DT_NULL)."""
        return [e for e in self.dents[:-1] if e.tag == tag]

    def dyn_val(self, tag: int):
        """(value, found) of a tag which must occur at most once."""
        es = self.dynamic(tag)
        if not es:
            return 0, False
        if len(es) == 1:
            return es[0].val, True
        raise PatchError("%s occurs %d times in the dynamic section" % (dt_name(tag), len(es)))

    def _dyn_val_noerr(self, tag: int) -> bool:
        """`_, ok, _ := dynVal(tag)`."""
        try:
            return self.dyn_val(tag)[1]
        except PatchError:
            return False

    def eager(self):
        """Everything which makes the dynamic linker bind all symbols at load
        time (empty if none)."""
        r = []
        v, ok = self.dyn_val(DT_FLAGS_1)
        if ok and v & DF_1_NOW:
            r.append("DF_1_NOW")
        v, ok = self.dyn_val(DT_FLAGS)
        if ok and v & DF_BIND_NOW:
            r.append("DF_BIND_NOW")
        if self.dynamic(DT_BIND_NOW):
            r.append("DT_BIND_NOW")
        return r

    def prog(self, typ: int):
        """(index, prog) of the only program header with the type, or (-1, None)."""
        idx = -1
        for i, pr in enumerate(self.progs):
            if pr.type == typ:
                if idx != -1:
                    raise PatchError("multiple %s program headers" % pt_name(typ))
                idx = i
        if idx == -1:
            return -1, None
        return idx, self.progs[idx]

    def vaddr_off(self, vaddr: int, n: int):
        """(file offset, PT_LOAD) of a virtual address."""
        for pr in self.progs:
            if pr.type == PT_LOAD and vaddr >= pr.vaddr and vaddr + n <= pr.vaddr + pr.filesz:
                return (vaddr - pr.vaddr + pr.off) & _M32, pr
        raise PatchError("address 0x%X (+%d) not in the file part of any PT_LOAD" % (vaddr, n))

    def rels(self, buf, addr_tag: int, size_tag: int):
        """(r_offset, r_info) of a REL table referenced by the dynamic section."""
        addr, ok1 = self.dyn_val(addr_tag)
        size, ok2 = self.dyn_val(size_tag)
        if not ok1 or not ok2 or size == 0:
            return []
        try:
            off, _ = self.vaddr_off(addr, size)
        except PatchError as e:
            raise PatchError("%s: %s" % (dt_name(addr_tag), e)) from None
        if size % 8 != 0:
            raise PatchError("%s not a multiple of 8" % dt_name(size_tag))
        data = _slice(buf, off, size, "read %s" % dt_name(addr_tag))
        return [struct.unpack_from("<II", data, i) for i in range(0, size, 8)]

    def jump_slots(self, buf):
        """The addresses of the GOT slots of the R_ARM_JUMP_SLOT relocations."""
        v, ok = self.dyn_val(DT_PLTREL)
        if ok and v != DT_REL:
            raise PatchError("DT_PLTREL is %s, expected DT_REL" % dt_name(v))
        return [off for off, info in self.rels(buf, DT_JMPREL, DT_PLTRELSZ) if info & 0xFF == R_ARM_JUMP_SLOT]


def parse_elf(buf) -> ElfInfo:
    try:
        f = ElfFile(buf)
    except Exception as e:  # noqa: BLE001 - debug/elf's errors
        raise PatchError("parse elf: %s" % e) from None
    if f.ei_class != ELFCLASS32 or f.ei_data != ELFDATA2LSB or f.e_machine != EM_ARM:
        raise PatchError(
            "not a 32-bit little-endian ARM ELF (class=%s data=%s machine=%s)"
            % (class_name(f.ei_class), data_name(f.ei_data), machine_name(f.e_machine))
        )
    if len(buf) < 52:
        raise PatchError("read elf header: unexpected EOF")
    if f.e_phentsize != 32:
        raise PatchError("unexpected e_phentsize %d" % f.e_phentsize)
    if f.e_phoff + f.e_phnum * 32 > len(buf):
        raise PatchError("program headers past end of file")
    dyn = None
    for pr in f.progs:
        if pr.type == PT_DYNAMIC:
            if dyn is not None:
                raise PatchError("multiple PT_DYNAMIC program headers")
            dyn = pr
    if dyn is None:
        raise PatchError("no PT_DYNAMIC program header")
    if dyn.off + dyn.filesz > len(buf):
        raise PatchError("PT_DYNAMIC past end of file")
    dents = []
    o = dyn.off
    while o + 8 <= dyn.off + dyn.filesz:
        tag, val = struct.unpack_from("<iI", buf, o)
        dents.append(ElfDynEnt(o & _M32, tag, val))
        if tag == DT_NULL:
            break
        o += 8
    if not dents or dents[-1].tag != DT_NULL:
        raise PatchError("dynamic section not terminated by DT_NULL")
    return ElfInfo(f, f.e_phoff, f.e_phentsize, f.progs, dyn, dents)


@dataclass
class ElfSym:
    sym: object  # elffile.Symbol
    addr: int  # without the Thumb bit
    thumb: bool

    @property
    def info(self):
        return self.sym.info

    @property
    def size(self):
        return self.sym.size


class _SymList(list):
    """The cached dynamic symbols, with their demangled names (computed once)."""

    demangled = None


def elf_sym(p, name: str) -> ElfSym:
    """A defined dynamic symbol by its mangled (or, as a fallback, demangled) name."""
    if p.elf_syms is None:
        try:
            f = ElfFile(p.buf)
        except Exception as e:  # noqa: BLE001
            raise PatchError("parse elf: %s" % e) from None
        try:
            syms = f.dynamic_symbols()
        except Exception as e:  # noqa: BLE001
            raise PatchError("read dynamic symbols: %s" % e) from None
        p.elf_syms = _SymList(syms)
    syms = p.elf_syms
    match = [s for s in syms if s.name == name]
    if not match:
        from ._demangle import DemangleError, to_string

        dm = getattr(syms, "demangled", None)
        if dm is None:
            dm = []
            for s in syms:
                try:
                    dm.append(to_string(s.name))
                except DemangleError:
                    dm.append(None)
            if isinstance(syms, _SymList):
                syms.demangled = dm
        match = [s for s, d in zip(syms, dm) if d is not None and d == name]
    defs = [s for s in match if s.section != SHN_UNDEF]
    if not match:
        raise PatchError("no such dynamic symbol %s" % quote(name))
    if not defs:
        raise PatchError("dynamic symbol %s is not defined in this file (imported)" % quote(name))
    for s in defs[1:]:
        if s.value != defs[0].value:
            raise PatchError("dynamic symbol %s is ambiguous (%d definitions)" % (quote(name), len(defs)))
    v = defs[0].value
    return ElfSym(defs[0], (v & _M32) & ~1, v & 1 != 0)


def write_at(p, off: int, find: bytes, replace: bytes) -> None:
    """Replace the bytes at an absolute offset after checking the original ones."""
    if len(find) != len(replace):
        raise PatchError("length mismatch in byte replacement")
    if off + len(find) > len(p.buf):
        raise PatchError("replaced value past end of buf")
    cur = bytes(p.buf[off : off + len(find)])
    if cur != bytes(find):
        raise PatchError(
            "could not find specified bytes at offset 0x%X (found %s, expected %s)"
            % (off, hexbytes_spaced(cur), hexbytes_spaced(find))
        )
    p._call_hook(_i32(off), find, replace)
    p.buf[off : off + len(replace)] = replace


def le32(v: int) -> bytes:
    return struct.pack("<I", v & _M32)


# --- the patches -----------------------------------------------------------


def _clear_dyn_flag(p, tag, mask):
    """ElfClearDynFlag: (old, found, err)."""
    if tag not in (DT_FLAGS, DT_FLAGS_1):
        return 0, False, _err(
            "ElfClearDynFlag: unsupported tag %s (only DT_FLAGS and DT_FLAGS_1 are bit fields)" % dt_name(tag)
        )
    if mask == 0:
        return 0, False, _err("ElfClearDynFlag: mask is 0")
    try:
        ei = parse_elf(p.buf)
    except PatchError as e:
        return 0, False, _err("ElfClearDynFlag: %s" % e)
    es = ei.dynamic(tag)
    if not es:
        return 0, False, None
    if len(es) > 1:
        return 0, False, _err("ElfClearDynFlag: %s occurs %d times" % (dt_name(tag), len(es)))
    e = es[0]
    new = e.val & ~mask & _M32
    if e.val & mask == 0:
        return e.val, True, _err(
            "ElfClearDynFlag: none of the bits 0x%X are set in %s (0x%X): already patched or a different build"
            % (mask, dt_name(tag), e.val)
        )
    try:
        write_at(p, (e.off + 4) & _M32, le32(e.val), le32(new))
    except PatchError as ex:
        return e.val, True, _err("ElfClearDynFlag: %s" % ex)
    try:
        nei = parse_elf(p.buf)
    except PatchError as ex:
        return e.val, True, _err("ElfClearDynFlag: re-parse: %s" % ex)
    err = None
    try:
        v, _ = nei.dyn_val(tag)
    except PatchError as ex:
        v, err = 0, ex
    if err is not None or v != new:
        return e.val, True, _err(
            "ElfClearDynFlag: sanity check failed: %s is 0x%X after patching, expected 0x%X (err=%s)"
            % (dt_name(tag), v, new, _goerr(err))
        )
    return e.val, True, None


def elf_clear_dyn_flag(p, tag: int, mask: int):
    """Clear the bits in mask from the value of the DT_FLAGS or DT_FLAGS_1
    dynamic entry; returns (old, found).

    If the tag is not present, nothing is changed and found is False. It is an
    error if none of the bits are set (i.e. the patch was already applied, or
    it is a different build).
    """
    old, found, err = _clear_dyn_flag(p, tag, mask)
    if err is not None:
        err.result = (old, found)
        raise err
    return old, found


def elf_remove_dyn_entry(p, tag: int) -> bool:
    """Remove the (only) dynamic entry with the tag by moving the following
    entries up and duplicating the terminating DT_NULL; returns found.

    Only DT_BIND_NOW (whose value is ignored) is supported. If the tag is not
    present, nothing is changed and found is False.
    """
    if tag != DT_BIND_NOW:
        raise _err(
            "ElfRemoveDynEntry: unsupported tag %s (only DT_BIND_NOW can be removed safely)" % dt_name(tag), False
        )
    try:
        ei = parse_elf(p.buf)
    except PatchError as e:
        raise _err("ElfRemoveDynEntry: %s" % e, False) from None
    idx = -1
    for i, e in enumerate(ei.dents):
        if e.tag == tag:
            if idx != -1:
                raise _err("ElfRemoveDynEntry: %s occurs more than once" % dt_name(tag), False)
            idx = i
    if idx == -1:
        return False
    start = ei.dents[idx].off
    end = ei.dents[-1].off + 8  # after the DT_NULL

    # nothing may point into the moved entries
    dyn_vaddr = ((ei.dyn.vaddr & _M32) + ((start - (ei.dyn.off & _M32)) & _M32)) & _M32
    dyn_vend = (dyn_vaddr + (end - start)) & _M32
    for t in ((DT_REL, DT_RELSZ), (DT_JMPREL, DT_PLTRELSZ)):
        try:
            rs = ei.rels(p.buf, t[0], t[1])
        except PatchError as e:
            raise _err("ElfRemoveDynEntry: %s" % e, True) from None
        for r_off, _ in rs:
            if dyn_vaddr <= r_off < dyn_vend:
                raise _err(
                    "ElfRemoveDynEntry: relocation at 0x%X points into the moved part of the dynamic section" % r_off,
                    True,
                )
    if ei._dyn_val_noerr(DT_RELA):
        raise _err("ElfRemoveDynEntry: RELA relocations are not supported", True)

    find = bytes(p.buf[start:end])
    replace = find[8:] + bytes(8)  # the last 8 bytes stay 0 (an extra DT_NULL)
    try:
        write_at(p, start, find, replace)
    except PatchError as e:
        raise _err("ElfRemoveDynEntry: %s" % e, True) from None

    try:
        nei = parse_elf(p.buf)
    except PatchError as e:
        raise _err("ElfRemoveDynEntry: re-parse: %s" % e, True) from None
    if len(nei.dents) != len(ei.dents) - 1 or nei.dynamic(tag):
        raise _err(
            "ElfRemoveDynEntry: sanity check failed: %d entries after patching, expected %d"
            % (len(nei.dents), len(ei.dents) - 1),
            True,
        )
    j = 0
    for i in range(len(ei.dents)):
        if i == idx:
            continue
        if ei.dents[i].tag != nei.dents[j].tag or ei.dents[i].val != nei.dents[j].val:
            raise _err("ElfRemoveDynEntry: sanity check failed: entry %d changed" % i, True)
        j += 1
    return True


def elf_disable_program_header(p, typ: int) -> bool:
    """Set the p_type of the only program header with the type to PT_NULL;
    returns found.

    Only PT_GNU_RELRO is supported, and its region must lie inside a writable
    PT_LOAD. If there is no such header, nothing is changed and found is False.
    """
    if typ != PT_GNU_RELRO:
        raise _err(
            "ElfDisableProgramHeader: unsupported type %s (only PT_GNU_RELRO can be disabled safely)" % pt_name(typ),
            False,
        )
    try:
        ei = parse_elf(p.buf)
    except PatchError as e:
        raise _err("ElfDisableProgramHeader: %s" % e, False) from None
    try:
        idx, pr = ei.prog(typ)
    except PatchError as e:
        raise _err("ElfDisableProgramHeader: %s" % e, False) from None
    if pr is None:
        return False
    inside = False
    for l in ei.progs:  # noqa: E741
        if l.type == PT_LOAD and l.flags & PF_W and pr.vaddr >= l.vaddr and pr.vaddr + pr.memsz <= l.vaddr + l.memsz:
            inside = True
    if not inside:
        raise _err(
            "ElfDisableProgramHeader: %s region 0x%X-0x%X is not inside a writable PT_LOAD"
            % (pt_name(typ), pr.vaddr, pr.vaddr + pr.memsz),
            True,
        )
    try:
        write_at(p, ei.phdr_off(idx), le32(typ), le32(PT_NULL))
    except PatchError as e:
        raise _err("ElfDisableProgramHeader: %s" % e, True) from None
    return True


def elf_lazy_binding(p):
    """Restore lazy binding; returns a description of the changes.

    Clears DF_1_NOW and DF_BIND_NOW, removes DT_BIND_NOW, and disables
    PT_GNU_RELRO (which would otherwise make the GOT read-only after loading
    and crash on the first lazy call). Refuses if the binary is not eagerly
    bound, or if the PLT GOT slots are not set up for lazy binding (i.e. they
    don't all point to the PLT header). On an error after the first change,
    the exception's result is the list of the changes made so far.
    """
    try:
        ei = parse_elf(p.buf)
        eager = ei.eager()
    except PatchError as e:
        raise _err("ElfLazyBinding: %s" % e, []) from None
    if not eager:
        raise _err("ElfLazyBinding: binary is not eagerly bound (already patched or a different build)", [])
    if not ei._dyn_val_noerr(DT_PLTGOT):
        raise _err("ElfLazyBinding: no DT_PLTGOT", [])
    try:
        slots = ei.jump_slots(p.buf)
    except PatchError as e:
        raise _err("ElfLazyBinding: %s" % e, []) from None
    if not slots:
        raise _err("ElfLazyBinding: no R_ARM_JUMP_SLOT relocations (nothing to bind lazily)", [])
    plt0 = 0
    for i, s in enumerate(slots):
        try:
            off, _ = ei.vaddr_off(s, 4)
        except PatchError as e:
            raise _err("ElfLazyBinding: GOT slot: %s" % e, []) from None
        (v,) = struct.unpack("<I", _slice(p.buf, off, 4, "ElfLazyBinding: GOT slot"))
        if i == 0:
            plt0 = v
        elif v != plt0:
            raise _err(
                "ElfLazyBinding: GOT slot 0x%X holds 0x%X, expected 0x%X like the others (not set up for lazy binding)"
                % (s, v, plt0),
                [],
            )
    sec = ei.f.section(".plt")
    if sec is not None and plt0 != sec.addr:
        raise _err("ElfLazyBinding: GOT slots point to 0x%X, expected the PLT header at 0x%X" % (plt0, sec.addr), [])
    try:
        _, l = ei.vaddr_off(plt0, 4)  # noqa: E741
        exe = l.flags & PF_X != 0
    except PatchError:
        exe = False
    if not exe:
        raise _err("ElfLazyBinding: GOT slots point to 0x%X, which is not executable" % plt0, [])

    log = []
    old, ok, err = _clear_dyn_flag(p, DT_FLAGS_1, DF_1_NOW)
    if ok and old & DF_1_NOW:
        if err is not None:
            raise _err("ElfLazyBinding: %s" % err, log)
        log.append("DT_FLAGS_1: 0x%X -> 0x%X" % (old, old & ~DF_1_NOW & _M32))
    old, ok, err = _clear_dyn_flag(p, DT_FLAGS, DF_BIND_NOW)
    if ok and old & DF_BIND_NOW:
        if err is not None:
            raise _err("ElfLazyBinding: %s" % err, log)
        log.append("DT_FLAGS: 0x%X -> 0x%X" % (old, old & ~DF_BIND_NOW & _M32))
    try:
        ok = elf_remove_dyn_entry(p, DT_BIND_NOW)
    except PatchError as e:
        raise _err("ElfLazyBinding: %s" % e, log) from None
    if ok:
        log.append("DT_BIND_NOW: removed")
    try:
        ok = elf_disable_program_header(p, PT_GNU_RELRO)
    except PatchError as e:
        raise _err("ElfLazyBinding: %s" % e, log) from None
    log.append("PT_GNU_RELRO: -> PT_NULL" if ok else "PT_GNU_RELRO: not present")

    try:
        nei = parse_elf(p.buf)
    except PatchError as e:
        raise _err("ElfLazyBinding: re-parse: %s" % e, log) from None
    err = None
    try:
        e = nei.eager()
    except PatchError as ex:
        e, err = [], ex
    if err is not None or e:
        raise _err(
            "ElfLazyBinding: sanity check failed: still eager after patching (%s, err=%s)" % (_gostrs(e), _goerr(err)),
            log,
        )
    return log


def stub_function(p, sym: str, ret):
    """Overwrite the entry of a function with an immediate return; returns
    (file offset, original bytes).

    If ret is None, the function returns void, otherwise it returns ret
    (0-255) in r0. The symbol must be a defined STT_FUNC in an executable
    segment, and large enough for the stub.
    """
    try:
        s = elf_sym(p, sym)
    except PatchError as e:
        raise _err("StubFunction: %s" % e, (0, None)) from None
    t = s.info & 0xF
    if t != STT_FUNC:
        raise _err("StubFunction: %s is %s, not STT_FUNC" % (quote(sym), stt_name(t)), (0, None))
    if ret is not None and not 0 <= ret <= 0xFF:
        raise _err("StubFunction: return value %d does not fit in an 8-bit immediate" % ret, (0, None))
    if s.thumb and ret is None:
        stub = THUMB_BX_LR + THUMB_NOP  # bx lr; nop
    elif s.thumb:
        stub = bytes([ret, 0x20]) + THUMB_BX_LR  # movs r0, #ret; bx lr
    elif ret is None:
        stub = ARM_BX_LR  # bx lr
    else:
        stub = bytes([ret, 0x00, 0xA0, 0xE3]) + ARM_BX_LR  # mov r0, #ret; bx lr
    if s.size < len(stub):
        raise _err("StubFunction: %s is only %d bytes long (need %d)" % (quote(sym), s.size, len(stub)), (0, None))
    if not s.thumb and s.addr % 4 != 0:
        raise _err("StubFunction: ARM function %s is not 4-byte aligned" % quote(sym), (0, None))
    try:
        ei = parse_elf(p.buf)
    except PatchError as e:
        raise _err("StubFunction: %s" % e, (0, None)) from None
    try:
        off, l = ei.vaddr_off(s.addr, len(stub))  # noqa: E741
    except PatchError as e:
        raise _err("StubFunction: %s: %s" % (quote(sym), e), (0, None)) from None
    if not l.flags & PF_X:
        raise _err("StubFunction: %s is not in an executable segment" % quote(sym), (0, None))
    orig = _slice(p.buf, off, len(stub), "StubFunction: %s" % quote(sym))
    if orig == stub:
        raise _err("StubFunction: %s is already stubbed" % quote(sym), (_i32(off), orig))
    try:
        write_at(p, off, orig, stub)
    except PatchError as e:
        raise _err("StubFunction: %s" % e, (_i32(off), orig)) from None
    p.elf_stubs.append(ElfStub(sym, stub))
    return _i32(off), orig


def decode_thumb_bl(pc: int, b: bytes):
    """(target, is_blx) of a Thumb BL/BLX at pc, or None (see asm)."""
    return asm.decode_thumb_bl(pc, b)


def nop_call(p, sym: str, rel: int, target: str) -> int:
    """Replace the BL/BLX at sym+rel with two Thumb NOPs after checking that it
    calls target (either directly, or through its PLT entry); returns the file
    offset."""
    try:
        s = elf_sym(p, sym)
    except PatchError as e:
        raise _err("NopCall: %s" % e, 0) from None
    if s.info & 0xF != STT_FUNC or not s.thumb:
        raise _err("NopCall: %s is not a Thumb function" % quote(sym), 0)
    if rel < 0 or rel % 2 != 0 or rel + 4 > s.size:
        raise _err("NopCall: offset %d is not a halfword inside %s (size %d)" % (rel, quote(sym), s.size), 0)
    try:
        ei = parse_elf(p.buf)
    except PatchError as e:
        raise _err("NopCall: %s" % e, 0) from None
    pc = (s.addr + rel) & _M32
    try:
        off, l = ei.vaddr_off(pc, 4)  # noqa: E741
    except PatchError as e:
        raise _err("NopCall: %s" % e, 0) from None
    if not l.flags & PF_X:
        raise _err("NopCall: %s is not in an executable segment" % quote(sym), 0)
    orig = _slice(p.buf, off, 4, "NopCall: %s" % quote(sym))
    r = asm.decode_thumb_bl(pc, orig)
    if r is None:
        raise _err("NopCall: %s at %s+0x%X is not a BL/BLX" % (hexbytes_spaced(orig), quote(sym), rel), _i32(off))
    tgt, blx = r

    # candidate addresses for target
    cands = []
    try:
        cands.append(elf_sym(p, target).addr)
    except PatchError:
        pass
    try:
        ds = p.extract_dynsyms(True)
    except PatchError as e:
        if not cands:
            raise _err("NopCall: resolve target %s: %s" % (quote(target), e), _i32(off)) from None
    else:
        for d in ds:
            if d.name == target or d.demangled == target:
                if d.offset_plt != 0:
                    cands.append(d.offset_plt)
                if d.offset_plt_tail != 0:
                    cands.append(d.offset_plt_tail)
    if not cands:
        raise _err("NopCall: no definition or PLT entry for target %s" % quote(target), _i32(off))
    if tgt not in cands:
        raise _err(
            "NopCall: %s at %s+0x%X calls 0x%X, not %s (0x[%s])"
            % ("BLX" if blx else "BL", quote(sym), rel, tgt, quote(target), " ".join("%X" % c for c in cands)),
            _i32(off),
        )
    nop = THUMB_NOP + THUMB_NOP
    try:
        write_at(p, off, orig, nop)
    except PatchError as e:
        raise _err("NopCall: %s" % e, _i32(off)) from None
    p.elf_nops.append(ElfNop(_i32(off), nop))
    return _i32(off)


def clean_path(s: str) -> str:
    """strings.TrimPrefix(path.Clean("/"+s with \\ as /), "/")."""
    out = []
    for seg in ("/" + s.replace("\\", "/")).split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/".join(out)


def tombstone(p, name: str) -> None:
    """Replace the whole file with a 0-byte one (this is how Kobo disables
    plugins).

    The path must match the path set with set_path (the name of the entry in
    KoboRoot.tgz, which guarantees that it exists in the firmware, or an
    absolute path ending with it).
    """
    if p.path == "":
        raise PatchError("Tombstone: the path of the file being patched is unknown")
    # exact match for paths in KoboRoot.tgz, suffix for filesystem paths (kobopatch-apply)
    want, have = clean_path(name), clean_path(p.path)
    if want == "" or (have != want and not (os.path.isabs(p.path) and have.endswith("/" + want))):
        raise PatchError(
            "Tombstone: path %s does not match the file being patched (%s)" % (quote(name), quote(p.path))
        )
    if len(p.buf) == 0:
        raise PatchError("Tombstone: file is already empty")
    p._call_hook(0, p.buf, None)
    # like p.buf[:0] in Go: a new (empty) buffer, the caller's one is unchanged
    p.buf = bytearray()
    p.cur = 0
    p.elf_syms, p._dynsyms, p._dynsyms_pltgot = None, None, False


_CHECK_TAGS = (
    DT_NEEDED,
    DT_SONAME,
    DT_HASH,
    DT_GNU_HASH,
    DT_STRTAB,
    DT_SYMTAB,
    DT_STRSZ,
    DT_REL,
    DT_RELSZ,
    DT_JMPREL,
    DT_PLTRELSZ,
    DT_PLTGOT,
    DT_INIT,
    DT_FINI,
    DT_INIT_ARRAY,
    DT_INIT_ARRAYSZ,
    DT_VERNEED,
    DT_VERNEEDNUM,
    DT_VERSYM,
)


def check_elf(p, orig: bytes) -> None:
    """Re-parse the patched buffer and compare it with the original.

    Makes sure the dynamic section and program headers are still well-formed,
    that eager binding and RELRO were changed together, and that all stubs and
    NOPs applied by this Patcher are still in place. Non-ELF and tombstoned
    files are ignored.
    """
    orig = bytes(orig)
    buf = p.buf
    if not orig.startswith(ELFMAG) or len(buf) == 0:
        return
    if len(buf) != len(orig):
        raise PatchError("CheckELF: size changed from %d to %d" % (len(orig), len(buf)))
    try:
        oi = parse_elf(orig)
    except PatchError:
        return  # not a file we understand, so we didn't patch it structurally
    try:
        ni = parse_elf(buf)
    except PatchError as e:
        raise PatchError("CheckELF: %s" % e) from None
    if orig[:52] != bytes(buf[:52]):
        raise PatchError("CheckELF: ELF header changed")
    if len(oi.progs) != len(ni.progs):
        raise PatchError("CheckELF: number of program headers changed")
    for i, op in enumerate(oi.progs):
        np_ = ni.progs[i]
        if op != np_ and not (
            op.type == PT_GNU_RELRO
            and np_.type == PT_NULL
            and op.off == np_.off
            and op.vaddr == np_.vaddr
            and op.filesz == np_.filesz
            and op.memsz == np_.memsz
        ):
            raise PatchError(
                "CheckELF: program header %d changed unexpectedly (%s -> %s)" % (i, pt_name(op.type), pt_name(np_.type))
            )
    if ni.dyn != oi.dyn:
        raise PatchError("CheckELF: PT_DYNAMIC changed")
    try:
        ni.vaddr_off(ni.dyn.vaddr & _M32, ni.dyn.filesz & _M32)
    except PatchError as e:
        raise PatchError("CheckELF: PT_DYNAMIC: %s" % e) from None
    try:
        ni.f.dynamic_symbols()
    except Exception as e:  # noqa: BLE001
        raise PatchError("CheckELF: read dynamic symbols: %s" % e) from None
    try:
        ni.f.imported_libraries()
    except Exception as e:  # noqa: BLE001
        raise PatchError("CheckELF: read DT_NEEDED: %s" % e) from None
    for t in _CHECK_TAGS:
        a, b = oi.dynamic(t), ni.dynamic(t)
        if len(a) != len(b):
            raise PatchError("CheckELF: number of %s entries changed" % dt_name(t))
        for x, y in zip(a, b):
            if x.val != y.val:
                raise PatchError("CheckELF: %s changed" % dt_name(t))

    try:
        oe = oi.eager()
        ne = ni.eager()
    except PatchError as e:
        raise PatchError("CheckELF: %s" % e) from None
    try:
        _, orelro = oi.prog(PT_GNU_RELRO)
    except PatchError:
        orelro = None
    try:
        _, nrelro = ni.prog(PT_GNU_RELRO)
    except PatchError as e:
        raise PatchError("CheckELF: %s" % e) from None
    if not ne and nrelro is not None:
        try:
            slots = ni.jump_slots(buf)
        except PatchError as e:
            raise PatchError("CheckELF: %s" % e) from None
        for s in slots:
            if nrelro.vaddr <= s < nrelro.vaddr + nrelro.memsz:
                raise PatchError(
                    "CheckELF: lazy binding, but PT_GNU_RELRO covers the PLT GOT slot 0x%X (would crash on the first lazy call)"
                    % s
                )
    if oe and ne and len(ne) < len(oe):
        raise PatchError("CheckELF: eager binding only partly removed (%s left of %s)" % (_gostrs(ne), _gostrs(oe)))
    if ne and orelro is not None and nrelro is None:
        raise PatchError("CheckELF: PT_GNU_RELRO removed, but still eagerly bound (%s)" % _gostrs(ne))
    if oe and not ne and nrelro is not None:
        raise PatchError("CheckELF: eager binding removed without removing PT_GNU_RELRO")

    p.elf_syms = None  # re-read them from the patched file
    for st in p.elf_stubs:
        try:
            ss = elf_sym(p, st.sym)
            off, _ = ni.vaddr_off(ss.addr, len(st.data))
        except PatchError as e:
            raise PatchError("CheckELF: stub %s: %s" % (quote(st.sym), e)) from None
        if _slice(buf, off, len(st.data), "CheckELF: stub %s" % quote(st.sym)) != st.data:
            raise PatchError("CheckELF: stub %s was overwritten" % quote(st.sym))
    for n in p.elf_nops:
        if bytes(buf[n.off : n.off + len(n.data)]) != n.data:
            raise PatchError("CheckELF: NOP at 0x%X was overwritten" % n.off)
