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

ENCODING = 'euc_jis_2004'
CTRL_OK  = frozenset([0x01,0x02,0x03,0x08,0x09,0x0a,0x0c,0x0d,0x1f,0x19])


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
    return s.replace('\r', '\\r').replace('\n', '\\n')

def from_display(s):
    return s.replace('\\r', '\r').replace('\\n', '\n')


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


def iter_strings(data, start=0):
    """
    start~EOF 구간 스캔, yield: (offset, raw_bytes, trailing_nulls)
    """
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


# ── 추출 ──────────────────────────────────────────────────────────────────────

def extract(bin_path, start=0):
    data     = open(bin_path, 'rb').read()
    base     = os.path.splitext(os.path.basename(bin_path))[0]
    out_path = os.path.join(os.path.dirname(os.path.abspath(bin_path)),
                            base + '_strings.txt')
    lines = [
        f'# {os.path.basename(bin_path)} string dump',
        f'# scan start: 0x{start:x}',
        '# format: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>',
        '#   orig  = 원본 바이트 수 (null terminator 제외)',
        '#   slack = 여유 공간 (trailing null 개수)',
        '#   max   = orig+slack = 번역 후 인코딩 결과 상한',
        '# - \\\\n = 0x0a,  \\\\r = 0x0d',
        '# - 의미 없는 행은 삭제해도 리빌드에 영향 없음',
        '',
    ]
    count = 0
    for off, raw, trailing in sorted(iter_strings(data, start), key=lambda x: x[0]):
        decoded = raw.decode(ENCODING, errors='replace')
        display = to_display(decoded)
        lines.append(f'{off:08x}|{len(raw)}/{trailing}|{display}')
        count += 1

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'[OK] {count}개 문자열 추출 -> {out_path}')


# ── 리빌드 ────────────────────────────────────────────────────────────────────

def rebuild(bin_path, txt_path):
    data  = bytearray(open(bin_path, 'rb').read())
    table = load_replace_table(bin_path)

    # start offset을 txt 헤더에서 읽기
    start = 0
    with open(txt_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('# scan start:'):
                try:
                    start = int(line.split(':')[1].strip(), 16)
                except Exception:
                    pass
                break

    orig = {}
    for off, raw, trailing in iter_strings(bytes(data), start):
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
            edits[offset] = from_display(text)

    if skipped:
        print(f'[INFO] {skipped}개 라인 건너뜀')

    patched = over_orig = over_slack = errors = 0

    for offset, new_text in sorted(edits.items()):
        if offset not in orig:
            print(f'[WARN] 0x{offset:08x}: 원본에 없는 오프셋, 건너뜀')
            errors += 1
            continue

        orig_raw, trailing = orig[offset]
        orig_len = len(orig_raw)
        slack    = trailing
        max_len  = orig_len + slack

        converted = apply_replace_table(new_text, table)
        try:
            new_raw = converted.encode(ENCODING)
        except Exception as e:
            print(f'[ERR] 0x{offset:08x}: 인코딩 실패 ({e})')
            errors += 1
            continue

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
