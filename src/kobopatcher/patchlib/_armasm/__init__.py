# Python port of rsc.io/arm/armasm
# (https://pkg.go.dev/rsc.io/arm/armasm, version v0.0.0-20150420010332-9c32f2193064).
#
# Copyright 2014 The Go Authors.  All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

"""ARM instruction decoder (port of Go's rsc.io/arm/armasm, ARM mode only).

Only what kobopatch uses is exported: :func:`decode` (Go ``Decode``) plus
the Inst/Op/Arg types it produces.  Go names are kept (``Inst.Op``,
``Mem.Base``, ``ImmAlt.Imm()``, ``armasm.ADD`` -> ``Op.ADD`` or ``ADD``) so
Go code using armasm can be ported line by line::

    try:
        t = decode(buf, ModeARM)
    except DecodeError:
        ...                  # Go: err != nil
    if t.Op != Op.LDR: ...

Differences from Go: errors are raised as DecodeError (message = Go error
text); ``Inst.Args`` is a 4-element list padded with None (Go: nil); the
Float32Imm/Float64Imm types (never produced by Decode) and the unexported
Plan 9 syntax printer are not ported.
"""

from .decode import DecodeError, decode, errMode, errShort, errUnknown
from .gnu import GNUSyntax
from .inst import (
    APSR,
    APSR_nzcv,
    FPSCR,
    LR,
    PC,
    SP,
    AddrLDM,
    AddrLDM_WB,
    AddrMode,
    AddrOffset,
    AddrPostIndex,
    AddrPreIndex,
    Arg,
    BigEndian,
    Endian,
    Imm,
    ImmAlt,
    Inst,
    Label,
    LittleEndian,
    Mem,
    Mode,
    ModeARM,
    ModeThumb,
    Op,
    PCRel,
    Reg,
    RegList,
    RegShift,
    RegShiftReg,
    RegX,
    RotateRight,
    RotateRightExt,
    Shift,
    ShiftLeft,
    ShiftRight,
    ShiftRightSigned,
    go_quote_ascii,
    gosyntax,
    gotype,
    quote_insts,
)
from .inst import (  # noqa: F401  (registers)
    R0, R1, R2, R3, R4, R5, R6, R7, R8, R9, R10, R11, R12, R13, R14, R15,
    S0, S1, S2, S3, S4, S5, S6, S7, S8, S9, S10, S11, S12, S13, S14, S15,
    S16, S17, S18, S19, S20, S21, S22, S23, S24, S25, S26, S27, S28, S29, S30, S31,
    D0, D1, D2, D3, D4, D5, D6, D7, D8, D9, D10, D11, D12, D13, D14, D15,
    D16, D17, D18, D19, D20, D21, D22, D23, D24, D25, D26, D27, D28, D29, D30, D31,
)
from . import tables as _tables

# The Op constants (armasm.ADD, armasm.LDR, ...), also available as Op.ADD.
globals().update({_n: getattr(_tables, _n) for _n in _tables.OP_NAMES})

__all__ = [
    "decode", "DecodeError", "errMode", "errShort", "errUnknown", "GNUSyntax",
    "Mode", "ModeARM", "ModeThumb", "Op", "Inst", "Arg",
    "Imm", "ImmAlt", "Label", "Reg", "RegX", "RegList", "Endian", "RegShift",
    "RegShiftReg", "PCRel", "Mem", "AddrMode", "Shift",
    "AddrPostIndex", "AddrPreIndex", "AddrOffset", "AddrLDM", "AddrLDM_WB",
    "ShiftLeft", "ShiftRight", "ShiftRightSigned", "RotateRight", "RotateRightExt",
    "LittleEndian", "BigEndian", "APSR", "APSR_nzcv", "FPSCR", "SP", "LR", "PC",
    "gosyntax", "gotype", "go_quote_ascii", "quote_insts",
] + [f"R{i}" for i in range(16)] + [f"S{i}" for i in range(32)] + [f"D{i}" for i in range(32)] \
  + list(_tables.OP_NAMES)
