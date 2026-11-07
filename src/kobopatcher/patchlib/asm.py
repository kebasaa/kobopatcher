"""Thumb-2 branch instruction encoders and decoders.

These are the 32-bit Thumb-2 encodings (not Thumb-1), see the Thumb-2
reference manual, sections 4.6.12 (B.W, encoding T4) and 4.6.18 (BLX,
encoding T2).
"""

import struct


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _halfwords(inst: int) -> bytes:
    # the instruction is two little-endian halfwords, first halfword first
    return struct.pack("<HH", (inst >> 16) & 0xFFFF, inst & 0xFFFF)


def asm_bw(pc: int, target: int) -> bytes:
    """Assemble a B.W (encoding T4) from pc to target (both file offsets).

    1 1 1 1 0 S imm10 1 0 J1 1 J2 imm11, with imm32 = target - PC - 4
    (between -16777216 and 16777214; larger values are truncated silently).
    """
    imm32 = _s32(_s32(target) - _s32(pc) - 4)
    imm11 = (imm32 >> 1) & 0x7FF
    imm10 = (imm32 >> 12) & 0x3FF
    i2 = (imm32 >> 22) & 1
    i1 = (imm32 >> 23) & 1
    s = 1 if imm32 < 0 else 0
    j2 = (~i2 ^ s) & 1
    j1 = (~i1 ^ s) & 1
    inst = (0b11110 << 27) | (s << 26) | (imm10 << 16) | (1 << 15) | (j1 << 13) | (1 << 12) | (j2 << 11) | imm11
    return _halfwords(inst)


def asm_blx(pc: int, target: int) -> bytes:
    """Assemble a BLX (encoding T2, Thumb to ARM) from pc to target.

    1 1 1 1 0 S imm10H 1 1 J1 0 J2 imm10L 0, with
    imm32 = target - (pc & ~3) - 4 (multiples of 4 between -16777216 and
    16777212; larger values are truncated silently).
    """
    imm32 = _s32(_s32(target) - _s32(pc & ~3) - 4)
    imm10l = (imm32 >> 2) & 0x3FF
    imm10h = (imm32 >> 12) & 0x3FF
    i2 = (imm32 >> 22) & 1
    i1 = (imm32 >> 23) & 1
    s = 1 if imm32 < 0 else 0
    j2 = (~i2 ^ s) & 1
    j1 = (~i1 ^ s) & 1
    inst = (0b11110 << 27) | (s << 26) | (imm10h << 16) | (1 << 15) | (1 << 14) | (j1 << 13) | (j2 << 11) | (imm10l << 1)
    return _halfwords(inst)


def decode_thumb_bl(pc: int, b: bytes):
    """Decode a Thumb-2 BL (T1) or BLX (T2) at vaddr pc.

    Returns (target, is_blx) or None if b is not a BL/BLX.
    """
    if len(b) < 4:
        return None
    hw1, hw2 = struct.unpack_from("<HH", b)
    if hw1 >> 11 != 0b11110 or hw2 >> 14 != 0b11:
        return None
    blx = (hw2 >> 12) & 1 == 0
    if blx and hw2 & 1 != 0:
        return None  # BLX requires H == 0
    s = (hw1 >> 10) & 1
    j1 = (hw2 >> 13) & 1
    j2 = (hw2 >> 11) & 1
    i1 = ~(j1 ^ s) & 1
    i2 = ~(j2 ^ s) & 1
    imm = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1)
    if imm & (1 << 24):
        imm -= 1 << 25
    if blx:
        target = (((pc + 4) & ~3) + (imm & ~3)) & 0xFFFFFFFF
    else:
        target = (pc + 4 + imm) & 0xFFFFFFFF
    return target, blx
