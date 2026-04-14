#!/usr/bin/env python3
"""
think_tool.py  -  Xenosaga Episode I  think/*.bin  텍스트 추출/삽입 도구

포맷 규칙:
  - 텍스트 opcode: 0x21 (화면 출력), 0x1A (조건 분기용)
  - opcode 형식: [opcode 1byte] [flag 1byte] [string: EUC-JIS2004, null-terminated]
  - 포인터 테이블 없음 → 텍스트가 코드에 인라인 embedded

사용법:
  python think_tool.py extract  input.bin  output.txt
  python think_tool.py import   input.bin  translated.txt  output.bin  [table.json]
"""

import sys
import os
import json
import struct
import re

ENCODING = 'euc_jis_2004'
TEXT_OPCODES = {0x21, 0x1A}


def find_text_entries(data: bytes) -> list:
    entries = []
    i = 0
    n = len(data)
    while i < n - 2:
        op = data[i]
        if op in TEXT_OPCODES:
            flag = data[i + 1]
            str_off = i + 2
            j = str_off
            while j < n and data[j] != 0x00:
                j += 1
            if j >= n:
                i += 1
                continue
            raw = data[str_off:j]
            if len(raw) < 4:
                i += 1
                continue
            try:
                text = raw.decode(ENCODING)
            except Exception:
                i += 1
                continue
            entries.append({
                'op_off':  i,
                'op':      op,
                'flag':    flag,
                'str_off': str_off,
                'str_end': j,
                'text':    text,
            })
            i = j + 1
        else:
            i += 1
    filtered = []
    for e in entries:
        skip = any(p['str_off'] <= e['op_off'] <= p['str_end'] for p in filtered)
        if not skip:
            filtered.append(e)
    return filtered


def build_code_ref_map(data: bytes, entries: list) -> dict:
    """코드 영역의 u16le 위치→값 매핑 (텍스트 영역 제외, 파일 범위 내 값만)."""
    text_segs = [(e['op_off'], e['str_end'] + 1) for e in entries]

    def in_text(off):
        return any(s <= off < e for s, e in text_segs)

    refs = {}
    i = 0
    while i + 1 < len(data):
        if not in_text(i):
            val = struct.unpack_from('<H', data, i)[0]
            if 0x0100 <= val <= len(data):
                refs[i] = val
        i += 2
    return refs


def escape_text(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp < 0x20 and cp != 0x0A:
            out.append(f'\\x{cp:02x}')
        else:
            out.append(ch)
    return ''.join(out)


def unescape_text(text: str) -> str:
    return re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)


def cmd_extract(bin_path: str, txt_path: str):
    data = open(bin_path, 'rb').read()
    entries = find_text_entries(data)
    lines = []
    lines.append(f'# think_tool extract: {os.path.basename(bin_path)}')
    lines.append(f'# 총 {len(entries)}개 텍스트 엔트리')
    lines.append(f'# 형식: >>> [번호] op=0xXX flag=0xXX offset=0xXXXX')
    lines.append(f'#        (텍스트 내용)')
    lines.append(f'#        <<< (엔트리 끝)')
    lines.append(f'# 번역 시 텍스트 내용만 수정하세요.')
    lines.append(f'# \\n = 줄바꿈, \\x1e\\xXX / \\x19\\xXX = 버튼/색상 제어코드')
    lines.append('')
    for idx, e in enumerate(entries):
        op_name = 'display' if e['op'] == 0x21 else 'condition'
        lines.append(
            f">>> [{idx:03d}] op=0x{e['op']:02X}({op_name}) "
            f"flag=0x{e['flag']:02X} offset=0x{e['op_off']:04X}"
        )
        lines.append(escape_text(e['text']))
        lines.append('<<<')
        lines.append('')
    open(txt_path, 'w', encoding='utf-8').write('\n'.join(lines))
    print(f'[extract] {len(entries)}개 엔트리 → {txt_path}')


def load_replace_table(json_path: str) -> dict:
    raw = json.load(open(json_path, encoding='utf-8-sig'))
    table = raw.get('replace-table', {})
    return dict(sorted(table.items(), key=lambda x: -len(x[0])))


def apply_replace_table(text: str, table: dict) -> str:
    for kor, jpn in table.items():
        text = text.replace(kor, jpn)
    return text


def parse_txt(txt_path: str) -> list:
    lines = open(txt_path, encoding='utf-8').readlines()
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\n')
        m = re.match(
            r'^>>> \[(\d+)\] op=0x([0-9A-Fa-f]+)\(\w+\) '
            r'flag=0x([0-9A-Fa-f]+) offset=0x([0-9A-Fa-f]+)',
            line
        )
        if m:
            idx    = int(m.group(1))
            op     = int(m.group(2), 16)
            flag   = int(m.group(3), 16)
            offset = int(m.group(4), 16)
            text_lines = []
            i += 1
            while i < len(lines) and lines[i].rstrip('\n') != '<<<':
                text_lines.append(lines[i].rstrip('\n'))
                i += 1
            entries.append({'idx': idx, 'op': op, 'flag': flag,
                            'offset': offset, 'text': '\n'.join(text_lines)})
        i += 1
    return entries


def cmd_import(bin_path: str, txt_path: str, out_path: str, json_path):
    orig = open(bin_path, 'rb').read()
    orig_entries = find_text_entries(orig)
    txt_entries  = parse_txt(txt_path)

    # replace-table 로드
    table = {}
    if json_path is None:
        search_dirs = list(dict.fromkeys([
            os.path.dirname(os.path.abspath(__file__)),
            os.path.dirname(os.path.abspath(bin_path)),
            os.getcwd(),
        ]))
        for d in search_dirs:
            for fname in os.listdir(d):
                if fname.lower().endswith('.json'):
                    candidate = os.path.join(d, fname)
                    try:
                        jdata = json.load(open(candidate, encoding='utf-8-sig'))
                        if 'replace-table' in jdata:
                            json_path = candidate
                            break
                    except Exception:
                        pass
            if json_path:
                break
        if json_path:
            print(f'[import] JSON 자동 탐색: {json_path}')
        else:
            print(f'[경고] replace-table JSON을 찾지 못했습니다.')
            print(f'       올바른 사용법: import <bin> <txt> <out> <table.json>')

    if json_path:
        table = load_replace_table(json_path)
        print(f'[import] replace-table: {len(table)}개 항목 로드')

    if len(txt_entries) != len(orig_entries):
        print(f'[경고] 원본 {len(orig_entries)}개 vs txt {len(txt_entries)}개 — 개수 불일치')

    # ── 1단계: 새 텍스트 바이트 준비 ──────────────────────────
    new_str_bytes = []
    for ti in range(len(orig_entries)):
        oe = orig_entries[ti]
        if ti < len(txt_entries):
            translated = unescape_text(txt_entries[ti]['text'])
            if table:
                translated = apply_replace_table(translated, table)
            try:
                new_str_bytes.append(translated.encode(ENCODING))
            except Exception as ex:
                print(f'[오류] 엔트리 [{ti}] 인코딩 실패: {ex}')
                print(f'       텍스트: {repr(translated[:60])}')
                new_str_bytes.append(orig[oe['str_off']:oe['str_end']])
        else:
            new_str_bytes.append(orig[oe['str_off']:oe['str_end']])

    # ── 2단계: 각 텍스트 이후의 누적 shift 계산 ─────────────
    # shifts[i] = 엔트리 0..i 까지 처리 후의 누적 크기 변화
    shifts = []
    cumulative = 0
    for ti, oe in enumerate(orig_entries):
        old_len = oe['str_end'] - oe['str_off']
        new_len = len(new_str_bytes[ti])
        cumulative += new_len - old_len
        shifts.append(cumulative)

    def shift_for_val(orig_val: int) -> int:
        """원본 오프셋 orig_val에 적용할 shift량: 그 앞까지의 텍스트 변화 합산."""
        result = 0
        for ti, oe in enumerate(orig_entries):
            if orig_val > oe['str_end']:
                result = shifts[ti]
            else:
                break
        return result

    def shift_for_pos(orig_pos: int) -> int:
        """원본 파일 위치 orig_pos에 대응하는 result 위치로의 shift."""
        result = 0
        for ti, oe in enumerate(orig_entries):
            if orig_pos >= oe['str_end'] + 1:
                result = shifts[ti]
            else:
                break
        return result

    # ── 3단계: 원본 code_refs 수집 ──────────────────────────
    code_refs = build_code_ref_map(orig, orig_entries)

    # ── 4단계: 파일 재조립 ───────────────────────────────────
    result = bytearray()
    prev = 0
    for ti, oe in enumerate(orig_entries):
        result.extend(orig[prev:oe['op_off']])   # 코드 블록
        result.append(oe['op'])
        result.append(oe['flag'])
        result.extend(new_str_bytes[ti])          # 새 문자열
        result.append(0x00)                       # null terminator
        prev = oe['str_end'] + 1
    result.extend(orig[prev:])                    # 마지막 코드 블록

    # ── 5단계: code_refs 패치 ───────────────────────────────
    patched = 0
    skip = 0
    for orig_pos, orig_val in code_refs.items():
        res_pos = orig_pos + shift_for_pos(orig_pos)
        new_val = orig_val + shift_for_val(orig_val)

        if res_pos + 1 >= len(result):
            skip += 1
            continue

        cur = struct.unpack_from('<H', result, res_pos)[0]
        if cur == orig_val:
            struct.pack_into('<H', result, res_pos, new_val & 0xFFFF)
            patched += 1
        # cur != orig_val → 텍스트 데이터가 차지한 위치이므로 건드리지 않음

    if skip:
        print(f'[import] 범위 초과로 스킵: {skip}개')
    print(f'[import] 오프셋 패치: {patched}/{len(code_refs)}개')
    open(out_path, 'wb').write(result)
    print(f'[import] → {out_path}  ({len(orig)}B → {len(result)}B)')


def usage():
    print(__doc__)
    sys.exit(1)


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        usage()
    cmd = args[0].lower()
    if cmd == 'extract':
        if len(args) < 3:
            print('사용법: think_tool.py extract <input.bin> <output.txt>')
            sys.exit(1)
        cmd_extract(args[1], args[2])
    elif cmd == 'import':
        if len(args) < 4:
            print('사용법: think_tool.py import <input.bin> <translated.txt> <output.bin> [table.json]')
            sys.exit(1)
        json_path = args[4] if len(args) >= 5 else None
        cmd_import(args[1], args[2], args[3], json_path)
    else:
        usage()
