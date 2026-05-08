#!/usr/bin/env python3
"""
xeno_evt.py  -  Xenosaga Episode 1 EVT / class 텍스트 추출·재조립

사용법:
  python xeno_evt.py <file.evt>            추출 → <file.evt>.txt  (바이트코드 실행 순서)
  python xeno_evt.py <file.evt> <번역.txt> 재조립 → <file.evt>.new
  python xeno_evt.py <file.evt> --verify   라운드트립 검증
  python xeno_evt.py <file.evt> --list     청크 목록

규칙:
  - txt는 바이트코드 실행 순서로 출력됨. 이 순서 그대로 번역하면 됨.
  - 줄 수 변경 금지. 번역 안 할 줄은 원문 그대로.
  - [sub] 태그: 줄 끝에 붙이면 반각→전각 변환 + 반각 공백→、
  - XENOSAGA_KOR-JPN.json이 같은 폴더에 있으면 재조립 시 자동 적용.
"""

import struct, sys, os, json, re

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
                    converted.append('\u3001')
                elif '!' <= c <= '~':
                    fw = chr(ord(c) + 0xFEE0)
                    converted.append(EUCJP_FIX.get(fw, fw))
                else:
                    converted.append(c)
            result.append(''.join(converted))
    return ''.join(result)

def process_sub_tag(s):
    if s.endswith('[sub]'):
        return to_fullwidth(s[:-5])
    return s

# ── CAFEBABE 파싱 ────────────────────────────────────────────────────────────
def parse(chunk):
    if chunk[:4] != CAFEBABE: return None
    p = 0
    magic = struct.unpack_from('>I',chunk,p)[0]; p+=4
    vj    = struct.unpack_from('>H',chunk,p)[0]; p+=2
    vn    = struct.unpack_from('>H',chunk,p)[0]; p+=2
    cnum  = struct.unpack_from('>H',chunk,p)[0]; p+=2
    entries=[]; tag8_order=[]; pool={}; pool_all={}
    for i in range(cnum-1):
        if p >= len(chunk): break
        tag = chunk[p]; p+=1
        if tag == 1:
            n = struct.unpack_from('>H',chunk,p)[0]; p+=2
            pool[i] = chunk[p:p+n]; p+=n
            pool_all[i] = (1, pool[i])
            entries.append((1, i))
        elif tag == 8:
            ref = struct.unpack_from('>H',chunk,p)[0]; p+=2
            str_idx = ref-1
            pool_all[i] = (8, str_idx)
            entries.append((8, str_idx)); tag8_order.append(str_idx)
        else:
            tl = tag_len(tag)
            entries.append((tag, chunk[p:p+tl])); p+=tl
            pool_all[i] = (tag, None)
    rest = chunk[p:]

    # 바이트코드에서 ldc/ldc_w 참조 순서 추출
    bytecode_str_order = []  # str_pool_idx 목록 (중복 제거, 첫 참조 순서)
    seen = set()
    i = 0
    while i < len(rest):
        op = rest[i]
        if op == 0x12:   cidx = rest[i+1]-1; i+=2
        elif op == 0x13: cidx = struct.unpack_from('>H',rest,i+1)[0]-1; i+=3
        else: i+=1; continue
        e = pool_all.get(cidx)
        if e and e[0] == 8:
            stridx = e[1]
            if stridx not in seen and stridx in pool:
                seen.add(stridx); bytecode_str_order.append(stridx)

    # str_pool_idx -> tag8_k (tag8 선언 순서에서 몇 번째)
    str_to_k = {s:k for k,s in enumerate(tag8_order)}

    # 바이트코드 순서 -> tag8_k 매핑
    bc_to_tag8k = [str_to_k[s] for s in bytecode_str_order if s in str_to_k]

    # tag8에만 있고 바이트코드에 없는 항목 (원본 유지 대상)
    bc_str_set = set(bytecode_str_order)
    unmapped = [k for k,s in enumerate(tag8_order) if s not in bc_str_set]

    return dict(header=(magic,vj,vn,cnum),
                entries=entries, rest=rest, pool=pool,
                tag8_order=tag8_order,       # tag8 선언 순서 str_pool_idx 목록
                bc_to_tag8k=bc_to_tag8k,     # 바이트코드 순서 n -> tag8_k
                unmapped=unmapped)           # 바이트코드에 없는 tag8_k 목록

EMPTY_MARKER = '[empty]'

def decode(raw):
    if raw == b'': return EMPTY_MARKER
    raw = raw.rstrip(b'\x00')
    if raw == b'': return EMPTY_MARKER
    for enc in ('euc-jp','shift-jis','latin-1'):
        try: return raw.decode(enc)
        except: pass
    return raw.decode('latin-1', errors='replace')

def get_strings_bc_order(p):
    """바이트코드 실행 순서로 문자열 반환."""
    pool = p['pool']
    return [decode(pool[p['tag8_order'][k]]).replace('\n','<lf>')
            for k in p['bc_to_tag8k']]

def get_strings_tag8_order(p):
    """tag8 선언 순서로 문자열 반환 (레거시)."""
    return [decode(p['pool'][i]).replace('\n','<lf>')
            for i in p['tag8_order'] if i in p['pool']]

# ── 재조립 ───────────────────────────────────────────────────────────────────
def rebuild(p, new_strs_bc, tbl):
    """
    new_strs_bc: 바이트코드 순서 문자열 목록
    내부적으로 tag8 선언 순서로 재매핑해서 패치.
    """
    magic,vj,vn,cnum = p['header']
    pool = p['pool']
    bc_to_tag8k = p['bc_to_tag8k']
    tag8_order  = p['tag8_order']

    # tag8_k -> 새 bytes  (바이트코드 순서 txt에서 매핑)
    pool_new = {}
    for n, new_str in enumerate(new_strs_bc):
        if n >= len(bc_to_tag8k): break
        tag8_k = bc_to_tag8k[n]
        if tag8_k >= len(tag8_order): continue
        str_pool_idx = tag8_order[tag8_k]
        orig_raw = pool.get(str_pool_idx, b'')
        has_null = orig_raw.endswith(b'\x00')
        if new_str == EMPTY_MARKER:
            pool_new[str_pool_idx] = orig_raw
        else:
            s = apply_table(process_sub_tag(new_str).replace('<lf>','\n'), tbl)
            try:    enc = s.encode('euc-jp')
            except: enc = s.encode('euc-jp', errors='replace')
            pool_new[str_pool_idx] = enc + (b'\x00' if has_null else b'')

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

# ── FL00 ─────────────────────────────────────────────────────────────────────
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
    out = bytearray(data)
    for ci in sorted(patches, key=lambda i: toc[i][0], reverse=True):
        orig_off = toc[ci][0]; orig_sz = toc[ci][1]
        nb = patches[ci]; delta = len(nb)-orig_sz
        out = out[:orig_off] + bytearray(nb) + out[orig_off+orig_sz:]
        if delta == 0:
            toc[ci][1] = len(nb); continue
        for j in range(len(toc)):
            if toc[j][0] > orig_off: toc[j][0] += delta
        toc[ci][1] = len(nb)
        tp = 0x18
        for j in range(len(toc)):
            struct.pack_into('<I',out,tp+4,toc[j][0])
            struct.pack_into('<I',out,tp+8,toc[j][1])
            tp+=16
        tp = 0x18
        for j in range(len(toc)):
            unk2 = struct.unpack_from('<I',out,tp+12)[0]
            if orig_off < unk2 < 0x80000000:
                struct.pack_into('<I',out,tp+12,unk2+delta)
            tp+=16
        for hdr_off in (0x0c, 0x14):
            val = struct.unpack_from('<I',out,hdr_off)[0]
            if orig_off < val < 0x80000000:
                struct.pack_into('<I',out,hdr_off,val+delta)
    struct.pack_into('<I',out,0x08,len(out))
    return bytes(out)

# ── 추출 ─────────────────────────────────────────────────────────────────────
def data_to_lines(data):
    """FL00/class 파일에서 바이트코드 순서로 텍스트 추출."""
    lines = []
    if data[:4] == b'FL00':
        toc = fl00_toc(data)
        if not toc: return []
        for i,(off,sz) in enumerate(toc):
            cnum = struct.unpack_from('>H',data,off+8)[0]
            if cnum==0: continue
            p = parse(data[off:off+sz])
            if not p: continue
            strs = get_strings_bc_order(p)
            if strs:
                # 헤더에 매핑 정보 삽입
                bc_map   = ','.join(map(str, p['bc_to_tag8k']))
                unmapped = ','.join(map(str, p['unmapped'])) if p['unmapped'] else ''
                total    = len(p['tag8_order'])
                lines.append(f"# chunk {i} @ {off:#x} cnum={cnum} total={total}")
                lines.append(f"# bc_map: {bc_map}")
                if unmapped:
                    lines.append(f"# bc_unmap: {unmapped}")
                lines.extend(strs)
                lines.append("")
    elif data[:4] == CAFEBABE:
        p = parse(data)
        if p: lines.extend(get_strings_bc_order(p))
    return lines

# ── txt 파싱 ─────────────────────────────────────────────────────────────────
def parse_txt(lines):
    """
    반환: {chunk_index: {'strs': [...], 'bc_map': [...], 'unmapped': [...], 'total': int}}
    bc_map/unmapped가 없으면 레거시(tag8 순서) 모드로 처리.
    """
    chunks = {}
    cur_ci = None
    cur_strs = []; cur_bc_map = None; cur_unmapped = []; cur_total = None

    def flush():
        if cur_ci is not None:
            chunks[cur_ci] = {
                'strs': cur_strs,
                'bc_map': cur_bc_map,
                'unmapped': cur_unmapped,
                'total': cur_total,
            }

    for line in lines:
        if line.startswith('# chunk '):
            flush()
            cur_strs=[]; cur_bc_map=None; cur_unmapped=[]; cur_total=None
            parts = line.split()
            try:    cur_ci = int(parts[2])
            except: cur_ci = None
            # total= 파싱
            for part in parts:
                if part.startswith('total='):
                    try: cur_total = int(part.split('=')[1])
                    except: pass
        elif line.startswith('# bc_map:'):
            cur_bc_map = list(map(int, line.split(':',1)[1].strip().split(',')))
        elif line.startswith('# bc_unmap:'):
            s = line.split(':',1)[1].strip()
            cur_unmapped = list(map(int, s.split(','))) if s else []
        elif line == '':
            continue
        else:
            cur_strs.append(line)
    flush()

    # 헤더 없는 단독 .class 케이스
    if not chunks and cur_strs:
        chunks[None] = {'strs': cur_strs, 'bc_map': None, 'unmapped': [], 'total': None}
    return chunks

# ── 패치 적용 ─────────────────────────────────────────────────────────────────
def apply_patches(data, chunks, tbl):
    if data[:4] == b'FL00':
        toc = fl00_toc(data)
        if not toc: print("FL00 TOC 파싱 실패"); return None
        patches = {}
        for ci, chunk_data in chunks.items():
            if ci is None or ci >= len(toc):
                print(f"  [건너뜀] 청크 {ci}: 범위 초과"); continue
            off,sz = toc[ci]
            p = parse(data[off:off+sz])
            if not p: print(f"  [건너뜀] 청크 {ci}: 파싱 실패"); continue

            strs    = chunk_data['strs']
            bc_map  = chunk_data['bc_map']

            if bc_map is not None:
                # 바이트코드 순서 모드
                expected = len(p['bc_to_tag8k'])
                if len(strs) != expected:
                    print(f"  [오류] 청크 {ci}: 바이트코드 항목 {expected}개 ≠ txt {len(strs)}줄"); continue
                rb = rebuild(p, strs, tbl)
            else:
                # 레거시: tag8 선언 순서
                expected = len(p['tag8_order'])
                if len(strs) != expected:
                    print(f"  [오류] 청크 {ci}: tag8 항목 {expected}개 ≠ txt {len(strs)}줄"); continue
                # tag8 순서 rebuild
                pool_new = {}
                for k, (str_pool_idx, new_str) in enumerate(zip(p['tag8_order'], strs)):
                    orig_raw = p['pool'].get(str_pool_idx, b'')
                    has_null = orig_raw.endswith(b'\x00')
                    s = apply_table(process_sub_tag(new_str).replace('<lf>','\n'), tbl)
                    try:    enc = s.encode('euc-jp')
                    except: enc = s.encode('euc-jp', errors='replace')
                    pool_new[str_pool_idx] = enc + (b'\x00' if has_null else b'')
                out = bytearray(struct.pack('>IHHH', *p['header']))
                for tag, val in p['entries']:
                    out += bytes([tag])
                    if tag == 1:
                        raw = pool_new.get(val, p['pool'][val])
                        out += struct.pack('>H', len(raw)) + raw
                    elif tag == 8:
                        out += struct.pack('>H', val+1)
                    else:
                        out += val
                rb = bytes(out + p['rest'])

            patches[ci] = rb
            print(f"  청크 {ci}: {sz}B → {len(rb)}B  ({len(rb)-sz:+d})")
        return fl00_write(data, toc, patches)

    elif data[:4] == CAFEBABE:
        cd = chunks.get(None, chunks.get(0, {}))
        strs = cd.get('strs', [])
        bc_map = cd.get('bc_map')
        p = parse(data)
        if not p: return None
        if bc_map is not None:
            rb = rebuild(p, strs, tbl)
        else:
            if len(strs) != len(p['tag8_order']):
                print(f"[오류] tag8 {len(p['tag8_order'])}개 ≠ txt {len(strs)}줄"); return None
            rb = rebuild(p, strs, tbl)  # fallback
        return rb
    print(f"알 수 없는 포맷: {data[:4].hex()}"); return None

# ── 커맨드 ───────────────────────────────────────────────────────────────────
def find_map(evt_path, txt_path, explicit=None):
    name = 'XENOSAGA_KOR-JPN.json'
    candidates = []
    if explicit: candidates.append(explicit)
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(evt_path)), name))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(txt_path)), name))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    for p in candidates:
        if os.path.exists(p): return p
    return None

def do_extract(evt_path):
    data = open(evt_path,'rb').read()
    lines = data_to_lines(data)
    if not lines: print("추출된 문자열 없음"); return
    out_path = evt_path + '.txt'
    open(out_path,'w',encoding='utf-8').write('\n'.join(lines))
    n = len([l for l in lines if l and not l.startswith('#')])
    print(f"추출: {out_path}  ({n}줄, 바이트코드 실행 순서)")

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
        print("치환 테이블 없음")
    out = apply_patches(data, chunks, tbl)
    if out is None: return
    out_path = evt_path + '.new'
    open(out_path,'wb').write(out)
    print(f"재조립: {out_path}")

def do_verify(evt_path):
    data  = open(evt_path,'rb').read()
    lines = data_to_lines(data)
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
        ns = len(get_strings_bc_order(p)) if p else 0
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
