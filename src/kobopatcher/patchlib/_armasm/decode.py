# Port of rsc.io/arm/armasm/decode.go
# (rsc.io/arm v0.0.0-20150420010332-9c32f2193064).
#
# Copyright 2014 The Go Authors.  All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

"""ARM (A32) instruction decoder.

:func:`decode` is a faithful port of Go's ``armasm.Decode``: it accepts the
same words and yields the same Op, Args and Len.  The only structural
difference is a lookup cache (see ``_candidates``) that skips table entries
which cannot match a given word; it preserves the table order, so the
first-highest-priority rule of the Go loop gives identical results.
"""

from __future__ import annotations

from .inst import (
    APSR,
    APSR_nzcv,
    FPSCR,
    PC,
    R15,
    SP,
    AddrLDM,
    AddrLDM_WB,
    AddrMode,
    AddrOffset,
    Endian,
    Imm,
    ImmAlt,
    Inst,
    Mem,
    ModeARM,
    Op,
    PCRel,
    Reg,
    RegList,
    RegShift,
    RegShiftReg,
    RegX,
    RotateRight,
    RotateRightExt,
    ShiftLeft,
    ShiftRight,
    ShiftRightSigned,
    _SHIFTS,
)
from .tables import *  # noqa: F401,F403  (arg_* constants)
from .tables import BKPT, BKPT_EQ, instFormats, opstr

# An instFormat describes the format of an instruction encoding.
# An instruction with 32-bit value x matches the format if x&mask == value
# and the condition matches.
# The condition matches if x>>28 == 0xF && value>>28==0xF
# or if x>>28 != 0xF and value>>28 == 0.
# If x matches the format, then the rest of the fields describe how to interpret x.
# The opBits describe bits that should be extracted from x and added to the opcode.
# For example opBits = 0x1234 means that the value
#   (2 bits at offset 1) followed by (4 bits at offset 3)
# should be added to op.
# Finally the args describe how to decode the instruction arguments.
# (In tables.py, args is a tuple holding only the arguments present.)


class DecodeError(ValueError):
    """Raised by decode (Go: the error returned by Decode)."""


errMode = "unsupported execution mode"
errShort = "truncated instruction"
errUnknown = "unknown instruction"

_condMask = 0xF0000000


def decode(src, mode) -> Inst:
    """Decode the leading bytes in src as a single instruction.

    src is a bytes-like object (bytes, bytearray, memoryview).  Raises
    DecodeError with the Go error text on failure.
    """
    if mode != ModeARM:
        raise DecodeError(errMode)
    if len(src) < 4:
        raise DecodeError(errShort)

    x = int.from_bytes(src[:4], "little")

    # The instFormat table contains both conditional and unconditional instructions.
    # Considering only the top 4 bits, the conditional instructions use mask=0, value=0,
    # while the unconditional instructions use mask=f, value=f.
    # Prepare a version of x with the condition cleared to 0 in conditional instructions
    # and then assume mask=f during matching.
    xNoCond = x
    if x & _condMask != _condMask:
        xNoCond &= ~_condMask

    key = ((xNoCond >> 16) & 0xFFF0) | ((x >> 4) & 0xF)
    cands = _cache[key]
    if cands is None:
        cands = _candidates(key)

    priority = 0
    inst_op = 0
    inst_args = None
    for mask, value, fpriority, fop, opfields, argfns in cands:
        if xNoCond & mask != value or fpriority <= priority:
            continue
        delta = 0
        for n_mask, off, shift in opfields:
            delta |= ((x >> off) & n_mask) << shift
        op = (fop + delta) & 0xFFFF

        # Special case: BKPT encodes with condition but cannot have one.
        if op & ~15 == BKPT_EQ and op != BKPT:
            continue

        args = [None, None, None, None]
        j = 0
        for fn in argfns:
            arg = fn(x)
            if arg is None:  # cannot decode argument
                break
            args[j] = arg
            j += 1
        else:
            inst_op = op
            inst_args = args
            priority = fpriority
    if inst_op != 0:
        return Inst(_op(inst_op), x, 4, inst_args)
    raise DecodeError(errUnknown)


# ---------------------------------------------------------------------------
# Format preprocessing.

# Bits used to build the candidate-cache key: the condition (after the
# xNoCond rewrite it is either 0 or 0xF), bits 27..20 and bits 7..4.
_KEY_BITS = 0xFFF000F0


def _key_word(key: int) -> int:
    return ((key & 0xFFF0) << 16) | ((key & 0xF) << 4)


def _prepare():
    out = []
    for mask, value, priority, op, opBits, args in instFormats:
        opfields = []
        deltaShift = 0
        while opBits != 0:
            n = opBits & 0xFF
            off = (opBits >> 8) & 0xFF
            opfields.append(((1 << n) - 1, off, deltaShift))
            deltaShift += n
            opBits >>= 16
        argfns = tuple(_decoders.get(a, _decode_nil) for a in args)
        out.append((mask | _condMask, value, priority, int(op), tuple(opfields), argfns))
    return tuple(out)


def _candidates(key: int):
    """Table entries (in table order) that can match a word with this key."""
    y = _key_word(key)
    cands = tuple(f for f in _formats if (y & f[0] & _KEY_BITS) == (f[1] & _KEY_BITS))
    _cache[key] = cands
    return cands


_OPS = tuple(Op(i) for i in range(len(opstr)))


def _op(v: int) -> Op:
    if v < len(_OPS):
        return _OPS[v]
    return Op(v)


# ---------------------------------------------------------------------------
# An instArg describes the encoding of a single argument.
# In the names used for arguments, _p_ means +, _m_ means -,
# _pm_ means +/- (usually keyed by the U bit).
# The _W suffix indicates a general addressing mode based on the P and W bits.
# The _offset and _postindex suffixes force the given addressing mode.
# The rest should be somewhat self-explanatory, at least given
# the decodeArg function.
#
# The instArg constants (arg_APSR, ...) are generated into tables.py from
# decode.go so that their values match Go exactly.


def decodeArg(aop: int, x: int):
    """Decode the arg described by aop from the instruction bits x.

    Returns None if x cannot be decoded according to aop.
    """
    return _decoders.get(aop, _decode_nil)(x)


def _decode_nil(x):
    return None


def _arg_APSR(x):
    return APSR


def _arg_FPSCR(x):
    return FPSCR


def _arg_R_0(x):
    return Reg(x & 0xF)


def _arg_R_8(x):
    return Reg((x >> 8) & 0xF)


def _arg_R_12(x):
    return Reg((x >> 12) & 0xF)


def _arg_R_16(x):
    return Reg((x >> 16) & 0xF)


def _arg_R_12_nzcv(x):
    r = Reg((x >> 12) & 0xF)
    if r == R15:
        return APSR_nzcv
    return r


def _arg_R_16_WB(x):
    mode = AddrLDM
    if (x >> 21) & 1 != 0:
        mode = AddrLDM_WB
    return Mem(Base=Reg((x >> 16) & 0xF), Mode=mode)


def _arg_R_rotate(x):
    Rm = Reg(x & 0xF)
    typ, count = decodeShift(x)
    # ROR #0 here means ROR #0, but decodeShift rewrites to RRX #1.
    if typ == RotateRightExt:
        return Reg(Rm)
    return RegShift(Rm, typ, count)


def _arg_R_shift_R(x):
    Rm = Reg(x & 0xF)
    Rs = Reg((x >> 8) & 0xF)
    typ = _SHIFTS[(x >> 5) & 3]
    return RegShiftReg(Rm, typ, Rs)


def _arg_R_shift_imm(x):
    Rm = Reg(x & 0xF)
    typ, count = decodeShift(x)
    if typ == ShiftLeft and count == 0:
        return Reg(Rm)
    return RegShift(Rm, typ, count)


def _arg_R1_0(x):
    return Reg(x & 0xF)


def _arg_R1_12(x):
    return Reg((x >> 12) & 0xF)


def _arg_R2_0(x):
    return Reg((x & 0xF) | 1)


def _arg_R2_12(x):
    return Reg(((x >> 12) & 0xF) | 1)


def _arg_SP(x):
    return SP


_D0 = 48
_S0 = 16


def _arg_Sd_Dd(x):
    v = (x >> 12) & 0xF
    vx = (x >> 22) & 1
    sz = (x >> 8) & 1
    if sz != 0:
        return Reg(_D0 + (vx << 4) + v)
    else:
        return Reg(_S0 + (v << 1) + vx)


def _arg_Dd_Sd(x):
    return decodeArg(arg_Sd_Dd, x ^ (1 << 8))  # noqa: F405


def _arg_Sd(x):
    v = (x >> 12) & 0xF
    vx = (x >> 22) & 1
    return Reg(_S0 + (v << 1) + vx)


def _arg_Sm_Dm(x):
    v = (x >> 0) & 0xF
    vx = (x >> 5) & 1
    sz = (x >> 8) & 1
    if sz != 0:
        return Reg(_D0 + (vx << 4) + v)
    else:
        return Reg(_S0 + (v << 1) + vx)


def _arg_Sm(x):
    v = (x >> 0) & 0xF
    vx = (x >> 5) & 1
    return Reg(_S0 + (v << 1) + vx)


def _arg_Dn_half(x):
    v = (x >> 16) & 0xF
    vx = (x >> 7) & 1
    return RegX(Reg(_D0 + (vx << 4) + v), (x >> 21) & 1)


def _arg_Sn_Dn(x):
    v = (x >> 16) & 0xF
    vx = (x >> 7) & 1
    sz = (x >> 8) & 1
    if sz != 0:
        return Reg(_D0 + (vx << 4) + v)
    else:
        return Reg(_S0 + (v << 1) + vx)


def _arg_Sn(x):
    v = (x >> 16) & 0xF
    vx = (x >> 7) & 1
    return Reg(_S0 + (v << 1) + vx)


def _arg_const(x):
    v = x & 0xFF
    rot = ((x >> 8) & 0xF) * 2
    if rot > 0 and v & 3 == 0:
        # could rotate less
        return ImmAlt(v, rot)
    # Go: rot >= 24 && ((v<<(32-rot))&0xFF)>>(32-rot) == v, in uint32
    # (rot <= 30 here, so 32-rot is in [2, 8]).
    if rot >= 24 and ((((v << (32 - rot)) & 0xFFFFFFFF) & 0xFF) >> (32 - rot)) == v:
        # could wrap around to rot==0.
        return ImmAlt(v, rot)
    if rot == 0:
        return Imm(v)  # Go: v<<32 == 0 for uint32
    return Imm((v >> rot) | ((v << (32 - rot)) & 0xFFFFFFFF))


def _arg_endian(x):
    return Endian((x >> 9) & 1)


def _arg_fbits(x):
    return Imm(((16 << ((x >> 7) & 1)) - (((x & 0xF) << 1) | ((x >> 5) & 1))) & 0xFFFFFFFF)


def _arg_fp_0(x):
    return Imm(0)


def _arg_imm24(x):
    return Imm(x & 0xFFFFFF)


def _arg_imm5(x):
    return Imm((x >> 7) & 0x1F)


def _arg_imm5_32(x):
    x = (x >> 7) & 0x1F
    if x == 0:
        x = 32
    return Imm(x)


def _arg_imm5_nz(x):
    x = (x >> 7) & 0x1F
    if x == 0:
        return None
    return Imm(x)


def _arg_imm_4at16_12at0(x):
    return Imm((((x >> 16) & 0xF) << 12) | (x & 0xFFF))


def _arg_imm_12at8_4at0(x):
    return Imm((((x >> 8) & 0xFFF) << 4) | (x & 0xF))


def _arg_imm_vfp(x):
    x = (((x >> 16) & 0xF) << 4) | (x & 0xF)
    return Imm(x)


def _sext26(imm: int) -> int:
    # Go: int32(imm<<6) >> 6 with imm uint32
    imm &= 0x3FFFFFF
    if imm & 0x2000000:
        imm -= 0x4000000
    return imm


def _arg_label24(x):
    imm = (x & 0xFFFFFF) << 2
    return PCRel(_sext26(imm))


def _arg_label24H(x):
    h = (x >> 24) & 1
    imm = ((x & 0xFFFFFF) << 2) | (h << 1)
    return PCRel(_sext26(imm))


def _arg_label_m_12(x):
    d = x & 0xFFF
    return Mem(Base=PC, Mode=AddrOffset, Offset=-d)


def _arg_label_p_12(x):
    d = x & 0xFFF
    return Mem(Base=PC, Mode=AddrOffset, Offset=d)


def _arg_label_pm_12(x):
    d = x & 0xFFF
    u = (x >> 23) & 1
    if u == 0:
        d = -d
    return Mem(Base=PC, Mode=AddrOffset, Offset=d)


def _arg_label_pm_4_4(x):
    d = (((x >> 8) & 0xF) << 4) | (x & 0xF)
    u = (x >> 23) & 1
    if u == 0:
        d = -d
    return PCRel(d)


def _arg_lsb_width(x):
    lsb = (x >> 7) & 0x1F
    msb = (x >> 16) & 0x1F
    if msb < lsb or msb >= 32:
        return None
    return Imm(msb + 1 - lsb)


def _arg_mem_R(x):
    Rn = Reg((x >> 16) & 0xF)
    return Mem(Base=Rn, Mode=AddrOffset)


def _arg_mem_R_pm_R_postindex(x):
    # Treat [<Rn>],+/-<Rm> like [<Rn>,+/-<Rm>{,<shift>}]{!}
    # by forcing shift bits to <<0 and P=0, W=0 (postindex=true).
    return _arg_mem_R_pm_R_shift_imm_W(x & ~((0x7F << 5) | (1 << 24) | (1 << 21)))


def _arg_mem_R_pm_R_W(x):
    # Treat [<Rn>,+/-<Rm>]{!} like [<Rn>,+/-<Rm>{,<shift>}]{!}
    # by forcing shift bits to <<0.
    return _arg_mem_R_pm_R_shift_imm_W(x & ~(0x7F << 5))


def _arg_mem_R_pm_R_shift_imm_offset(x):
    # Treat [<Rn>],+/-<Rm>{,<shift>} like [<Rn>,+/-<Rm>{,<shift>}]{!}
    # by forcing P=1, W=0 (index=false, wback=false).
    return _arg_mem_R_pm_R_shift_imm_W((x & ~(1 << 21)) | (1 << 24))


def _arg_mem_R_pm_R_shift_imm_postindex(x):
    # Treat [<Rn>],+/-<Rm>{,<shift>} like [<Rn>,+/-<Rm>{,<shift>}]{!}
    # by forcing P=0, W=0 (postindex=true).
    return _arg_mem_R_pm_R_shift_imm_W(x & ~((1 << 24) | (1 << 21)))


def _arg_mem_R_pm_R_shift_imm_W(x):
    Rn = Reg((x >> 16) & 0xF)
    Rm = Reg(x & 0xF)
    typ, count = decodeShift(x)
    u = (x >> 23) & 1
    w = (x >> 21) & 1
    p = (x >> 24) & 1
    if p == 0 and w == 1:
        return None
    sign = +1
    if u == 0:
        sign = -1
    mode = AddrMode((p << 1) | (w ^ 1))
    return Mem(Base=Rn, Mode=mode, Sign=sign, Index=Rm, Shift=typ, Count=count)


def _arg_mem_R_pm_imm12_offset(x):
    # Treat [<Rn>,#+/-<imm12>] like [<Rn>{,#+/-<imm12>}]{!}
    # by forcing P=1, W=0 (index=false, wback=false).
    return _arg_mem_R_pm_imm12_W((x & ~(1 << 21)) | (1 << 24))


def _arg_mem_R_pm_imm12_postindex(x):
    # Treat [<Rn>],#+/-<imm12> like [<Rn>{,#+/-<imm12>}]{!}
    # by forcing P=0, W=0 (postindex=true).
    return _arg_mem_R_pm_imm12_W(x & ~((1 << 24) | (1 << 21)))


def _arg_mem_R_pm_imm12_W(x):
    Rn = Reg((x >> 16) & 0xF)
    u = (x >> 23) & 1
    w = (x >> 21) & 1
    p = (x >> 24) & 1
    if p == 0 and w == 1:
        return None
    sign = +1
    if u == 0:
        sign = -1
    imm = x & 0xFFF
    mode = AddrMode((p << 1) | (w ^ 1))
    return Mem(Base=Rn, Mode=mode, Offset=sign * imm)


def _arg_mem_R_pm_imm8_postindex(x):
    # Treat [<Rn>],#+/-<imm8> like [<Rn>{,#+/-<imm8>}]{!}
    # by forcing P=0, W=0 (postindex=true).
    return _arg_mem_R_pm_imm8_W(x & ~((1 << 24) | (1 << 21)))


def _arg_mem_R_pm_imm8_W(x):
    Rn = Reg((x >> 16) & 0xF)
    u = (x >> 23) & 1
    w = (x >> 21) & 1
    p = (x >> 24) & 1
    if p == 0 and w == 1:
        return None
    sign = +1
    if u == 0:
        sign = -1
    imm = (((x >> 8) & 0xF) << 4) | (x & 0xF)
    mode = AddrMode((p << 1) | (w ^ 1))
    return Mem(Base=Rn, Mode=mode, Offset=sign * imm)


def _arg_mem_R_pm_imm8at0_offset(x):
    Rn = Reg((x >> 16) & 0xF)
    u = (x >> 23) & 1
    sign = +1
    if u == 0:
        sign = -1
    imm = (x & 0xFF) << 2
    return Mem(Base=Rn, Mode=AddrOffset, Offset=sign * imm)


def _arg_option(x):
    return Imm(x & 0xF)


def _arg_registers(x):
    return RegList(x & 0xFFFF)


def _arg_registers2(x):
    x &= 0xFFFF
    n = bin(x).count("1")
    if n < 2:
        return None
    return RegList(x)


def _arg_registers1(x):
    Rt = (x >> 12) & 0xF
    return RegList(1 << Rt)


def _arg_satimm4(x):
    return Imm((x >> 16) & 0xF)


def _arg_satimm5(x):
    return Imm((x >> 16) & 0x1F)


def _arg_satimm4m1(x):
    return Imm(((x >> 16) & 0xF) + 1)


def _arg_satimm5m1(x):
    return Imm(((x >> 16) & 0x1F) + 1)


def _arg_widthm1(x):
    return Imm(((x >> 16) & 0x1F) + 1)


def decodeShift(x: int):
    """Decode the shift-by-immediate encoded in x: returns (Shift, count)."""
    count = (x >> 7) & 0x1F
    typ = (x >> 5) & 3
    if typ == ShiftRight or typ == ShiftRightSigned:
        if count == 0:
            count = 32
    elif typ == RotateRight:
        if count == 0:
            typ = RotateRightExt
            count = 1
    return _SHIFTS[typ], count


# Dispatch table for decodeArg (Go: the switch statement).  Names missing
# here fall through to the Go default case (return nil).
_decoders = {}
for _name, _fn in list(globals().items()):
    if _name.startswith("_arg_") and callable(_fn):
        _decoders[globals()[_name[1:]]] = _fn
del _name, _fn

_formats = _prepare()
_cache: list = [None] * 0x10000
