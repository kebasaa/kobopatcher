"""Dynamic symbols, PLT relocations and PLT entries of 32-bit ARM ELF files.

Tested with libnickel and libadobe from several firmware versions. It should
work on any ARM binary for the ARMv6 ABI or newer that uses the recommended
PLT format (as GCC and Clang emit it).
"""

import struct
from dataclasses import dataclass

from ..compat import go_compat
from ..gofmt import quote
from .elffile import ELFCLASS32, EM_ARM, R_ARM_JUMP_SLOT, STT_FUNC, ElfFile


class SymsError(Exception):
    pass


@dataclass
class Dynsym:
    # from the dynamic symbol table
    name: str
    offset: int  # st_value & ~1 (a vaddr)
    index: int
    type: int
    # from the R_ARM_JUMP_SLOT relocations
    offset_got: int = 0
    # from the PLT
    offset_plt: int = 0
    offset_plt_tail: int = 0
    # generated
    demangled: str = ""

    def go_repr(self) -> str:
        """%#v of kobopatch's *dynsym (as printed in its error messages)."""
        return (
            "&patchlib.dynsym{Name:%s, Offset:0x%x, Index:0x%x, Type:%d, OffsetGOT:0x%x, "
            "OffsetPLT:0x%x, OffsetPLTTail:0x%x, Demangled:%s}"
            % (
                quote(self.name),
                self.offset,
                self.index,
                self.type,
                self.offset_got,
                self.offset_plt,
                self.offset_plt_tail,
                quote(self.demangled),
            )
        )


def _check_arm32(e: ElfFile):
    # kobopatch wrote `Class != ELFCLASS32 && Machine != EM_ARM`, which only
    # rejected files that were neither
    if go_compat():
        bad = e.ei_class != ELFCLASS32 and e.e_machine != EM_ARM
    else:
        bad = e.ei_class != ELFCLASS32 or e.e_machine != EM_ARM
    if bad:
        raise SymsError("not a 32-bit arm elf")


def decdynsym(e: ElfFile, skip_pltgot: bool):
    from .demangled import demangle

    _check_arm32(e)
    try:
        edynsyms = e.dynamic_symbols()
    except Exception as ex:  # noqa: BLE001
        raise SymsError("get dynamic symbols: %s" % ex) from None
    dynsyms = []
    for i, s in enumerate(edynsyms):
        if s.name == "":
            continue  # unnamed symbols (usually _init and similar)
        dm = demangle(s.name) or ""
        dynsyms.append(
            Dynsym(
                name=s.name,
                # AAELF32: for relocation the value is the instruction address (st_value & ~1)
                offset=(s.value & 0xFFFFFFFF) & ~1,
                # the index into .dynsym (dynamic_symbols drops the null entry)
                index=i + 1,
                type=s.info & 0xF,
                demangled=dm,
            )
        )
    if skip_pltgot:
        return dynsyms

    try:
        pltrels = decpltrel(e)
    except SymsError as ex:
        raise SymsError("read plt relocs: %s" % ex) from None
    pltrelidx = {}
    for off, info in pltrels:
        pltrelidx[info >> 8] = (off, info)
    try:
        pltents = decplt(e)
    except SymsError as ex:
        raise SymsError("decode plt: %s" % ex) from None
    pltentidx = {ent[2]: ent for ent in pltents}
    for s in dynsyms:
        if s.type != STT_FUNC:
            continue  # only functions are in the PLT/GOT
        rel = pltrelidx.get(s.index)
        if rel is None:
            continue
        off, info = rel
        if info & 0xFF != R_ARM_JUMP_SLOT:
            continue
        s.offset_got = off
        ent = pltentidx.get(off)
        if ent is None:
            continue
        s.offset_plt, s.offset_plt_tail = ent[0], ent[1]
    return dynsyms


def decpltrel(e: ElfFile):
    """(r_offset, r_info) pairs from .rel.plt."""
    _check_arm32(e)
    sec = e.section(".rel.plt")
    if sec is None:
        raise SymsError("read .rel.plt: no such section")
    data = sec.data()
    rels = []
    for i in range(0, len(data) - 7, 8):
        rels.append(struct.unpack_from(e.bo + "II", data, i))
    # kobopatch appended a zero entry when it hit EOF (harmless: symbol 0 is
    # never looked up)
    rels.append((0, 0))
    return rels


def decplt(e: ElfFile):
    """Decode the PLT into (plt_offset, plt_tail_offset, got_offset) entries."""
    from ._plt import decode_plt

    _check_arm32(e)
    return decode_plt(e)
