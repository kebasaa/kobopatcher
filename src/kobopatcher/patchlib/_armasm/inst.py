# Port of rsc.io/arm/armasm/inst.go
# (rsc.io/arm v0.0.0-20150420010332-9c32f2193064).
#
# Copyright 2014 The Go Authors.  All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

"""Instruction and argument types produced by :func:`decode`.

The Go named integer types (Op, Reg, Imm, ...) are ``int`` subclasses whose
``str()`` is the Go ``String()`` method.  Go struct types (ImmAlt, Mem, ...)
are frozen dataclasses with the Go field names.  The argument integer types
compare equal only to values of the same type (or plain ``int``), mirroring
Go interface comparison: ``Imm(12) != R12`` just as in Go.

:func:`gosyntax` renders a value like Go's ``%#v`` and :func:`quote_insts`
like ``%+q`` of an ``[]Inst``, for error messages that must match kobopatch.
"""

from __future__ import annotations

from dataclasses import dataclass


def _format_str(self, spec: str) -> str:
    # Like Go's %v / %s: use the String() form unless a numeric spec is given.
    if not spec or spec[-1] == "s":
        return format(str(self), spec[:-1] if spec.endswith("s") else spec)
    return int.__format__(int(self), spec)



# A Mode is an instruction execution mode.
class Mode(int):
    __slots__ = ()
    __format__ = _format_str

    def __str__(self) -> str:
        if self == 1:
            return "ARM"
        if self == 2:
            return "Thumb"
        return f"Mode({int(self)})"

    def __repr__(self) -> str:
        return f"Mode({int(self)})"


ModeARM = Mode(1)
ModeThumb = Mode(2)


# An Op is an ARM opcode.
#
# NOTE: The actual Op values are defined in tables.py.
# They are chosen to simplify instruction decoding and
# are not a dense packing from 0 to N, although the
# density is high, probably at least 90%.
class Op(int):
    __slots__ = ()
    _opstr: tuple = ()  # set by tables.py

    def __str__(self) -> str:
        opstr = Op._opstr
        if self >= len(opstr) or opstr[self] == "":
            return f"Op({int(self)})"
        return opstr[self]

    __format__ = _format_str

    def __repr__(self) -> str:
        return f"Op({int(self)}) {str(self)!r}"


class _ArgInt(int):
    """Base for Go named integer types that implement the Arg interface.

    Equality follows Go interface semantics: values of different Arg types
    never compare equal.  Comparison with a plain ``int`` compares values.
    """

    __slots__ = ()

    def __eq__(self, other):
        t = type(other)
        if t is type(self) or t is int:
            return int.__eq__(self, other)
        return NotImplemented

    def __ne__(self, other):
        t = type(other)
        if t is type(self) or t is int:
            return int.__ne__(self, other)
        return NotImplemented

    __hash__ = int.__hash__
    __format__ = _format_str

    def __repr__(self) -> str:
        return f"{type(self).__name__}({int(self)})"

    def IsArg(self) -> None:
        pass


# An Imm is an integer constant (Go uint32).
class Imm(_ArgInt):
    __slots__ = ()

    def __str__(self) -> str:
        return f"#{_hex(int(self) & 0xFFFFFFFF)}"


# A ImmAlt is an alternate encoding of an integer constant.
@dataclass(frozen=True, slots=True)
class ImmAlt:
    Val: int  # uint8
    Rot: int  # uint8

    def IsArg(self) -> None:
        pass

    def Imm(self) -> Imm:
        v = self.Val & 0xFF
        r = self.Rot & 0xFF
        return Imm(_shr32(v, r) | _shl32(v, (32 - r) & _UINT_MASK))

    def __str__(self) -> str:
        return f"#{_hex(self.Val)}, {self.Rot}"


# A Label is a text (code) address.
class Label(_ArgInt):
    __slots__ = ()

    def __str__(self) -> str:
        return _hex(int(self) & 0xFFFFFFFF)


# A Reg is a single register.
# The zero value denotes R0, not the absence of a register.
class Reg(_ArgInt):
    __slots__ = ()

    def __str__(self) -> str:
        r = int(self)
        if r == _APSR:
            return "APSR"
        if r == _APSR_nzcv:
            return "APSR_nzcv"
        if r == _FPSCR:
            return "FPSCR"
        if r == 13:
            return "SP"
        if r == 15:
            return "PC"
        if r == 14:
            return "LR"
        if 0 <= r <= 15:
            return f"R{r}"
        if 16 <= r <= 47:
            return f"S{r - 16}"
        if 48 <= r <= 79:
            return f"D{r - 48}"
        return f"Reg({r})"


_APSR, _APSR_nzcv, _FPSCR = 80, 81, 82

R0, R1, R2, R3, R4, R5, R6, R7 = (Reg(i) for i in range(0, 8))
R8, R9, R10, R11, R12, R13, R14, R15 = (Reg(i) for i in range(8, 16))
(S0, S1, S2, S3, S4, S5, S6, S7, S8, S9, S10, S11, S12, S13, S14, S15, S16, S17,
 S18, S19, S20, S21, S22, S23, S24, S25, S26, S27, S28, S29, S30, S31) = (Reg(i) for i in range(16, 48))
(D0, D1, D2, D3, D4, D5, D6, D7, D8, D9, D10, D11, D12, D13, D14, D15, D16, D17,
 D18, D19, D20, D21, D22, D23, D24, D25, D26, D27, D28, D29, D30, D31) = (Reg(i) for i in range(48, 80))
APSR = Reg(_APSR)
APSR_nzcv = Reg(_APSR_nzcv)
FPSCR = Reg(_FPSCR)
SP = R13
LR = R14
PC = R15


# A RegX represents a fraction of a multi-value register.
# The Index field specifies the index number,
# but the size of the fraction is not specified.
# It must be inferred from the instruction and the register type.
# For example, in a VMOV instruction, RegX{D5, 1} represents
# the top 32 bits of the 64-bit D5 register.
@dataclass(frozen=True, slots=True)
class RegX:
    Reg: Reg
    Index: int

    def IsArg(self) -> None:
        pass

    def __str__(self) -> str:
        return f"{self.Reg}[{self.Index}]"


# A RegList is a register list.
# Bits at indexes x = 0 through 15 indicate whether the corresponding Rx
# register is in the list.
class RegList(_ArgInt):
    __slots__ = ()

    def __str__(self) -> str:
        parts = [str(Reg(i)) for i in range(16) if int(self) & (1 << i)]
        return "{" + ",".join(parts) + "}"


# An Endian is the argument to the SETEND instruction.
class Endian(_ArgInt):
    __slots__ = ()

    def __str__(self) -> str:
        if int(self) != 0:
            return "BE"
        return "LE"


LittleEndian = Endian(0)
BigEndian = Endian(1)


# A Shift describes an ARM shift operation.
class Shift(int):
    __slots__ = ()

    def __str__(self) -> str:
        if self < 5:
            return _shiftName[self]
        return f"Shift({int(self)})"

    __format__ = _format_str

    def __repr__(self) -> str:
        return f"Shift({int(self)})"


ShiftLeft = Shift(0)  # left shift
ShiftRight = Shift(1)  # logical (unsigned) right shift
ShiftRightSigned = Shift(2)  # arithmetic (signed) right shift
RotateRight = Shift(3)  # right rotate
RotateRightExt = Shift(4)  # right rotate through carry (Count will always be 1)

_shiftName = ("LSL", "LSR", "ASR", "ROR", "RRX")
_SHIFTS = (ShiftLeft, ShiftRight, ShiftRightSigned, RotateRight, RotateRightExt)


# A RegShift is a register shifted by a constant.
@dataclass(frozen=True, slots=True)
class RegShift:
    Reg: Reg
    Shift: Shift
    Count: int  # uint8

    def IsArg(self) -> None:
        pass

    def __str__(self) -> str:
        return f"{self.Reg} {self.Shift} #{self.Count}"


# A RegShiftReg is a register shifted by a register.
@dataclass(frozen=True, slots=True)
class RegShiftReg:
    Reg: Reg
    Shift: Shift
    RegCount: Reg

    def IsArg(self) -> None:
        pass

    def __str__(self) -> str:
        return f"{self.Reg} {self.Shift} {self.RegCount}"


# A PCRel describes a memory address (usually a code label)
# as a distance relative to the program counter (Go int32).
class PCRel(_ArgInt):
    __slots__ = ()

    def __str__(self) -> str:
        return f"PC{_signed_hex(int(self))}"


# An AddrMode is an ARM addressing mode.
class AddrMode(int):
    __slots__ = ()

    def __repr__(self) -> str:
        return f"AddrMode({int(self)})"


AddrPostIndex = AddrMode(1)  # [R], X - use address R, set R = R + X
AddrPreIndex = AddrMode(2)  # [R, X]! - use address R + X, set R = R + X
AddrOffset = AddrMode(3)  # [R, X] - use address R + X
AddrLDM = AddrMode(4)  # R - [R] but formats as R, for LDM/STM only
AddrLDM_WB = AddrMode(5)  # R! - [R], X where X is instruction-specific amount, for LDM/STM only


# A Mem is a memory reference made up of a base R and index expression X.
# The effective memory address is R or R+X depending on AddrMode.
# The index expression is X = Sign*(Index Shift Count) + Offset,
# but in any instruction either Sign = 0 or Offset = 0.
@dataclass(frozen=True, slots=True)
class Mem:
    Base: Reg = R0
    Mode: AddrMode = AddrMode(0)
    Sign: int = 0  # int8
    Index: Reg = R0
    Shift: Shift = ShiftLeft
    Count: int = 0  # uint8
    Offset: int = 0  # int16

    def IsArg(self) -> None:
        pass

    def __str__(self) -> str:
        R = str(self.Base)
        if self.Sign != 0:
            X = "+"
            if self.Sign < 0:
                X = "-"
            X += str(self.Index)
            if self.Shift != ShiftLeft or self.Count != 0:
                X += f", {Shift(self.Shift)} #{self.Count}"
        else:
            X = f"#{self.Offset}"

        mode = self.Mode
        if mode == AddrOffset:
            if X == "#0":
                return f"[{R}]"
            return f"[{R}, {X}]"
        if mode == AddrPreIndex:
            return f"[{R}, {X}]!"
        if mode == AddrPostIndex:
            return f"[{R}], {X}"
        if mode == AddrLDM:
            if X == "#0":
                return R
        elif mode == AddrLDM_WB:
            if X == "#0":
                return R + "!"
        return f"[{R} Mode({int(mode)}) {X}]"


Arg = (Imm, ImmAlt, Label, Reg, RegX, RegList, Endian, RegShift, RegShiftReg, PCRel, Mem)


class Inst:
    """A single instruction (Go armasm.Inst).

    Args always has 4 entries; if an instruction has fewer than 4 arguments,
    the final elements are None (Go: nil).
    """

    __slots__ = ("Op", "Enc", "Len", "Args")

    def __init__(self, Op: Op, Enc: int, Len: int, Args: list):
        self.Op = Op  # Opcode mnemonic
        self.Enc = Enc  # Raw encoding bits.
        self.Len = Len  # Length of encoding in bytes.
        self.Args = Args  # Instruction arguments, in ARM manual order.

    def __str__(self) -> str:
        buf = [str(self.Op)]
        for j, arg in enumerate(self.Args):
            if arg is None:
                break
            buf.append(" " if j == 0 else ", ")
            buf.append(str(arg))
        return "".join(buf)

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __repr__(self) -> str:
        return f"<Inst {str(self)!r} enc={self.Enc:#010x} len={self.Len}>"

    def __eq__(self, other):
        if type(other) is not Inst:
            return NotImplemented
        return (self.Op == other.Op and self.Enc == other.Enc and self.Len == other.Len
                and self.Args == other.Args)

    __hash__ = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Go fmt helpers.

_UINT_MASK = (1 << 64) - 1


def _shl32(v: int, n: int) -> int:
    """Go uint32 v << n (n unsigned; shifts >= 32 give 0)."""
    if n >= 32:
        return 0
    return (v << n) & 0xFFFFFFFF


def _shr32(v: int, n: int) -> int:
    """Go uint32 v >> n (n unsigned; shifts >= 32 give 0)."""
    if n >= 32:
        return 0
    return v >> n


def _hex(v: int) -> str:
    """Go %#x of a non-negative integer."""
    return f"0x{v:x}"


def _signed_hex(v: int) -> str:
    """Go %+#x of a signed integer."""
    if v < 0:
        return f"-0x{-v:x}"
    return f"+0x{v:x}"


_GO_TYPE = {
    Imm: "armasm.Imm",
    ImmAlt: "armasm.ImmAlt",
    Label: "armasm.Label",
    Reg: "armasm.Reg",
    RegX: "armasm.RegX",
    RegList: "armasm.RegList",
    Endian: "armasm.Endian",
    RegShift: "armasm.RegShift",
    RegShiftReg: "armasm.RegShiftReg",
    PCRel: "armasm.PCRel",
    Mem: "armasm.Mem",
}


def gotype(arg) -> str:
    """Go %T of an Arg (``<nil>`` for None)."""
    if arg is None:
        return "<nil>"
    return _GO_TYPE[type(arg)]


def gosyntax(arg) -> str:
    """Go %#v of an Arg (``<nil>`` for None)."""
    if arg is None:
        return "<nil>"
    t = type(arg)
    if t is PCRel:
        return str(int(arg))  # int32: decimal
    if t in (Imm, Label, Reg, RegList, Endian):
        return _hex(int(arg))  # unsigned: hex
    if t is ImmAlt:
        return f"armasm.ImmAlt{{Val:{_hex(arg.Val)}, Rot:{_hex(arg.Rot)}}}"
    if t is RegX:
        return f"armasm.RegX{{Reg:{_hex(arg.Reg)}, Index:{arg.Index}}}"
    if t is RegShift:
        return f"armasm.RegShift{{Reg:{_hex(arg.Reg)}, Shift:{_hex(arg.Shift)}, Count:{_hex(arg.Count)}}}"
    if t is RegShiftReg:
        return (f"armasm.RegShiftReg{{Reg:{_hex(arg.Reg)}, Shift:{_hex(arg.Shift)}, "
                f"RegCount:{_hex(arg.RegCount)}}}")
    if t is Mem:
        return (f"armasm.Mem{{Base:{_hex(arg.Base)}, Mode:{_hex(arg.Mode)}, Sign:{arg.Sign}, "
                f"Index:{_hex(arg.Index)}, Shift:{_hex(arg.Shift)}, Count:{_hex(arg.Count)}, "
                f"Offset:{arg.Offset}}}")
    raise TypeError(f"not an armasm Arg: {arg!r}")


def go_quote_ascii(s: str) -> str:
    """Go strconv.QuoteToASCII (what %+q uses for strings)."""
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch == '"' or ch == "\\":
            out.append("\\" + ch)
        elif 0x20 <= c < 0x7F:
            out.append(ch)
        elif ch in "\a\b\f\n\r\t\v":
            out.append({"\a": "\\a", "\b": "\\b", "\f": "\\f", "\n": "\\n",
                        "\r": "\\r", "\t": "\\t", "\v": "\\v"}[ch])
        elif c < 0x80:
            out.append(f"\\x{c:02x}")
        elif c < 0x10000:
            out.append(f"\\u{c:04x}")
        else:
            out.append(f"\\U{c:08x}")
    out.append('"')
    return "".join(out)


def quote_insts(insts) -> str:
    """Go ``fmt.Sprintf("%+q", insts)`` for an []armasm.Inst."""
    return "[" + " ".join(go_quote_ascii(str(i)) for i in insts) + "]"
