#!/usr/bin/env python3
"""
think_patch.py - think*.bin 인플레이스 텍스트 패치 도구

텍스트 크기를 원본과 동일하게 유지.
번역 텍스트가 원본보다 짧으면 전각 스페이스(A1 A1) + 반각(0x20) 패딩.
번역 텍스트가 원본보다 길면 잘라냄 (경고 출력).

사용법:
  python think_patch.py extract  input.bin  output.txt
  python think_patch.py patch    input.bin  translated.txt  output.bin  [table.json]
"""

import sys, os, json, struct, re

ENCODING = 'euc_jis_2004'
TEXT_OPCODES = {0x21, 0x1A}
PAD_FULL  = b'\xa1\xa1'   # 전각 스페이스 (2바이트)
PAD_HALF  = b'\x20'       # 반각 스페이스 (1바이트)


def make_padding(n):
    """n바이트 패딩: 전각 스페이스 최대 + 홀수 나머지는 반각."""
    return PAD_FULL * (n // 2) + (PAD_HALF if n % 2 else b'')


def find_text_entries(data):
    entries = []
    i = 0
    n = len(data)
    while i < n - 2:
        op = data[i]
        if op in TEXT_OPCODES:
            flag = data[i+1]
            str_off = i+2
            j = str_off
            while j < n and data[j] != 0:
                j += 1
            if j >= n:
                i += 1; continue
            raw = data[str_off:j]
            if len(raw) < 4:
                i += 1; continue
            try:
                text = raw.decode(ENCODING)
            except:
                i += 1; continue
            entries.append({'op_off': i, 'op': op, 'flag': flag,
                            'str_off': str_off, 'str_end': j, 'text': text})
            i = j + 1
        else:
            i += 1
    filtered = []
    for e in entries:
        if not any(p['str_off'] <= e['op_off'] <= p['str_end'] for p in filtered):
            filtered.append(e)
    return filtered


def escape_text(text):
    return ''.join(f'\\x{ord(c):02x}' if ord(c) < 0x20 and ord(c) != 0x0A else c
                   for c in text)


def unescape_text(text):
    return re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)


def load_replace_table(json_path):
    raw = json.load(open(json_path, encoding='utf-8-sig'))
    return dict(sorted(raw.get('replace-table', {}).items(), key=lambda x: -len(x[0])))


def apply_replace_table(text, table):
    for k, v in table.items():
        text = text.replace(k, v)
    return text


def parse_txt(txt_path):
    lines = open(txt_path, encoding='utf-8').readlines()
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\n')
        m = re.match(r'^>>> \[(\d+)\] op=0x([0-9A-Fa-f]+)\(\w+\) flag=0x([0-9A-Fa-f]+) offset=0x([0-9A-Fa-f]+)', line)
        if m:
            idx = int(m.group(1)); op = int(m.group(2), 16)
            flag = int(m.group(3), 16); offset = int(m.group(4), 16)
            text_lines = []
            i += 1
            while i < len(lines) and lines[i].rstrip('\n') != '<<<':
                text_lines.append(lines[i].rstrip('\n'))
                i += 1
            entries.append({'idx': idx, 'op': op, 'flag': flag,
                            'offset': offset, 'text': '\n'.join(text_lines)})
        i += 1
    return entries


def cmd_extract(bin_path, txt_path):
    data = open(bin_path, 'rb').read()
    entries = find_text_entries(data)
    lines = [
        f'# think_patch extract: {os.path.basename(bin_path)}',
        f'# 총 {len(entries)}개 텍스트 엔트리',
        f'# 번역 시 텍스트 내용만 수정하세요.',
        f'# 텍스트가 원본보다 길면 자동으로 잘림.',
        ''
    ]
    for idx, e in enumerate(entries):
        op_name = 'display' if e['op'] == 0x21 else 'condition'
        lines.append(f">>> [{idx:03d}] op=0x{e['op']:02X}({op_name}) flag=0x{e['flag']:02X} offset=0x{e['op_off']:04X}")
        lines.append(escape_text(e['text']))
        lines.append('<<<')
        lines.append('')
    open(txt_path, 'w', encoding='utf-8').write('\n'.join(lines))
    print(f'[extract] {len(entries)}개 엔트리 → {txt_path}')


def cmd_patch(bin_path, txt_path, out_path, json_path):
    data = bytearray(open(bin_path, 'rb').read())
    orig_entries = find_text_entries(bytes(data))
    txt_entries  = parse_txt(txt_path)

    # replace-table 로드
    table = {}
    if json_path is None:
        for d in list(dict.fromkeys([
            os.path.dirname(os.path.abspath(__file__)),
            os.path.dirname(os.path.abspath(bin_path)),
            os.getcwd()
        ])):
            for fname in os.listdir(d):
                if fname.lower().endswith('.json'):
                    try:
                        j = json.load(open(os.path.join(d, fname), encoding='utf-8-sig'))
                        if 'replace-table' in j:
                            json_path = os.path.join(d, fname)
                            break
                    except: pass
            if json_path: break
        if json_path: print(f'[patch] JSON 자동 탐색: {json_path}')
        else: print('[경고] JSON 없음')

    if json_path:
        table = load_replace_table(json_path)
        print(f'[patch] replace-table: {len(table)}개 로드')

    if len(txt_entries) != len(orig_entries):
        print(f'[경고] 원본 {len(orig_entries)}개 vs txt {len(txt_entries)}개')

    patched = 0
    truncated = 0

    print(f'\n{"idx":>4}  {"슬롯":>5}  {"번역":>5}  {"delta":>6}  비고')
    print('-' * 48)

    for ti, oe in enumerate(orig_entries):
        slot_len = oe['str_end'] - oe['str_off']

        if ti >= len(txt_entries):
            continue

        translated = unescape_text(txt_entries[ti]['text'])
        if table:
            translated = apply_replace_table(translated, table)

        try:
            new_bytes = bytearray(translated.encode(ENCODING))
        except Exception as ex:
            print(f'[오류] [{ti:03d}] 인코딩 실패: {ex}')
            continue

        note = ''
        if len(new_bytes) > slot_len:
            cut = bytes(new_bytes[:slot_len])
            if cut and cut[-1] >= 0x80:
                cut = cut[:-1]
            new_bytes = bytearray(cut)
            truncated += 1
            note = '잘림'

        actual_len = len(new_bytes)
        pad_len = slot_len - actual_len
        delta = actual_len - slot_len

        # 슬롯: 텍스트 + 패딩 (전각 최대 + 홀수 나머지 반각)
        slot = bytearray(slot_len)
        slot[:actual_len] = new_bytes
        slot[actual_len:] = make_padding(pad_len)

        data[oe['str_off']:oe['str_end']] = slot
        patched += 1

        if delta != 0 or note:
            pad_desc = ''
            if pad_len > 0:
                n_full = pad_len // 2
                n_half = pad_len % 2
                parts = []
                if n_full: parts.append(f'전각{n_full}')
                if n_half: parts.append(f'반각{n_half}')
                pad_desc = f'패딩 {pad_len}B ({"+".join(parts)})'
            print(f'[{ti:03d}]  {slot_len:>4}B  {actual_len:>4}B  {delta:>+5}B  {note or pad_desc}')

    print('-' * 48)
    open(out_path, 'wb').write(data)
    print(f'\n[patch] {patched}개 패치, {truncated}개 잘림')
    print(f'[patch] → {out_path}  ({len(data)}B)')


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(1)
    cmd = args[0].lower()
    if cmd == 'extract':
        if len(args) < 3: print('사용법: think_patch.py extract <input.bin> <output.txt>'); sys.exit(1)
        cmd_extract(args[1], args[2])
    elif cmd == 'patch':
        if len(args) < 4: print('사용법: think_patch.py patch <input.bin> <translated.txt> <output.bin> [table.json]'); sys.exit(1)
        json_path = args[4] if len(args) >= 5 else None
        cmd_patch(args[1], args[2], args[3], json_path)
    else:
        print(__doc__); sys.exit(1)
