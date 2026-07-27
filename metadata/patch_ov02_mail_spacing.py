#!/usr/bin/env python3
"""Build translated OV02 with Korean UI and database-search fixes.

The source OVL and translation text are never modified. The output must not
already exist.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import euc_scan
from ov02_database_search import apply_database_search_patch


LENGTH_TABLE_OFFSET = 0x132D8
EXPECTED_LENGTHS = b"\x03\x05\x03\x03\x05"
LABEL_OFFSETS = (0x132D0, 0x132C0, 0x132B8, 0x132B0, 0x132A0)


def parse_translations(path: Path) -> dict[int, str]:
    translations = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
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


def build_translated_image(source: Path, translations_path: Path) -> bytes:
    """Apply the established euc_scan rebuild rules entirely in memory."""
    data = bytearray(source.read_bytes())
    replace_table = euc_scan.load_replace_table(str(source))
    start = 0
    with translations_path.open(encoding="utf-8-sig", newline="") as stream:
        for line in stream:
            line = line.strip()
            if line.startswith("# scan start:"):
                start = int(line.split(":", 1)[1].strip(), 16)
                break
    ranges = euc_scan.parse_scan_ranges(str(translations_path), len(data))
    if not ranges:
        ranges = euc_scan.file_scan_ranges(str(source), len(data), start)

    edits, malformed = euc_scan.parse_translation_edits(str(translations_path))
    stats = euc_scan.apply_grouped_translations(
        data,
        ranges,
        edits,
        replace_table,
        label="OV02",
    )
    print(
        f"[OK] OV02 translated logical strings: {stats.patched_groups}; "
        f"records={stats.patched_records}, malformed={malformed}, "
        f"missing={stats.missing}, overflow={stats.overflow}, "
        f"invalid={stats.invalid}, control={stats.control_warnings}"
    )
    return bytes(data)


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
    data = bytearray(build_translated_image(source, translations_path))
    actual = bytes(data[LENGTH_TABLE_OFFSET : LENGTH_TABLE_OFFSET + len(EXPECTED_LENGTHS)])
    if actual != EXPECTED_LENGTHS:
        raise ValueError(
            f"unexpected mail length table at 0x{LENGTH_TABLE_OFFSET:08x}: "
            f"expected {EXPECTED_LENGTHS.hex(' ')}, found {actual.hex(' ')}"
        )

    translations = parse_translations(translations_path)
    labels = []
    replacement = bytearray()
    for offset in LABEL_OFFSETS:
        if offset not in translations:
            raise ValueError(f"missing translation for 0x{offset:08x}")
        text = translations[offset]
        cells = visible_cells(text)
        if not 0 < cells < 0x100:
            raise ValueError(f"invalid visible length for 0x{offset:08x}: {cells}")
        replacement.append(cells)
        labels.append(f"{text!r}={cells}")

    data[LENGTH_TABLE_OFFSET : LENGTH_TABLE_OFFSET + len(replacement)] = replacement
    replace_table = euc_scan.load_replace_table(str(source))
    database_changes = apply_database_search_patch(
        data,
        lambda text: euc_scan.encode_display(text, replace_table),
        ov02_offset=0,
        memsz_offset=0x48,
        expected_memsz=0x1493C,
    )
    write_output(output, data, args.replace_output)

    print(
        f"[OK] mail spacing 0x{LENGTH_TABLE_OFFSET:08x}: "
        f"{EXPECTED_LENGTHS.hex(' ')} -> {bytes(replacement).hex(' ')} "
        f"({', '.join(labels)})"
    )
    for change in database_changes:
        print(f"[OK] database search {change}")
    print(f"[OK] source SHA-256: {sha256(original)}")
    print(f"[OK] output SHA-256: {sha256(data)}")
    print(f"[OK] output: {output}")


if __name__ == "__main__":
    main()
