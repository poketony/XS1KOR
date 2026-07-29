#!/usr/bin/env python3
"""Extract OV01 text through ELF symbols and runtime pointer references.

Unlike ``ov01_strings.py``, this analyzer does not treat every nonzero run as
text. It starts from the named OV01 data tables, follows their string pointers,
and adds the three strings referenced directly by MIPS address construction.
The output is an analysis/editing source; it does not modify an OVL.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import struct


ENCODING = "euc_jis_2004"
TEXT_RANGES = ((0x40E38, 0x40EC0), (0x411E8, 0x48BB0))
KNOWN_DIRECT_REFERENCES = {
    # menuKeyGuideHelp carries the LUI result across a long branch body, so a
    # short local address-construction scan cannot pair these two instructions.
    0x40EB0: "code@00a29304/00a29500",
}
DIRECT_REFERENCE_PATTERN = re.compile(
    r"code@(?P<lui>[0-9a-fA-F]{8})/(?P<combine>[0-9a-fA-F]{8})"
)

RECORD_TABLES = (
    "ethNameTbl",
    "itmNameTbl",
    "sklNameTbl",
    "accNameTbl",
    "wepNameTbl",
    "bltNameTbl",
    "nrmNameTbl",
    "spcNameTbl",
    "engNameTbl",
    "frmNameTbl",
)
POINTER_TABLES = (
    "atr1Name",
    "atr2Name",
    "atr3Name",
    "mapPartsName",
    "enemyNameTbl",
    "playerNameTbl",
    "ziggyName",
    "ba",
    "ma",
    "bb",
    "mb",
    "aa1",
    "aa2",
    "ab",
)
RECORD_ROLES = ("name", "description", "sort_key")
DESCRIPTION_TABLES = (
    ("ether", "ethNameTbl"),
    ("item", "itmNameTbl"),
    ("normal_tech", "nrmNameTbl"),
    ("special_tech", "spcNameTbl"),
)
DESCRIPTION_WIDTH = 0x138
STATUS_WIDTH = 0xB0
PATCHED_STATUS_WIDTH = 0xD0
STATUS_WIDTH_INSTRUCTION_VA = 0x00A28C9C


@dataclass(frozen=True)
class LoadSegment:
    file_offset: int
    virtual_address: int
    file_size: int


@dataclass(frozen=True)
class Symbol:
    name: str
    value: int
    size: int
    kind: int


class Elf32:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        if self.data[:4] != b"\x7fELF" or self.data[4] != 1 or self.data[5] != 1:
            raise ValueError(f"{path} is not a little-endian ELF32 file")
        self.loads = self._read_loads()
        self.symbols = self._read_symbols()
        self.symbol_by_name = {symbol.name: symbol for symbol in self.symbols}

    def _read_loads(self) -> list[LoadSegment]:
        phoff = struct.unpack_from("<I", self.data, 0x1C)[0]
        phentsize = struct.unpack_from("<H", self.data, 0x2A)[0]
        phnum = struct.unpack_from("<H", self.data, 0x2C)[0]
        loads = []
        for index in range(phnum):
            fields = struct.unpack_from(
                "<IIIIIIII", self.data, phoff + index * phentsize
            )
            if fields[0] == 1:
                loads.append(LoadSegment(fields[1], fields[2], fields[4]))
        return loads

    def _read_symbols(self) -> list[Symbol]:
        shoff = struct.unpack_from("<I", self.data, 0x20)[0]
        shentsize = struct.unpack_from("<H", self.data, 0x2E)[0]
        shnum = struct.unpack_from("<H", self.data, 0x30)[0]
        sections = [
            struct.unpack_from("<IIIIIIIIII", self.data, shoff + i * shentsize)
            for i in range(shnum)
        ]

        def section_data(index: int) -> bytes:
            section = sections[index]
            return self.data[section[4] : section[4] + section[5]]

        symbols = []
        for index, section in enumerate(sections):
            if section[1] not in (2, 11):
                continue
            strings = section_data(section[6])
            entries = section_data(index)
            entry_size = section[9] or 16
            for offset in range(0, len(entries) - 15, entry_size):
                name_offset, value, size, info, _other, _shndx = struct.unpack_from(
                    "<IIIBBH", entries, offset
                )
                if name_offset >= len(strings):
                    continue
                end = strings.find(b"\0", name_offset)
                name = strings[name_offset:end].decode("ascii", errors="replace")
                if name:
                    symbols.append(Symbol(name, value, size, info & 0x0F))
        return symbols

    def va_to_offset(self, address: int) -> int:
        for segment in self.loads:
            delta = address - segment.virtual_address
            if 0 <= delta < segment.file_size:
                return segment.file_offset + delta
        raise ValueError(f"VA 0x{address:08x} is outside PT_LOAD")

    def offset_to_va(self, offset: int) -> int:
        for segment in self.loads:
            delta = offset - segment.file_offset
            if 0 <= delta < segment.file_size:
                return segment.virtual_address + delta
        raise ValueError(f"file offset 0x{offset:x} is outside PT_LOAD")


def in_text_range(offset: int) -> bool:
    return any(start <= offset < end for start, end in TEXT_RANGES)


def read_raw_string(data: bytes, offset: int) -> bytes:
    end = data.find(b"\0", offset)
    if end < 0:
        raise ValueError(f"unterminated string at 0x{offset:06x}")
    return data[offset:end]


def display_text(raw: bytes) -> str:
    out = []
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == 0x0A:
            out.append("\\n")
            index += 1
        elif value == 0x0D:
            out.append("\\r")
            index += 1
        elif value < 0x20 or value in (0x7F, 0x80):
            out.append(f"\\x{value:02x}")
            index += 1
        elif value < 0x80:
            out.append(chr(value))
            index += 1
        else:
            width = 3 if value == 0x8F else 2
            chunk = raw[index : index + width]
            try:
                out.append(chunk.decode(ENCODING))
                index += width
            except UnicodeDecodeError:
                out.append(f"\\x{value:02x}")
                index += 1
    return "".join(out)


def load_replace_table(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data.get("replace-table", {})


def encode_display(text: str, replace_table: dict[str, str]) -> bytes:
    output = bytearray()
    literal = []

    def flush() -> None:
        if not literal:
            return
        replaced = "".join(replace_table.get(char, char) for char in literal)
        output.extend(replaced.encode(ENCODING))
        literal.clear()

    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            escape = text[index + 1]
            if escape in ("n", "r"):
                flush()
                output.append(0x0A if escape == "n" else 0x0D)
                index += 2
                continue
            if escape in ("x", "X") and index + 3 < len(text):
                digits = text[index + 2 : index + 4]
                if re.fullmatch(r"[0-9a-fA-F]{2}", digits):
                    flush()
                    output.append(int(digits, 16))
                    index += 4
                    continue
        literal.append(text[index])
        index += 1
    flush()
    return bytes(output)


def parse_translation(path: Path) -> dict[int, str]:
    translations = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            offset = int(parts[0], 16)
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: invalid offset") from error
        translations[offset] = parts[2]
    return translations


def glyph_width_bounds(char: str, replace_table: dict[str, str]) -> tuple[int, int]:
    """Return the xglFontGetStringWidth lower/upper bound for one character.

    Battle system-font ASCII bytes, including spaces, advance by 16 pixels.
    Ordinary EUC double-byte glyphs use 20 pixels.
    Fullwidth Roman glyphs and three punctuation cases use runtime proportional
    metrics; without a RAM font dump their exact width is bounded between 1 and
    20 pixels.
    """
    replaced = replace_table.get(char, char)
    raw = replaced.encode(ENCODING)
    if len(raw) == 1:
        return (16, 16)
    first, second = raw[0], raw[1]
    proportional = (
        first == 0xA3
        or (first == 0xA1 and second == 0xA5)
        or (first == 0xA7 and second in (0xA1, 0xA3, 0xA6))
    )
    return (1, 20) if proportional else (20, 20)


def line_width_bounds(line: str, replace_table: dict[str, str]) -> tuple[int, int]:
    bounds = [glyph_width_bounds(char, replace_table) for char in line]
    return sum(low for low, _high in bounds), sum(high for _low, high in bounds)


def trailing_zero_count(data: bytes, terminator: int, limit: int) -> int:
    cursor = terminator + 1
    while cursor < limit and data[cursor] == 0:
        cursor += 1
    return cursor - terminator - 1


def collect_table_references(elf: Elf32) -> dict[int, list[str]]:
    references: dict[int, list[str]] = defaultdict(list)

    for table_name in RECORD_TABLES:
        symbol = elf.symbol_by_name[table_name]
        if symbol.size % 12:
            raise ValueError(f"{table_name} size 0x{symbol.size:x} is not record-aligned")
        table_offset = elf.va_to_offset(symbol.value)
        for record_index in range(symbol.size // 12):
            for role_index, role in enumerate(RECORD_ROLES):
                pointer_offset = table_offset + record_index * 12 + role_index * 4
                target = struct.unpack_from("<I", elf.data, pointer_offset)[0]
                try:
                    target_offset = elf.va_to_offset(target)
                except ValueError:
                    continue
                if in_text_range(target_offset):
                    references[target_offset].append(
                        f"{table_name}[{record_index}].{role}@{pointer_offset:06x}"
                    )

    for table_name in POINTER_TABLES:
        symbol = elf.symbol_by_name[table_name]
        if symbol.size % 4:
            raise ValueError(f"{table_name} size 0x{symbol.size:x} is not pointer-aligned")
        table_offset = elf.va_to_offset(symbol.value)
        for entry_index in range(symbol.size // 4):
            pointer_offset = table_offset + entry_index * 4
            target = struct.unpack_from("<I", elf.data, pointer_offset)[0]
            try:
                target_offset = elf.va_to_offset(target)
            except ValueError:
                continue
            if in_text_range(target_offset):
                references[target_offset].append(
                    f"{table_name}[{entry_index}]@{pointer_offset:06x}"
                )

    return references


def strict_candidates(elf: Elf32) -> dict[int, bytes]:
    candidates = {}
    for start, end in TEXT_RANGES:
        cursor = start
        while cursor < end:
            if elf.data[cursor] == 0:
                cursor += 1
                continue
            terminator = elf.data.find(b"\0", cursor, end)
            if terminator < 0:
                break
            raw = elf.data[cursor:terminator]
            try:
                raw.replace(b"\n", b"").replace(b"\r", b"").decode(ENCODING)
            except UnicodeDecodeError:
                cursor = terminator + 1
                continue
            candidates[cursor] = raw
            cursor = terminator + 1
    return candidates


def collect_direct_references(
    elf: Elf32, candidates: dict[int, bytes]
) -> dict[int, list[str]]:
    """Find LUI plus ADDIU/ORI constructions of candidate string addresses."""
    target_by_va = {elf.offset_to_va(offset): offset for offset in candidates}
    references: dict[int, list[str]] = defaultdict(list)
    for segment in elf.loads:
        start = segment.file_offset
        end = start + segment.file_size
        for offset in range(start, end - 4, 4):
            word = struct.unpack_from("<I", elf.data, offset)[0]
            if word >> 26 != 0x0F:
                continue
            source_register = (word >> 16) & 0x1F
            high = word & 0xFFFF
            for step in range(1, 13):
                combine_offset = offset + step * 4
                if combine_offset + 4 > end:
                    break
                combine = struct.unpack_from("<I", elf.data, combine_offset)[0]
                opcode = combine >> 26
                if ((combine >> 21) & 0x1F) != source_register or opcode not in (9, 13):
                    continue
                low = combine & 0xFFFF
                if opcode == 9 and low >= 0x8000:
                    low -= 0x10000
                address = ((high << 16) + low) & 0xFFFFFFFF
                target_offset = target_by_va.get(address)
                if target_offset is None:
                    continue
                source_va = elf.offset_to_va(offset)
                references[target_offset].append(
                    f"code@{source_va:08x}/{elf.offset_to_va(combine_offset):08x}"
                )
    return references


def extract(source: Path, output: Path) -> None:
    elf = Elf32(source)
    candidates = strict_candidates(elf)
    references = collect_table_references(elf)
    direct = collect_direct_references(elf, candidates)
    for offset, refs in direct.items():
        references[offset].extend(refs)
    for offset, reference in KNOWN_DIRECT_REFERENCES.items():
        if offset not in candidates:
            raise ValueError(f"known direct string 0x{offset:06x} is not valid text")
        if reference not in references[offset]:
            references[offset].append(reference)

    rows = []
    for offset in sorted(references):
        raw = candidates.get(offset)
        if raw is None:
            if elf.data[offset] == 0:
                raw = b""
            else:
                raise ValueError(f"referenced target 0x{offset:06x} is not valid text")
        terminator = offset + len(raw)
        range_end = next(end for start, end in TEXT_RANGES if start <= offset < end)
        slack = trailing_zero_count(elf.data, terminator, range_end)
        ref_text = ",".join(references[offset])
        rows.append(
            f"S|{offset:06x}|{len(raw)}/{slack}|{ref_text}|{display_text(raw)}"
        )

    header = [
        "# OV01 ELF/pointer-based original string dump",
        f"# source: {source.name}",
        "# format: S|file_offset|orig_bytes/slack|references|text",
        "# 12-byte records: [name pointer, description pointer, sort-key pointer]",
        "# direct code references identify their LUI/combine instruction VAs",
        "# \\n and \\r are literal control-byte escapes",
        "",
    ]
    output.write_text("\n".join(header + rows) + "\n", encoding="utf-8")
    print(f"[OK] {len(rows)} unique referenced strings -> {output}")


def write_layout_report(
    source: Path,
    translation_path: Path,
    replace_table_path: Path,
    output: Path,
) -> None:
    elf = Elf32(source)
    translations = parse_translation(translation_path)
    replace_table = load_replace_table(replace_table_path)
    candidates = strict_candidates(elf)
    all_references = collect_table_references(elf)

    slot_overflows = []
    for offset, text in sorted(translations.items()):
        original = candidates.get(offset)
        if original is None:
            continue
        terminator = offset + len(original)
        range_end = next(end for start, end in TEXT_RANGES if start <= offset < end)
        slack = trailing_zero_count(elf.data, terminator, range_end)
        encoded = encode_display(text, replace_table)
        if len(encoded) > len(original) + slack:
            slot_overflows.append(
                (offset, len(original), slack, len(encoded), text)
            )

    description_rows = []
    category_counts = defaultdict(lambda: [0, 0])
    seen_descriptions = set()
    for category, table_name in DESCRIPTION_TABLES:
        symbol = elf.symbol_by_name[table_name]
        table_offset = elf.va_to_offset(symbol.value)
        for record_index in range(symbol.size // 12):
            pointer = struct.unpack_from(
                "<I", elf.data, table_offset + record_index * 12 + 4
            )[0]
            try:
                description_offset = elf.va_to_offset(pointer)
            except ValueError:
                continue
            identity = (category, description_offset)
            if identity in seen_descriptions or description_offset not in translations:
                continue
            seen_descriptions.add(identity)
            text = translations[description_offset]
            line_bounds = [
                line_width_bounds(line, replace_table) for line in text.split("\\n")
            ]
            low = max(bound[0] for bound in line_bounds)
            high = max(bound[1] for bound in line_bounds)
            if high <= DESCRIPTION_WIDTH:
                continue
            certainty = "definite" if low > DESCRIPTION_WIDTH else "proportional-check"
            category_counts[category][0 if certainty == "definite" else 1] += 1
            description_rows.append(
                (certainty, category, record_index, description_offset, low, high, text)
            )

    status_rows = []
    status_table_prefixes = ("ba[", "ma[", "bb[", "mb[", "aa1[", "aa2[", "ab[")
    for offset, refs in all_references.items():
        if offset not in translations or not any(
            reference.startswith(status_table_prefixes) for reference in refs
        ):
            continue
        text = translations[offset]
        low, high = line_width_bounds(text, replace_table)
        if high > PATCHED_STATUS_WIDTH - 6:
            status_rows.append((high, low, offset, text))

    lines = [
        "OV01 Layout Analysis",
        "====================",
        f"source: {source.name}",
        f"translation: {translation_path.name}",
        "",
        "Verified Rendering Geometry",
        "---------------------------",
        "- Ether/item information text: 312 px (SLPS eBattleWinOpen, VA 0x0027dc48).",
        "- Status-name window: x=8, patched width=208 px (OV01 menuStatNamePut, VAs 0x00a28cac and 0x00a28c9c).",
        "- Battle system font: double-byte glyph=20 px, ASCII byte including space=16 px.",
        "- Fullwidth Roman/proportional glyphs require runtime font metrics; reports crossing",
        "  the limit only in their 1-20 px range are marked proportional-check.",
        "",
        "Current In-place Capacity",
        "-------------------------",
        f"- translations exceeding original slot plus zero slack: {len(slot_overflows)}",
    ]
    for offset, original_length, slack, encoded_length, text in slot_overflows:
        lines.append(
            f"- 0x{offset:06x}: original={original_length} slack={slack} "
            f"encoded={encoded_length} over={encoded_length-original_length-slack} | {text}"
        )

    lines.extend(
        [
            "",
            "Technique/Item Description Width",
            "--------------------------------",
        ]
    )
    for category, _table_name in DESCRIPTION_TABLES:
        definite, proportional = category_counts[category]
        lines.append(
            f"- {category}: definite={definite}, proportional-check={proportional}"
        )
    lines.append("")
    for certainty, category, index, offset, low, high, text in description_rows:
        rendered = text.replace("\\n", " / ")
        lines.append(
            f"- [{certainty}] {category}[{index}] 0x{offset:06x}: "
            f"{low}-{high}px | {rendered}"
        )

    lines.extend(
        [
            "",
            "Status Name Width",
            "-----------------",
            "- Patched eBattleWinOpen2 centers STATUS by xglFontGetStringWidth:",
            "  start=(W-6-rendered_width)/2.",
            "- The original counter advanced two source bytes per non-newline glyph; a",
            "  one-byte ASCII space skipped part of the next Korean glyph and shifted text right.",
            "- The original W=176 reserves 170 pixels; patched W=208 reserves 202 pixels.",
            "- The longest current labels are 172 pixels, leaving 30 pixels.",
            "- Only this STATUS window is enlarged.",
        ]
    )
    for high, low, offset, text in sorted(status_rows, reverse=True):
        lines.append(f"- 0x{offset:06x}: actual={low}-{high}px | {text}")

    lines.extend(
        [
            "",
            "Relocation Rules",
            "----------------",
            "- The 12-byte tables are [name pointer, description pointer, sort-key pointer].",
            "- Relocate a unique string once and patch every pointer reference listed by the",
            "  pointer-based dump. Shared strings must stay shared unless deliberately split.",
            "- Preserve the canonical empty string at file offset 0x0448c0.",
            "- Direct strings at 0x040ea0, 0x040ea8 and 0x040eb0 are MIPS address references;",
            "  either keep them in place or patch their LUI/ADDIU instruction pairs.",
            "- The current OV01 battle-font extension ends at runtime VA 0x00a53364. A new",
            "  pool must start after that point and must not overwrite its mapping data.",
            "- Keep the fixed OVL file size. Extend PT_LOAD only into the unused non-ALLOC",
            "  symbol-table tail; fail the build if that internal pool is exhausted.",
        ]
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] layout report -> {output}")


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & -boundary


def original_sections(elf: Elf32) -> list[list[int]]:
    shoff = struct.unpack_from("<I", elf.data, 0x20)[0]
    shentsize = struct.unpack_from("<H", elf.data, 0x2E)[0]
    shnum = struct.unpack_from("<H", elf.data, 0x30)[0]
    return [
        list(struct.unpack_from("<IIIIIIIIII", elf.data, shoff + i * shentsize))
        for i in range(shnum)
    ]


def patch_direct_reference(
    layout: Elf32,
    rebuilt: bytearray,
    source_offset: int,
    destination_va: int,
    reference: str,
) -> None:
    """Retarget one MIPS LUI plus ADDIU/ORI string-address construction."""
    match = DIRECT_REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError(f"invalid direct reference {reference!r}")

    lui_va = int(match.group("lui"), 16)
    combine_va = int(match.group("combine"), 16)
    lui_offset = layout.va_to_offset(lui_va)
    combine_offset = layout.va_to_offset(combine_va)
    original_lui = struct.unpack_from("<I", layout.data, lui_offset)[0]
    original_combine = struct.unpack_from("<I", layout.data, combine_offset)[0]
    current_lui = struct.unpack_from("<I", rebuilt, lui_offset)[0]
    current_combine = struct.unpack_from("<I", rebuilt, combine_offset)[0]

    combine_opcode = original_combine >> 26
    source_register = (original_lui >> 16) & 0x1F
    if original_lui >> 26 != 0x0F or combine_opcode not in (0x09, 0x0D):
        raise ValueError(f"unsupported direct reference instructions at {reference}")
    if ((original_combine >> 21) & 0x1F) != source_register:
        raise ValueError(f"direct reference register mismatch at {reference}")
    if (
        current_lui & 0xFFFF0000 != original_lui & 0xFFFF0000
        or current_combine & 0xFFFF0000 != original_combine & 0xFFFF0000
    ):
        raise ValueError(f"direct reference code was modified at {reference}")

    high = original_lui & 0xFFFF
    low = original_combine & 0xFFFF
    if combine_opcode == 0x09:
        signed_low = low - 0x10000 if low & 0x8000 else low
        original_va = ((high << 16) + signed_low) & 0xFFFFFFFF
        destination_high = ((destination_va + 0x8000) >> 16) & 0xFFFF
    else:
        original_va = ((high << 16) | low) & 0xFFFFFFFF
        destination_high = (destination_va >> 16) & 0xFFFF
    expected_va = layout.offset_to_va(source_offset)
    if original_va != expected_va:
        raise ValueError(
            f"direct reference {reference} resolves to 0x{original_va:08x}, "
            f"expected 0x{expected_va:08x}"
        )

    struct.pack_into(
        "<I", rebuilt, lui_offset, (current_lui & 0xFFFF0000) | destination_high
    )
    struct.pack_into(
        "<I", rebuilt, combine_offset, (current_combine & 0xFFFF0000) | (destination_va & 0xFFFF)
    )


def rebuild_with_relocation(
    layout_source: Path,
    current_source: Path,
    translation_path: Path,
    replace_table_path: Path,
    output: Path,
    replace_output: bool = False,
) -> None:
    """Build a translated OV01 while relocating only over-capacity strings."""
    inputs = {
        layout_source.resolve(),
        current_source.resolve(),
        translation_path.resolve(),
        replace_table_path.resolve(),
    }
    output = output.resolve()
    temporary = output.with_name(output.name + ".tmp")
    if output in inputs or temporary in inputs:
        raise ValueError("OV01 output path collides with an input")
    if output.exists() and not replace_output:
        raise FileExistsError(f"refusing to overwrite {output}")

    layout = Elf32(layout_source)
    current = Elf32(current_source)
    if len(layout.loads) != 1 or len(current.loads) != 1:
        raise ValueError("OV01 relocation expects one PT_LOAD segment")
    layout_load = layout.loads[0]
    current_load = current.loads[0]
    if (
        layout_load.file_offset != current_load.file_offset
        or layout_load.virtual_address != current_load.virtual_address
    ):
        raise ValueError("layout and current OV01 load addresses differ")

    translations = parse_translation(translation_path)
    replace_table = load_replace_table(replace_table_path)
    candidates = strict_candidates(layout)
    references = collect_table_references(layout)
    direct_references = collect_direct_references(layout, candidates)
    for offset, reference in KNOWN_DIRECT_REFERENCES.items():
        if offset not in candidates:
            raise ValueError(f"known direct string 0x{offset:06x} is not valid text")
        if reference not in direct_references[offset]:
            direct_references[offset].append(reference)
    runtime_end = current_load.file_offset + current_load.file_size
    rebuilt = bytearray(current.data)
    original_file_size = len(rebuilt)

    status_width_offset = current.va_to_offset(STATUS_WIDTH_INSTRUCTION_VA)
    original_status_width = struct.pack("<I", 0x34E700B0)  # ori a3, a3, 0xb0
    patched_status_width = struct.pack("<I", 0x34E700D0)   # ori a3, a3, 0xd0
    actual_status_width = bytes(rebuilt[status_width_offset : status_width_offset + 4])
    if actual_status_width not in (original_status_width, patched_status_width):
        raise ValueError(
            f"unexpected STATUS window width instruction at VA "
            f"0x{STATUS_WIDTH_INSTRUCTION_VA:08x}: {actual_status_width.hex(' ')}"
        )
    rebuilt[status_width_offset : status_width_offset + 4] = patched_status_width

    relocated = []
    patched_in_place = 0

    for offset, text in sorted(translations.items()):
        original = candidates.get(offset)
        if original is None:
            continue
        terminator = offset + len(original)
        range_end = next(end for start, end in TEXT_RANGES if start <= offset < end)
        slack = trailing_zero_count(layout.data, terminator, range_end)
        slot_size = len(original) + 1 + slack
        encoded = encode_display(text, replace_table)
        if len(encoded) <= len(original) + slack:
            rebuilt[offset : offset + slot_size] = b"\0" * slot_size
            rebuilt[offset : offset + len(encoded)] = encoded
            patched_in_place += 1
            continue
        pointer_offsets = []
        for reference in references.get(offset, []):
            if "@" in reference:
                pointer_offsets.append(int(reference.rsplit("@", 1)[1], 16))
        code_references = sorted(set(direct_references.get(offset, [])))
        if not pointer_offsets and not code_references:
            raise ValueError(
                f"0x{offset:06x} exceeds its slot but has no relocatable reference"
            )
        relocated.append(
            (offset, encoded, sorted(set(pointer_offsets)), code_references)
        )

    pool_start = align(runtime_end, 0x10)
    cursor = pool_start
    relocation_plans = []
    for source_offset, encoded, pointer_offsets, code_references in relocated:
        destination_offset = align(cursor, 4)
        destination_end = destination_offset + len(encoded) + 1
        if destination_end > original_file_size:
            raise ValueError(
                f"OV01 fixed-size relocation pool exhausted at 0x{destination_offset:06x}: "
                f"needs {len(encoded) + 1}B, file ends at 0x{original_file_size:06x}"
            )
        destination_va = (
            current_load.virtual_address
            + destination_offset
            - current_load.file_offset
        )
        relocation_plans.append(
            (
                source_offset,
                destination_offset,
                destination_va,
                encoded,
                pointer_offsets,
                code_references,
            )
        )
        cursor = destination_end

    new_runtime_end = align(cursor, 0x10) if relocation_plans else runtime_end
    if new_runtime_end > original_file_size:
        raise ValueError(
            f"OV01 fixed-size PT_LOAD end 0x{new_runtime_end:06x} "
            f"exceeds file size 0x{original_file_size:06x}"
        )
    rebuilt[runtime_end:new_runtime_end] = b"\0" * (new_runtime_end - runtime_end)

    relocation_rows = []
    for (
        source_offset,
        destination_offset,
        destination_va,
        encoded,
        pointer_offsets,
        code_references,
    ) in relocation_plans:
        rebuilt[destination_offset : destination_offset + len(encoded) + 1] = (
            encoded + b"\0"
        )
        expected_source_va = layout.offset_to_va(source_offset)
        for pointer_offset in pointer_offsets:
            actual_pointer = struct.unpack_from("<I", rebuilt, pointer_offset)[0]
            if actual_pointer != expected_source_va:
                raise ValueError(
                    f"table pointer at 0x{pointer_offset:06x} was modified: "
                    f"expected 0x{expected_source_va:08x}, found 0x{actual_pointer:08x}"
                )
            struct.pack_into("<I", rebuilt, pointer_offset, destination_va)
        for reference in code_references:
            patch_direct_reference(
                layout, rebuilt, source_offset, destination_va, reference
            )
        relocation_rows.append(
            (
                source_offset,
                destination_offset,
                destination_va,
                len(encoded),
                pointer_offsets,
                code_references,
            )
        )

    new_load_size = new_runtime_end - current_load.file_offset
    phoff = struct.unpack_from("<I", rebuilt, 0x1C)[0]
    struct.pack_into("<I", rebuilt, phoff + 0x10, new_load_size)
    struct.pack_into("<I", rebuilt, phoff + 0x14, new_load_size)

    # Keep the original fixed-size OVL container entry. The existing custom
    # battle-font extension already consumes the beginning of the non-ALLOC
    # symbol table; relocation continues in that unused runtime tail. Section
    # headers and the existing battle-font extension stay byte-identical.
    shoff = struct.unpack_from("<I", rebuilt, 0x20)[0]
    shentsize = struct.unpack_from("<H", rebuilt, 0x2E)[0]
    shnum = struct.unpack_from("<H", rebuilt, 0x30)[0]
    if not shoff or shnum < 2 or shoff + shentsize * shnum > runtime_end:
        raise ValueError("OV01 section headers are not preserved before the relocation pool")
    if len(rebuilt) != original_file_size:
        raise AssertionError("OV01 fixed-size rebuild changed the container entry size")
    temporary.write_bytes(rebuilt)
    os.replace(temporary, output)

    print(
        f"[OK] in-place={patched_in_place}, relocated={len(relocation_rows)}, "
        f"STATUS_width={PATCHED_STATUS_WIDTH}px, PT_LOAD=0x{new_load_size:x}, "
        f"file_size=0x{original_file_size:x} -> {output}"
    )
    for (
        source_offset,
        destination_offset,
        destination_va,
        size,
        pointer_offsets,
        code_references,
    ) in relocation_rows:
        print(
            f"  0x{source_offset:06x} -> file 0x{destination_offset:06x} "
            f"VA 0x{destination_va:08x}, {size}B, "
            f"pointers={len(pointer_offsets)}, code={len(code_references)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--translation", type=Path)
    parser.add_argument("--replace-table", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--current-source", type=Path)
    parser.add_argument("--rebuild-output", type=Path)
    parser.add_argument(
        "--replace-output",
        action="store_true",
        help="atomically replace only the named rebuild output",
    )
    args = parser.parse_args()
    if args.output is not None:
        extract(args.source, args.output)
    if args.report is not None:
        if args.translation is None or args.replace_table is None:
            parser.error("--translation, --replace-table and --report must be used together")
        write_layout_report(
            args.source, args.translation, args.replace_table, args.report
        )
    rebuild_arguments = (args.current_source, args.rebuild_output)
    if any(rebuild_arguments):
        if not all(rebuild_arguments) or not args.translation or not args.replace_table:
            parser.error(
                "--current-source and --rebuild-output require --translation and --replace-table"
            )
        rebuild_with_relocation(
            args.source,
            args.current_source,
            args.translation,
            args.replace_table,
            args.rebuild_output,
            args.replace_output,
        )
    elif args.output is None and args.report is None:
        parser.error("output is required unless --report or --rebuild-output is used")


if __name__ == "__main__":
    main()
