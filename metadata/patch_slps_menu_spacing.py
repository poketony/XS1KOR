#!/usr/bin/env python3
"""Build a translated SLPS ELF with Korean UI and database-search fixes.

The source ELF and translation text are read-only. The output path must not
already exist, preventing accidental replacement of either an original or a
previous build.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import struct

import euc_scan
import slps_strings
from ov02_database_search import apply_database_search_patch


LENGTH_TABLE_PATCHES = {
    # table offset: (expected original bytes, translation string offsets)
    # MenuFilePas message order is Save, Load, /Slot 1, /Slot 2, /HDD.
    0x2D6070: (b"\x03\x03\x06\x06\x06", (0x2D6068, 0x2D6060, None, None, None)),
    0x2D62E8: (b"\x06\x03\x03\x04\x04", (0x2C0CE0, None, None, None, None)),
    # AgwsPasMain positions each following breadcrumb from the visible length
    # of the preceding label.  Index zero is the control-built A.G.W.S. title.
    0x2C2410: (
        b"\x05\x03\x03\x04\x04\x03\x03\x03\x03\x04\x04\x02\x07",
        (
            None,
            0x2D6438,
            0x2D6430,
            0x2C23D8,
            0x2C23C8,
            0x2D6428,
            0x2D6420,
            0x2D6418,
            0x2D6410,
            0x2C23B8,
            0x2C23A8,
            0x2D6408,
            0x2C2398,
        ),
    ),
    0x2D6750: (b"\x04\x03\x04\x07", (0x2C3540, 0x2D6748, 0x2C3530, 0x2C3520)),
    # Embedded OV02 copy. The standalone OV02 used at runtime has the same table.
    0x2EA2D8: (b"\x03\x05\x03\x03\x05", (0x2EA2D0, 0x2EA2C0, 0x2EA2B8, 0x2EA2B0, 0x2EA2A0)),
}

# MIPS ADDIU immediate fields used for the two Skill breadcrumb variants.
SKILL_X_PATCHES = {
    0x0B35FC: b"\x4c\x00",
    0x0B3614: b"\x4c\x00",
}

# Message layout uses several independent line-width accumulators. The original
# code advances half-width bytes by 8 or 10 in those paths, while
# xglFontGetStringWidth() and actual rendering advance them by 16. This affects
# both centered cutscene subtitles and the field-dialogue continuation cursor.
EVT_HALF_WIDTH_PATCHES = {
    # MSG_queuePop:  addiu v0, v0, 8  -> addiu v0, v0, 16
    0x058DD0: (b"\x08\x00\x42\x24", b"\x10\x00\x42\x24"),
    # MSG_copyln:    addiu s3, s3, 8  -> addiu s3, s3, 16
    0x059158: (b"\x08\x00\x73\x26", b"\x10\x00\x73\x26"),
    # eMessageDrawType01 cursor X accumulator:
    # addiu s5, s5, 10 -> addiu s5, s5, 16
    0x078A0C: (b"\x0a\x00\xb5\x26", b"\x10\x00\xb5\x26"),
}

# Expanded Korean file-menu strings do not all fit their original slots.  The
# two longest strings occupy the 0x48-byte alignment gap between .sdata and
# .sbss; their vacated slots are reused by other strings.  The compound
# 0x2bf100 stream may then extend into the vacated 0x2bf160 slot.  No .sbss
# storage is consumed.
LOAD_TEXT_RELOCATION_OFFSETS = frozenset({
    0x002BF100,
    0x002BF134,
    0x002BF160,
    0x002BF388,
    0x002BF528,
    0x002BF7B8,
    0x002D6060,
})
LOAD_TEXT_RELOCATIONS = {
    # translation offset: (destination file offset, destination span)
    0x002BF528: (0x002D7938, 0x24),
    0x002BF7B8: (0x002D795C, 0x24),
    0x002BF388: (0x002BF528, 0x20),
    0x002D6060: (0x002BF388, 0x18),
    0x002BF160: (0x002BF7B8, 0x20),
}
LOAD_TEXT_SOURCE_LENGTHS = {
    0x002BF160: 0x1A,
    0x002BF388: 0x16,
    0x002BF528: 0x1F,
    0x002BF7B8: 0x1F,
    0x002D6060: 0x06,
}
LOAD_TEXT_POINTER_PATCHES = {
    0x002BF4CC: (0x004BE388, 0x004BE528),
    0x002BF574: (0x004BE528, 0x004D6938),
    0x002BF894: (0x004BE7B8, 0x004D695C),
    0x0016952C: (0x004D5060, 0x004BE388),
    0x002BF0A8: (0x004D5060, 0x004BE388),
    0x002BF218: (0x004BE160, 0x004BE7B8),
}
LOAD_TEXT_COMPOUND_PARTS = (0x002BF100, 0x002BF134)
LOAD_TEXT_COMPOUND_BRIDGE = (0x002BF132, 0x002BF134)
LOAD_TEXT_COMPOUND_DESTINATION = (0x002BF100, 0x80)
LOAD_TEXT_COMPOUND_SOURCE_LENGTH = 0x5A
LOAD_TEXT_SDATA_GAP = (0x002D7938, 0x002D7980)
LOAD_TEXT_PHDR_FILESZ_OFFSET = 0x84
LOAD_TEXT_PHDR_FILESZ = (0x001A2C38, 0x001A2C80)
LOAD_TEXT_SDATA_SH_SIZE_OFFSET = 0x002FC9B4
LOAD_TEXT_SDATA_SH_SIZE = (0x000034B8, 0x00003500)

# The fourteen AGWS equipment labels occupy one contiguous 0x70-byte block.
# Several Korean labels exceed their original eight-byte slots.  The four
# labels without a leading slash are exact suffixes of their breadcrumb forms,
# so they can safely share the same NUL-terminated storage.  The resulting
# strings fit on two-byte boundaries.  These are the only runtime pointer-table
# entries that refer to the individual labels.
AGWS_TEXT_OFFSETS = tuple(range(0x002D6400, 0x002D6470, 8))
AGWS_TEXT_REGION = (0x002D6400, 0x002D6470)
AGWS_TEXT_POINTERS = {
    0x0016983C: 0x002D6438,
    0x00169840: 0x002D6430,
    0x0016984C: 0x002D6428,
    0x00169850: 0x002D6420,
    0x00169854: 0x002D6418,
    0x00169858: 0x002D6410,
    0x00169864: 0x002D6408,
    0x0016986C: 0x002D6400,
    0x00169870: 0x002D6430,
    0x00169898: 0x002D6468,
    0x0016989C: 0x002D6460,
    0x001698A0: 0x002D6458,
    0x001698A4: 0x002D6450,
    0x001698A8: 0x002D6448,
    0x001698AC: 0x002D6440,
}

TEXT_VA_DELTA = 0x1FF000
MENU_SPACE_CAVE_VA = 0x002805C0
MENU_SPACE_CAVE_SIZE = 0x124
MENU_SPACE_CAVE_SHA256 = "9263b6f908b343b00eb4c0995591591608f80772b91646564421c295567b65b3"
MENU_SPACE_SCOPE_FLAG_VA = 0x004D6C94
MENU_SPACE_SBSS_SIZE_OFFSET = 0x002FC9DC
MENU_SPACE_SBSS_SIZE = (0x314, 0x318)
MENU_SPACE_SBSS_VA = 0x004D6980
MENU_SPACE_BSS_VA = 0x004D6D00
MENU_SPACE_NORMAL_HEADER_VA = 0x004C22A8


def _mips_i(opcode: int, rs: int, rt: int, immediate: int) -> int:
    return (
        ((opcode & 0x3F) << 26)
        | ((rs & 0x1F) << 21)
        | ((rt & 0x1F) << 16)
        | (immediate & 0xFFFF)
    )


def _mips_r(rs: int, rt: int, rd: int, function: int) -> int:
    return ((rs & 0x1F) << 21) | ((rt & 0x1F) << 16) | ((rd & 0x1F) << 11) | function


def _mips_shift(rt: int, rd: int, amount: int, function: int) -> int:
    return (
        ((rt & 0x1F) << 16)
        | ((rd & 0x1F) << 11)
        | ((amount & 0x1F) << 6)
        | function
    )


def _mips_j(opcode: int, target: int) -> int:
    if target & 3:
        raise ValueError(f"unaligned MIPS jump target 0x{target:x}")
    return ((opcode & 0x3F) << 26) | ((target >> 2) & 0x03FFFFFF)


def _build_menu_space_patch() -> tuple[bytes, dict[str, int]]:
    """Build scoped 8-pixel spaces without changing the original 0x20 bytes."""
    words: list[int | tuple[str, int, int, str | int]] = []
    labels: dict[str, int] = {}

    def emit(word: int) -> None:
        words.append(word)

    def label(name: str) -> None:
        labels[name] = len(words)

    def branch(opcode: int, rs: int, rt: int, target: str | int) -> None:
        words.append(("branch", opcode, (rs << 5) | rt, target))

    def jump(opcode: int, target: str | int) -> None:
        words.append(("jump", opcode, 0, target))

    # Item and shop callers enter WindowSPMain here. Keep the scope in a
    # dedicated zero-initialized .sbss word so nested renderer calls cannot
    # clobber it as they did when this used s7.
    label("scope_entry")
    emit(_mips_i(0x09, 29, 29, -0x10))             # addiu sp, sp, -0x10
    emit(_mips_i(0x3F, 29, 31, 0))                 # sd ra, 0(sp)
    emit(_mips_i(0x0F, 0, 8, MENU_SPACE_SCOPE_FLAG_VA >> 16))
    emit(_mips_i(0x09, 0, 9, 1))                   # li t1, 1
    emit(_mips_i(0x2B, 8, 9, MENU_SPACE_SCOPE_FLAG_VA & 0xFFFF))
    jump(0x03, 0x002800F0)                         # jal WindowSPMain
    emit(0)
    emit(_mips_i(0x0F, 0, 8, MENU_SPACE_SCOPE_FLAG_VA >> 16))
    emit(_mips_i(0x2B, 8, 0, MENU_SPACE_SCOPE_FLAG_VA & 0xFFFF))
    emit(_mips_i(0x37, 29, 31, 0))                 # ld ra, 0(sp)
    emit(_mips_r(31, 0, 0, 0x08))                  # jr ra
    emit(_mips_i(0x09, 29, 29, 0x10))              # addiu sp, sp, 0x10

    # The battle-result path does not enter through WindowSPMain. Give its
    # eMessage call the same temporary memory scope.
    label("force_entry")
    emit(_mips_i(0x09, 29, 29, -0x10))             # addiu sp, sp, -0x10
    emit(_mips_i(0x3F, 29, 31, 0))                 # sd ra, 0(sp)
    emit(_mips_i(0x0F, 0, 8, MENU_SPACE_SCOPE_FLAG_VA >> 16))
    emit(_mips_i(0x09, 0, 9, 1))                   # li t1, 1
    emit(_mips_i(0x2B, 8, 9, MENU_SPACE_SCOPE_FLAG_VA & 0xFFFF))
    jump(0x03, 0x00277BD8)                         # jal eMessageMain
    emit(0)
    emit(_mips_i(0x0F, 0, 8, MENU_SPACE_SCOPE_FLAG_VA >> 16))
    emit(_mips_i(0x2B, 8, 0, MENU_SPACE_SCOPE_FLAG_VA & 0xFFFF))
    emit(_mips_i(0x37, 29, 31, 0))                 # ld ra, 0(sp)
    emit(_mips_r(31, 0, 0, 0x08))                  # jr ra
    emit(_mips_i(0x09, 29, 29, 0x10))              # addiu sp, sp, 0x10

    # Convert ASCII spaces only while one of the scoped callers is active. The
    # source message, eMessage flags and deferred queue length stay intact.
    label("copy_hook")
    emit(_mips_i(0x0F, 0, 8, MENU_SPACE_SCOPE_FLAG_VA >> 16))
    emit(_mips_i(0x23, 8, 8, MENU_SPACE_SCOPE_FLAG_VA & 0xFFFF))
    branch(0x04, 8, 0, "copy_store")               # beq t0, zero, copy_store
    emit(0)
    # The A.G.W.S. parts list uses literal spaces to position its Price header.
    emit(_mips_i(0x23, 19, 8, 0x18))               # lw t0, 0x18(s3)
    emit(_mips_i(0x0F, 0, 9, MENU_SPACE_NORMAL_HEADER_VA >> 16))
    emit(_mips_i(0x0D, 9, 9, MENU_SPACE_NORMAL_HEADER_VA & 0xFFFF))
    branch(0x04, 8, 9, "copy_store")               # beq t0, t1, copy_store
    emit(0)
    emit(_mips_i(0x09, 0, 2, 0x7F))                # li v0, 0x7f
    label("copy_store")
    emit(_mips_i(0x28, 18, 2, -1))                 # sb v0, -1(s2)
    branch(0x05, 3, 4, 0x00277A00)                 # bne v1, a0, space loop
    emit(0)
    jump(0x02, 0x00277A1C)                         # j original loop exit
    emit(0)

    # Flush the sentinel with the exact normal-space glyph coordinates. The
    # previous 0x0000 coordinate caused the visible dot-like replacement.
    label("glyph_hook")
    emit(_mips_i(0x09, 0, 8, 0x7F))                # li t0, 0x7f
    branch(0x05, 18, 8, "glyph_tail")              # bne s2, t0, glyph_tail
    emit(0)
    emit(_mips_i(0x09, 0, 4, 0x1000))              # li a0, 0x1000
    emit(_mips_i(0x09, 0, 5, 0x2F00))              # li a1, 0x2f00
    emit(_mips_i(0x09, 0, 6, 0x0408))              # li a2, 0x0408
    label("glyph_tail")
    jump(0x02, 0x002194B0)                         # j xglFontFlushSub
    emit(0)

    # Negative-width eBattleWinOpen2 callers request centered text. Its normal
    # counter advances two source bytes for every non-newline character, so a
    # one-byte ASCII space skips part of the next Korean glyph. Current gameplay
    # has one such caller (OV01 STATUS); use the renderer's actual pixel width.
    label("status_center_hook")
    emit(_mips_i(0x09, 29, 29, -0x10))             # addiu sp, sp, -0x10
    emit(_mips_i(0x3F, 29, 31, 0))                 # sd ra, 0(sp)
    emit(_mips_i(0x3F, 29, 9, 8))                  # sd t1, 8(sp)
    emit(_mips_i(0x23, 17, 4, 0x10))               # lw a0, 0x10(s1)
    jump(0x03, 0x0021AE20)                         # jal xglFontGetStringWidth
    emit(0)
    emit(_mips_i(0x25, 17, 3, 0x0C))               # lhu v1, 0x0c(s1)
    emit(_mips_i(0x09, 3, 3, -6))                  # addiu v1, v1, -6
    emit(_mips_r(3, 2, 3, 0x23))                   # subu v1, v1, v0
    emit(_mips_shift(3, 3, 1, 0x03))               # sra v1, v1, 1
    emit(_mips_i(0x37, 29, 9, 8))                  # ld t1, 8(sp)
    emit(_mips_i(0x37, 29, 31, 0))                 # ld ra, 0(sp)
    emit(_mips_r(31, 0, 0, 0x08))                  # jr ra
    emit(_mips_i(0x09, 29, 29, 0x10))              # addiu sp, sp, 0x10

    resolved: list[int] = []
    for index, word in enumerate(words):
        if isinstance(word, int):
            resolved.append(word)
            continue
        kind, opcode, registers, target = word
        target_va = MENU_SPACE_CAVE_VA + labels[target] * 4 if isinstance(target, str) else target
        source_va = MENU_SPACE_CAVE_VA + index * 4
        if kind == "branch":
            delta_bytes = target_va - (source_va + 4)
            if delta_bytes & 3:
                raise AssertionError(f"unaligned branch target 0x{target_va:08x}")
            delta = delta_bytes // 4
            if not -0x8000 <= delta <= 0x7FFF:
                raise AssertionError(f"branch target out of range: 0x{target_va:08x}")
            rs, rt = registers >> 5, registers & 0x1F
            resolved.append(_mips_i(opcode, rs, rt, delta))
        elif kind == "jump":
            resolved.append(_mips_j(opcode, target_va))
        else:
            raise AssertionError(kind)

    code = struct.pack(f"<{len(resolved)}I", *resolved)
    if len(code) > MENU_SPACE_CAVE_SIZE:
        raise AssertionError(
            f"menu-space patch is {len(code)} bytes, capacity is {MENU_SPACE_CAVE_SIZE}"
        )
    addresses = {name: MENU_SPACE_CAVE_VA + index * 4 for name, index in labels.items()}
    return code.ljust(MENU_SPACE_CAVE_SIZE, b"\0"), addresses


def _assert_menu_space_cave_unreferenced(data: bytearray) -> None:
    cave_end = MENU_SPACE_CAVE_VA + MENU_SPACE_CAVE_SIZE
    cave_offset = MENU_SPACE_CAVE_VA - TEXT_VA_DELTA
    text_start = 0x1000
    text_end = 0x134CB0
    branch_opcodes = {0x01, 0x04, 0x05, 0x06, 0x07, 0x14, 0x15, 0x16, 0x17}
    for offset in range(text_start, text_end, 4):
        if cave_offset <= offset < cave_offset + MENU_SPACE_CAVE_SIZE:
            continue
        word = struct.unpack_from("<I", data, offset)[0]
        opcode = word >> 26
        pc = offset + TEXT_VA_DELTA
        target = None
        if opcode in (0x02, 0x03):
            target = ((pc + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)
        elif opcode in branch_opcodes:
            immediate = word & 0xFFFF
            if immediate & 0x8000:
                immediate -= 0x10000
            target = pc + 4 + immediate * 4
        if target is not None and MENU_SPACE_CAVE_VA <= target < cave_end:
            raise ValueError(
                f"menu-space cave has an external code reference from VA 0x{pc:08x}"
            )

    for start, end in ((0x134D00, 0x2D7938), (0x2D8000, 0x2EB93C)):
        for offset in range(start, end - 3, 4):
            pointer = struct.unpack_from("<I", data, offset)[0]
            if MENU_SPACE_CAVE_VA <= pointer < cave_end:
                raise ValueError(
                    f"menu-space cave has a loaded pointer reference at 0x{offset:08x}"
                )


def _apply_scoped_menu_half_spaces(data: bytearray) -> list[str]:
    cave_offset = MENU_SPACE_CAVE_VA - TEXT_VA_DELTA
    actual_hash = hashlib.sha256(
        data[cave_offset : cave_offset + MENU_SPACE_CAVE_SIZE]
    ).hexdigest()
    if actual_hash != MENU_SPACE_CAVE_SHA256:
        raise ValueError(
            f"unexpected menu-space cave at 0x{cave_offset:08x}: "
            f"expected SHA-256 {MENU_SPACE_CAVE_SHA256}, found {actual_hash}"
        )
    _assert_menu_space_cave_unreferenced(data)

    code, addresses = _build_menu_space_patch()
    data[cave_offset : cave_offset + MENU_SPACE_CAVE_SIZE] = code

    if MENU_SPACE_SCOPE_FLAG_VA != MENU_SPACE_SBSS_VA + MENU_SPACE_SBSS_SIZE[0]:
        raise AssertionError("menu-space scope flag is not at the original .sbss end")
    if MENU_SPACE_SBSS_VA + MENU_SPACE_SBSS_SIZE[1] > MENU_SPACE_BSS_VA:
        raise AssertionError("expanded .sbss overlaps .bss")
    current_sbss_size = struct.unpack_from("<I", data, MENU_SPACE_SBSS_SIZE_OFFSET)[0]
    if current_sbss_size != MENU_SPACE_SBSS_SIZE[0]:
        raise ValueError(
            f"unexpected .sbss size 0x{current_sbss_size:x}; "
            f"expected 0x{MENU_SPACE_SBSS_SIZE[0]:x}"
        )
    struct.pack_into(
        "<I", data, MENU_SPACE_SBSS_SIZE_OFFSET, MENU_SPACE_SBSS_SIZE[1]
    )

    patches = (
        (
            "scoped eMessage ASCII-space queue marker",
            0x00277A10,
            bytes.fromhex("00 00 42 a2 fa ff 64 14"),
            struct.pack(
                "<2I",
                _mips_j(0x02, addresses["copy_hook"]),
                _mips_i(0x09, 18, 18, 1),
            ),
        ),
        (
            "xgl scoped ASCII-space glyph",
            0x0021A458,
            bytes.fromhex("2c 65 08 0c"),
            struct.pack("<I", _mips_j(0x03, addresses["glyph_hook"])),
        ),
        (
            "OV01 STATUS actual-pixel centering",
            0x0027DE98,
            bytes.fromhex("2a 10 e8 00 fa ff 63 24 0b 38 02 01 42 18 03 00"),
            struct.pack(
                "<4I",
                _mips_j(0x03, addresses["status_center_hook"]),
                0,
                _mips_i(0x04, 0, 0, 9),
                _mips_i(0x2B, 9, 3, 4),
            ),
        ),
        (
            "item list WindowSP initialization",
            0x00287D9C,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "item list WindowSP draw",
            0x00287F0C,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "shop list WindowSP draw",
            0x002A01C4,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "ether point list WindowSP 1",
            0x002A8670,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "ether point list WindowSP 2",
            0x002A8718,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "ether point list WindowSP 3",
            0x002A87D4,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "ether point list WindowSP 4",
            0x002A8878,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "ether point list WindowSP 5",
            0x002A892C,
            struct.pack("<I", _mips_j(0x03, 0x002800F0)),
            struct.pack("<I", _mips_j(0x03, addresses["scope_entry"])),
        ),
        (
            "battle-result item eMessage",
            0x0029C0B8,
            bytes.fromhex("f6 de 09 0c"),
            struct.pack("<I", _mips_j(0x03, addresses["force_entry"])),
        ),
    )
    changes = []
    for label, va, expected, replacement in patches:
        offset = va - TEXT_VA_DELTA
        actual = bytes(data[offset : offset + len(expected)])
        if actual != expected:
            raise ValueError(
                f"unexpected {label} code at VA 0x{va:08x}: "
                f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
            )
        data[offset : offset + len(replacement)] = replacement
        changes.append(f"{label}: VA 0x{va:08x}")

    changes.append(
        "item/shop/ether-point/battle-result ASCII spaces: .sbss-scoped exact blank glyph at 8px, eMessage flags untouched"
    )
    changes.append(
        f"menu-space scope flag: VA 0x{MENU_SPACE_SCOPE_FLAG_VA:08x}, "
        f".sbss 0x{MENU_SPACE_SBSS_SIZE[0]:x} -> 0x{MENU_SPACE_SBSS_SIZE[1]:x}"
    )
    return changes


def parse_translations(path: Path) -> dict[int, str]:
    translations = {}
    with path.open(encoding="utf-8-sig") as stream:
        for lineno, raw_line in enumerate(stream, 1):
            line = raw_line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                offset_text, _info, text = parts
            elif len(parts) == 2:
                offset_text, text = parts
            else:
                continue
            try:
                offset = int(offset_text.strip(), 16)
            except ValueError as exc:
                raise ValueError(f"line {lineno}: invalid offset {offset_text!r}") from exc
            translations[offset] = text
    return translations


def visible_cells(text: str) -> int:
    """Count fixed-width cells while ignoring escaped control-byte syntax."""
    count = 0
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            if text[index + 1] in "nr":
                index += 2
                continue
            if text[index + 1] in "xX" and index + 3 < len(text):
                try:
                    int(text[index + 2 : index + 4], 16)
                except ValueError:
                    pass
                else:
                    index += 4
                    continue
        count += 1
        index += 1
    return count


def apply_translations(
    data: bytearray, source: Path, edits: dict[int, euc_scan.TranslationEdit]
) -> tuple[int, int, int, int, int]:
    replace_table = slps_strings.load_replace_table(str(source))
    regular_edits = {
        offset: edit
        for offset, edit in edits.items()
        if offset not in LOAD_TEXT_RELOCATION_OFFSETS
        and offset not in AGWS_TEXT_OFFSETS
    }
    stats = euc_scan.apply_grouped_translations(
        data,
        [(
            slps_strings.SCAN_START,
            min(slps_strings.SCAN_END, len(data)),
            False,
        )],
        regular_edits,
        replace_table,
        label="SLPS",
        skip_polluted=False,
    )
    return (
        stats.patched_groups,
        stats.patched_records,
        stats.missing + stats.invalid,
        stats.overflow,
        stats.control_warnings,
    )


def _replace_exact_bytes(
    data: bytearray,
    offset: int,
    expected: bytes,
    replacement: bytes,
    label: str,
) -> None:
    actual = bytes(data[offset : offset + len(expected)])
    if actual != expected:
        raise ValueError(
            f"unexpected {label} at 0x{offset:08x}: "
            f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
        )
    data[offset : offset + len(replacement)] = replacement


def apply_load_text_relocations(
    data: bytearray,
    original: bytes,
    translations: dict[int, str],
    encode_display,
) -> list[str]:
    """Relocate the four expanded Korean Load strings without touching .sbss."""
    gap_start, gap_end = LOAD_TEXT_SDATA_GAP
    if bytes(original[gap_start:gap_end]) != b"\0" * (gap_end - gap_start):
        raise ValueError("SLPS .sdata/.sbss alignment gap is not empty")
    if bytes(data[gap_start:gap_end]) != bytes(original[gap_start:gap_end]):
        raise ValueError("SLPS .sdata/.sbss alignment gap was modified before relocation")

    compound_parts = []
    for source_offset in LOAD_TEXT_COMPOUND_PARTS:
        text = translations.get(source_offset)
        if text is None:
            raise ValueError(
                f"missing compound Load translation for 0x{source_offset:08x}"
            )
        compound_parts.append(encode_display(text))
    bridge_start, bridge_end = LOAD_TEXT_COMPOUND_BRIDGE
    compound = compound_parts[0] + original[bridge_start:bridge_end] + compound_parts[1]
    compound_destination, compound_span = LOAD_TEXT_COMPOUND_DESTINATION
    if len(compound) >= compound_span:
        raise ValueError(
            f"compound Load string needs {len(compound)}B plus terminator, "
            f"destination span is {compound_span}B"
        )
    if bytes(data[compound_destination:compound_destination + compound_span]) != (
        original[compound_destination:compound_destination + compound_span]
    ):
        raise ValueError("compound Load destination was modified before relocation")
    original_compound = original[
        compound_destination:compound_destination + LOAD_TEXT_COMPOUND_SOURCE_LENGTH
    ]
    original_parsed = euc_scan.parse_control_aware_string(original_compound + b"\0", 0)
    rebuilt_parsed = euc_scan.parse_control_aware_string(compound + b"\0", 0)
    if (
        original_parsed is None
        or rebuilt_parsed is None
        or rebuilt_parsed.terminator != len(compound)
        or original_parsed.control_packets != rebuilt_parsed.control_packets
    ):
        raise ValueError("compound Load control stream is invalid or changed")

    encoded_strings = {}
    for source_offset, (destination, span) in LOAD_TEXT_RELOCATIONS.items():
        text = translations.get(source_offset)
        if text is None:
            raise ValueError(
                f"missing relocated Load translation for 0x{source_offset:08x}"
            )
        encoded = encode_display(text)
        if len(encoded) >= span:
            raise ValueError(
                f"relocated Load string 0x{source_offset:08x} needs "
                f"{len(encoded)}B plus terminator, destination span is {span}B"
            )

        source_length = LOAD_TEXT_SOURCE_LENGTHS[source_offset]
        original_raw = original[source_offset : source_offset + source_length]
        original_parsed = euc_scan.parse_control_aware_string(original_raw + b"\0", 0)
        rebuilt_parsed = euc_scan.parse_control_aware_string(encoded + b"\0", 0)
        if (
            original_parsed is None
            or rebuilt_parsed is None
            or rebuilt_parsed.terminator != len(encoded)
        ):
            raise ValueError(
                f"invalid relocated Load string at 0x{source_offset:08x}"
            )
        if original_parsed.control_packets != rebuilt_parsed.control_packets:
            raise ValueError(
                f"relocated Load control packets differ at 0x{source_offset:08x}: "
                f"{euc_scan.format_control_packets(original_parsed.control_packets)} -> "
                f"{euc_scan.format_control_packets(rebuilt_parsed.control_packets)}"
            )

        expected_destination = original[destination : destination + span]
        if bytes(data[destination : destination + span]) != expected_destination:
            raise ValueError(
                f"relocated Load destination 0x{destination:08x} was modified"
            )
        encoded_strings[source_offset] = encoded

    _replace_exact_bytes(
        data,
        LOAD_TEXT_PHDR_FILESZ_OFFSET,
        LOAD_TEXT_PHDR_FILESZ[0].to_bytes(4, "little"),
        LOAD_TEXT_PHDR_FILESZ[1].to_bytes(4, "little"),
        "main data PT_LOAD p_filesz",
    )
    _replace_exact_bytes(
        data,
        LOAD_TEXT_SDATA_SH_SIZE_OFFSET,
        LOAD_TEXT_SDATA_SH_SIZE[0].to_bytes(4, "little"),
        LOAD_TEXT_SDATA_SH_SIZE[1].to_bytes(4, "little"),
        ".sdata section size",
    )
    for pointer_offset, (expected, replacement) in LOAD_TEXT_POINTER_PATCHES.items():
        _replace_exact_bytes(
            data,
            pointer_offset,
            expected.to_bytes(4, "little"),
            replacement.to_bytes(4, "little"),
            "Load text pointer",
        )

    changes = []
    for source_offset, (destination, span) in LOAD_TEXT_RELOCATIONS.items():
        encoded = encoded_strings[source_offset]
        data[destination : destination + span] = b"\0" * span
        data[destination : destination + len(encoded)] = encoded
        changes.append(
            f"0x{source_offset:08x} -> 0x{destination:08x} "
            f"({len(encoded)}B/{span - 1}B)"
        )
    data[compound_destination:compound_destination + compound_span] = b"\0" * compound_span
    data[compound_destination:compound_destination + len(compound)] = compound
    changes.append(
        f"0x002bf100+0x002bf134 -> 0x{compound_destination:08x} "
        f"({len(compound)}B/{compound_span - 1}B)"
    )
    changes.append(
        "main data PT_LOAD/.sdata extended through 0x002d797f; .sbss at 0x004d6980 unchanged"
    )
    return changes


def apply_agws_text_relocation(
    data: bytearray,
    original: bytes,
    translations: dict[int, str],
    encode_display,
) -> list[str]:
    """Pack expanded AGWS labels inside their original aggregate region."""
    region_start, region_end = AGWS_TEXT_REGION
    if bytes(data[region_start:region_end]) != original[region_start:region_end]:
        raise ValueError("AGWS text region was modified before relocation")

    encoded_strings: dict[int, bytes] = {}
    destinations: dict[int, int] = {}
    stored_offsets: list[int] = []
    cursor = region_start
    for source_offset in AGWS_TEXT_OFFSETS:
        text = translations.get(source_offset)
        if text is None:
            raise ValueError(f"missing AGWS translation for 0x{source_offset:08x}")
        encoded = encode_display(text)
        if not encoded or b"\0" in encoded:
            raise ValueError(f"invalid AGWS text at 0x{source_offset:08x}")
        parsed = euc_scan.parse_control_aware_string(encoded + b"\0", 0)
        if parsed is None or parsed.terminator != len(encoded):
            raise ValueError(f"malformed AGWS text at 0x{source_offset:08x}")
        if parsed.control_packets:
            raise ValueError(f"unexpected control code in AGWS text 0x{source_offset:08x}")

        shared_with = next(
            (
                stored_offset
                for stored_offset in stored_offsets
                if encoded_strings[stored_offset].endswith(encoded)
            ),
            None,
        )
        if shared_with is not None:
            prefix_length = len(encoded_strings[shared_with]) - len(encoded)
            destination = destinations[shared_with] + prefix_length
            if destination & 1:
                raise ValueError(
                    f"unaligned shared AGWS text destination 0x{destination:08x}"
                )
            destinations[source_offset] = destination
            encoded_strings[source_offset] = encoded
            continue

        cursor = (cursor + 1) & ~1
        destination_end = cursor + len(encoded) + 1
        if destination_end > region_end:
            raise ValueError(
                f"AGWS labels need 0x{destination_end - region_start:x} bytes; "
                f"region capacity is 0x{region_end - region_start:x}"
            )
        destinations[source_offset] = cursor
        encoded_strings[source_offset] = encoded
        stored_offsets.append(source_offset)
        cursor = destination_end

    for pointer_offset, source_offset in AGWS_TEXT_POINTERS.items():
        expected = source_offset + TEXT_VA_DELTA
        replacement = destinations[source_offset] + TEXT_VA_DELTA
        _replace_exact_bytes(
            data,
            pointer_offset,
            expected.to_bytes(4, "little"),
            replacement.to_bytes(4, "little"),
            "AGWS text pointer",
        )

    data[region_start:region_end] = b"\0" * (region_end - region_start)
    changes = []
    for source_offset in stored_offsets:
        destination = destinations[source_offset]
        encoded = encoded_strings[source_offset]
        data[destination:destination + len(encoded)] = encoded

    for source_offset in AGWS_TEXT_OFFSETS:
        destination = destinations[source_offset]
        encoded = encoded_strings[source_offset]
        shared_with = next(
            (
                stored_offset
                for stored_offset in stored_offsets
                if stored_offset != source_offset
                and destinations[stored_offset] < destination
                and destinations[stored_offset] + len(encoded_strings[stored_offset])
                == destination + len(encoded)
            ),
            None,
        )
        suffix_note = (
            f", suffix of 0x{shared_with:08x}" if shared_with is not None else ""
        )
        changes.append(
            f"0x{source_offset:08x} -> 0x{destination:08x} "
            f"({len(encoded)}B plus NUL{suffix_note})"
        )

    used = cursor - region_start
    changes.append(
        f"packed size 0x{used:x}/0x{region_end - region_start:x}; "
        f"runtime pointers={len(AGWS_TEXT_POINTERS)}"
    )
    return changes


def apply_spacing_fixes(data: bytearray, translations: dict[int, str]) -> list[str]:
    changes = []
    for table_offset, (expected, string_offsets) in LENGTH_TABLE_PATCHES.items():
        actual = bytes(data[table_offset : table_offset + len(expected)])
        if actual != expected:
            raise ValueError(
                f"unexpected length table at 0x{table_offset:08x}: "
                f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
            )
        replacement = bytearray(expected)
        labels = []
        for index, string_offset in enumerate(string_offsets):
            if string_offset is None:
                continue
            if string_offset not in translations:
                raise ValueError(f"missing translation for 0x{string_offset:08x}")
            label = translations[string_offset]
            cells = visible_cells(label)
            if not 0 < cells < 0x100:
                raise ValueError(f"invalid visible length for 0x{string_offset:08x}: {cells}")
            replacement[index] = cells
            labels.append(f"{label!r}={cells}")
        data[table_offset : table_offset + len(replacement)] = replacement
        changes.append(
            f"0x{table_offset:08x}: {expected.hex(' ')} -> {bytes(replacement).hex(' ')} "
            f"({', '.join(labels)})"
        )

    skill_text = translations.get(0x2D6A48)
    if skill_text is None:
        raise ValueError("missing Skill title translation at 0x002d6a48")
    skill_x = 0x10 + visible_cells(skill_text) * 0x14
    if skill_x > 0xFFFF:
        raise ValueError(f"Skill title X coordinate is out of range: 0x{skill_x:x}")
    replacement = skill_x.to_bytes(2, "little")
    for offset, expected in SKILL_X_PATCHES.items():
        actual = bytes(data[offset : offset + 2])
        if actual != expected:
            raise ValueError(
                f"unexpected Skill X immediate at 0x{offset:08x}: "
                f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
            )
        data[offset : offset + 2] = replacement
        changes.append(f"0x{offset:08x}: X 0x4c -> 0x{skill_x:x} ({skill_text!r})")

    for offset, (expected, replacement_bytes) in EVT_HALF_WIDTH_PATCHES.items():
        actual = bytes(data[offset : offset + len(expected)])
        if actual == replacement_bytes:
            changes.append(f"0x{offset:08x}: EVT half-width advance already patched")
            continue
        if actual != expected:
            raise ValueError(
                f"unexpected EVT half-width instruction at 0x{offset:08x}: "
                f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
            )
        data[offset : offset + len(expected)] = replacement_bytes
        changes.append(
            f"0x{offset:08x}: EVT half-width advance "
            f"{expected.hex(' ')} -> {replacement_bytes.hex(' ')}"
        )
    changes.extend(_apply_scoped_menu_half_spaces(data))
    return changes


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_output(output: Path, data: bytes, replace_output: bool) -> None:
    if output.exists() and not replace_output:
        raise SystemExit(f"[ERROR] refusing to overwrite existing output: {output}")
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("translations", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--replace-output",
        action="store_true",
        help="atomically replace only the named output file if it already exists",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    translations_path = args.translations.resolve()
    output = args.output.resolve()
    if output in (source, translations_path):
        raise SystemExit("[ERROR] output must differ from both input paths")
    if output.with_name(output.name + ".tmp") in (source, translations_path):
        raise SystemExit("[ERROR] temporary output path collides with an input path")
    if output.exists() and not args.replace_output:
        raise SystemExit(f"[ERROR] refusing to overwrite existing output: {output}")

    original = source.read_bytes()
    data = bytearray(original)
    translations = parse_translations(translations_path)
    translation_edits, malformed = euc_scan.parse_translation_edits(
        str(translations_path)
    )
    (
        translated_count,
        translated_records,
        missing_count,
        overflow_count,
        control_warnings,
    ) = apply_translations(data, source, translation_edits)
    replace_table = slps_strings.load_replace_table(str(source))
    load_text_changes = apply_load_text_relocations(
        data,
        original,
        translations,
        lambda text: slps_strings.encode_display(text, replace_table),
    )
    agws_text_changes = apply_agws_text_relocation(
        data,
        original,
        translations,
        lambda text: slps_strings.encode_display(text, replace_table),
    )
    spacing_changes = apply_spacing_fixes(data, translations)
    database_changes = apply_database_search_patch(
        data,
        lambda text: slps_strings.encode_display(text, replace_table),
        ov02_offset=0x2D7000,
        memsz_offset=0xA8,
        expected_memsz=0x1393C,
    )
    if len(data) != len(original):
        raise AssertionError("SLPS rebuild changed the executable file size")
    write_output(output, data, args.replace_output)

    print(
        f"[OK] translated logical strings: {translated_count}; "
        f"records={translated_records}"
    )
    print(
        f"[OK] translation warnings: malformed={malformed}, missing={missing_count}, "
        f"overflow={overflow_count}, control={control_warnings}"
    )
    for change in spacing_changes:
        print(f"[OK] spacing {change}")
    for change in load_text_changes:
        print(f"[OK] Load text {change}")
    for change in agws_text_changes:
        print(f"[OK] AGWS text {change}")
    for change in database_changes:
        print(f"[OK] database search {change}")
    print(f"[OK] source SHA-256: {sha256(original)}")
    print(f"[OK] output SHA-256: {sha256(data)}")
    print(f"[OK] output: {output}")


if __name__ == "__main__":
    main()
