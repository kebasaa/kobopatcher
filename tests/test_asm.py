"""Ported from kobopatch patchlib/asm_test.go and the decodeThumbBL test."""

import struct

import pytest

from kobopatcher.patchlib.asm import asm_blx, asm_bw, decode_thumb_bl


@pytest.mark.parametrize(
    "pc,target,inst",
    [
        (0x83EDE8, 0x40EF40, 0xD0F7AAB0),
        (0x83EDE8, 0x41A4A0, 0xDBF75AB3),
        (0x83D426, 0x40EF40, 0xD1F78BB5),
        (0x83D426, 0x41A4A0, 0xDDF73BB0),
        (0x02EFE6, 0x0189C8, 0xE9F7EFBC),
    ],
)
def test_asm_bw(pc, target, inst):
    assert asm_bw(pc, target) == struct.pack(">I", inst)


@pytest.mark.parametrize(
    "pc,target,inst",
    [
        (0x83EDE8, 0x40EF40, 0xD0F7AAE0),
        (0x83EDE8, 0x41A4A0, 0xDBF75AE3),
        (0x83D426, 0x40EF40, 0xD1F78CE5),
        (0x83D426, 0x41A4A0, 0xDDF73CE0),
    ],
)
def test_asm_blx(pc, target, inst):
    assert asm_blx(pc, target) == struct.pack(">I", inst)


def test_decode_thumb_bl_roundtrip():
    # BLX round-trips through asm_blx (ARM targets are 4-byte aligned)
    for pc, tgt in [(0xF5BB28, 0x68EB2C), (0x1000, 0x2000), (0x83EDE8, 0x40EF40), (0x0F00002, 0x1000)]:
        got = decode_thumb_bl(pc, asm_blx(pc, tgt))
        assert got == (tgt & ~3, True)
    # B.W is not a BL/BLX
    assert decode_thumb_bl(0x1000, asm_bw(0x1000, 0x2000)) is None
    # BL (T1): bit 12 of the second halfword set
    hw1, hw2 = struct.unpack("<HH", asm_bw(0x1000, 0x1100))
    bl = struct.pack("<HH", hw1, hw2 | 0x4000)
    assert decode_thumb_bl(0x1000, bl) == (0x1100, False)
    assert decode_thumb_bl(0x1000, b"\x00\xbf") is None
    # BLX with H=1 is undefined
    hw1, hw2 = struct.unpack("<HH", asm_blx(0x1000, 0x2000))
    assert decode_thumb_bl(0x1000, struct.pack("<HH", hw1, hw2 | 1)) is None
