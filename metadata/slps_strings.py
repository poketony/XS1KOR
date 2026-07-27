#!/usr/bin/env python3
"""
slps_290.02 문자열 추출/리빌드 툴

대상 범위: 0x1665e0 ~ EOF
추출 조건:
  - EUC-JIS-2004 디코딩 성공 + 일본어 바이트 포함: 전체 null-segment를 추출
  - 디코딩 실패 segment: 내부에서 일본어 연속 run을 추출 (제어코드 앞뒤로 끼어있는 텍스트)

txt 포맷: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>
  ex)  002bf088|16/7|キャンセルする。

슬롯 구조: [문자열 바이트] [0x00 null terminator] [trailing 0x00]
  여유 공간 = trailing null 개수 (null terminator 자체는 제외)
  리빌드 시 (원본+여유) 바이트 이내여야 적용 가능

제어 코드 표기: \\n = 0x0a,  \\r = 0x0d,  \\xNN = 기타 제어 바이트

사용법:
  추출: python3 slps_strings.py extract slps_290.02
        -> slps_290_strings.txt 생성

  리빌드: python3 slps_strings.py rebuild slps_290.02 slps_290_strings.txt
          -> slps_290_patched.02 생성
          -> 같은 폴더의 XENOSAGA_KOR-JPN.json 자동 인식, 한글->한자 치환 후 EUC-JIS-2004 인코딩
"""

import sys, os, json

import euc_scan

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

SCAN_START = 0x1665e0
SCAN_END   = 0x2eb93c  # end of the last loaded CPU section (embedded OV02)
ENCODING   = 'euc_jis_2004'
CTRL_OK    = frozenset(range(1, 0x20))

# A.G.W.S. 탑승자 등록/해제 메시지는 중간 0x00이 문자열 종료자가 아니라
# 바로 앞 0x19 제어코드의 인자로 쓰이는 논리적 단일 문자열이다.
# 일반 null-segment 스캔으로 나누면 번역문을 넣을 수 없으므로 이 두 곳만
# 최종 종료 0x00 직전까지 하나의 슬롯으로 취급한다.
LOGICAL_STRING_ENDS = {
    # Save confirmation strings include the nested button text in one physical
    # slot. Editing the inner offsets separately zero-fills the middle and
    # makes the game stop after the question line.
    0x002bc51f: 0x002bc56f,
    0x002bc579: 0x002bc5db,
    0x002bc621: 0x002bc663,

    0x002c2640: 0x002c267d,
    0x002c2680: 0x002c26bf,
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


def to_display(s):
    out = []
    for ch in s:
        code = ord(ch)
        if ch == '\r':
            out.append('\\r')
        elif ch == '\n':
            out.append('\\n')
        elif code < 0x20 or code == 0x7f:
            out.append(f'\\x{code:02x}')
        else:
            out.append(ch)
    return ''.join(out)


def _raw_to_display_legacy(raw):
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
            i += 1
            continue
        if b < 0x20 or b == 0x7f or b == 0x80:
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


def raw_to_display(raw):
    return euc_scan.raw_to_display(raw)


def from_display(s):
    out = []
    i = 0
    hexdigits = '0123456789abcdefABCDEF'
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            esc = s[i + 1]
            if esc == 'n':
                out.append('\n')
                i += 2
                continue
            if esc == 'r':
                out.append('\r')
                i += 2
                continue
            if esc in ('x', 'X') and i + 3 < len(s):
                hx = s[i + 2:i + 4]
                if all(c in hexdigits for c in hx):
                    out.append(chr(int(hx, 16)))
                    i += 4
                    continue
        out.append(ch)
        i += 1
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


def control_bytes(raw):
    return bytes(b for b in raw if 0 < b < 0x20 and b not in (0x0a, 0x0d))


def format_control_bytes(raw):
    ctrl = control_bytes(raw)
    return ' '.join(f'{b:02x}' for b in ctrl) if ctrl else '-'


def logical_string_end(offset):
    return LOGICAL_STRING_ENDS.get(offset)


def covering_logical_string(offset):
    for start, end in LOGICAL_STRING_ENDS.items():
        if start < offset < end:
            return start
    return None


def logical_string_in_segment(pos, terminator_off):
    matches = [
        (start, end)
        for start, end in LOGICAL_STRING_ENDS.items()
        if pos <= start < terminator_off and end <= terminator_off
    ]
    return min(matches) if matches else None


def trailing_nulls_after(data, terminator_off):
    scan = terminator_off + 1
    while scan < len(data) and data[scan] == 0:
        scan += 1
    return scan - terminator_off - 1


def _jp_runs(seg, base_off):
    """seg 내에서 일본어 포함 EUC 연속 구간 yield: (abs_offset, raw_bytes)"""
    slen = len(seg)
    i = 0
    while i < slen:
        b = seg[i]
        if b >= 0xa1 or b in (0x8e, 0x8f):
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
                yield (base_off + (i - len(run) if j > i else i - 1), bytes(run))
                # run_start 정확히 계산
        else:
            i += 1


def _jp_runs_fixed(seg, base_off):
    """run_start 오프셋을 정확하게 추적하는 버전"""
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


def _iter_strings_manual_legacy(data, start):
    """
    start~EOF 구간 스캔, yield: (offset, raw_bytes, trailing_nulls)
    두 가지 추출 방식 통합:
      1) null-segment 전체가 EUC 디코딩 성공 + 일본어 포함 -> 전체를 하나의 문자열로
      2) 디코딩 실패 -> 내부에서 일본어 run 추출
    """
    end = len(data)
    pos = start
    seen = set()

    while pos < end:
        logical_end = logical_string_end(pos)
        if logical_end is not None and logical_end < end:
            if pos not in seen:
                seen.add(pos)
                yield (pos, bytes(data[pos:logical_end]), trailing_nulls_after(data, logical_end))
            pos = logical_end + 1 + trailing_nulls_after(data, logical_end)
            continue

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

        logical_range = logical_string_in_segment(pos, np)
        if logical_range is not None:
            logical_start, logical_end = logical_range
            if logical_start not in seen:
                seen.add(logical_start)
                yield (
                    logical_start,
                    bytes(data[logical_start:logical_end]),
                    trailing_nulls_after(data, logical_end),
                )
            pos = logical_end + 1 + trailing_nulls_after(data, logical_end)
            continue

        if any(b >= 0xa1 for b in seg):
            try:
                seg.decode(ENCODING)
                # 방법1: 전체 성공
                if pos not in seen:
                    seen.add(pos)
                    yield (pos, bytes(seg), trailing)
                pos = np + 1
                continue
            except Exception:
                pass

            # 방법2: run 추출
            for run_off, run_raw in _jp_runs_fixed(seg, pos):
                if run_off not in seen:
                    seen.add(run_off)
                    yield (run_off, run_raw, trailing)

        pos = np + 1


def iter_strings(data, start):
    yield from euc_scan.iter_strings(data, start, min(SCAN_END, len(data)), False)


# ── 추출 ──────────────────────────────────────────────────────────────────────

def extract(bin_path):
    data     = open(bin_path, 'rb').read()
    base     = os.path.splitext(os.path.basename(bin_path))[0]
    out_path = os.path.join(os.path.dirname(os.path.abspath(bin_path)),
                            base + '_strings.txt')
    lines = [
        '# slps_290.02 string dump',
        f'# scan start: 0x{SCAN_START:x}',
        '# format: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>',
        '#   orig  = 원본 문자열 바이트 수 (null terminator 제외)',
        '#   slack = 여유 공간 바이트 수 (trailing null 개수)',
        '#   max   = orig + slack = 번역 후 인코딩 결과가 이 값 이하여야 적용 가능',
        '# - \\\\n = 0x0a,  \\\\r = 0x0d,  \\\\xNN = 기타 제어 바이트',
        '# - 의미 없는 행은 삭제해도 리빌드에 영향 없음',
        '',
    ]
    count = 0
    for off, raw, trailing in sorted(iter_strings(data, SCAN_START), key=lambda x: x[0]):
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

    orig = {}
    for off, raw, trailing in iter_strings(bytes(data), SCAN_START):
        orig[off] = (raw, trailing)

    edits = {}
    skipped = 0
    with open(txt_path, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip('\r\n')
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
            edits[offset] = text

    if skipped:
        print(f'[INFO] {skipped}개 라인 건너뜀')

    patched = over_orig = over_slack = ctrl_warn = errors = 0

    for offset, new_text in sorted(edits.items()):
        cover_start = covering_logical_string(offset)
        if cover_start is not None and cover_start in edits:
            continue

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
            print(f'[ERR] 0x{offset:08x}: 인코딩 실패 ({e})')
            errors += 1
            continue

        if control_bytes(orig_raw) != control_bytes(new_raw):
            print(f'[WARN] 0x{offset:08x}: control bytes differ '
                  f'orig={format_control_bytes(orig_raw)} '
                  f'new={format_control_bytes(new_raw)}')
            ctrl_warn += 1

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
          f'여유초과(미적용)={over_slack}  control_warn={ctrl_warn}  오류={errors}')
    print(f'[OK] 출력: {out_path}')


# ── main ──────────────────────────────────────────────────────────────────────

def rebuild(bin_path, txt_path):
    data = bytearray(open(bin_path, 'rb').read())
    table = load_replace_table(bin_path)
    edits, malformed = euc_scan.parse_translation_edits(txt_path)
    stats = euc_scan.apply_grouped_translations(
        data,
        [(SCAN_START, min(SCAN_END, len(data)), False)],
        edits,
        table,
        label=os.path.basename(bin_path),
        skip_polluted=False,
    )

    base, ext = os.path.splitext(bin_path)
    out_path = base + '_patched' + ext
    with open(out_path, 'wb') as stream:
        stream.write(data)

    print(
        f'[DONE] groups={stats.patched_groups} records={stats.patched_records} '
        f'malformed={malformed} missing={stats.missing} '
        f'overflow={stats.overflow} invalid={stats.invalid} '
        f'control_warn={stats.control_warnings}'
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
        extract(sys.argv[2])
    elif cmd == 'rebuild':
        if len(sys.argv) < 4:
            usage()
        rebuild(sys.argv[2], sys.argv[3])
    else:
        usage()
