#!/usr/bin/env python3
"""Build a translated SLPS ELF with Korean menu breadcrumb spacing fixes.

The source ELF and translation text are read-only. The output path must not
already exist, preventing accidental replacement of either an original or a
previous build.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import slps_strings


LENGTH_TABLE_PATCHES = {
    # table offset: (expected original bytes, translation string offsets)
    0x2D6070: (b"\x03\x03\x06\x06\x06", (0x2D6060, 0x2D6068, None, None, None)),
    0x2D62E8: (b"\x06\x03\x03\x04\x04", (0x2C0CE0, None, None, None, None)),
    0x2D6750: (b"\x04\x03\x04\x07", (0x2C3540, 0x2D6748, 0x2C3530, 0x2C3520)),
    # Embedded OV02 copy. The standalone OV02 used at runtime has the same table.
    0x2EA2D8: (b"\x03\x05\x03\x03\x05", (0x2EA2D0, 0x2EA2C0, 0x2EA2B8, 0x2EA2B0, 0x2EA2A0)),
}

# MIPS ADDIU immediate fields used for the two Skill breadcrumb variants.
SKILL_X_PATCHES = {
    0x0B35FC: b"\x4c\x00",
    0x0B3614: b"\x4c\x00",
}


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
    data: bytearray, source: Path, translations: dict[int, str]
) -> tuple[int, int, int, int]:
    replace_table = slps_strings.load_replace_table(str(source))
    originals = {
        offset: (raw, trailing)
        for offset, raw, trailing in slps_strings.iter_strings(bytes(data), slps_strings.SCAN_START)
    }
    patched = 0
    skipped_missing = 0
    skipped_overflow = 0
    control_warnings = 0
    for offset, new_text in sorted(translations.items()):
        cover_start = slps_strings.covering_logical_string(offset)
        if cover_start is not None and cover_start in translations:
            continue
        if offset not in originals:
            print(f"[WARN] translation offset 0x{offset:08x} is not a source string; skipped")
            skipped_missing += 1
            continue
        original, trailing = originals[offset]
        encoded = slps_strings.encode_display(new_text, replace_table)
        slot_size = len(original) + 1 + trailing
        if len(encoded) > len(original) + trailing:
            print(
                f"[WARN] translation at 0x{offset:08x} needs {len(encoded)} bytes; "
                f"slot capacity is {len(original) + trailing}; skipped"
            )
            skipped_overflow += 1
            continue
        if slps_strings.control_bytes(original) != slps_strings.control_bytes(encoded):
            print(f"[WARN] translation at 0x{offset:08x} changes control bytes")
            control_warnings += 1
        if encoded == original:
            continue
        data[offset : offset + slot_size] = b"\0" * slot_size
        data[offset : offset + len(encoded)] = encoded
        patched += 1
    return patched, skipped_missing, skipped_overflow, control_warnings


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
    translated_count, missing_count, overflow_count, control_warnings = apply_translations(
        data, source, translations
    )
    spacing_changes = apply_spacing_fixes(data, translations)
    write_output(output, data, args.replace_output)

    print(f"[OK] translated strings: {translated_count}")
    print(
        f"[OK] translation warnings: missing={missing_count}, "
        f"overflow={overflow_count}, control={control_warnings}"
    )
    for change in spacing_changes:
        print(f"[OK] spacing {change}")
    print(f"[OK] source SHA-256: {sha256(original)}")
    print(f"[OK] output SHA-256: {sha256(data)}")
    print(f"[OK] output: {output}")


if __name__ == "__main__":
    main()
