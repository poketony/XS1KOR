#!/usr/bin/env python3
"""
xeno_evt.py  -  Xenosaga Episode 1 EVT / class 텍스트 추출·재조립

사용법:
  python xeno_evt.py <file.evt>                        추출 → <file.evt>.txt
  python xeno_evt.py <file.evt> <번역.txt>             재조립 → <file.evt>.new  (치환 없음)
  python xeno_evt.py <file.evt> <번역.txt> <map.json>  재조립 → <file.evt>.new  (한글→한자 치환)
  python xeno_evt.py <file.evt> --verify               라운드트립 검증
  python xeno_evt.py <file.evt> --list                 청크 목록

규칙:
  - 추출은 항상 전체 출력. 줄 수 변경 금지.
  - 번역 안 할 줄은 원문 그대로.
"""

import struct, sys, os, json

CAFEBABE   = b'\xca\xfe\xba\xbe'
CONST_LENS = {5:8, 6:8, 7:2, 8:2, 16:2}

def tag_len(tag): return CONST_LENS.get(tag, 4)

def load_table(path):
    for enc in ('utf-8-sig', 'utf-8'):
        try:
            with open(path, encoding=enc) as f:
                return json.load(f).get('replace-table', {})
        except: pass
    return {}

def apply_table(s, tbl):
    return ''.join(tbl.get(c, c) for c in s) if tbl else s

def to_fullwidth(s):
    """반각 공백→、, 반각 문장부호/숫자/알파벳→전각. <lf>는 보존.
    EUC-JP 인코딩 불가 전각 문자는 올바른 대응 코드로 교체:
      ＂(FF02)→"(201C)  ＇(FF07)→'(2018)  －(FF0D)→−(2212)  ～(FF5E)→〜(301C)
    """
    import re
    EUCJP_FIX = {'\uff02':'\u201c', '\uff07':'\u2018',
                 '\uff0d':'\u2212', '\uff5e':'\u301c'}
    parts = re.split(r'(<lf>)', s)
    result = []
    for part in parts:
        if part == '<lf>':
            result.append('<lf>')
        else:
            converted = []
            for c in part:
                if c == ' ':
                    converted.append('\u3001')  # 、
                elif '!' <= c <= '~':
                    fw = chr(ord(c) + 0xFEE0)
                    converted.append(EUCJP_FIX.get(fw, fw))
                else:
                    converted.append(c)
            result.append(''.join(converted))
    return ''.join(result)

def process_sub_tag(s):
    """줄 끝의 [sub] 태그를 처리. 있으면 전각 변환 후 태그 제거."""
    if s.endswith('[sub]'):
        return to_fullwidth(s[:-5])
    return s

def parse(chunk):
    if chunk[:4] != CAFEBABE: return None
    p = 0
    magic = struct.unpack_from('>I',chunk,p)[0]; p+=4
    vj    = struct.unpack_from('>H',chunk,p)[0]; p+=2
    vn    = struct.unpack_from('>H',chunk,p)[0]; p+=2
    cnum  = struct.unpack_from('>H',chunk,p)[0]; p+=2
    entries=[]; idx=[]; pool={}
    for i in range(cnum-1):
        if p >= len(chunk): break
        tag = chunk[p]; p+=1
        if tag == 1:
            n = struct.unpack_from('>H',chunk,p)[0]; p+=2
            pool[i] = chunk[p:p+n]; p+=n
            entries.append((1, i))
        elif tag == 8:
            ref = struct.unpack_from('>H',chunk,p)[0]; p+=2
            entries.append((8, ref-1)); idx.append(ref-1)
        else:
            tl = tag_len(tag)
            entries.append((tag, chunk[p:p+tl])); p+=tl
    return dict(header=(magic,vj,vn,cnum),
                entries=entries, rest=chunk[p:], idx=idx, pool=pool)

def decode(raw):
    raw = raw.rstrip(b'\x00')
    for enc in ('euc-jp','shift-jis','latin-1'):
        try: return raw.decode(enc)
        except: pass
    return raw.decode('latin-1', errors='replace')

def get_strings(p):
    return [decode(p['pool'][i]).replace('\n','<lf>')
            for i in p['idx'] if i in p['pool']]

def rebuild(p, new_strs, tbl):
    magic,vj,vn,cnum = p['header']
    pool = p['pool']
    pool_new = {}
    for k, pidx in enumerate(p['idx']):
        if k >= len(new_strs): break
        orig_raw = pool[pidx]
        has_null = orig_raw.endswith(b'\x00')
        s = apply_table(process_sub_tag(new_strs[k]).replace('<lf>','\n'), tbl)
        try:    enc = s.encode('euc-jp')
        except: enc = s.encode('euc-jp', errors='replace')
        pool_new[pidx] = enc + (b'\x00' if has_null else b'')
    out = bytearray(struct.pack('>IHHH', magic,vj,vn,cnum))
    for tag, val in p['entries']:
        out += bytes([tag])
        if tag == 1:
            raw = pool_new.get(val, pool[val])
            out += struct.pack('>H', len(raw)) + raw
        elif tag == 8:
            out += struct.pack('>H', val+1)
        else:
            out += val
    return bytes(out + p['rest'])

def fl00_toc(data):
    if data[:4] != b'FL00': return None
    toc, pos = [], 0x18
    while pos+16 <= len(data):
        _,off,sz,_ = struct.unpack_from('<4I',data,pos)
        if not (4<=off<len(data) and 0<sz<=len(data)): break
        if data[off:off+4] != CAFEBABE: break
        toc.append([off,sz]); pos+=16
    return toc

def fl00_write(data, toc, patches):
    """
    청크를 교체하고 FL00 헤더 내 모든 오프셋 포인터를 갱신.
    갱신 대상:
      - 0x08: file_size
      - 0x0c: 파일명 테이블 시작 (마지막 청크 끝)
      - 0x14: 파일명 테이블 시작+2
      - TOC[+4]: 각 청크 offset
      - TOC[+8]: 각 청크 size
      - TOC[+12] (unk2): 변경된 청크 이후 영역 포인터 → delta 적용
    """
    out = bytearray(data)

    # 청크를 뒤에서부터 교체
    for ci in sorted(patches, key=lambda i: toc[i][0], reverse=True):
        orig_off = toc[ci][0]
        orig_sz  = toc[ci][1]
        nb       = patches[ci]
        delta    = len(nb) - orig_sz

        out = out[:orig_off] + bytearray(nb) + out[orig_off+orig_sz:]

        # delta가 0이면 포인터 갱신 불필요
        if delta == 0:
            toc[ci][1] = len(nb)
            continue

        # orig_off보다 뒤에 있는 포인터들을 delta만큼 조정
        # 1) TOC off/sz 갱신
        tp = 0x18
        for j in range(len(toc)):
            if toc[j][0] > orig_off:
                toc[j][0] += delta
            tp += 16
        toc[ci][1] = len(nb)

        # TOC 바이트 갱신 (off, sz)
        tp = 0x18
        for j in range(len(toc)):
            struct.pack_into('<I', out, tp+4, toc[j][0])
            struct.pack_into('<I', out, tp+8, toc[j][1])
            tp += 16

        # 2) TOC unk2 갱신: orig_off보다 뒤를 가리키는 값이면 delta 적용
        tp = 0x18
        for j in range(len(toc)):
            unk2 = struct.unpack_from('<I', out, tp+12)[0]
            # 유효한 파일 오프셋 범위이고 변경된 청크 이후를 가리키면
            if orig_off < unk2 < 0x80000000:
                struct.pack_into('<I', out, tp+12, unk2 + delta)
            tp += 16

        # 3) 헤더 0x0c, 0x14 갱신
        for hdr_off in (0x0c, 0x14):
            val = struct.unpack_from('<I', out, hdr_off)[0]
            if orig_off < val < 0x80000000:
                struct.pack_into('<I', out, hdr_off, val + delta)

    # file_size 갱신
    struct.pack_into('<I', out, 0x08, len(out))
    return bytes(out)

def data_to_lines(data):
    lines = []
    if data[:4] == b'FL00':
        toc = fl00_toc(data)
        if not toc: return []
        for i,(off,sz) in enumerate(toc):
            cnum = struct.unpack_from('>H',data,off+8)[0]
            if cnum==0: continue
            p = parse(data[off:off+sz])
            if not p: continue
            strs = get_strings(p)
            if strs:
                lines.append(f"# chunk {i} @ {off:#x} cnum={cnum} strings={len(strs)}")
                lines.extend(strs); lines.append("")
    elif data[:4] == CAFEBABE:
        p = parse(data)
        if p: lines.extend(get_strings(p))
    return lines

def parse_txt(lines):
    chunks = {}
    cur_ci, cur = None, []
    for line in lines:
        if line.startswith('# chunk '):
            if cur_ci is not None: chunks[cur_ci] = cur
            try:    cur_ci = int(line.split()[2])
            except: cur_ci = None
            cur = []
        elif line == '': continue
        else: cur.append(line)
    if cur_ci is not None:   chunks[cur_ci] = cur
    elif not chunks and cur: chunks[None]   = cur
    return chunks

def apply_patches(data, chunks, tbl):
    if data[:4] == b'FL00':
        toc = fl00_toc(data)
        if not toc: print("FL00 TOC 파싱 실패"); return None
        patches = {}
        for ci,strs in chunks.items():
            if ci is None or ci >= len(toc):
                print(f"  [건너뜀] 청크 {ci}: 범위 초과"); continue
            off,sz = toc[ci]
            p = parse(data[off:off+sz])
            if not p: print(f"  [건너뜀] 청크 {ci}: 파싱 실패"); continue
            orig_n = len(p['idx'])
            if len(strs) != orig_n:
                print(f"  [오류] 청크 {ci}: 원본 {orig_n}줄 ≠ 번역 {len(strs)}줄"); continue
            rb = rebuild(p, strs, tbl)
            patches[ci] = rb
            print(f"  청크 {ci}: {sz}B → {len(rb)}B  ({len(rb)-sz:+d})")
        return fl00_write(data, toc, patches)
    elif data[:4] == CAFEBABE:
        strs = chunks.get(None, chunks.get(0,[]))
        p = parse(data)
        if not p: return None
        if len(strs) != len(p['idx']):
            print(f"[오류] 원본 {len(p['idx'])}줄 ≠ 번역 {len(strs)}줄"); return None
        return rebuild(p, strs, tbl)
    print(f"알 수 없는 포맷: {data[:4].hex()}"); return None

def do_extract(evt_path):
    data = open(evt_path,'rb').read()
    lines = data_to_lines(data)
    if not lines: print("추출된 문자열 없음"); return
    out_path = evt_path + '.txt'
    open(out_path,'w',encoding='utf-8').write('\n'.join(lines))
    n = len([l for l in lines if l and not l.startswith('#')])
    print(f"추출: {out_path}  ({n}줄)")

def find_map(evt_path, txt_path, explicit=None):
    """XENOSAGA_KOR-JPN.json 탐색: 명시 경로 → evt 폴더 → 스크립트 폴더"""
    name = 'XENOSAGA_KOR-JPN.json'
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(evt_path)), name))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(txt_path)), name))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def do_rebuild(evt_path, txt_path, map_path=None):
    data   = open(evt_path,'rb').read()
    lines  = open(txt_path, encoding='utf-8').read().splitlines()
    chunks = parse_txt(lines)
    tbl = {}
    found = find_map(evt_path, txt_path, map_path)
    if found:
        tbl = load_table(found)
        print(f"치환 테이블: {found}  ({len(tbl)}개)")
    else:
        print("치환 테이블 없음 (XENOSAGA_KOR-JPN.json을 같은 폴더에 두거나 세 번째 인자로 지정)")
    out = apply_patches(data, chunks, tbl)
    if out is None: return
    out_path = evt_path + '.new'
    open(out_path,'wb').write(out)
    print(f"재조립: {out_path}")

def do_verify(evt_path):
    data    = open(evt_path,'rb').read()
    lines   = data_to_lines(data)
    rebuilt = apply_patches(data, parse_txt(lines), {})
    if rebuilt is None: return
    if data == rebuilt:
        print(f"✓ 라운드트립 성공: {os.path.basename(evt_path)}")
    else:
        print(f"✗ 라운드트립 실패: {len(data)}B / {len(rebuilt)}B")
        for i,(a,b) in enumerate(zip(data,rebuilt)):
            if a!=b: print(f"  첫 번째 차이: {i:#x}  원본={a:#04x}  재조립={b:#04x}"); break

def do_list(evt_path):
    data = open(evt_path,'rb').read()
    if data[:4] != b'FL00': print("FL00 파일이 아닙니다"); return
    toc = fl00_toc(data)
    print(f"{'#':<4} {'offset':<12} {'size':<10} {'cnum':<6} strings")
    print("-"*44)
    for i,(off,sz) in enumerate(toc):
        cnum = struct.unpack_from('>H',data,off+8)[0]
        p = parse(data[off:off+sz]) if cnum>0 else None
        ns = len(get_strings(p)) if p else 0
        print(f"{i:<4} {off:#012x} {sz:<10} {cnum:<6} {ns}")

def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h','--help'):
        print(__doc__); return
    evt_path = args[0]
    if len(args) == 1:
        do_extract(evt_path)
    elif args[1] == '--verify':
        do_verify(evt_path)
    elif args[1] == '--list':
        do_list(evt_path)
    elif os.path.isfile(args[1]):
        txt_path = args[1]
        map_path = args[2] if len(args) >= 3 and os.path.isfile(args[2]) else None
        do_rebuild(evt_path, txt_path, map_path)
    else:
        print(f"파일을 찾을 수 없습니다: {args[1]}")

if __name__ == '__main__':
    main()
