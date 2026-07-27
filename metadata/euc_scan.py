#!/usr/bin/env python3
"""
EUC-JIS-2004 문자열 범용 추출/리빌드 툴
대상: PS2 ELF/OVL 등 임의 바이너리

추출 방식:
  1) null-segment 전체가 EUC 디코딩 성공 + 일본어 바이트 포함 -> 전체 추출
  2) 디코딩 실패 segment -> 내부에서 일본어 연속 run 추출

txt 포맷: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>
  slack = trailing null 개수 (null terminator 제외) = 여유 공간
  max   = orig + slack = 번역 후 인코딩 결과 상한

제어 코드: \\n = 0x0a,  \\r = 0x0d

사용법:
  추출: python3 euc_scan.py extract <file> [start_offset]
        start_offset 생략 시 파일 전체 스캔 (hex: 0x1234 또는 dec: 4660)
        -> <file>_strings.txt 생성

  리빌드: python3 euc_scan.py rebuild <file> <txt>
          -> <file>_patched.<ext> 생성
          -> 같은 폴더의 XENOSAGA_KOR-JPN.json 자동 인식, 한글->한자 치환
"""

import sys, os, json
from bisect import bisect_right
from dataclasses import dataclass

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

ENCODING = 'euc_jis_2004'
CTRL_OK  = frozenset([0x01,0x02,0x03,0x08,0x09,0x0a,0x0c,0x0d,0x1f,0x19])

# xglFontGetSPcodeSize (SLPS VA 0x00218d08) indexes this runtime table.
# Each value is the number of operand bytes following control code 0x00-0x1f.
# 0x08 and 0x15 are variable-sized and are handled below.
SPCODE_OPERAND_COUNTS = (
    0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x03, 0x00,
    0xff, 0x00, 0x00, 0x00, 0x03, 0x01, 0x05, 0x03,
    0x05, 0x06, 0x02, 0x02, 0x00, 0xff, 0x00, 0x07,
    0x01, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,
)
SPCODE_08_FLAG_WIDTHS = (0x02, 0x02, 0x03, 0x02, 0x02, 0x00, 0x00, 0x00)
# eMessageNextGyou/eMessageCat handle 0x1e before the xgl layer and always
# consume its following icon/style ID (SLPS VA 0x00277048 / 0x00277f58).
SOURCE_CONTROL_OPERAND_OVERRIDES = {0x1e: 0x01}


@dataclass(frozen=True)
class ParsedString:
    terminator: int
    embedded_zero_offsets: tuple
    control_packets: tuple
    control_ranges: tuple


@dataclass(frozen=True)
class StringSlot:
    offset: int
    raw: bytes
    trailing: int

    @property
    def end(self):
        return self.offset + len(self.raw)


@dataclass(frozen=True)
class TranslationEdit:
    offset: int
    text: str
    declared_length: object
    line_number: int


@dataclass
class GroupedRebuildStats:
    patched_groups: int = 0
    patched_records: int = 0
    unchanged_groups: int = 0
    missing: int = 0
    overflow: int = 0
    invalid: int = 0
    control_warnings: int = 0
    polluted: int = 0

# OV10 text lives in a dense card-help block. A blind scan from 0x0 catches
# executable/table bytes that only coincidentally look like EUC-JIS-2004.
FILE_SCAN_RANGES = {
    'OV10.OVL': [(0x42830, 0x4fd6a)],
}


# ── 공통 ──────────────────────────────────────────────────────────────────────

def load_replace_table(bin_path):
    folder    = os.path.dirname(os.path.abspath(bin_path))
    json_path = os.path.join(folder, 'XENOSAGA_KOR-JPN.json')
    if not os.path.exists(json_path):
        print(f'[INFO] {json_path} 없음 - 한글 치환 없이 진행')
        return {}
    with open(json_path, encoding='utf-8-sig') as f:
        d = json.load(f)
    table = d.get('replace-table', {})
    print(f'[INFO] replace-table 로드: {len(table)}개 ({json_path})')
    return table


def apply_replace_table(text, table):
    if not table:
        return text
    return ''.join(table.get(ch, ch) for ch in text)


def is_polluted_legacy_text(text):
    # OV10's old blind scan sometimes decoded packet payloads as these glyphs.
    return any(fragment in text for fragment in ('寸咤', '釘味', '釘達'))


def raw_to_display(raw):
    out = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x0a:
            out.append('\\n')
            i += 1
            continue
        if b == 0x0d:
            out.append('\\r')
            operand_count = source_control_operand_count(raw, i, len(raw))
            if operand_count is None:
                i += 1
            else:
                packet_end = i + 1 + operand_count
                out.extend(f'\\x{value:02x}' for value in raw[i + 1:packet_end])
                i = packet_end
            continue
        if b < 0x20:
            operand_count = source_control_operand_count(raw, i, len(raw))
            if operand_count is not None:
                packet_end = i + 1 + operand_count
                out.extend(f'\\x{value:02x}' for value in raw[i:packet_end])
                i = packet_end
                continue
            out.append(f'\\x{b:02x}')
            i += 1
            continue
        if b == 0x7f or b == 0x80:
            out.append(f'\\x{b:02x}')
            i += 1
            continue
        if b < 0x80:
            out.append(chr(b))
            i += 1
            continue

        decoded = None
        if b == 0x8f:
            sizes = (3,)
        elif b == 0x8e or 0xa1 <= b <= 0xfe:
            sizes = (2,)
        else:
            sizes = ()

        for size in sizes:
            chunk = raw[i:i + size]
            if len(chunk) != size:
                continue
            try:
                decoded = chunk.decode(ENCODING)
                i += size
                break
            except UnicodeDecodeError:
                pass

        if decoded is None:
            out.append(f'\\x{b:02x}')
            i += 1
        else:
            out.append(decoded)

    return ''.join(out)


def encode_display(s, table):
    out = bytearray()
    literal = []
    hexdigits = '0123456789abcdefABCDEF'

    def flush_literal():
        if not literal:
            return
        converted = apply_replace_table(''.join(literal), table)
        out.extend(converted.encode(ENCODING))
        literal.clear()

    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            esc = s[i + 1]
            if esc == 'n':
                flush_literal()
                out.append(0x0a)
                i += 2
                continue
            if esc == 'r':
                flush_literal()
                out.append(0x0d)
                i += 2
                continue
            if esc in ('x', 'X') and i + 3 < len(s):
                hx = s[i + 2:i + 4]
                if all(c in hexdigits for c in hx):
                    flush_literal()
                    out.append(int(hx, 16))
                    i += 4
                    continue

        literal.append(ch)
        i += 1

    flush_literal()
    return bytes(out)


def spcode_operand_count(data, offset, end=None):
    """Return the operand size used by xglFontGetSPcodeSize."""
    if end is None:
        end = len(data)
    if offset < 0 or offset >= end:
        return None

    command = data[offset]
    if command >= 0x20:
        return None

    count = SPCODE_OPERAND_COUNTS[command]
    if command == 0x08:
        if offset + 1 >= end:
            return None
        flags = data[offset + 1]
        count = 1 + sum(
            width for bit, width in enumerate(SPCODE_08_FLAG_WIDTHS)
            if flags & (1 << bit)
        )
    elif command == 0x15:
        if offset + 1 >= end:
            return None
        count = data[offset + 1] + 2
    elif count == 0xff:
        return None

    if offset + 1 + count > end:
        return None
    return count


def source_control_operand_count(data, offset, end=None):
    """Return source-message operands, including eMessage-only controls."""
    if end is None:
        end = len(data)
    if offset < 0 or offset >= end:
        return None
    override = SOURCE_CONTROL_OPERAND_OVERRIDES.get(data[offset])
    if override is None:
        return spcode_operand_count(data, offset, end)
    if offset + 1 + override > end:
        return None
    return override


def _euc_character_size(data, offset, end):
    lead = data[offset]
    if lead == 0x8f:
        size = 3
    elif lead == 0x8e or 0xa1 <= lead <= 0xfe:
        size = 2
    else:
        return None

    if offset + size > end:
        return None
    try:
        data[offset:offset + size].decode(ENCODING)
    except UnicodeDecodeError:
        return None
    return size


def parse_control_aware_string(data, start, end=None):
    """Parse one runtime string and return its real NUL terminator.

    A zero byte inside a control-code operand is data, not a terminator. This
    follows the same operand sizes as xglFontGetSPcodeSize rather than guessing
    from neighboring text.
    """
    if end is None:
        end = len(data)
    if not 0 <= start < end:
        return None

    offset = start
    embedded_zeros = []
    control_packets = []
    control_ranges = []
    while offset < end:
        value = data[offset]
        if value == 0:
            return ParsedString(
                offset,
                tuple(embedded_zeros),
                tuple(control_packets),
                tuple(control_ranges),
            )

        if value < 0x20:
            operand_count = source_control_operand_count(data, offset, end)
            if operand_count is None:
                return None
            packet_end = offset + 1 + operand_count
            packet = bytes(data[offset:packet_end])
            control_ranges.append((offset, packet_end))
            if value not in (0x0a, 0x0d):
                control_packets.append(packet)
            embedded_zeros.extend(
                index
                for index in range(offset + 1, packet_end)
                if data[index] == 0
            )
            offset = packet_end
            continue

        if 0x20 <= value <= 0x7e:
            offset += 1
            continue

        size = _euc_character_size(data, offset, end)
        if size is None:
            return None
        offset += size

    return None


def trailing_nulls_after(data, terminator, end=None):
    if end is None:
        end = len(data)
    scan = terminator + 1
    while scan < end and data[scan] == 0:
        scan += 1
    return scan - terminator - 1


def parse_translation_edits(txt_path):
    edits = {}
    skipped = 0
    with open(txt_path, encoding='utf-8-sig', newline='') as stream:
        for line_number, raw_line in enumerate(stream, 1):
            line = raw_line.rstrip('\r\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                offset_text, info, text = parts
            elif len(parts) == 2:
                offset_text, text = parts
                info = ''
            else:
                skipped += 1
                continue
            try:
                offset = int(offset_text.strip(), 16)
            except ValueError:
                print(f'[WARN] line {line_number}: invalid offset {offset_text!r}')
                skipped += 1
                continue

            declared_length = None
            if info:
                try:
                    declared_length = int(info.split('/', 1)[0].strip(), 10)
                except ValueError:
                    pass
            edits[offset] = TranslationEdit(
                offset,
                text,
                declared_length,
                line_number,
            )
    return edits, skipped


def file_scan_ranges(bin_path, data_len, start=0):
    if start:
        return [(start, data_len, False)]

    ranges = FILE_SCAN_RANGES.get(os.path.basename(bin_path).upper())
    if not ranges:
        return [(0, data_len, False)]

    return [(lo, min(hi, data_len), True) for lo, hi in ranges]


def parse_scan_ranges(txt_path, data_len):
    ranges = []
    with open(txt_path, encoding='utf-8', newline='') as f:
        for line in f:
            line = line.strip()
            if not line.startswith('# scan ranges:'):
                continue
            spec = line.split(':', 1)[1]
            for item in spec.split(','):
                item = item.strip()
                if not item or '-' not in item:
                    continue
                lo_s, hi_s = item.split('-', 1)
                try:
                    lo = int(lo_s.strip(), 16)
                    hi = int(hi_s.strip(), 16)
                except ValueError:
                    continue
                if lo < hi:
                    ranges.append((lo, min(hi, data_len), True))
            break
    return ranges


def format_scan_ranges(ranges):
    return ','.join(f'0x{lo:x}-0x{hi:x}' for lo, hi, _preserve in ranges)


def _jp_runs(seg, base_off):
    """segment 내에서 일본어 포함 EUC 연속 run yield: (abs_offset, raw_bytes)"""
    slen = len(seg)
    i = 0
    while i < slen:
        b = seg[i]
        if b >= 0xa1 or b in (0x8e, 0x8f):
            run_start = i
            run = bytearray()
            j = i
            while j < slen:
                b2 = seg[j]
                if 0xa1 <= b2 <= 0xfe and j+1 < slen and 0xa1 <= seg[j+1] <= 0xfe:
                    run += seg[j:j+2]; j += 2
                elif b2 == 0x8e and j+1 < slen and 0xa1 <= seg[j+1] <= 0xdf:
                    run += seg[j:j+2]; j += 2
                elif 0x20 <= b2 <= 0x7e or b2 in CTRL_OK:
                    run.append(b2); j += 1
                else:
                    break
            i = j if j > i else i + 1
            if len(run) >= 4 and any(x >= 0xa1 for x in run):
                yield (base_off + run_start, bytes(run))
        else:
            i += 1


def leading_jp_run(seg):
    for run_off, run_raw in _jp_runs(seg, 0):
        if run_off == 0:
            return run_raw
        break
    return None


def append_legacy_suffix(orig_raw, new_raw):
    """Keep bytes that older partial-run dumps could not represent."""
    if new_raw == orig_raw:
        return new_raw, 0

    run = leading_jp_run(orig_raw)
    if not run or len(run) >= len(orig_raw):
        return new_raw, 0

    suffix = orig_raw[len(run):]
    if new_raw.endswith(suffix):
        return new_raw, 0
    return new_raw + suffix, len(suffix)


def iter_legacy_strings(data, start=0, end=None, preserve_segments=False):
    """
    start~EOF 구간 스캔, yield: (offset, raw_bytes, trailing_nulls)
    """
    if end is None:
        end = len(data)
    pos = start
    seen = set()

    while pos < end:
        if data[pos] == 0:
            pos += 1
            continue

        np = data.find(b'\x00', pos, end)
        if np == -1:
            np = end

        scan = np + 1
        while scan < end and data[scan] == 0:
            scan += 1
        trailing = scan - np - 1

        seg = data[pos:np]

        if any(b >= 0xa1 for b in seg):
            if preserve_segments:
                if pos not in seen:
                    seen.add(pos)
                    yield (pos, bytes(seg), trailing)
                pos = np + 1
                continue

            try:
                seg.decode(ENCODING)
                if pos not in seen:
                    seen.add(pos)
                    yield (pos, bytes(seg), trailing)
                pos = np + 1
                continue
            except Exception:
                pass

            for run_off, run_raw in _jp_runs(seg, pos):
                if run_off not in seen:
                    seen.add(run_off)
                    yield (run_off, run_raw, trailing)

        pos = np + 1


def iter_legacy_aliases(data, start, end):
    pos = start
    seen = set()

    while pos < end:
        if data[pos] == 0:
            pos += 1
            continue

        np = data.find(b'\x00', pos, end)
        if np == -1:
            np = end

        scan = np + 1
        while scan < end and data[scan] == 0:
            scan += 1
        trailing = scan - np - 1

        seg = data[pos:np]
        if any(b >= 0xa1 for b in seg):
            for run_off, run_raw in _jp_runs(seg, pos):
                if run_off not in seen:
                    seen.add(run_off)
                    yield (run_off, run_raw, trailing)

        pos = np + 1


def build_string_slots(data, start=0, end=None, preserve_segments=False):
    """Return non-overlapping strings using the renderer's control grammar."""
    if end is None:
        end = len(data)

    legacy = sorted(
        iter_legacy_strings(data, start, end, preserve_segments),
        key=lambda item: item[0],
    )
    slots = []
    covered_until = start
    for offset, raw, trailing in legacy:
        if offset < covered_until:
            continue

        parsed = parse_control_aware_string(data, offset, end)
        legacy_end = offset + len(raw)
        bridged_by_control = (
            parsed is not None
            and any(
                packet_start < legacy_end < packet_end
                for packet_start, packet_end in parsed.control_ranges
            )
        )
        expands_over_control_packet = (
            parsed is not None
            and parsed.terminator > legacy_end
            and bridged_by_control
        )
        if expands_over_control_packet:
            raw = bytes(data[offset:parsed.terminator])
            trailing = trailing_nulls_after(data, parsed.terminator, end)
            covered_until = parsed.terminator
        else:
            covered_until = offset + len(raw)
        slots.append(StringSlot(offset, bytes(raw), trailing))
    return slots


def iter_strings(data, start=0, end=None, preserve_segments=False):
    for slot in build_string_slots(data, start, end, preserve_segments):
        yield slot.offset, slot.raw, slot.trailing


def build_string_catalog(data, ranges):
    slots = []
    legacy = {}
    for low, high, preserve in ranges:
        slots.extend(build_string_slots(data, low, high, preserve))
        for offset, raw, trailing in iter_legacy_strings(data, low, high, preserve):
            legacy.setdefault(offset, StringSlot(offset, raw, trailing))
        for offset, raw, trailing in iter_legacy_aliases(data, low, high):
            legacy.setdefault(offset, StringSlot(offset, raw, trailing))
    slots.sort(key=lambda slot: slot.offset)
    return slots, legacy


def _slot_for_offset(slots, starts, offset):
    index = bisect_right(starts, offset) - 1
    if index < 0:
        return None
    slot = slots[index]
    if slot.offset <= offset < slot.end:
        return slot
    return None


def format_control_packets(packets):
    if not packets:
        return '-'
    return ' '.join(packet.hex() for packet in packets)


def apply_grouped_translations(
    data,
    ranges,
    edits,
    replace_table,
    label='string',
    skip_polluted=True,
):
    """Apply legacy fragments and new full records one logical string at a time."""
    source = bytes(data)
    slots, legacy = build_string_catalog(source, ranges)
    starts = [slot.offset for slot in slots]
    grouped = {}
    stats = GroupedRebuildStats()

    for edit in edits.values():
        if skip_polluted and is_polluted_legacy_text(edit.text):
            stats.polluted += 1
            continue
        slot = _slot_for_offset(slots, starts, edit.offset)
        if slot is None:
            print(
                f'[WARN] {label} line {edit.line_number} 0x{edit.offset:08x}: '
                'source string not found; skipped'
            )
            stats.missing += 1
            continue
        grouped.setdefault(slot.offset, []).append(edit)

    slot_by_offset = {slot.offset: slot for slot in slots}
    for slot_offset in sorted(grouped):
        slot = slot_by_offset[slot_offset]
        group_edits = sorted(grouped[slot_offset], key=lambda edit: edit.offset)
        original_validation = slot.raw + b'\x00' * (slot.trailing + 1)
        original_parsed = parse_control_aware_string(original_validation, 0)

        full_edits = [
            edit for edit in group_edits
            if edit.offset == slot.offset
            and edit.declared_length == len(slot.raw)
        ]
        if full_edits:
            group_edits = [full_edits[-1]]

        cursor = slot.offset
        rebuilt = bytearray()
        encoded_records = 0
        group_invalid = False
        for edit in group_edits:
            span = edit.declared_length
            if span is None:
                legacy_slot = legacy.get(edit.offset)
                if legacy_slot is not None:
                    span = len(legacy_slot.raw)
                elif edit.offset == slot.offset:
                    span = len(slot.raw)

            if span is None or span <= 0:
                print(
                    f'[WARN] {label} line {edit.line_number} 0x{edit.offset:08x}: '
                    'original byte length is unavailable; logical string skipped'
                )
                group_invalid = True
                break
            if edit.offset < cursor or edit.offset + span > slot.end:
                print(
                    f'[WARN] {label} line {edit.line_number} 0x{edit.offset:08x}: '
                    f'original span {span}B overlaps or exceeds logical slot '
                    f'0x{slot.offset:08x}-0x{slot.end:08x}; skipped'
                )
                group_invalid = True
                break

            try:
                encoded = encode_display(edit.text, replace_table)
            except Exception as error:
                print(
                    f'[ERR] {label} line {edit.line_number} 0x{edit.offset:08x}: '
                    f'encode failed ({error})'
                )
                group_invalid = True
                break

            # Old dumps could end a record in the middle of a control packet.
            # Keep that legacy boundary exact: trim accidentally exposed extra
            # operands, or restore omitted operands from the source prefix. A
            # normalized full logical record can still replace the whole packet.
            original_end = edit.offset + span
            if original_parsed is not None and original_end < slot.end:
                relative_end = original_end - slot.offset
                for packet_start, packet_end in original_parsed.control_ranges:
                    if packet_start < relative_end < packet_end:
                        command = slot.raw[packet_start]
                        translated_start = encoded.rfind(bytes((command,)))
                        if translated_start != -1:
                            source_prefix = relative_end - packet_start
                            translated_prefix = len(encoded) - translated_start
                            if translated_prefix > source_prefix:
                                encoded = encoded[
                                    :translated_start + source_prefix
                                ]
                            elif translated_prefix < source_prefix:
                                encoded += slot.raw[
                                    packet_start + translated_prefix:relative_end
                                ]
                        break

            rebuilt.extend(source[cursor:edit.offset])
            rebuilt.extend(encoded)
            cursor = edit.offset + span
            encoded_records += 1

        if group_invalid:
            stats.invalid += 1
            continue

        rebuilt.extend(source[cursor:slot.end])
        rebuilt = bytes(rebuilt)
        capacity = len(slot.raw) + slot.trailing
        if len(rebuilt) > capacity:
            print(
                f'[WARN] {label} logical slot 0x{slot.offset:08x}: '
                f'needs {len(rebuilt)}B, capacity is {capacity}B; skipped'
            )
            stats.overflow += 1
            continue

        parsed = None
        if original_parsed is not None:
            validation = rebuilt + b'\x00' * (capacity - len(rebuilt) + 1)
            parsed = parse_control_aware_string(validation, 0)
            if (
                parsed is None
                or parsed.terminator < len(rebuilt)
                or parsed.terminator > capacity
            ):
                print(
                    f'[WARN] {label} logical slot 0x{slot.offset:08x}: rebuilt bytes '
                    'contain an early NUL or an incomplete control/EUC sequence; skipped'
                )
                stats.invalid += 1
                continue

        if (
            original_parsed is not None
            and parsed is not None
            and original_parsed.control_packets != parsed.control_packets
        ):
            print(
                f'[WARN] {label} logical slot 0x{slot.offset:08x}: control packets differ '
                f'orig={format_control_packets(original_parsed.control_packets)} '
                f'new={format_control_packets(parsed.control_packets)}'
            )
            stats.control_warnings += 1

        if rebuilt == slot.raw:
            stats.unchanged_groups += 1
            continue

        slot_size = len(slot.raw) + 1 + slot.trailing
        data[slot.offset:slot.offset + slot_size] = b'\x00' * slot_size
        data[slot.offset:slot.offset + len(rebuilt)] = rebuilt
        stats.patched_groups += 1
        stats.patched_records += encoded_records

    return stats


# ── 추출 ──────────────────────────────────────────────────────────────────────

def extract(bin_path, start=0):
    data     = open(bin_path, 'rb').read()
    ranges   = file_scan_ranges(bin_path, len(data), start)
    base     = os.path.splitext(os.path.basename(bin_path))[0]
    out_path = os.path.join(os.path.dirname(os.path.abspath(bin_path)),
                            base + '_strings.txt')
    lines = [
        f'# {os.path.basename(bin_path)} string dump',
        f'# scan start: 0x{start:x}',
        f'# scan ranges: {format_scan_ranges(ranges)}',
        '# format: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>',
        '#   orig  = 원본 바이트 수 (null terminator 제외)',
        '#   slack = 여유 공간 (trailing null 개수)',
        '#   max   = orig+slack = 번역 후 인코딩 결과 상한',
        '# - \\\\n = 0x0a,  \\\\r = 0x0d',
        '# - 의미 없는 행은 삭제해도 리빌드에 영향 없음',
        '',
    ]
    count = 0
    for lo, hi, preserve in ranges:
        for off, raw, trailing in sorted(
            iter_strings(data, lo, hi, preserve),
            key=lambda x: x[0],
        ):
            display = raw_to_display(raw)
            lines.append(f'{off:08x}|{len(raw)}/{trailing}|{display}')
            count += 1

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'[OK] {count}개 문자열 추출 -> {out_path}')


# ── 리빌드 ────────────────────────────────────────────────────────────────────

def _rebuild_individual_legacy(bin_path, txt_path):
    data  = bytearray(open(bin_path, 'rb').read())
    table = load_replace_table(bin_path)

    # start offset을 txt 헤더에서 읽기
    start = 0
    with open(txt_path, encoding='utf-8', newline='') as f:
        for line in f:
            line = line.strip()
            if line.startswith('# scan start:'):
                try:
                    start = int(line.split(':')[1].strip(), 16)
                except Exception:
                    pass
                break

    ranges = parse_scan_ranges(txt_path, len(data)) or file_scan_ranges(bin_path, len(data), start)

    orig = {}
    for lo, hi, preserve in ranges:
        for off, raw, trailing in iter_strings(bytes(data), lo, hi, preserve):
            orig[off] = (raw, trailing)
        if preserve:
            for off, raw, trailing in iter_legacy_aliases(bytes(data), lo, hi):
                orig.setdefault(off, (raw, trailing))

    edits = {}
    skipped = 0
    with open(txt_path, encoding='utf-8', newline='') as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip('\n')
            if line.endswith('\r'):
                line = line[:-1]
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                hex_off, _info, text = parts
            elif len(parts) == 2:
                hex_off, text = parts
            else:
                skipped += 1
                continue
            try:
                offset = int(hex_off.strip(), 16)
            except ValueError:
                print(f'[WARN] line {lineno}: 오프셋 파싱 실패: {hex_off!r}')
                skipped += 1
                continue
            if is_polluted_legacy_text(text):
                skipped += 1
                continue
            edits[offset] = text

    if skipped:
        print(f'[INFO] {skipped}개 라인 건너뜀')

    patched = over_orig = over_slack = legacy_suffix = errors = 0

    for offset, new_text in sorted(edits.items()):
        if offset not in orig:
            print(f'[WARN] 0x{offset:08x}: 원본에 없는 오프셋, 건너뜀')
            errors += 1
            continue

        orig_raw, trailing = orig[offset]
        orig_len = len(orig_raw)
        slack    = trailing
        max_len  = orig_len + slack

        try:
            new_raw = encode_display(new_text, table)
        except Exception as e:
            print(f'[ERR] 0x{offset:08x}: 인코딩 실패 ({e}): {new_text!r}')
            errors += 1
            continue

        new_raw, suffix_len = append_legacy_suffix(orig_raw, new_raw)
        if suffix_len:
            legacy_suffix += 1

        new_len = len(new_raw)
        if new_raw == orig_raw:
            continue

        if new_len > max_len:
            orig_dec = orig_raw.decode(ENCODING, errors='replace')
            print(f'[여유 공간 초과하여 미적용] 0x{offset:08x} '
                  f'원본={orig_len}B 여유={slack}B 신규={new_len}B | {orig_dec!r}')
            over_slack += 1
            continue

        if new_len > orig_len:
            orig_dec = orig_raw.decode(ENCODING, errors='replace')
            print(f'[원본 길이 초과] 0x{offset:08x} '
                  f'원본={orig_len}B -> 신규={new_len}B (여유 {slack}B 내 적용) | {orig_dec!r}')
            over_orig += 1

        slot_size = orig_len + 1 + trailing
        data[offset:offset + slot_size] = b'\x00' * slot_size
        data[offset:offset + new_len]   = new_raw
        patched += 1

    base, ext = os.path.splitext(bin_path)
    out_path = base + '_patched' + ext
    with open(out_path, 'wb') as f:
        f.write(data)

    print()
    print(f'[완료] 패치={patched} (원본길이초과 포함 {over_orig})  '
          f'여유초과(미적용)={over_slack}  오류={errors}')
    print(f'[OK] 출력: {out_path}')


# ── main ──────────────────────────────────────────────────────────────────────

def rebuild(bin_path, txt_path):
    data = bytearray(open(bin_path, 'rb').read())
    table = load_replace_table(bin_path)

    start = 0
    with open(txt_path, encoding='utf-8-sig', newline='') as stream:
        for line in stream:
            if line.startswith('# scan start:'):
                try:
                    start = int(line.split(':', 1)[1].strip(), 16)
                except ValueError:
                    pass
                break

    ranges = (
        parse_scan_ranges(txt_path, len(data))
        or file_scan_ranges(bin_path, len(data), start)
    )
    edits, malformed = parse_translation_edits(txt_path)
    stats = apply_grouped_translations(
        data,
        ranges,
        edits,
        table,
        label=os.path.basename(bin_path),
    )

    base, ext = os.path.splitext(bin_path)
    out_path = base + '_patched' + ext
    with open(out_path, 'wb') as stream:
        stream.write(data)

    print(
        f'[DONE] groups={stats.patched_groups} records={stats.patched_records} '
        f'malformed={malformed} missing={stats.missing} '
        f'overflow={stats.overflow} invalid={stats.invalid} '
        f'control_warn={stats.control_warnings} polluted={stats.polluted}'
    )
    print(f'[OK] output: {out_path}')


def usage():
    print(__doc__)
    sys.exit(1)

if __name__ == '__main__':
    if len(sys.argv) < 3:
        usage()
    cmd = sys.argv[1].lower()
    if cmd == 'extract':
        bin_path = sys.argv[2]
        start = 0
        if len(sys.argv) >= 4:
            try:
                start = int(sys.argv[3], 0)  # 0x... 또는 십진수 모두 지원
            except ValueError:
                print(f'[ERR] start_offset 파싱 실패: {sys.argv[3]!r}')
                sys.exit(1)
        extract(bin_path, start)
    elif cmd == 'rebuild':
        if len(sys.argv) < 4:
            usage()
        rebuild(sys.argv[2], sys.argv[3])
    else:
        usage()
