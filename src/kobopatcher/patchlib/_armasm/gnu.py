# Port of rsc.io/arm/armasm/gnu.go
# (rsc.io/arm v0.0.0-20150420010332-9c32f2193064).
#
# Copyright 2014 The Go Authors.  All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

"""GNU assembler syntax (GNUSyntax), used by the armasm decode tests."""

from __future__ import annotations

import re

from .inst import (
    R10,
    R11,
    R12,
    AddrLDM,
    AddrLDM_WB,
    AddrOffset,
    AddrPostIndex,
    AddrPreIndex,
    Imm,
    ImmAlt,
    Inst,
    Mem,
    PCRel,
    Reg,
    RegList,
    RegShift,
    RegShiftReg,
    RotateRightExt,
    Shift,
    ShiftLeft,
    _signed_hex,
)
from .tables import BKPT_EQ, LDRD_EQ, LDREX_EQ, LDREXD_EQ, STRD_EQ, STREXD_EQ, SVC_EQ

_saveDot_pairs = (
    (".F16", "_dot_F16"),
    (".F32", "_dot_F32"),
    (".F64", "_dot_F64"),
    (".S32", "_dot_S32"),
    (".U32", "_dot_U32"),
    (".FXS", "_dot_S"),
    (".FXU", "_dot_U"),
    (".32", "_dot_32"),
)
_saveDot_map = dict(_saveDot_pairs)
# strings.NewReplacer: leftmost match, earlier pairs win on ties (none of
# these patterns is a prefix of another, so alternation order is exact).
_saveDot_re = re.compile("|".join(re.escape(old) for old, _ in _saveDot_pairs))


def _go_hex_zero(v: int, width: int) -> str:
    # Go %#0<width>x: the 0x prefix is not counted in the width.
    return "0x" + format(v, "x").rjust(width, "0")


def _int32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def GNUSyntax(inst: Inst) -> str:
    """The GNU assembler syntax for the instruction, as defined by GNU binutils.

    This form typically matches the syntax defined in the ARM Reference Manual.
    """
    op = str(inst.Op)
    op = _saveDot_re.sub(lambda m: _saveDot_map[m.group(0)], op)
    op = op.replace(".", "")
    op = op.replace("_dot_", ".")
    op = op.lower()
    buf = [op]
    sep = " "
    for i, arg in enumerate(inst.Args):
        if arg is None:
            break
        text = gnuArg(inst, i, arg)
        if text == "":
            continue
        buf.append(sep)
        sep = ", "
        buf.append(text)
    return "".join(buf)


def _shift_lower(s: int) -> str:
    return str(Shift(s)).lower()


def gnuArg(inst: Inst, argIndex: int, arg) -> str:
    opc = inst.Op & ~15
    if opc in (LDRD_EQ, LDREXD_EQ, STRD_EQ):
        if argIndex == 1:
            # second argument in consecutive pair not printed
            return ""
    elif opc == STREXD_EQ:
        if argIndex == 2:
            # second argument in consecutive pair not printed
            return ""

    t = type(arg)
    if t is Imm:
        if opc == BKPT_EQ:
            return _go_hex_zero(int(arg), 4)
        if opc == SVC_EQ:
            return _go_hex_zero(int(arg), 8)
        return f"#{_int32(int(arg))}"

    elif t is ImmAlt:
        return f"#{arg.Val}, {arg.Rot}"

    elif t is Mem:
        R = gnuArg(inst, -1, arg.Base)
        X = ""
        if arg.Sign != 0:
            X = ""
            if arg.Sign < 0:
                X = "-"
            X += gnuArg(inst, -1, arg.Index)
            if arg.Shift == ShiftLeft and arg.Count == 0:
                pass  # nothing
            elif arg.Shift == RotateRightExt:
                X += ", rrx"
            else:
                X += f", {_shift_lower(arg.Shift)} #{arg.Count}"
        else:
            X = f"#{arg.Offset}"

        mode = arg.Mode
        if mode == AddrOffset:
            if X == "#0":
                return f"[{R}]"
            return f"[{R}, {X}]"
        elif mode == AddrPreIndex:
            return f"[{R}, {X}]!"
        elif mode == AddrPostIndex:
            return f"[{R}], {X}"
        elif mode == AddrLDM:
            if X == "#0":
                return R
        elif mode == AddrLDM_WB:
            if X == "#0":
                return R + "!"
        return f"[{R} Mode({int(mode)}) {X}]"

    elif t is PCRel:
        return "." + _signed_hex(_int32(int(arg) + 4))

    elif t is Reg:
        if opc == LDREX_EQ:
            if argIndex == 0:
                return f"r{int(arg)}"
        if arg == R10:
            return "sl"
        if arg == R11:
            return "fp"
        if arg == R12:
            return "ip"

    elif t is RegList:
        parts = [gnuArg(inst, -1, Reg(i)) for i in range(16) if int(arg) & (1 << i)]
        return "{" + ", ".join(parts) + "}"

    elif t is RegShift:
        if arg.Shift == ShiftLeft and arg.Count == 0:
            return gnuArg(inst, -1, arg.Reg)
        if arg.Shift == RotateRightExt:
            return gnuArg(inst, -1, arg.Reg) + ", rrx"
        return f"{gnuArg(inst, -1, arg.Reg)}, {_shift_lower(arg.Shift)} #{arg.Count}"

    elif t is RegShiftReg:
        return f"{gnuArg(inst, -1, arg.Reg)}, {_shift_lower(arg.Shift)} {gnuArg(inst, -1, arg.RegCount)}"

    return str(arg).lower()
