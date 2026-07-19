#!/usr/bin/env python3
"""Exact binary patches for the Korean OV02 database search index."""

from __future__ import annotations

from collections.abc import Callable
import struct


CATEGORY_TEXT = "가나다라마바사아자차카타파하"
CATEGORY_TABLE_OFFSET = 0x144B0
INITIAL_STYLE_OFFSET = 0x144D8
RELOCATED_INITIAL_STYLE_OFFSET = 0x0E1B0
INITIAL_STYLE_PACKET = b"\x0e\x02\x02\x00\x00\x00\x0d\x02" + b"\0" * 8


def _words(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}I", *values)


def _i(opcode: int, rs: int, rt: int, immediate: int) -> int:
    return (
        ((opcode & 0x3F) << 26)
        | ((rs & 0x1F) << 21)
        | ((rt & 0x1F) << 16)
        | (immediate & 0xFFFF)
    )


def _r(rs: int, rt: int, rd: int, function: int) -> int:
    return ((rs & 0x1F) << 21) | ((rt & 0x1F) << 16) | ((rd & 0x1F) << 11) | function


def _branch(pc: int, target: int) -> int:
    delta = target - (pc + 4)
    if delta % 4:
        raise ValueError(f"unaligned branch from 0x{pc:x} to 0x{target:x}")
    return delta // 4


def _build_get_fileno() -> bytes:
    """Map a visible sorted row to its real dbheader ID and ordinal."""
    # Registers: a0=row, a1=&ordinal, a2=tbl, t0=valid ordinal,
    # t1=-2 (end), t2=-1 (category separator), t3=-3 (locked row).
    return _words(
        _i(0x0F, 0, 2, 0x00A1),                 # lui v0, 0xa1
        _i(0x23, 2, 6, 0x3940),                 # lw a2, 0x3940(v0)
        _r(0, 0, 8, 0x2D),                      # clear t0
        _i(0x09, 0, 9, -2),                     # li t1, -2
        _i(0x09, 0, 10, -1),                    # li t2, -1
        _i(0x09, 0, 11, -3),                    # li t3, -3
        _i(0x21, 6, 2, 0),                      # loop: lh v0, 0(a2)
        _i(0x04, 2, 9, _branch(0xA0D164, 0xA0D1A4)),
        0,
        _i(0x04, 2, 10, _branch(0xA0D16C, 0xA0D188)),
        0,
        _i(0x04, 4, 0, _branch(0xA0D174, 0xA0D190)),
        _i(0x09, 4, 4, -1),                    # row-- (branch delay)
        _i(0x04, 2, 11, _branch(0xA0D17C, 0xA0D188)),
        0,
        _i(0x09, 8, 8, 1),                     # valid ordinal++
        _i(0x04, 0, 0, _branch(0xA0D188, 0xA0D160)),
        _i(0x09, 6, 6, 2),                     # advance: tbl++ (branch delay)
        _i(0x04, 2, 11, _branch(0xA0D190, 0xA0D1A4)),
        _i(0x09, 8, 8, 1),                     # target: one-based ordinal
        _i(0x2B, 5, 8, 0),                     # sw t0, 0(a1)
        _r(31, 0, 0, 0x08),                    # jr ra
        0,
        _i(0x09, 0, 2, -1),                    # fail: return -1
        _r(31, 0, 0, 0x08),
        _i(0x2B, 5, 2, 0),                     # sw v0, 0(a1)
    )


ORIGINAL_GET_FILENO = bytes.fromhex(
    "A100023CFEFF03243839468C2D4000000000C284140043100000C794FFFF0B24"
    "FDFF0A24FEFF092400140700031C02000A006B500200C62407006A10D8DC6224"
    "05004414010008250000A8AC0800E0032D106000000000000200C6240000C284"
    "F1FF49140000C794FFFF0324FFFF02240800E0030000A3AC"
)
PATCHED_GET_FILENO = _build_get_fileno() + INITIAL_STYLE_PACKET

# Offsets are relative to standalone OV02.OVL. Add 0x2d7000 for the copy
# embedded in slps_290.02.
OV02_PATCHES = (
    (
        "initial category style pointer",
        0x0E60C,
        b"\xd8\x34\xe7\x24",  # addiu a3, a3, 0x34d8
        b"\xb0\xd1\xe7\x24",  # addiu a3, a3, -0x2e50 (0xa0d1b0)
    ),
    (
        "category X coordinate",
        0x0E674,
        b"\x34\xf8\x84\x24",  # addiu a0, a0, -0x7cc
        b"\xf4\xf7\x84\x24",  # addiu a0, a0, -0x80c
    ),
    (
        "category render count",
        0x0E6E8,
        b"\x0a\x00\x42\x2a",  # slti v0, s2, 10
        b"\x0e\x00\x42\x2a",  # slti v0, s2, 14
    ),
    (
        "last category index",
        0x0EB28,
        b"\x09\x00\x15\x24",  # li s5, 9
        b"\x0d\x00\x15\x24",  # li s5, 13
    ),
    (
        "category input bound",
        0x0EB48,
        b"\x0a\x00\x02\x2a",  # slti v0, s0, 10
        b"\x0e\x00\x02\x2a",  # slti v0, s0, 14
    ),
    (
        "exit highlight sentinel",
        0x0EAC8,
        b"\x0b\x00\x02\x24",  # li v0, 11
        b"\x0f\x00\x02\x24",  # li v0, 15
    ),
    (
        "search label X coordinate",
        0x0BF0C,
        b"\x5c\x00\x03\x24",  # li v1, 92
        b"\x1c\x00\x03\x24",  # li v1, 28
    ),
    (
        "relocated tbl store",
        0x0E490,
        b"\x38\x39\x70\xac",  # sw s0, 0x3938(v1)
        b"\x40\x39\x70\xac",  # sw s0, 0x3940(v1)
    ),
    (
        "get_indexno row counter init",
        0x0E1EC,
        b"\x00\x00\x00\x00",  # nop
        b"\x2d\x58\x00\x00",  # clear t3
    ),
    (
        "get_indexno return category",
        0x0E20C,
        b"\xd8\xdc\x43\x24",  # addiu v1, v0, -9000
        b"\x2d\x10\xc0\x00",  # daddu v0, a2, zero
    ),
    (
        "get_indexno compare row",
        0x0E210,
        b"\x06\x00\x68\x10",  # beq v1, t0, return
        b"\x06\x00\x68\x11",  # beq t3, t0, return
    ),
    (
        "get_indexno advance row",
        0x0E214,
        b"\x2d\x10\xc0\x00",  # daddu v0, a2, zero
        b"\x01\x00\x6b\x25",  # addiu t3, t3, 1
    ),
)


def _replace_exact(
    data: bytearray, offset: int, expected: bytes, replacement: bytes, label: str
) -> None:
    actual = bytes(data[offset : offset + len(expected)])
    if actual != expected:
        raise ValueError(
            f"unexpected {label} at 0x{offset:08x}: "
            f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
        )
    data[offset : offset + len(replacement)] = replacement


def apply_database_search_patch(
    data: bytearray,
    encode_display: Callable[[str], bytes],
    *,
    ov02_offset: int,
    memsz_offset: int,
    expected_memsz: int,
) -> list[str]:
    """Patch one OV02 load image and return a concise change log."""
    encoded_categories = bytearray()
    for character in CATEGORY_TEXT:
        encoded = encode_display(character)
        if len(encoded) != 2:
            raise ValueError(
                f"category {character!r} must encode to exactly 2 bytes, got {len(encoded)}"
            )
        encoded_categories.extend(encoded)
        encoded_categories.extend(b"\0\0")

    table_offset = ov02_offset + CATEGORY_TABLE_OFFSET
    original_table_and_style = bytes.fromhex(
        "A4A20000A4AB0000A4B50000A4BF0000A4CA0000"
        "A4CF0000A4DE0000A4E40000A4E90000A4EF0000"
        "0E02020000000D020000000000000000"
    )
    _replace_exact(
        data,
        table_offset,
        original_table_and_style,
        bytes(encoded_categories),
        "original category table and overlapping style",
    )

    relocated_style_offset = ov02_offset + RELOCATED_INITIAL_STYLE_OFFSET

    get_fileno_offset = ov02_offset + 0x0E148
    _replace_exact(
        data,
        get_fileno_offset,
        ORIGINAL_GET_FILENO,
        PATCHED_GET_FILENO,
        "get_fileno function",
    )

    changes = [
        f"in-place category table 0x{table_offset:08x}: {CATEGORY_TEXT}",
        f"initial category style 0x{relocated_style_offset:08x}: embedded after get_fileno",
        f"get_fileno 0x{get_fileno_offset:08x}: row-to-ID lookup replaced",
    ]
    for label, relative_offset, expected, replacement in OV02_PATCHES:
        offset = ov02_offset + relative_offset
        _replace_exact(data, offset, expected, replacement, label)
        changes.append(
            f"{label} 0x{offset:08x}: {expected.hex(' ')} -> {replacement.hex(' ')}"
        )

    replacement_memsz = expected_memsz + 8
    _replace_exact(
        data,
        memsz_offset,
        expected_memsz.to_bytes(4, "little"),
        replacement_memsz.to_bytes(4, "little"),
        "OV02 PT_LOAD p_memsz",
    )
    changes.append(
        f"OV02 PT_LOAD p_memsz 0x{memsz_offset:08x}: "
        f"0x{expected_memsz:x} -> 0x{replacement_memsz:x}"
    )
    return changes
