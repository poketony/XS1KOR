#!/usr/bin/env python3
"""
headerlst_tool.py  –  Xenosaga UMN 메일/용어사전 인덱스 파일 추출/재삽입 도구
지원: header.lst (UMN 메일), dbheader.lst (용어사전)
python headerlst_tool.py extract header.lst
python headerlst_tool.py rebuild header.lst header.tsv
"""

import sys, os, json, struct

ENCODE_SRC = 'euc-jp'
REC_HDR_SZ = 8 + 0x60   # META + UML헤더

MAIL_SUBJ_PREFIX = '件名：'
MAIL_SNDR_PREFIX = '差出人：'
MAIL_SUBJ_MARKER = b'\x0d\x02'
DB_SUBJ_LEADER   = b'\x0d\x02'
DB_SUBJ_TRAILER  = b'\x0d\x00'
DB_IDX_MIN       = 9000


# ── null 탐색 ──────────────────────────────────────────────
def find_real_null(data, start):
    i = start
    while i < len(data):
        b = data[i]
        if b == 0x00:           return i
        elif b == 0x0C and i+3 < len(data): i += 4
        elif b == 0x0D and i+1 < len(data): i += 2
        elif b == 0x19 and i+1 < len(data): i += 2
        elif b >= 0xA1 and i+1 < len(data): i += 2
        else: i += 1
    return -1


# ── 치환표 ────────────────────────────────────────────────
def load_charmap(lst_path):
    dirpath = os.path.dirname(os.path.abspath(lst_path))
    for name in os.listdir(dirpath):
        if name.lower().endswith('.json'):
            try:
                j = json.loads(open(os.path.join(dirpath, name), 'rb').read().decode('utf-8-sig'))
                if 'replace-table' in j:
                    rt = j['replace-table']
                    return dict(sorted(rt.items(), key=lambda x: len(x[0]), reverse=True))
            except Exception:
                pass
    return None


def apply_charmap(text, charmap):
    result, i = [], 0
    while i < len(text):
        for k, v in charmap.items():
            if text[i:i+len(k)] == k:
                result.append(v); i += len(k); break
        else:
            result.append(text[i]); i += 1
    return ''.join(result)


def encode_str(text, charmap):
    if charmap:
        text = apply_charmap(text, charmap)
    return text.encode(ENCODE_SRC, errors='replace') + b'\x00'


# ── 레코드 ────────────────────────────────────────────────
class LSTRecord:
    def __init__(self, rec_size, idx, uml_hdr, subj_raw, sndr_raw):
        self.rec_size = rec_size
        self.idx      = idx
        self.uml_hdr  = uml_hdr
        self.subj_raw = subj_raw
        self.sndr_raw = sndr_raw

    @property
    def is_db(self):
        return self.idx >= DB_IDX_MIN

    @property
    def subj_text(self):
        return self.subj_raw.decode(ENCODE_SRC, errors='replace')

    @property
    def sndr_text(self):
        return self.sndr_raw.decode(ENCODE_SRC, errors='replace')

    @property
    def subj_content(self):
        s = self.subj_text
        if self.is_db:
            if s.startswith('\r\x02'): s = s[2:]
            if s.endswith('\r\x00'):   s = s[:-2]
            elif s.endswith('\r'):     s = s[:-1]
        else:
            if s.startswith(MAIL_SUBJ_PREFIX): s = s[len(MAIL_SUBJ_PREFIX):]
            if s.startswith('\r\x02'):         s = s[2:]
        return s.rstrip('\r\x00')

    @property
    def sndr_content(self):
        s = self.sndr_text
        if s.startswith(MAIL_SNDR_PREFIX): s = s[len(MAIL_SNDR_PREFIX):]
        return s.rstrip('\r\x00')

    @property
    def has_mail_marker(self):
        return MAIL_SUBJ_MARKER in self.subj_raw

    def to_bytes(self, new_subj=None, new_sndr=None, charmap=None):
        # 제목
        if new_subj is not None and new_subj != self.subj_content:
            if self.is_db:
                body = encode_str(new_subj, charmap)   # null-terminated
                subj_bytes = DB_SUBJ_LEADER + body[:-1] + DB_SUBJ_TRAILER + b'\x00'
            else:
                prefix = MAIL_SUBJ_PREFIX.encode(ENCODE_SRC)
                marker = MAIL_SUBJ_MARKER if self.has_mail_marker else b''
                subj_bytes = prefix + marker + encode_str(new_subj, charmap)
        else:
            subj_bytes = self.subj_raw + b'\x00'

        # 발신자 (db는 항상 원본 raw)
        if not self.is_db and new_sndr is not None and new_sndr != self.sndr_content:
            prefix = MAIL_SNDR_PREFIX.encode(ENCODE_SRC)
            sndr_bytes = prefix + encode_str(new_sndr, charmap)
        else:
            sndr_bytes = self.sndr_raw + b'\x00'

        # 4바이트 정렬 패딩
        str_section = subj_bytes + sndr_bytes
        pad = ((len(str_section) + 3) & ~3) - len(str_section)

        # rec_size = 자신을 포함한 4바이트 블록 뒤부터 끝까지의 길이
        # = 전체 레코드 크기 - 4
        total = 4 + 4 + len(self.uml_hdr) + len(str_section) + pad
        new_rec_size = total - 4

        out = bytearray()
        out += struct.pack('<I', new_rec_size)
        out += struct.pack('<I', self.idx)
        out += self.uml_hdr
        out += str_section
        out += b'\x00' * pad
        return bytes(out)


# ── 파일 파싱 ─────────────────────────────────────────────
def parse_lst(data):
    uml_pos = []
    i = 0
    while i < len(data) - 3:
        if data[i:i+4] == b'UML\x00':
            uml_pos.append(i); i += 4
        else:
            i += 1

    records = []
    for rs in [p - 8 for p in uml_pos]:
        rec_size = int.from_bytes(data[rs:rs+4],   'little')
        idx      = int.from_bytes(data[rs+4:rs+8], 'little')
        uml_hdr  = data[rs+8 : rs+REC_HDR_SZ]
        str_start = rs + REC_HDR_SZ
        end1 = find_real_null(data, str_start)
        end2 = find_real_null(data, end1 + 1)
        records.append(LSTRecord(rec_size, idx, uml_hdr,
                                 data[str_start:end1], data[end1+1:end2]))
    return records


def records_to_bytes(records, translations=None, charmap=None, orig_size=None):
    out = bytearray()
    for r in records:
        if translations and r.idx in translations:
            t = translations[r.idx]
            out += r.to_bytes(t[0], t[1] if len(t) > 1 else None, charmap)
        else:
            out += r.to_bytes()
    if orig_size and len(out) < orig_size:
        out += b'\x00' * (orig_size - len(out))
    return bytes(out)


# ── TSV ───────────────────────────────────────────────────
def records_to_tsv(records):
    is_db = records[0].is_db if records else False
    hdr   = 'index\tsubject\n' if is_db else 'index\tsubject\tsender\n'
    lines = [hdr]
    for r in records:
        if is_db:
            lines.append(f'{r.idx}\t{r.subj_content}\n')
        else:
            lines.append(f'{r.idx}\t{r.subj_content}\t{r.sndr_content}\n')
    return ''.join(lines)


def parse_tsv(tsv_text):
    result = {}
    for line in tsv_text.split('\n'):
        line = line.rstrip('\r')
        if not line or line.startswith('index\t'): continue
        parts = line.split('\t')
        if len(parts) >= 2:
            try:    result[int(parts[0])] = tuple(parts[1:])
            except: pass
    return result


# ── CLI ───────────────────────────────────────────────────
def cmd_extract(args):
    if not args: print('사용법: extract <파일.lst> [출력.tsv]'); return
    lst_path = args[0]
    tsv_path = args[1] if len(args) > 1 else lst_path.replace('.lst', '.tsv')
    data    = open(lst_path, 'rb').read()
    records = parse_lst(data)
    open(tsv_path, 'w', encoding='utf-8').write(records_to_tsv(records))
    kind = 'dbheader' if records[0].is_db else 'header'
    print(f'[추출] {lst_path}  ({kind}, {len(records)}개)  →  {tsv_path}')
    for r in records[:5]:
        line = f'  [{r.idx}] {r.subj_content!r}'
        if not r.is_db: line += f'  /  {r.sndr_content!r}'
        print(line)
    print('  ...')


def cmd_rebuild(args):
    if len(args) < 2: print('사용법: rebuild <원본.lst> <번역.tsv> [출력.lst]'); return
    lst_path = args[0]
    tsv_path = args[1]
    out_path = args[2] if len(args) > 2 else lst_path.replace('.lst', '_new.lst')
    data    = open(lst_path, 'rb').read()
    records = parse_lst(data)
    trans   = parse_tsv(open(tsv_path, 'r', encoding='utf-8').read())
    charmap = load_charmap(lst_path)
    if charmap: print(f'  치환표 로드됨: {len(charmap)} 항목')
    else:       print('  치환표 없음')
    result = records_to_bytes(records, trans, charmap, orig_size=len(data))
    open(out_path, 'wb').write(result)
    kind = 'dbheader' if records[0].is_db else 'header'
    print(f'[재조립] {lst_path}  ({kind}, TSV {len(trans)}개)  →  {out_path}  ({len(result)} bytes)')


def cmd_roundtrip(args):
    if not args: print('사용법: roundtrip <파일.lst>'); return
    lst_path = args[0]
    data    = open(lst_path, 'rb').read()
    records = parse_lst(data)
    result  = records_to_bytes(records, orig_size=len(data))
    kind    = 'dbheader' if records[0].is_db else 'header'
    if result == data:
        print(f'✓ Roundtrip 완벽 일치  ({kind}, {len(records)}개, {len(data)} bytes)')
    else:
        print(f'✗ Roundtrip 불일치  ({len(result)} vs {len(data)})')
        for i, (a, b) in enumerate(zip(result, data)):
            if a != b:
                print(f'  첫 불일치 0x{i:x}: got {a:02x} expected {b:02x}')
                print(f'  ctx: {data[i-4:i+8].hex()}')
                break


def main():
    if len(sys.argv) < 2: print(__doc__); sys.exit(1)
    {'extract': cmd_extract, 'rebuild': cmd_rebuild, 'roundtrip': cmd_roundtrip
     }.get(sys.argv[1].lower(), lambda a: print(f'알 수 없는 명령: {sys.argv[1]}'))(sys.argv[2:])

if __name__ == '__main__':
    main()
