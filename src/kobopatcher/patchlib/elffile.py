"""A small ELF reader covering what kobopatcher needs.

It mirrors the parts of Go's debug/elf that kobopatch used (header, program
and section headers, dynamic symbols, imported libraries), so that
behaviour and error messages stay the same.
"""

import struct
from dataclasses import dataclass, field

ELFMAG = b"\x7fELF"

ELFCLASS32 = 1
ELFCLASS64 = 2
ELFDATA2LSB = 1
ELFDATA2MSB = 2
EM_ARM = 40

# program header types and flags
PT_NULL = 0
PT_LOAD = 1
PT_DYNAMIC = 2
PT_GNU_STACK = 0x6474E551
PT_GNU_RELRO = 0x6474E552
PF_X = 0x1
PF_W = 0x2
PF_R = 0x4

# section types
SHT_DYNSYM = 11
SHT_DYNAMIC = 6
SHT_NOBITS = 8

# symbol types and section indexes
STT_FUNC = 2
SHN_UNDEF = 0

# dynamic tags
DT_NULL = 0
DT_NEEDED = 1
DT_PLTRELSZ = 2
DT_PLTGOT = 3
DT_HASH = 4
DT_STRTAB = 5
DT_SYMTAB = 6
DT_RELA = 7
DT_RELASZ = 8
DT_STRSZ = 10
DT_INIT = 12
DT_FINI = 13
DT_SONAME = 14
DT_REL = 17
DT_RELSZ = 18
DT_PLTREL = 20
DT_JMPREL = 23
DT_BIND_NOW = 24
DT_INIT_ARRAY = 25
DT_INIT_ARRAYSZ = 27
DT_FLAGS = 30
DT_GNU_HASH = 0x6FFFFEF5
DT_VERSYM = 0x6FFFFFF0
DT_FLAGS_1 = 0x6FFFFFFB
DT_VERNEED = 0x6FFFFFFE
DT_VERNEEDNUM = 0x6FFFFFFF

DF_BIND_NOW = 0x8
DF_1_NOW = 0x1

R_ARM_JUMP_SLOT = 22

def _names(s):
    it = iter(s.split())
    return [(int(v, 0), n) for v, n in zip(it, it)]


# The String() tables of Go's debug/elf (in its order, duplicates included).
_CLASS_STRINGS = _names(
    "0 ELFCLASSNONE 1 ELFCLASS32 2 ELFCLASS64"
)
_DATA_STRINGS = _names(
    "0 ELFDATANONE 1 ELFDATA2LSB 2 ELFDATA2MSB"
)
_MACHINE_STRINGS = _names(
    "0 EM_NONE 1 EM_M32 2 EM_SPARC 3 EM_386 4 EM_68K 5 EM_88K 7 EM_860 8 EM_MIPS 9 EM_S370 0xa EM_MIPS_RS3_LE "
    "0xf EM_PARISC 0x11 EM_VPP500 0x12 EM_SPARC32PLUS 0x13 EM_960 0x14 EM_PPC 0x15 EM_PPC64 0x16 EM_S390 "
    "0x24 EM_V800 0x25 EM_FR20 0x26 EM_RH32 0x27 EM_RCE 0x28 EM_ARM 0x2a EM_SH 0x2b EM_SPARCV9 "
    "0x2c EM_TRICORE 0x2d EM_ARC 0x2e EM_H8_300 0x2f EM_H8_300H 0x30 EM_H8S 0x31 EM_H8_500 0x32 EM_IA_64 "
    "0x33 EM_MIPS_X 0x34 EM_COLDFIRE 0x35 EM_68HC12 0x36 EM_MMA 0x37 EM_PCP 0x38 EM_NCPU 0x39 EM_NDR1 "
    "0x3a EM_STARCORE 0x3b EM_ME16 0x3c EM_ST100 0x3d EM_TINYJ 0x3e EM_X86_64 0x3f EM_PDSP 0x40 EM_PDP10 "
    "0x41 EM_PDP11 0x42 EM_FX66 0x43 EM_ST9PLUS 0x44 EM_ST7 0x45 EM_68HC16 0x46 EM_68HC11 0x47 EM_68HC08 "
    "0x48 EM_68HC05 0x49 EM_SVX 0x4a EM_ST19 0x4b EM_VAX 0x4c EM_CRIS 0x4d EM_JAVELIN 0x4e EM_FIREPATH "
    "0x4f EM_ZSP 0x50 EM_MMIX 0x51 EM_HUANY 0x52 EM_PRISM 0x53 EM_AVR 0x54 EM_FR30 0x55 EM_D10V 0x56 EM_D30V "
    "0x57 EM_V850 0x58 EM_M32R 0x59 EM_MN10300 0x5a EM_MN10200 0x5b EM_PJ 0x5c EM_OPENRISC "
    "0x5d EM_ARC_COMPACT 0x5e EM_XTENSA 0x5f EM_VIDEOCORE 0x60 EM_TMM_GPP 0x61 EM_NS32K 0x62 EM_TPC "
    "0x63 EM_SNP1K 0x64 EM_ST200 0x65 EM_IP2K 0x66 EM_MAX 0x67 EM_CR 0x68 EM_F2MC16 0x69 EM_MSP430 "
    "0x6a EM_BLACKFIN 0x6b EM_SE_C33 0x6c EM_SEP 0x6d EM_ARCA 0x6e EM_UNICORE 0x6f EM_EXCESS 0x70 EM_DXP "
    "0x71 EM_ALTERA_NIOS2 0x72 EM_CRX 0x73 EM_XGATE 0x74 EM_C166 0x75 EM_M16C 0x76 EM_DSPIC30F 0x77 EM_CE "
    "0x78 EM_M32C 0x83 EM_TSK3000 0x84 EM_RS08 0x85 EM_SHARC 0x86 EM_ECOG2 0x87 EM_SCORE7 0x88 EM_DSP24 "
    "0x89 EM_VIDEOCORE3 0x8a EM_LATTICEMICO32 0x8b EM_SE_C17 0x8c EM_TI_C6000 0x8d EM_TI_C2000 "
    "0x8e EM_TI_C5500 0x8f EM_TI_ARP32 0x90 EM_TI_PRU 0xa0 EM_MMDSP_PLUS 0xa1 EM_CYPRESS_M8C 0xa2 EM_R32C "
    "0xa3 EM_TRIMEDIA 0xa4 EM_QDSP6 0xa5 EM_8051 0xa6 EM_STXP7X 0xa7 EM_NDS32 0xa8 EM_ECOG1 0xa8 EM_ECOG1X "
    "0xa9 EM_MAXQ30 0xaa EM_XIMO16 0xab EM_MANIK 0xac EM_CRAYNV2 0xad EM_RX 0xae EM_METAG 0xaf EM_MCST_ELBRUS "
    "0xb0 EM_ECOG16 0xb1 EM_CR16 0xb2 EM_ETPU 0xb3 EM_SLE9X 0xb4 EM_L10M 0xb5 EM_K10M 0xb7 EM_AARCH64 "
    "0xb9 EM_AVR32 0xba EM_STM8 0xbb EM_TILE64 0xbc EM_TILEPRO 0xbd EM_MICROBLAZE 0xbe EM_CUDA 0xbf EM_TILEGX "
    "0xc0 EM_CLOUDSHIELD 0xc1 EM_COREA_1ST 0xc2 EM_COREA_2ND 0xc3 EM_ARC_COMPACT2 0xc4 EM_OPEN8 0xc5 EM_RL78 "
    "0xc6 EM_VIDEOCORE5 0xc7 EM_78KOR 0xc8 EM_56800EX 0xc9 EM_BA1 0xca EM_BA2 0xcb EM_XCORE 0xcc EM_MCHP_PIC "
    "0xcd EM_INTEL205 0xce EM_INTEL206 0xcf EM_INTEL207 0xd0 EM_INTEL208 0xd1 EM_INTEL209 0xd2 EM_KM32 "
    "0xd3 EM_KMX32 0xd4 EM_KMX16 0xd5 EM_KMX8 0xd6 EM_KVARC 0xd7 EM_CDP 0xd8 EM_COGE 0xd9 EM_COOL "
    "0xda EM_NORC 0xdb EM_CSR_KALIMBA  0xdc EM_Z80  0xdd EM_VISIUM  0xde EM_FT32  0xdf EM_MOXIE "
    "0xe0 EM_AMDGPU 0xf3 EM_RISCV 0xf4 EM_LANAI 0xf7 EM_BPF 0x102 EM_LOONGARCH 6 EM_486 0xa EM_MIPS_RS4_BE "
    "0x29 EM_ALPHA_STD 0x9026 EM_ALPHA"
)
_PT_STRINGS = _names(
    "0 PT_NULL 1 PT_LOAD 2 PT_DYNAMIC 3 PT_INTERP 4 PT_NOTE 5 PT_SHLIB 6 PT_PHDR 7 PT_TLS 0x60000000 PT_LOOS "
    "0x6474e550 PT_GNU_EH_FRAME 0x6474e551 PT_GNU_STACK 0x6474e552 PT_GNU_RELRO 0x6474e553 PT_GNU_PROPERTY "
    "0x65041580 PT_PAX_FLAGS 0x65a3dbe6 PT_OPENBSD_RANDOMIZE 0x65a3dbe7 PT_OPENBSD_WXNEEDED "
    "0x65a41be6 PT_OPENBSD_BOOTDATA 0x6ffffffb PT_SUNWSTACK 0x6fffffff PT_HIOS 0x70000000 PT_LOPROC "
    "0x7fffffff PT_HIPROC"
)
_DT_STRINGS = _names(
    "0 DT_NULL 1 DT_NEEDED 2 DT_PLTRELSZ 3 DT_PLTGOT 4 DT_HASH 5 DT_STRTAB 6 DT_SYMTAB 7 DT_RELA 8 DT_RELASZ "
    "9 DT_RELAENT 0xa DT_STRSZ 0xb DT_SYMENT 0xc DT_INIT 0xd DT_FINI 0xe DT_SONAME 0xf DT_RPATH "
    "0x10 DT_SYMBOLIC 0x11 DT_REL 0x12 DT_RELSZ 0x13 DT_RELENT 0x14 DT_PLTREL 0x15 DT_DEBUG 0x16 DT_TEXTREL "
    "0x17 DT_JMPREL 0x18 DT_BIND_NOW 0x19 DT_INIT_ARRAY 0x1a DT_FINI_ARRAY 0x1b DT_INIT_ARRAYSZ "
    "0x1c DT_FINI_ARRAYSZ 0x1d DT_RUNPATH 0x1e DT_FLAGS 0x20 DT_ENCODING 0x20 DT_PREINIT_ARRAY "
    "0x21 DT_PREINIT_ARRAYSZ 0x22 DT_SYMTAB_SHNDX 0x6000000d DT_LOOS 0x6ffff000 DT_HIOS "
    "0x6ffffd00 DT_VALRNGLO 0x6ffffdf5 DT_GNU_PRELINKED 0x6ffffdf6 DT_GNU_CONFLICTSZ "
    "0x6ffffdf7 DT_GNU_LIBLISTSZ 0x6ffffdf8 DT_CHECKSUM 0x6ffffdf9 DT_PLTPADSZ 0x6ffffdfa DT_MOVEENT "
    "0x6ffffdfb DT_MOVESZ 0x6ffffdfc DT_FEATURE 0x6ffffdfd DT_POSFLAG_1 0x6ffffdfe DT_SYMINSZ "
    "0x6ffffdff DT_SYMINENT 0x6ffffdff DT_VALRNGHI 0x6ffffe00 DT_ADDRRNGLO 0x6ffffef5 DT_GNU_HASH "
    "0x6ffffef6 DT_TLSDESC_PLT 0x6ffffef7 DT_TLSDESC_GOT 0x6ffffef8 DT_GNU_CONFLICT 0x6ffffef9 DT_GNU_LIBLIST "
    "0x6ffffefa DT_CONFIG 0x6ffffefb DT_DEPAUDIT 0x6ffffefc DT_AUDIT 0x6ffffefd DT_PLTPAD "
    "0x6ffffefe DT_MOVETAB 0x6ffffeff DT_SYMINFO 0x6ffffeff DT_ADDRRNGHI 0x6ffffff0 DT_VERSYM "
    "0x6ffffff9 DT_RELACOUNT 0x6ffffffa DT_RELCOUNT 0x6ffffffb DT_FLAGS_1 0x6ffffffc DT_VERDEF "
    "0x6ffffffd DT_VERDEFNUM 0x6ffffffe DT_VERNEED 0x6fffffff DT_VERNEEDNUM 0x70000000 DT_LOPROC "
    "0x7ffffffd DT_AUXILIARY 0x7ffffffe DT_USED 0x7fffffff DT_FILTER"
)
_STT_STRINGS = _names(
    "0 STT_NOTYPE 1 STT_OBJECT 2 STT_FUNC 3 STT_SECTION 4 STT_FILE 5 STT_COMMON 6 STT_TLS 8 STT_RELC "
    "9 STT_SRELC 0xa STT_LOOS 0xc STT_HIOS 0xd STT_LOPROC 0xf STT_HIPROC"
)


def _string_name(i: int, names) -> str:
    """debug/elf's stringName (the String() form of its integer types)."""
    i &= 0xFFFFFFFF
    for v, n in names:
        if v == i:
            return n
    for v, n in reversed(names):
        if v < i:
            return "%s+%d" % (n, i - v)
    return str(i)


def dt_name(tag: int) -> str:
    """elf.DynTag(tag).String()."""
    return _string_name(tag, _DT_STRINGS)


def pt_name(typ: int) -> str:
    """elf.ProgType(typ).String()."""
    return _string_name(typ, _PT_STRINGS)


def stt_name(typ: int) -> str:
    """elf.SymType(typ).String()."""
    return _string_name(typ, _STT_STRINGS)


def class_name(c: int) -> str:
    return _string_name(c, _CLASS_STRINGS)


def data_name(d: int) -> str:
    return _string_name(d, _DATA_STRINGS)


def machine_name(m: int) -> str:
    return _string_name(m, _MACHINE_STRINGS)


class FormatError(Exception):
    """Like Go's elf.FormatError."""

    def __init__(self, off: int, msg: str, val=None):
        self.off, self.msg, self.val = off, msg, val
        s = msg
        if val is not None:
            s += " '%s' " % val
        s += "in record at byte %#x" % off
        super().__init__(s)


def _go_bytes(b: bytes) -> str:
    return "[" + " ".join(str(x) for x in b) + "]"


@dataclass
class Prog:
    type: int
    flags: int
    off: int
    vaddr: int
    paddr: int
    filesz: int
    memsz: int
    align: int


@dataclass
class Section:
    name: str
    type: int
    flags: int
    addr: int
    offset: int
    size: int
    link: int
    info: int
    addralign: int
    entsize: int
    _file: "ElfFile" = field(repr=False, default=None)

    def data(self) -> bytes:
        if self.type == SHT_NOBITS:
            return b""
        if self.offset + self.size > len(self._file.buf):
            raise FormatError(self.offset, "section data out of range")
        return bytes(self._file.buf[self.offset : self.offset + self.size])


@dataclass
class Symbol:
    name: str
    info: int
    other: int
    section: int
    value: int
    size: int

    @property
    def type(self) -> int:
        return self.info & 0xF


class ElfFile:
    """Parse an ELF file from a bytes-like buffer (it is not copied)."""

    def __init__(self, buf):
        self.buf = buf
        if len(buf) < 16:
            raise EOFError("unexpected EOF")
        ident = bytes(buf[:16])
        if ident[:4] != ELFMAG:
            raise FormatError(0, "bad magic number", _go_bytes(ident[:4]))
        self.ei_class = ident[4]
        if self.ei_class not in (ELFCLASS32, ELFCLASS64):
            raise FormatError(4, "unknown ELF class", "ELFCLASS%d" % self.ei_class if self.ei_class else "ELFCLASSNONE")
        self.ei_data = ident[5]
        if self.ei_data == ELFDATA2LSB:
            self.bo = "<"
        elif self.ei_data == ELFDATA2MSB:
            self.bo = ">"
        else:
            raise FormatError(5, "unknown ELF data encoding", "ELFDATA%d" % self.ei_data if self.ei_data else "ELFDATANONE")
        if ident[6] != 1:
            raise FormatError(6, "unknown ELF version", "EV_%d" % ident[6])
        bo = self.bo
        try:
            if self.ei_class == ELFCLASS32:
                (
                    self.e_type,
                    self.e_machine,
                    self.e_version,
                    self.e_entry,
                    self.e_phoff,
                    self.e_shoff,
                    self.e_flags,
                    self.e_ehsize,
                    self.e_phentsize,
                    self.e_phnum,
                    self.e_shentsize,
                    self.e_shnum,
                    self.e_shstrndx,
                ) = struct.unpack_from(bo + "HHIIIIIHHHHHH", buf, 16)
            else:
                (
                    self.e_type,
                    self.e_machine,
                    self.e_version,
                    self.e_entry,
                    self.e_phoff,
                    self.e_shoff,
                    self.e_flags,
                    self.e_ehsize,
                    self.e_phentsize,
                    self.e_phnum,
                    self.e_shentsize,
                    self.e_shnum,
                    self.e_shstrndx,
                ) = struct.unpack_from(bo + "HHIQQQIHHHHHH", buf, 16)
        except struct.error:
            raise EOFError("unexpected EOF") from None
        self.progs = self._read_progs()
        self.sections = self._read_sections()

    @property
    def is_arm32le(self) -> bool:
        return self.ei_class == ELFCLASS32 and self.ei_data == ELFDATA2LSB and self.e_machine == EM_ARM

    def _read_progs(self):
        progs = []
        if self.e_phnum == 0:
            return progs
        bo = self.bo
        fmt32, fmt64 = bo + "IIIIIIII", bo + "IIQQQQQQ"
        need = 32 if self.ei_class == ELFCLASS32 else 56
        if self.e_phentsize < need:
            raise FormatError(0, "invalid ELF phentsize", self.e_phentsize)
        for i in range(self.e_phnum):
            off = self.e_phoff + i * self.e_phentsize
            if off + need > len(self.buf):
                raise FormatError(off, "program header out of range")
            if self.ei_class == ELFCLASS32:
                t, o, va, pa, fs, ms, fl, al = struct.unpack_from(fmt32, self.buf, off)
            else:
                t, fl, o, va, pa, fs, ms, al = struct.unpack_from(fmt64, self.buf, off)
            progs.append(Prog(t, fl, o, va, pa, fs, ms, al))
        return progs

    def _read_sections(self):
        secs = []
        if self.e_shnum == 0 or self.e_shoff == 0:
            return secs
        bo = self.bo
        need = 40 if self.ei_class == ELFCLASS32 else 64
        if self.e_shentsize < need:
            raise FormatError(0, "invalid ELF shentsize", self.e_shentsize)
        if self.e_shstrndx >= self.e_shnum:
            raise FormatError(0, "invalid ELF shstrndx", self.e_shstrndx)
        raw = []
        for i in range(self.e_shnum):
            off = self.e_shoff + i * self.e_shentsize
            if off + need > len(self.buf):
                raise FormatError(off, "section header out of range")
            if self.ei_class == ELFCLASS32:
                raw.append(struct.unpack_from(bo + "IIIIIIIIII", self.buf, off))
            else:
                raw.append(struct.unpack_from(bo + "IIQQQQIIQQ", self.buf, off))
        shstr = raw[self.e_shstrndx]
        stroff, strsize = shstr[4], shstr[5]
        strtab = bytes(self.buf[stroff : stroff + strsize]) if shstr[1] != SHT_NOBITS else b""
        for r in raw:
            nm, t, fl, addr, off, size, link, info, align, entsize = r
            name = _cstr(strtab, nm)
            secs.append(Section(name, t, fl, addr, off, size, link, info, align, entsize, self))
        return secs

    def section(self, name: str):
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def section_by_type(self, typ: int):
        for s in self.sections:
            if s.type == typ:
                return s
        return None

    def dynamic_symbols(self):
        """Symbols from the SHT_DYNSYM section, without the initial null entry."""
        symsec = self.section_by_type(SHT_DYNSYM)
        if symsec is None:
            raise ErrNoSymbols()
        data = symsec.data()
        if self.ei_class == ELFCLASS32:
            entsz, fmt = 16, self.bo + "IIIBBH"
        else:
            entsz, fmt = 24, self.bo + "IBBHQQ"
        if len(data) % entsz != 0:
            raise FormatError(symsec.offset, "length of symbol section is not a multiple of SymSize")
        if symsec.link >= len(self.sections):
            raise FormatError(symsec.offset, "invalid section link")
        strdata = self.sections[symsec.link].data()
        syms = []
        for i in range(1, len(data) // entsz):
            if self.ei_class == ELFCLASS32:
                nm, val, size, info, other, shndx = struct.unpack_from(fmt, data, i * entsz)
            else:
                nm, info, other, shndx, val, size = struct.unpack_from(fmt, data, i * entsz)
            syms.append(Symbol(_cstr(strdata, nm), info, other, shndx, val, size))
        return syms

    def imported_libraries(self):
        """DT_NEEDED strings (like Go's ImportedLibraries)."""
        ds = self.section_by_type(SHT_DYNAMIC)
        if ds is None:
            return []
        d = ds.data()
        if ds.link >= len(self.sections):
            raise FormatError(ds.offset, "invalid section link")
        strdata = self.sections[ds.link].data()
        out = []
        if self.ei_class == ELFCLASS32:
            step, fmt = 8, self.bo + "iI"
        else:
            step, fmt = 16, self.bo + "qQ"
        for i in range(0, len(d) - step + 1, step):
            tag, val = struct.unpack_from(fmt, d, i)
            if tag == DT_NEEDED:
                out.append(_cstr(strdata, val))
        return out


class ErrNoSymbols(Exception):
    def __init__(self):
        super().__init__("no symbol section")


def _cstr(tab: bytes, off: int) -> str:
    if off >= len(tab):
        return ""
    end = tab.find(b"\x00", off)
    if end < 0:
        end = len(tab)
    return tab[off:end].decode("utf-8", "surrogateescape")
