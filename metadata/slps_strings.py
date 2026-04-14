#!/usr/bin/env python3
"""
slps_290.02 문자열 추출/리빌드 툴

대상 범위: 0x1665e0 ~ EOF
추출 조건: EUC-JIS-2004 디코딩 성공 + 일본어 바이트(0xa1 이상) 포함

txt 포맷: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>
  ex)  002bf088|16/7|キャンセルする。
         -> 원본 16바이트, 여유(trailing null) 7바이트 (최대 23바이트까지 쓸 수 있음)

슬롯 구조: [문자열 바이트] [0x00 null terminator] [trailing 0x00]
  여유 공간 = trailing null 개수 (null terminator 자체는 제외)

제어 코드 표기:
  \\n = 0x0a,  \\r = 0x0d  (txt에서 이스케이프로 표기, 리빌드 시 원래 바이트로 복원)

사용법:
  추출: python3 slps_strings.py extract slps_290.02
        -> slps_290_strings.txt 생성

  리빌드: python3 slps_strings.py rebuild slps_290.02 slps_290_strings.txt
          -> slps_290_patched.02 생성
          -> 같은 폴더의 XENOSAGA_KOR-JPN.json 자동 인식, 한글->한자 치환 후 EUC-JIS-2004 인코딩
"""

import sys
import os
import json

SCAN_START = 0x1665e0
ENCODING   = 'euc_jis_2004'


# ── 공통 ──────────────────────────────────────────────────────────────────────

def load_replace_table(bin_path):
    folder    = os.path.dirname(os.path.abspath(bin_path))
    json_path = os.path.join(folder, 'XENOSAGA_KOR-JPN.json')
    if not os.path.exists(json_path):
        print(f'[INFO] {json_path} 없음 - 한글 치환 없이 진행')
        return {}
    with open(json_path, encoding='utf-8-sig') as f:
        data = json.load(f)
    table = data.get('replace-table', {})
    print(f'[INFO] replace-table 로드: {len(table)}개 ({json_path})')
    return table


def apply_replace_table(text, table):
    if not table:
        return text
    return ''.join(table.get(ch, ch) for ch in text)


def to_display(decoded):
    """디코딩된 문자열 -> txt 표기 (\\n, \\r 이스케이프)"""
    return decoded.replace('\r', '\\r').replace('\n', '\\n')


def from_display(text):
    """txt 표기 -> 실제 문자열 (\\n, \\r 복원)"""
    return text.replace('\\r', '\r').replace('\\n', '\n')


def iter_strings(data, start):
    """
    start ~ EOF 구간에서 EUC-JIS-2004 디코딩 가능 + 일본어 바이트 포함 문자열 yield.
    yield: (offset, raw_bytes, trailing_nulls)
      trailing_nulls = null terminator 이후 ~ 다음 non-null 직전까지의 0x00 개수
    """
    end = len(data)
    pos = start
    while pos < end:
        if data[pos] == 0:
            pos += 1
            continue
        null_pos = data.find(b'\x00', pos, end)
        if null_pos == -1:
            null_pos = end
        raw = data[pos:null_pos]

        scan = null_pos + 1
        while scan < end and data[scan] == 0:
            scan += 1
        trailing = scan - null_pos - 1

        try:
            raw.decode(ENCODING)
            if any(b >= 0xa1 for b in raw):
                yield (pos, raw, trailing)
        except Exception:
            pass

        pos = null_pos + 1


# ── 추출 ──────────────────────────────────────────────────────────────────────

def extract(bin_path):
    data     = open(bin_path, 'rb').read()
    base     = os.path.splitext(os.path.basename(bin_path))[0]
    out_path = os.path.join(os.path.dirname(os.path.abspath(bin_path)),
                            base + '_strings.txt')

    lines = [
        f'# slps_290.02 string dump',
        f'# scan start: 0x{SCAN_START:x}',
        f'# format: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>',
        f'#   orig  = 원본 문자열 바이트 수 (null terminator 제외)',
        f'#   slack = 여유 공간 바이트 수 (trailing null 개수)',
        f'#   max   = orig + slack = 번역 후 인코딩 결과가 이 값 이하여야 적용 가능',
        f'# - \\\\n = 0x0a newline,  \\\\r = 0x0d carriage return (제어 코드)',
        f'# - 그 외 \\x?? 제어 코드 포함 문자열은 수정 시 코드 보존 필요',
        '',
    ]

    count = 0
    for off, raw, trailing in iter_strings(data, SCAN_START):
        decoded = raw.decode(ENCODING)
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

    orig = {}
    for off, raw, trailing in iter_strings(bytes(data), SCAN_START):
        orig[off] = (raw, trailing)

    edits   = {}
    skipped = 0
    with open(txt_path, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip('\r\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                hex_off, _size_info, text = parts
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
        print(f'[INFO] {skipped}개 라인 건너뜀 (주석/빈 줄 포함)')

    patched    = 0
    over_orig  = 0
    over_slack = 0
    errors     = 0

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
            print(f'[ERR] 0x{offset:08x}: 인코딩 실패 ({e}): {converted!r}')
            errors += 1
            continue

        new_len = len(new_raw)

        if new_raw == orig_raw:
            continue

        if new_len > max_len:
            orig_dec = orig_raw.decode(ENCODING, errors='replace')
            print(f'[여유 공간 초과하여 미적용] 0x{offset:08x} '
                  f'원본={orig_len}B 여유={slack}B 신규={new_len}B '
                  f'| {orig_dec!r}')
            over_slack += 1
            continue

        if new_len > orig_len:
            orig_dec = orig_raw.decode(ENCODING, errors='replace')
            print(f'[원본 길이 초과] 0x{offset:08x} '
                  f'원본={orig_len}B -> 신규={new_len}B (여유 {slack}B 내 적용) '
                  f'| {orig_dec!r}')
            over_orig += 1

        slot_size = orig_len + 1 + trailing
        data[offset : offset + slot_size] = b'\x00' * slot_size
        data[offset : offset + new_len]   = new_raw
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
        extract(sys.argv[2])
    elif cmd == 'rebuild':
        if len(sys.argv) < 4:
            usage()
        rebuild(sys.argv[2], sys.argv[3])
    else:
        usage()
