#!/usr/bin/env python3
"""Extract and rebuild fixed-width text fields in tanaka/CASINO.res."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path


ENCODING = "euc_jis_2004"
SOURCE_NAME = "CASINO.res"
TEXT_NAME = "CASINO.txt"
META_NAME = "CASINO.json"
REPORT_NAME = "last_rebuild_report.json"

ITEM_TABLE_OFFSET = 0x0008
ITEM_COUNT = 51
ITEM_STRIDE = 0x7C
ITEM_VALUE_OFFSET = 0x00
ITEM_NAME_OFFSET = 0x04
ITEM_NAME_SIZE = 0x28
ITEM_DESCRIPTION_OFFSET = 0x2C
ITEM_DESCRIPTION_SIZE = 0x50

BUNDLE_TABLE_OFFSET = 0x18BC
BUNDLE_COUNT = 4
BUNDLE_STRIDE = 0x20
BUNDLE_VALUE_OFFSET = 0x00
BUNDLE_LABEL_OFFSET = 0x04
BUNDLE_LABEL_SIZE = 0x1C
BINARY_DATA_OFFSET = 0x193C

DEFAULT_CHARMAP = Path("..") / "carddata" / "XENOSAGA_KOR-JPN.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def escape_txt(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def unescape_txt(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            out.append(char)
            index += 1
            continue
        following = text[index + 1]
        out.append({"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}.get(following, following))
        index += 2
    return "".join(out)


def decode_field(blob: bytes, offset: int, size: int, where: str) -> tuple[str, bytes]:
    if offset < 0 or offset + size > len(blob):
        raise ValueError(f"{where}: field 0x{offset:X}+0x{size:X} is outside the file")
    raw = blob[offset : offset + size]
    terminator = raw.find(b"\0")
    if terminator < 0:
        raise ValueError(f"{where}: fixed field has no null terminator")
    try:
        text = raw[:terminator].decode(ENCODING)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{where}: invalid {ENCODING} data") from exc
    return text, raw


def make_field(
    blob: bytes,
    kind: str,
    row_id: int,
    field: str,
    offset: int,
    size: int,
) -> dict:
    key = f"{kind}.{row_id:03d}.{field}"
    text, raw = decode_field(blob, offset, size, key)
    return {
        "key": key,
        "kind": kind,
        "id": row_id,
        "field": field,
        "offset": offset,
        "size": size,
        "max_encoded_bytes": size - 1,
        "original_text": text,
        "original_encoded_bytes": len(text.encode(ENCODING)),
        "original_raw_sha256": sha256_bytes(raw),
    }


def parse_source(blob: bytes) -> tuple[list[dict], dict]:
    if len(blob) < BINARY_DATA_OFFSET:
        raise ValueError(f"{SOURCE_NAME}: expected at least 0x{BINARY_DATA_OFFSET:X} bytes")

    fields: list[dict] = []
    item_values: list[int] = []
    for row_id in range(ITEM_COUNT):
        record = ITEM_TABLE_OFFSET + row_id * ITEM_STRIDE
        item_values.append(struct.unpack_from("<I", blob, record + ITEM_VALUE_OFFSET)[0])
        fields.append(
            make_field(
                blob,
                "item",
                row_id,
                "name",
                record + ITEM_NAME_OFFSET,
                ITEM_NAME_SIZE,
            )
        )
        fields.append(
            make_field(
                blob,
                "item",
                row_id,
                "description",
                record + ITEM_DESCRIPTION_OFFSET,
                ITEM_DESCRIPTION_SIZE,
            )
        )

    bundle_values: list[int] = []
    for row_id in range(BUNDLE_COUNT):
        record = BUNDLE_TABLE_OFFSET + row_id * BUNDLE_STRIDE
        bundle_values.append(struct.unpack_from("<I", blob, record + BUNDLE_VALUE_OFFSET)[0])
        fields.append(
            make_field(
                blob,
                "bundle",
                row_id,
                "label",
                record + BUNDLE_LABEL_OFFSET,
                BUNDLE_LABEL_SIZE,
            )
        )

    structure = {
        "header": {
            "offset": 0,
            "size": ITEM_TABLE_OFFSET,
            "u32_values": list(struct.unpack_from("<II", blob, 0)),
            "notes": "Semantics unknown; preserved verbatim.",
        },
        "tables": [
            {
                "name": "items",
                "offset": ITEM_TABLE_OFFSET,
                "count": ITEM_COUNT,
                "stride": ITEM_STRIDE,
                "record_layout": {
                    "value_u32": [ITEM_VALUE_OFFSET, 4],
                    "name": [ITEM_NAME_OFFSET, ITEM_NAME_SIZE],
                    "description": [ITEM_DESCRIPTION_OFFSET, ITEM_DESCRIPTION_SIZE],
                },
                "values": item_values,
            },
            {
                "name": "bundles",
                "offset": BUNDLE_TABLE_OFFSET,
                "count": BUNDLE_COUNT,
                "stride": BUNDLE_STRIDE,
                "record_layout": {
                    "value_u32": [BUNDLE_VALUE_OFFSET, 4],
                    "label": [BUNDLE_LABEL_OFFSET, BUNDLE_LABEL_SIZE],
                },
                "values": bundle_values,
            },
        ],
        "binary_data": {
            "offset": BINARY_DATA_OFFSET,
            "size": len(blob) - BINARY_DATA_OFFSET,
            "notes": "Non-text numeric tables and padding; preserved verbatim.",
        },
    }
    return fields, structure


def write_txt(path: Path, fields: list[dict]) -> None:
    lines = [
        "# source=CASINO.res",
        "# format: <zero-based id> TAB <item.name|item.description|bundle.label> TAB <UTF-8 text>",
        "# edit only the third column; keep ids and field names unchanged",
        "# escapes: \\n, \\r, \\t, \\\\",
        "# fixed encoded limits: item.name=39, item.description=79, bundle.label=27 bytes",
    ]
    for field in fields:
        label = f"{field['kind']}.{field['field']}"
        lines.append(f"{field['id']:03d}\t{label}\t{escape_txt(field['original_text'])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def read_txt(path: Path, expected: dict[tuple[int, str], dict]) -> dict[str, str]:
    updates: dict[str, str] = {}
    seen: set[tuple[int, str]] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t", 2)
        if len(parts) != 3:
            raise ValueError(f"{path}:{line_number}: expected '<id> TAB <field> TAB <text>'")
        id_text, label, text = parts
        try:
            row_id = int(id_text, 10)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: invalid decimal id {id_text!r}") from exc
        identity = (row_id, label)
        if identity not in expected:
            raise ValueError(f"{path}:{line_number}: unknown field {row_id:03d} {label}")
        if identity in seen:
            raise ValueError(f"{path}:{line_number}: duplicate field {row_id:03d} {label}")
        seen.add(identity)
        updates[expected[identity]["key"]] = unescape_txt(text)

    missing = sorted(set(expected) - seen)
    if missing:
        preview = ", ".join(f"{row_id:03d} {label}" for row_id, label in missing[:5])
        raise ValueError(f"{path}: missing {len(missing)} field(s), beginning with {preview}")
    return updates


def load_charmap(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"charmap not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    table = data.get("replace-table")
    if not isinstance(table, dict):
        raise ValueError(f"{path}: missing JSON object 'replace-table'")
    return dict(sorted(table.items(), key=lambda item: len(item[0]), reverse=True))


def apply_charmap(text: str, charmap: dict[str, str]) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        for source, replacement in charmap.items():
            if text.startswith(source, index):
                out.append(replacement)
                index += len(source)
                break
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def encode_text(text: str, charmap: dict[str, str], where: str) -> bytes:
    mapped = apply_charmap(text, charmap)
    try:
        return mapped.encode(ENCODING)
    except UnicodeEncodeError as exc:
        bad = exc.object[exc.start : exc.end]
        raise ValueError(f"{where}: cannot encode {bad!r}; update the Korean charmap") from exc


def byte_diff_summary(original: bytes, rebuilt: bytes) -> dict:
    changed = [index for index, pair in enumerate(zip(original, rebuilt)) if pair[0] != pair[1]]
    return {
        "original_size": len(original),
        "rebuilt_size": len(rebuilt),
        "changed_bytes": len(changed),
        "changed_span": None if not changed else [changed[0], changed[-1]],
    }


def extract(source: Path, out_dir: Path, force: bool) -> tuple[Path, Path]:
    text_path = out_dir / TEXT_NAME
    meta_path = out_dir / META_NAME
    if not force and (text_path.exists() or meta_path.exists()):
        raise FileExistsError(f"extraction already exists in {out_dir}; use extract --force to replace it")
    blob = source.read_bytes()
    fields, structure = parse_source(blob)
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "tool": "casino_text_tool",
        "version": 1,
        "source": source.name,
        "source_size": len(blob),
        "source_sha256": sha256_bytes(blob),
        "encoding": ENCODING,
        "structure": structure,
        "fields": fields,
        "rebuild_rule": "Patch only changed fixed-width strings; preserve every other source byte.",
    }
    write_txt(text_path, fields)
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return text_path, meta_path


def rebuild(source: Path, text_path: Path, meta_path: Path, out_path: Path, charmap_path: Path) -> dict:
    blob = source.read_bytes()
    metadata = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    if metadata.get("tool") != "casino_text_tool" or metadata.get("version") != 1:
        raise ValueError(f"unsupported metadata: {meta_path}")
    if len(blob) != int(metadata["source_size"]) or sha256_bytes(blob) != metadata["source_sha256"]:
        raise ValueError("CASINO.res differs from the source recorded during extraction")

    fields = metadata["fields"]
    expected: dict[tuple[int, str], dict] = {}
    for field in fields:
        label = f"{field['kind']}.{field['field']}"
        expected[(int(field["id"]), label)] = field
        offset, size = int(field["offset"]), int(field["size"])
        if sha256_bytes(blob[offset : offset + size]) != field["original_raw_sha256"]:
            raise ValueError(f"source field changed: {field['key']}")

    updates = read_txt(text_path, expected)
    charmap = load_charmap(charmap_path)
    rebuilt = bytearray(blob)
    changed_fields: list[dict] = []
    allowed_offsets: set[int] = set()
    for field in fields:
        text = updates[field["key"]]
        if text == field["original_text"]:
            continue
        encoded = encode_text(text, charmap, field["key"])
        limit = int(field["max_encoded_bytes"])
        if len(encoded) > limit:
            raise ValueError(
                f"{field['key']}: {len(encoded)} encoded bytes exceeds the {limit}-byte limit"
            )
        offset, size = int(field["offset"]), int(field["size"])
        replacement = encoded + b"\0" * (size - len(encoded))
        rebuilt[offset : offset + size] = replacement
        allowed_offsets.update(range(offset, offset + size))
        changed_fields.append(
            {
                "key": field["key"],
                "offset": offset,
                "size": size,
                "encoded_bytes": len(encoded),
            }
        )

    changed_offsets = {index for index, pair in enumerate(zip(blob, rebuilt)) if pair[0] != pair[1]}
    if not changed_offsets.issubset(allowed_offsets):
        raise AssertionError("rebuild changed bytes outside edited text fields")
    if len(rebuilt) != len(blob):
        raise AssertionError("rebuild changed CASINO.res size")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(rebuilt)
    report = {
        "source": source.name,
        "output": str(out_path),
        "changed_fields": changed_fields,
        **byte_diff_summary(blob, rebuilt),
        "source_sha256": sha256_bytes(blob),
        "output_sha256": sha256_bytes(rebuilt),
    }
    (meta_path.parent / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    base_dir = Path(__file__).resolve().parent
    default_source = base_dir / SOURCE_NAME
    default_text_dir = base_dir / "text"

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract")
    extract_parser.add_argument("--source", type=Path, default=default_source)
    extract_parser.add_argument("--out-dir", type=Path, default=default_text_dir)
    extract_parser.add_argument("--force", action="store_true")

    rebuild_parser = subparsers.add_parser("rebuild")
    rebuild_parser.add_argument("--source", type=Path, default=default_source)
    rebuild_parser.add_argument("--text", type=Path, default=default_text_dir / TEXT_NAME)
    rebuild_parser.add_argument("--meta", type=Path, default=default_text_dir / META_NAME)
    rebuild_parser.add_argument("--out", type=Path, default=base_dir / "text_rebuilt" / SOURCE_NAME)
    rebuild_parser.add_argument("--charmap", type=Path, default=(base_dir / DEFAULT_CHARMAP).resolve())
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "extract":
        text_path, meta_path = extract(args.source, args.out_dir, args.force)
        print(f"[TEXT] {text_path}")
        print(f"[META] {meta_path}")
        return
    report = rebuild(args.source, args.text, args.meta, args.out, args.charmap)
    print(f"[REBUILT] {args.out}")
    print(f"[FIELDS] {len(report['changed_fields'])}")
    print(f"[BYTES] {report['changed_bytes']}; span={report['changed_span']}")


if __name__ == "__main__":
    main()
