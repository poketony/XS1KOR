#!/usr/bin/env python3
"""
ov01.ovl 문자열 추출/리빌드 툴

대상 블록:
  BLOCK_A: 0x40e38 ~ 0x40ec0  (Status 직후 텍스트: 上空, 直線 ... メニュー)
  BLOCK_B: 0x411e8 ~ 0x48bb0  (스킬/아이템 설명 및 이름)

인코딩: EUC-JIS-2004

슬롯 구조: [문자열 바이트] [0x00 null terminator] [trailing 0x00 padding]
  - 슬롯 크기는 항상 8바이트 정렬
  - 여유 공간 = trailing 0x00 개수 (null terminator 자체는 제외)
  - 번역 후 EUC-JIS-2004 인코딩 길이가 (원본 + 여유) 이하여야 적용 가능

txt 포맷: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>
  ex)  0411e8|25/6|自分\\n行動速度５０％アップ
         -> 원본 25바이트, 여유 6바이트 (최대 31바이트까지 쓸 수 있음)

사용법:
  추출: python3 ov01_strings.py extract ov01.ovl
        -> ov01_strings.txt 생성

  리빌드: python3 ov01_strings.py rebuild ov01.ovl ov01_strings.txt
          -> ov01_patched.ovl 생성
          -> 같은 폴더의 XENOSAGA_KOR-JPN.json 자동 인식, 한글->한자 치환 후 EUC-JIS-2004 인코딩
"""

import sys
import os
import json

BLOCKS   = [(0x40e38, 0x40ec0), (0x411e8, 0x48bb0)]
ENCODING = 'euc_jis_2004'


# ── 공통 ──────────────────────────────────────────────────────────────────────

def load_replace_table(ovl_path):
    folder    = os.path.dirname(os.path.abspath(ovl_path))
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


def iter_strings(data, start, end):
    """
    start~end 구간의 null-terminated 문자열을 순서대로 yield.
    yield: (offset, raw_bytes, trailing_nulls)
      trailing_nulls = null terminator 이후 ~ 다음 non-null 직전까지의 0x00 개수
                       (null terminator 자체는 포함하지 않음)
    슬롯 구조는 항상 8바이트 정렬: len(raw)+1+trailing ≡ 0 (mod 8)
    """
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

        yield (pos, raw, trailing)
        pos = null_pos + 1


# ── 추출 ──────────────────────────────────────────────────────────────────────

def extract(ovl_path):
    data     = open(ovl_path, 'rb').read()
    out_path = os.path.splitext(ovl_path)[0] + '_strings.txt'

    lines = [
        '# ov01.ovl string dump',
        '# blocks: 0x40e38-0x40ec0 (Status 직후), 0x411e8-0x48bb0 (스킬/아이템)',
        '# format: <hex_offset>|<orig_bytes>/<slack_bytes>|<text>',
        '#   orig  = 원본 문자열 바이트 수 (null terminator 제외)',
        '#   slack = 여유 공간 바이트 수 (trailing null 개수)',
        '#   max   = orig + slack = 번역 후 인코딩 결과가 이 값 이하여야 적용 가능',
        '# - \\n in text = 0x0a newline in binary',
        '# - 1글자 히라가나/한자 엔트리 = 정렬 인덱스 키 (수정 불필요)',
        '',
    ]

    count = 0
    for block_start, block_end in BLOCKS:
        lines.append(f'## BLOCK 0x{block_start:x}-0x{block_end:x}')
        for off, raw, trailing in iter_strings(data, block_start, block_end):
            try:
                decoded = raw.decode(ENCODING)
            except Exception:
                decoded = f'[DECODE_ERR:{raw.hex()}]'
            display = decoded.replace('\n', '\\n')
            lines.append(f'{off:06x}|{len(raw)}/{trailing}|{display}')
            count += 1
        lines.append('')

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f'[OK] {count}개 문자열 추출 -> {out_path}')


# ── 리빌드 ────────────────────────────────────────────────────────────────────

def rebuild(ovl_path, txt_path):
    data  = bytearray(open(ovl_path, 'rb').read())
    table = load_replace_table(ovl_path)

    # 원본 슬롯 정보: offset -> (raw_bytes, trailing_nulls)
    orig = {}
    for block_start, block_end in BLOCKS:
        for off, raw, trailing in iter_strings(bytes(data), block_start, block_end):
            orig[off] = (raw, trailing)

    # txt 파싱 - 포맷: offset|orig/slack|text
    edits   = {}
    skipped = 0
    with open(txt_path, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip('\r\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('|', 2)
            if len(parts) < 3:
                # 구버전 포맷(offset|text) 호환
                if len(parts) == 2:
                    hex_off, text = parts
                else:
                    skipped += 1
                    continue
            else:
                hex_off, _size_info, text = parts
            try:
                offset = int(hex_off.strip(), 16)
            except ValueError:
                print(f'[WARN] line {lineno}: 오프셋 파싱 실패: {hex_off!r}')
                skipped += 1
                continue
            edits[offset] = text.replace('\\n', '\n')

    if skipped:
        print(f'[INFO] {skipped}개 라인 건너뜀 (주석/빈 줄 포함)')

    patched    = 0
    over_orig  = 0
    over_slack = 0
    errors     = 0

    for offset, new_text in sorted(edits.items()):
        if offset not in orig:
            print(f'[WARN] 0x{offset:06x}: 원본에 없는 오프셋, 건너뜀')
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
            print(f'[ERR] 0x{offset:06x}: 인코딩 실패 ({e}): {converted!r}')
            errors += 1
            continue

        new_len = len(new_raw)

        if new_raw == orig_raw:
            continue

        if new_len > max_len:
            orig_dec = orig_raw.decode(ENCODING, errors='replace')
            print(f'[여유 공간 초과하여 미적용] 0x{offset:06x} '
                  f'원본={orig_len}B 여유={slack}B 신규={new_len}B '
                  f'| {orig_dec!r}')
            over_slack += 1
            continue

        if new_len > orig_len:
            orig_dec = orig_raw.decode(ENCODING, errors='replace')
            print(f'[원본 길이 초과] 0x{offset:06x} '
                  f'원본={orig_len}B -> 신규={new_len}B (여유 {slack}B 내 적용) '
                  f'| {orig_dec!r}')
            over_orig += 1

        slot_size = orig_len + 1 + trailing
        data[offset : offset + slot_size] = b'\x00' * slot_size
        data[offset : offset + new_len]   = new_raw
        patched += 1

    out_path = os.path.splitext(ovl_path)[0] + '_patched.ovl'
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
