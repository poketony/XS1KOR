#!/usr/bin/env python3
"""Structure-aware OV11 text extractor and rebuilder.

OV11 is an ELF32/MIPS overlay.  Its display text is stored in two forms:

* ordinary NUL-terminated strings referenced by MIPS code or pointer tables;
* a contiguous gallery block whose records are
  ``0c <3-byte style> <EUC/control payload> 00 <zero padding>``.

The old blind EUC scan started after the 0x0c header and treated zero-valued
control operands as terminators.  This tool discovers the real records and
rebuilds one complete slot at a time.

Usage:
  python ov11_elf_strings.py extract OV11.OVL [OV11_elf_strings.txt]
  python ov11_elf_strings.py migrate OV11.OVL OV11_strings_KOR.txt [OV11_elf_strings_KOR.txt]
  python ov11_elf_strings.py rebuild OV11.OVL OV11_elf_strings_KOR.txt [OV11_patched.OVL]
  python ov11_elf_strings.py verify OV11.OVL OV11_elf_strings_KOR.txt
"""

from __future__ import annotations

import bisect
import os
import re
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, replace

try:
    from . import euc_scan
except ImportError:
    import euc_scan


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


MAX_RECORD_BYTES = 0x400
MIN_GALLERY_STREAMS = 10
SOURCE_DISPLAY_RE = re.compile(
    r"[\u3040-\u30ff\uff01-\uff60\u3000-\u303f]"
)


@dataclass(frozen=True)
class Record:
    kind: str
    offset: int
    name: str
    raw: bytes
    trailing: int

    @property
    def end(self) -> int:
        return self.offset + len(self.raw)

    @property
    def capacity(self) -> int:
        return len(self.raw) + self.trailing

    @property
    def slot_end(self) -> int:
        return self.end + 1 + self.trailing


@dataclass(frozen=True)
class FileEdit:
    kind: str
    offset: int
    name: str
    declared_length: int
    declared_slack: int
    text: str
    line_number: int


def _zstr(data: bytes, offset: int) -> str:
    end = data.find(b"\x00", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("ascii", errors="replace")


def read_elf(path: str):
    with open(path, "rb") as stream:
        data = stream.read()
    if data[:6] != b"\x7fELF\x01\x01":
        raise ValueError("expected little-endian ELF32")

    values = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    keys = (
        "e_type", "e_machine", "e_version", "e_entry", "e_phoff",
        "e_shoff", "e_flags", "e_ehsize", "e_phentsize", "e_phnum",
        "e_shentsize", "e_shnum", "e_shstrndx",
    )
    elf = dict(zip(keys, values))
    if elf["e_machine"] != 8:
        raise ValueError("expected MIPS ELF")

    phdrs = []
    for index in range(elf["e_phnum"]):
        offset = elf["e_phoff"] + index * elf["e_phentsize"]
        values = struct.unpack_from("<IIIIIIII", data, offset)
        phdrs.append(dict(zip(
            (
                "p_type", "p_offset", "p_vaddr", "p_paddr", "p_filesz",
                "p_memsz", "p_flags", "p_align",
            ),
            values,
        )))

    shdrs = []
    for index in range(elf["e_shnum"]):
        offset = elf["e_shoff"] + index * elf["e_shentsize"]
        values = struct.unpack_from("<IIIIIIIIII", data, offset)
        shdrs.append(dict(zip(
            (
                "sh_name", "sh_type", "sh_flags", "sh_addr", "sh_offset",
                "sh_size", "sh_link", "sh_info", "sh_addralign",
                "sh_entsize",
            ),
            values,
        )))

    shstr = shdrs[elf["e_shstrndx"]]
    names = data[shstr["sh_offset"]:shstr["sh_offset"] + shstr["sh_size"]]
    for section in shdrs:
        section["name"] = _zstr(names, section["sh_name"]) if section["sh_name"] else ""
    return data, elf, phdrs, shdrs


def vaddr_to_offset(phdrs, vaddr: int):
    for segment in phdrs:
        if segment["p_type"] != 1:
            continue
        start = segment["p_vaddr"]
        end = start + segment["p_filesz"]
        if start <= vaddr < end:
            return segment["p_offset"] + vaddr - start
    return None


def read_symbols(data: bytes, phdrs, shdrs):
    symtab = next((item for item in shdrs if item["name"] == ".symtab"), None)
    strtab = next((item for item in shdrs if item["name"] == ".strtab"), None)
    if symtab is None or strtab is None or not symtab["sh_entsize"]:
        raise ValueError("ELF symbol/string table not found")

    names = data[strtab["sh_offset"]:strtab["sh_offset"] + strtab["sh_size"]]
    symbols = []
    count = symtab["sh_size"] // symtab["sh_entsize"]
    for index in range(count):
        offset = symtab["sh_offset"] + index * symtab["sh_entsize"]
        st_name, st_value, st_size, st_info, _other, st_shndx = (
            struct.unpack_from("<IIIBBH", data, offset)
        )
        if not st_value or not st_shndx:
            continue
        file_offset = vaddr_to_offset(phdrs, st_value)
        if file_offset is None:
            continue
        symbols.append({
            "name": _zstr(names, st_name),
            "vaddr": st_value,
            "offset": file_offset,
            "size": st_size,
            "info": st_info,
            "shndx": st_shndx,
        })
    return sorted(symbols, key=lambda item: (item["offset"], item["name"]))


def _function_reference_offsets(data: bytes, phdrs, symbols):
    """Find addresses constructed by LUI followed by ADDIU/ORI.

    OV11 has no relocation section, but its code uses the normal MIPS
    ``lui`` + low-half sequence for static strings.  Restricting the scan to
    symbolized functions prevents data tables from being mistaken for code.
    """
    references = defaultdict(set)
    functions = [item for item in symbols if (item["info"] & 0x0f) == 2 and item["size"]]
    for function in functions:
        start = function["offset"]
        end = min(len(data), start + function["size"])
        for offset in range(start, end - 3, 4):
            instruction = struct.unpack_from("<I", data, offset)[0]
            if instruction >> 26 != 0x0f:
                continue
            register = (instruction >> 16) & 0x1f
            high = instruction & 0xffff
            for next_offset in range(offset + 4, min(offset + 36, end), 4):
                following = struct.unpack_from("<I", data, next_offset)[0]
                opcode = following >> 26
                source = (following >> 21) & 0x1f
                target = (following >> 16) & 0x1f
                if source != register or target != register or opcode not in (0x09, 0x0d):
                    continue
                low = following & 0xffff
                if opcode == 0x09 and low & 0x8000:
                    low -= 0x10000
                vaddr = (high << 16) + low
                file_offset = vaddr_to_offset(phdrs, vaddr)
                if file_offset is not None:
                    references[file_offset].add(function["name"] or f"func_{start:08x}")
                break
    return references


def _pointer_reference_offsets(data: bytes, phdrs, start: int, end: int):
    references = defaultdict(set)
    aligned = (start + 3) & ~3
    for offset in range(aligned, end - 3, 4):
        value = struct.unpack_from("<I", data, offset)[0]
        target = vaddr_to_offset(phdrs, value)
        if target is not None:
            references[target].add(f"ptr_{offset:08x}")
    return references


def _source_display_score(text: str) -> int:
    return len(SOURCE_DISPLAY_RE.findall(text))


def _stream_record(data: bytes, offset: int, end: int):
    if data[offset:offset + 1] != b"\x0c":
        return None
    parsed = euc_scan.parse_control_aware_string(data, offset, end)
    if parsed is None:
        return None
    raw_length = parsed.terminator - offset
    if not 5 <= raw_length <= MAX_RECORD_BYTES:
        return None
    if not parsed.control_ranges or parsed.control_ranges[0] != (offset, offset + 4):
        return None
    payload = data[offset + 4:parsed.terminator]
    if _source_display_score(euc_scan.raw_to_display(payload)) < 1:
        return None
    trailing = euc_scan.trailing_nulls_after(data, parsed.terminator, end)
    return Record("S", offset, "", bytes(data[offset:parsed.terminator]), trailing)


def find_gallery_records(data: bytes, start: int, end: int):
    best = []
    for offset in range(start, end):
        if data[offset] != 0x0c:
            continue
        records = []
        cursor = offset
        while cursor < end:
            record = _stream_record(data, cursor, end)
            if record is None:
                break
            records.append(record)
            cursor = record.slot_end
            if cursor >= end or data[cursor] != 0x0c:
                break
        if len(records) > len(best):
            best = records

    if len(best) < MIN_GALLERY_STREAMS:
        raise ValueError("could not identify the contiguous OV11 gallery stream block")

    role_counts = defaultdict(int)
    named = []
    for record in best:
        header = record.raw[1:4]
        if header == b"\xde\xb6\xff":
            role = "ViewerDescription"
        elif header == b"\xbb\xff\xe5":
            role = "ViewerTitle"
        else:
            role = "ViewerStream"
        index = role_counts[role]
        role_counts[role] += 1
        named.append(replace(record, name=f"{role}[{index:02d}]"))
    return named


def find_plain_records(data: bytes, phdrs, symbols, data_start: int, data_end: int, excluded):
    references = _function_reference_offsets(data, phdrs, symbols)
    pointer_references = _pointer_reference_offsets(data, phdrs, data_start, data_end)
    for target, sources in pointer_references.items():
        references[target].update(sources)

    records = []
    for offset in sorted(references):
        if not data_start <= offset < data_end or data[offset] < 0x20:
            continue
        if any(low <= offset < high for low, high in excluded):
            continue
        parsed = euc_scan.parse_control_aware_string(data, offset, data_end)
        if parsed is None:
            continue
        raw_length = parsed.terminator - offset
        if not 2 <= raw_length <= MAX_RECORD_BYTES:
            continue
        raw = bytes(data[offset:parsed.terminator])
        if _source_display_score(euc_scan.raw_to_display(raw)) < 2:
            continue
        trailing = euc_scan.trailing_nulls_after(data, parsed.terminator, data_end)
        records.append(Record("N", offset, "", raw, trailing))

    records.sort(key=lambda item: item.offset)
    non_overlapping = []
    for record in records:
        if non_overlapping and record.offset < non_overlapping[-1].slot_end:
            continue
        non_overlapping.append(record)
    return [
        replace(record, name=f"Plain[{index:02d}]")
        for index, record in enumerate(non_overlapping)
    ]


def discover_records(path: str):
    data, _elf, phdrs, shdrs = read_elf(path)
    symbols = read_symbols(data, phdrs, shdrs)
    load = next((item for item in phdrs if item["p_type"] == 1), None)
    if load is None:
        raise ValueError("loadable ELF segment not found")
    data_end = load["p_offset"] + load["p_filesz"]
    function_ends = [
        item["offset"] + item["size"]
        for item in symbols
        if (item["info"] & 0x0f) == 2 and item["size"]
    ]
    if not function_ends:
        raise ValueError("symbolized code range not found")
    data_start = max(function_ends)

    gallery = find_gallery_records(data, data_start, data_end)
    following_symbols = sorted({
        item["offset"] for item in symbols
        if gallery[-1].end < item["offset"] <= data_end
    })
    if following_symbols:
        boundary = following_symbols[0]
        trailing = boundary - gallery[-1].end - 1
        if trailing >= 0:
            gallery[-1] = replace(gallery[-1], trailing=trailing)
    gallery_range = [(gallery[0].offset, gallery[-1].slot_end)]
    plain = find_plain_records(
        data, phdrs, symbols, data_start, data_end, gallery_range,
    )
    records = sorted(plain + gallery, key=lambda item: item.offset)
    for previous, current in zip(records, records[1:]):
        if previous.slot_end > current.offset:
            raise ValueError(
                f"overlapping records at 0x{previous.offset:08x} and 0x{current.offset:08x}"
            )
    return data, records


def stream_to_display(raw: bytes) -> str:
    if len(raw) < 4 or raw[0] != 0x0c:
        raise ValueError("stream does not begin with a complete CTRL0C packet")
    header = " ".join(f"{value:02x}" for value in raw[1:4])
    return f"{{CTRL0C:{header}|{euc_scan.raw_to_display(raw[4:])}}}"


def record_to_display(record: Record) -> str:
    if record.kind == "S":
        return stream_to_display(record.raw)
    return euc_scan.raw_to_display(record.raw)


def encode_stream_display(text: str, table) -> bytes:
    if not text.startswith("{CTRL0C:") or not text.endswith("}"):
        raise ValueError("stream must use {CTRL0C:hh hh hh|payload}")
    body = text[len("{CTRL0C:"):-1]
    if "|" not in body:
        raise ValueError("CTRL0C stream is missing the payload separator")
    header_text, payload = body.split("|", 1)
    try:
        header = bytes.fromhex(header_text)
    except ValueError as error:
        raise ValueError(f"invalid CTRL0C header: {error}") from error
    if len(header) != 3:
        raise ValueError("CTRL0C header must contain exactly three bytes")
    return b"\x0c" + header + euc_scan.encode_display(payload, table)


def encode_record_text(record: Record, text: str, table) -> bytes:
    if record.kind == "S":
        return encode_stream_display(text, table)
    return euc_scan.encode_display(text, table)


def write_record_file(
    path: str,
    source_name: str,
    records,
    raw_overrides=None,
    display_overrides=None,
):
    raw_overrides = raw_overrides or {}
    display_overrides = display_overrides or {}
    lines = [
        f"# OV11 structure-aware text dump: {source_name}",
        "# format: <kind>|<offset>|<record>|<orig>/<slack>|<text>",
        "#   N = code/pointer-referenced NUL string",
        "#   S = complete gallery stream, including its CTRL0C style packet",
        "# Edit payload text only; control packets are verified during rebuild.",
        "",
    ]
    for record in records:
        raw = raw_overrides.get(record.offset, record.raw)
        display_record = replace(record, raw=raw)
        display = display_overrides.get(
            record.offset, record_to_display(display_record),
        )
        lines.append(
            f"{record.kind}|{record.offset:08x}|{record.name}|"
            f"{len(record.raw)}/{record.trailing}|{display}"
        )
    with open(path, "w", encoding="utf-8-sig", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")


def parse_record_file(path: str):
    edits = {}
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            line = raw_line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 4)
            if len(parts) != 5:
                raise ValueError(f"line {line_number}: expected five fields")
            kind, offset_text, name, size_text, text = parts
            if kind not in ("N", "S"):
                raise ValueError(f"line {line_number}: invalid record kind {kind!r}")
            try:
                offset = int(offset_text, 16)
                length_text, slack_text = size_text.split("/", 1)
                length = int(length_text, 10)
                slack = int(slack_text, 10)
            except ValueError as error:
                raise ValueError(f"line {line_number}: invalid offset or size") from error
            if offset in edits:
                raise ValueError(f"line {line_number}: duplicate offset 0x{offset:08x}")
            edits[offset] = FileEdit(
                kind, offset, name, length, slack, text, line_number,
            )
    return edits


def validate_encoded_record(record: Record, encoded: bytes):
    if len(encoded) > record.capacity:
        raise ValueError(
            f"0x{record.offset:08x} {record.name}: needs {len(encoded)}B, "
            f"capacity is {record.capacity}B"
        )
    original = euc_scan.parse_control_aware_string(record.raw + b"\x00", 0)
    rebuilt = euc_scan.parse_control_aware_string(encoded + b"\x00", 0)
    if original is None or rebuilt is None or rebuilt.terminator != len(encoded):
        raise ValueError(
            f"0x{record.offset:08x} {record.name}: incomplete EUC/control stream or early NUL"
        )
    if original.control_packets != rebuilt.control_packets:
        raise ValueError(
            f"0x{record.offset:08x} {record.name}: control packets changed "
            f"({euc_scan.format_control_packets(original.control_packets)} -> "
            f"{euc_scan.format_control_packets(rebuilt.control_packets)})"
        )


def prepare_rebuild(bin_path: str, txt_path: str):
    data, records = discover_records(bin_path)
    table = euc_scan.load_replace_table(bin_path)
    edits = parse_record_file(txt_path)
    by_offset = {record.offset: record for record in records}
    encoded_records = {}
    errors = []

    for offset, edit in edits.items():
        record = by_offset.get(offset)
        if record is None:
            errors.append(f"line {edit.line_number}: unknown record 0x{offset:08x}")
            continue
        if edit.kind != record.kind:
            errors.append(
                f"line {edit.line_number}: kind mismatch at 0x{offset:08x} "
                f"({edit.kind} != {record.kind})"
            )
            continue
        if edit.declared_length != len(record.raw) or edit.declared_slack != record.trailing:
            errors.append(
                f"line {edit.line_number}: source slot changed at 0x{offset:08x}; "
                f"file={edit.declared_length}/{edit.declared_slack}, "
                f"source={len(record.raw)}/{record.trailing}"
            )
            continue
        try:
            encoded = encode_record_text(record, edit.text, table)
            validate_encoded_record(record, encoded)
        except Exception as error:
            errors.append(f"line {edit.line_number}: {error}")
            continue
        encoded_records[offset] = encoded

    if errors:
        raise ValueError("\n".join(errors))
    return bytearray(data), records, encoded_records


def rebuild(bin_path: str, txt_path: str, out_path: str | None = None, write=True):
    data, records, encoded_records = prepare_rebuild(bin_path, txt_path)
    patched = 0
    for record in records:
        encoded = encoded_records.get(record.offset)
        if encoded is None or encoded == record.raw:
            continue
        data[record.offset:record.slot_end] = b"\x00" * (record.slot_end - record.offset)
        data[record.offset:record.offset + len(encoded)] = encoded
        patched += 1

    if write:
        if out_path is None:
            base, extension = os.path.splitext(bin_path)
            out_path = base + "_patched" + extension
        with open(out_path, "wb") as stream:
            stream.write(data)
        print(f"[OK] patched={patched}, records={len(encoded_records)} -> {out_path}")
    return bytes(data), patched, len(encoded_records)


def _record_for_legacy_edit(records, starts, edit):
    index = bisect.bisect_right(starts, edit.offset) - 1
    if index < 0:
        return None
    record = records[index]
    span = edit.declared_length
    if span is None or span <= 0:
        return None
    if record.offset <= edit.offset and edit.offset + span <= record.end:
        return record
    return None


def _escape_literal_controls(text: str) -> str:
    out = []
    for character in text:
        value = ord(character)
        if character == "\n":
            out.append("\\n")
        elif character == "\r":
            out.append("\\r")
        elif value < 0x20 or value in (0x7f, 0x80):
            out.append(f"\\x{value:02x}")
        else:
            out.append(character)
    return "".join(out)


def _apply_legacy_edits(record: Record, edits, table):
    original = record.raw
    parsed = euc_scan.parse_control_aware_string(original + b"\x00", 0)
    if parsed is None:
        raise ValueError(f"cannot parse source record 0x{record.offset:08x}")

    cursor = record.offset
    rebuilt = bytearray()
    changed = 0
    equivalent = 0
    changed_edits = []
    for edit in sorted(edits, key=lambda item: item.offset):
        span = edit.declared_length
        relative_start = edit.offset - record.offset
        relative_end = relative_start + span
        source_span = original[relative_start:relative_end]
        encoded = euc_scan.encode_display(edit.text, table)
        if encoded == source_span:
            equivalent += 1
            continue

        for packet_start, packet_end in parsed.control_ranges:
            if packet_start < relative_start < packet_end:
                raise ValueError(
                    f"legacy line {edit.line_number} starts inside a control packet "
                    f"at 0x{edit.offset:08x}"
                )

        for packet_start, packet_end in parsed.control_ranges:
            if packet_start < relative_end < packet_end:
                command = original[packet_start]
                translated_start = encoded.rfind(bytes((command,)))
                if translated_start < 0:
                    raise ValueError(
                        f"legacy line {edit.line_number} ends inside control 0x{command:02x} "
                        "but the translated fragment omits that command"
                    )
                source_prefix = relative_end - packet_start
                translated_prefix = len(encoded) - translated_start
                if translated_prefix > source_prefix:
                    encoded = encoded[:translated_start + source_prefix]
                elif translated_prefix < source_prefix:
                    encoded += original[
                        packet_start + translated_prefix:relative_end
                    ]
                break

        if edit.offset < cursor:
            raise ValueError(f"overlapping legacy edit at 0x{edit.offset:08x}")
        rebuilt.extend(original[cursor - record.offset:relative_start])
        rebuilt.extend(encoded)
        cursor = edit.offset + span
        changed += 1
        changed_edits.append(edit)

    rebuilt.extend(original[cursor - record.offset:])
    rebuilt = bytes(rebuilt)
    validate_encoded_record(record, rebuilt)

    payload_start = record.offset + (4 if record.kind == "S" else 0)
    display_cursor = payload_start
    display_parts = []
    for edit in changed_edits:
        if edit.offset < payload_start:
            raise ValueError(
                f"legacy line {edit.line_number} changes the CTRL0C header"
            )
        display_parts.append(euc_scan.raw_to_display(
            original[
                display_cursor - record.offset:edit.offset - record.offset
            ]
        ))
        display_parts.append(_escape_literal_controls(edit.text))
        display_cursor = edit.offset + edit.declared_length
    display_parts.append(euc_scan.raw_to_display(
        original[display_cursor - record.offset:]
    ))
    payload_display = "".join(display_parts)
    if record.kind == "S":
        header = " ".join(f"{value:02x}" for value in original[1:4])
        display = f"{{CTRL0C:{header}|{payload_display}}}"
    else:
        display = payload_display

    if encode_record_text(record, display, table) != rebuilt:
        raise ValueError(
            "legacy display text cannot represent the rebuilt bytes exactly"
        )
    return rebuilt, display, changed, equivalent


def migrate(bin_path: str, legacy_path: str, out_path: str | None = None):
    _data, records = discover_records(bin_path)
    table = euc_scan.load_replace_table(bin_path)
    legacy, malformed = euc_scan.parse_translation_edits(legacy_path)
    starts = [record.offset for record in records]
    grouped = defaultdict(list)
    unmatched = []
    for edit in legacy.values():
        record = _record_for_legacy_edit(records, starts, edit)
        if record is None:
            unmatched.append(edit)
        else:
            grouped[record.offset].append(edit)

    overrides = {}
    display_overrides = {}
    changed = equivalent = 0
    errors = []
    for record in records:
        edits = grouped.get(record.offset)
        if not edits:
            continue
        try:
            raw, display, changed_count, equivalent_count = _apply_legacy_edits(
                record, edits, table,
            )
        except Exception as error:
            errors.append(f"0x{record.offset:08x} {record.name}: {error}")
            continue
        overrides[record.offset] = raw
        display_overrides[record.offset] = display
        changed += changed_count
        equivalent += equivalent_count

    if errors:
        raise ValueError("\n".join(errors))
    if out_path is None:
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(bin_path)),
            "OV11_elf_strings_KOR.txt",
        )
    write_record_file(
        out_path,
        os.path.basename(bin_path),
        records,
        overrides,
        display_overrides,
    )
    print(
        f"[OK] migrated changed={changed}, equivalent={equivalent}, "
        f"unmatched={len(unmatched)}, malformed={malformed} -> {out_path}"
    )
    for edit in unmatched:
        print(
            f"[WARN] legacy line {edit.line_number} 0x{edit.offset:08x}: "
            "not part of a referenced string or gallery stream"
        )
    return out_path, unmatched


def extract(bin_path: str, out_path: str | None = None):
    _data, records = discover_records(bin_path)
    if out_path is None:
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(bin_path)),
            "OV11_elf_strings.txt",
        )
    write_record_file(out_path, os.path.basename(bin_path), records)
    plain = sum(record.kind == "N" for record in records)
    streams = len(records) - plain
    print(f"[OK] plain={plain}, streams={streams} -> {out_path}")
    return out_path


def verify(bin_path: str, txt_path: str):
    _data, records, encoded = prepare_rebuild(bin_path, txt_path)
    changed = sum(
        encoded.get(record.offset, record.raw) != record.raw
        for record in records
        if record.offset in encoded
    )
    print(f"[OK] verified records={len(encoded)}, changed={changed}")


def usage():
    print(__doc__)
    raise SystemExit(1)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 3:
        usage()
    command = argv[1].lower()
    try:
        if command == "extract" and len(argv) in (3, 4):
            extract(argv[2], argv[3] if len(argv) == 4 else None)
        elif command == "migrate" and len(argv) in (4, 5):
            migrate(argv[2], argv[3], argv[4] if len(argv) == 5 else None)
        elif command == "rebuild" and len(argv) in (4, 5):
            rebuild(argv[2], argv[3], argv[4] if len(argv) == 5 else None)
        elif command == "verify" and len(argv) == 4:
            verify(argv[2], argv[3])
        else:
            usage()
    except Exception as error:
        print(f"[ERROR] {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
