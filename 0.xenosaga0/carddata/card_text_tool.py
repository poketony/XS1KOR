#!/usr/bin/env python3
"""
Extract and rebuild text in XS1 carddata/card.dat and deckdata/deckdata.dat.

card.dat:
  0x00 u32 card count
  0x04 "CardData1.10"
  0x10 card records, 0x24 bytes each
  record+0x18..0x23: three little-endian absolute string offsets

deckdata.dat:
  48 records, 0x80 bytes each
  record+0x00..0x1f: fixed-size null-terminated deck name field

Usage:
  python card_text_tool.py extract
  python card_text_tool.py rebuild
  python rebuild card
  python rebuild deckdata
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


ENCODING = "euc_jis_2004"
CARD_MAGIC = b"CardData1.10"
CARD_HEADER_SIZE = 0x10
CARD_RECORD_SIZE = 0x24
CARD_TEXT_PTR_OFFSET = 0x18
CARD_TEXT_FIELDS = ("name", "effect", "quote")
DECK_RECORD_SIZE = 0x80
DECK_NAME_SIZE = 0x20
DEFAULT_CHARMAP_NAME = "XENOSAGA_KOR-JPN.json"


def load_charmap(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"charmap not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    table = data.get("replace-table", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path}: missing JSON object 'replace-table'")
    return dict(sorted(table.items(), key=lambda item: len(item[0]), reverse=True))


def apply_charmap(text: str, charmap: dict[str, str]) -> str:
    if not charmap:
        return text

    out: list[str] = []
    i = 0
    while i < len(text):
        for src, dst in charmap.items():
            if text.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def encode_text(text: str, charmap: dict[str, str], where: str) -> bytes:
    mapped = apply_charmap(text, charmap)
    try:
        return mapped.encode(ENCODING)
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{where}: cannot encode {exc.object[exc.start:exc.end]!r}; "
            "add it to the Korean-to-Japanese charmap or replace it"
        ) from exc


def decode_c_string(blob: bytes, offset: int, where: str) -> str:
    if offset < 0 or offset >= len(blob):
        raise ValueError(f"{where}: string offset 0x{offset:X} is outside file")
    end = blob.find(b"\0", offset)
    if end < 0:
        raise ValueError(f"{where}: string at 0x{offset:X} is not null-terminated")
    return blob[offset:end].decode(ENCODING)


def escape_txt(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def unescape_txt(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "n":
            out.append("\n")
        elif nxt == "r":
            out.append("\r")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "\\":
            out.append("\\")
        else:
            out.append(nxt)
        i += 2
    return "".join(out)


def read_tsv(path: Path, expected_fields: set[str]) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            raise ValueError(f"{path}:{lineno}: expected '<id> TAB <field> TAB <text>'")
        id_text, field, text = parts
        try:
            index = int(id_text, 10)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: invalid decimal id {id_text!r}") from exc
        if field not in expected_fields:
            raise ValueError(f"{path}:{lineno}: invalid field {field!r}")
        rows.setdefault(index, {})[field] = unescape_txt(text)
    return rows


def write_tsv(path: Path, header: list[str], rows: list[tuple[int, str, str]]) -> None:
    lines = [f"# {line}" for line in header]
    lines.extend(f"{index:03d}\t{field}\t{escape_txt(text)}" for index, field, text in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def card_pool_start(count: int) -> int:
    return CARD_HEADER_SIZE + count * CARD_RECORD_SIZE


def read_card_entries(path: Path) -> tuple[bytes, list[bytearray], list[dict[str, str]]]:
    blob = path.read_bytes()
    if len(blob) < CARD_HEADER_SIZE or blob[4:0x10].rstrip(b"\0") != CARD_MAGIC:
        raise ValueError(f"{path}: not a supported card.dat")

    count = struct.unpack_from("<I", blob, 0)[0]
    pool_start = card_pool_start(count)
    if pool_start > len(blob):
        raise ValueError(f"{path}: record table extends beyond file")

    header = blob[:CARD_HEADER_SIZE]
    records: list[bytearray] = []
    entries: list[dict[str, str]] = []
    for index in range(count):
        rec_offset = CARD_HEADER_SIZE + index * CARD_RECORD_SIZE
        record = bytearray(blob[rec_offset : rec_offset + CARD_RECORD_SIZE])
        pointers = struct.unpack_from("<III", record, CARD_TEXT_PTR_OFFSET)
        entry: dict[str, str] = {}
        for field, pointer in zip(CARD_TEXT_FIELDS, pointers):
            entry[field] = decode_c_string(blob, pointer, f"card {index:03d} {field}") if pointer else ""
        records.append(record)
        entries.append(entry)

    return header, records, entries


def extract_card(path: Path, txt_path: Path) -> None:
    _, _, entries = read_card_entries(path)
    rows = [
        (index, field, entry[field])
        for index, entry in enumerate(entries)
        for field in CARD_TEXT_FIELDS
    ]
    write_tsv(
        txt_path,
        [
            f"source={path.name}",
            "format: <zero-based card id> TAB <name|effect|quote> TAB <utf-8 text>",
            "edit only the third column; Korean text is mapped through XENOSAGA_KOR-JPN.json during rebuild",
            "escapes: \\n, \\r, \\t, \\\\",
        ],
        rows,
    )


def rebuild_card(src_path: Path, txt_path: Path, out_path: Path, charmap: dict[str, str]) -> None:
    header, records, entries = read_card_entries(src_path)
    updates = read_tsv(txt_path, set(CARD_TEXT_FIELDS))

    pool = bytearray()
    for index, record in enumerate(records):
        entry = entries[index].copy()
        entry.update(updates.get(index, {}))

        pointers: list[int] = []
        for field in CARD_TEXT_FIELDS:
            text = entry[field]
            if text:
                pointers.append(card_pool_start(len(records)) + len(pool))
                pool.extend(encode_text(text, charmap, f"card {index:03d} {field}"))
                pool.append(0)
            else:
                pointers.append(0)
        struct.pack_into("<III", record, CARD_TEXT_PTR_OFFSET, *pointers)

    out_path.write_bytes(header + b"".join(records) + pool)


def read_deck_entries(path: Path) -> tuple[list[bytearray], list[str]]:
    blob = path.read_bytes()
    if len(blob) % DECK_RECORD_SIZE:
        raise ValueError(f"{path}: size is not a multiple of 0x{DECK_RECORD_SIZE:X}")

    records: list[bytearray] = []
    names: list[str] = []
    for index in range(len(blob) // DECK_RECORD_SIZE):
        record = bytearray(blob[index * DECK_RECORD_SIZE : (index + 1) * DECK_RECORD_SIZE])
        raw_name = bytes(record[:DECK_NAME_SIZE]).split(b"\0", 1)[0]
        names.append(raw_name.decode(ENCODING))
        records.append(record)
    return records, names


def extract_deck(path: Path, txt_path: Path) -> None:
    _, names = read_deck_entries(path)
    write_tsv(
        txt_path,
        [
            f"source={path.name}",
            "format: <zero-based deck id> TAB name TAB <utf-8 text>",
            "deck names are fixed in-record fields; encoded text must fit in 31 bytes plus null",
            "escapes: \\n, \\r, \\t, \\\\",
        ],
        [(index, "name", name) for index, name in enumerate(names)],
    )


def rebuild_deck(src_path: Path, txt_path: Path, out_path: Path, charmap: dict[str, str]) -> None:
    records, names = read_deck_entries(src_path)
    updates = read_tsv(txt_path, {"name"})

    for index, record in enumerate(records):
        text = updates.get(index, {}).get("name", names[index])
        encoded = encode_text(text, charmap, f"deck {index:03d} name")
        if len(encoded) >= DECK_NAME_SIZE:
            raise ValueError(
                f"deck {index:03d} name is {len(encoded)} bytes after mapping; "
                f"deckdata.dat allows at most {DECK_NAME_SIZE - 1} bytes"
            )
        record[:DECK_NAME_SIZE] = encoded + b"\0" * (DECK_NAME_SIZE - len(encoded))

    out_path.write_bytes(b"".join(records))


def default_paths(base_dir: Path) -> dict[str, Path]:
    return {
        "card": base_dir / "card.dat",
        "deck": base_dir / "deckdata" / "deckdata.dat",
        "out_dir": base_dir / "text",
        "built_card": base_dir / "card.rebuilt.dat",
        "built_deck": base_dir / "deckdata" / "deckdata.rebuilt.dat",
        "charmap": base_dir / DEFAULT_CHARMAP_NAME,
    }


def cmd_extract(args: argparse.Namespace) -> None:
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    extract_card(args.card, out_dir / "card.txt")
    extract_deck(args.deck, out_dir / "deckdata.txt")
    print(f"extracted card text: {out_dir / 'card.txt'}")
    print(f"extracted deck text: {out_dir / 'deckdata.txt'}")


def cmd_rebuild(args: argparse.Namespace) -> None:
    charmap = load_charmap(args.charmap)
    rebuild_card(args.card, args.out_dir / "card.txt", args.out_card, charmap)
    rebuild_deck(args.deck, args.out_dir / "deckdata.txt", args.out_deck, charmap)
    print(f"rebuilt card: {args.out_card}")
    print(f"rebuilt deck: {args.out_deck}")


def build_parser() -> argparse.ArgumentParser:
    base_dir = Path(__file__).resolve().parent
    paths = default_paths(base_dir)

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract")
    extract.add_argument("--card", type=Path, default=paths["card"])
    extract.add_argument("--deck", type=Path, default=paths["deck"])
    extract.add_argument("--out-dir", type=Path, default=paths["out_dir"])
    extract.set_defaults(func=cmd_extract)

    rebuild = subparsers.add_parser("rebuild")
    rebuild.add_argument("--card", type=Path, default=paths["card"])
    rebuild.add_argument("--deck", type=Path, default=paths["deck"])
    rebuild.add_argument("--out-dir", type=Path, default=paths["out_dir"])
    rebuild.add_argument("--out-card", type=Path, default=paths["built_card"])
    rebuild.add_argument("--out-deck", type=Path, default=paths["built_deck"])
    rebuild.add_argument("--charmap", type=Path, default=paths["charmap"])
    rebuild.set_defaults(func=cmd_rebuild)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
