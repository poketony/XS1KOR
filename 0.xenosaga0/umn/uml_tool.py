#!/usr/bin/env python3
"""
uml_tool.py  –  Xenosaga UMN 메일 파일(.uml) 추출/재삽입 도구
사용법:
  extract  :  python3 uml_tool.py extract <파일.uml>  <출력디렉토리>
  rebuild  :  python3 uml_tool.py rebuild  <파일.uml>  <출력디렉토리> <파일.uml.new>
              (텍스트파일 없이 .uml만 주면 원본 텍스트를 그대로 재조립해서 roundtrip 검증)

구조 개요:
  0x00–0x03  : 매직 "UML\0"
  0x04–0x07  : "@@@@" (고정)
  0x08–0x0f  : 헤더 플래그 영역 (그대로 보존)
  0x20–0x23  : 텍스트 섹션 끝 오프셋 (= JPEG 시작 위치)
  0x24–0x5f  : 추가 이미지 포인터/표시 좌표 테이블 또는 0 패딩
  0x60–(val@0x20)  : EUC-JP 텍스트 + 컨트롤 코드
  (val@0x20)–EOF   : 임베디드 JPEG + 테일 패딩

컨트롤 코드:
  0x0C xx xx xx  : 4바이트 스타일 태그 (TTH, ���, x**, o▽, ]]W, TTP, 0?? ...)
  0x0D xx        : 2바이트 섹션 구분 (0x02=제목 마커, 0x00=텍스트 종료)
  0x19 xx        : 2바이트 인라인 변수 마커 (0x01=on, 0x00=off)
  0x40 0x2A      : @* 페이지 브레이크

추출 형식 (.txt):
  첫 줄: #SUBJECT:<제목>
  두번째 줄: #FROM:<발신자>
  이후: 본문 (컨트롤 태그는 <TAG:XXYYZZ>, 단일 제어 바이트는 <BYTE:XX> 형태로 표시)
  이미지 영역:
    <basename>_00.jpg... : 블록 안의 개별 JPEG 스트림
"""

import sys
import os
import json
import struct
import re

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(errors='replace')

# ────────────────────────────────────────────────────────────
# 상수 / 인코딩
# ────────────────────────────────────────────────────────────
MAGIC       = b'UML\x00'
FIXED_4     = b'@@@@'
TEXT_START  = 0x60
HEADER_SIZE = 0x60
ENCODE_SRC  = 'euc_jis_2004'

# 텍스트에서 컨트롤 코드를 보존하기 위한 플레이스홀더 태그
# <TAG:XXYYZZ>  – XX YY ZZ는 0x0C 이후 3바이트의 hex
# <CTL:XXYY>    – XX YY는 0x0D / 0x19 이후 1바이트 + 그 값
# <BYTE:XX>     – 단일 제어/비표준 바이트. <NUL>, <BS> 같은 별칭도 지원.

TAG_PATTERN = re.compile(r'<TAG:([0-9A-Fa-f]{6})>')
CTL_PATTERN = re.compile(r'<CTL:([0-9A-Fa-f]{4})>')
BYTE_PATTERN = re.compile(r'<BYTE:([0-9A-Fa-f]{2})>')
PBR_PATTERN = re.compile(r'<PBR>')   # @* 페이지 브레이크

BYTE_NAME_TO_VALUE = {
    'NUL': 0x00,
    'BS':  0x08,
    'SI':  0x0F,
    'DLE': 0x10,
    'DC2': 0x12,
    'CAN': 0x18,
}
BYTE_VALUE_TO_NAME = {v:k for k,v in BYTE_NAME_TO_VALUE.items()}
BYTE_NAME_PATTERN = re.compile(
    r'<(' + '|'.join(re.escape(k) for k in BYTE_NAME_TO_VALUE) + r')>'
)

def byte_tag(b: int) -> str:
    name = BYTE_VALUE_TO_NAME.get(b)
    return f'<{name}>' if name else f'<BYTE:{b:02X}>'


def _next_jpeg_marker(blob: bytes, pos: int, in_scan: bool) -> tuple[int, int] | None:
    while pos < len(blob):
        marker_pos = blob.find(b'\xff', pos)
        if marker_pos < 0:
            return None

        pos = marker_pos + 1
        while pos < len(blob) and blob[pos] == 0xff:
            pos += 1
        if pos >= len(blob):
            return None

        marker = blob[pos]
        pos += 1
        if in_scan and marker == 0x00:
            continue
        if marker == 0x00:
            continue
        return marker, pos
    return None


def find_jpeg_end(blob: bytes, soi: int) -> int | None:
    """JPEG 마커 구조를 따라가며 실제 EOI 직후 오프셋을 찾는다."""
    if blob[soi:soi + 2] != b'\xff\xd8':
        return None

    marker = None
    pos = soi + 2
    no_length_markers = {0x01, 0xd8, 0xd9, *range(0xd0, 0xd8)}

    while pos < len(blob):
        if marker is None:
            found = _next_jpeg_marker(blob, pos, in_scan=False)
            if found is None:
                return None
            marker, pos = found

        if marker == 0xd9:
            return pos

        if marker in no_length_markers:
            marker = None
            continue

        if pos + 2 > len(blob):
            return None
        seg_len = int.from_bytes(blob[pos:pos + 2], 'big')
        if seg_len < 2 or pos + seg_len > len(blob):
            return None

        if marker == 0xda:  # SOS: 이후 entropy-coded data에서는 FF00/RST를 건너뛴다.
            pos += seg_len
            while True:
                found = _next_jpeg_marker(blob, pos, in_scan=True)
                if found is None:
                    return None
                marker, pos = found
                if marker == 0xd9:
                    return pos
                if 0xd0 <= marker <= 0xd7:
                    marker = None
                    continue
                break
        else:
            pos += seg_len
            marker = None

    return None


def find_jpeg_streams(blob: bytes, start: int = 0) -> list[tuple[int, int]]:
    """blob 안의 JPEG 스트림 범위를 절대 오프셋 기준으로 찾는다."""
    streams = []
    pos = start
    while pos < len(blob):
        soi = blob.find(b'\xff\xd8\xff', pos)
        if soi < 0:
            break

        end = find_jpeg_end(blob, soi)
        if end is None:
            streams.append((soi, len(blob)))
            break

        streams.append((soi, end))
        pos = end
    return streams

# ────────────────────────────────────────────────────────────
# 치환표 로드 (한글 → EUC-JP 대응 한자)
# ────────────────────────────────────────────────────────────
def load_charmap(json_path: str) -> dict:
    with open(json_path, 'rb') as f:
        raw = f.read()
    j = json.loads(raw.decode('utf-8-sig'))
    rt = j.get('replace-table', {})
    # 긴 키 우선 정렬 (longest-key-first)
    return dict(sorted(rt.items(), key=lambda x: len(x[0]), reverse=True))


def apply_charmap(text: str, charmap: dict) -> str:
    result = []
    i = 0
    while i < len(text):
        matched = False
        for key, val in charmap.items():
            if text[i:i+len(key)] == key:
                result.append(val)
                i += len(key)
                matched = True
                break
        if not matched:
            result.append(text[i])
            i += 1
    return ''.join(result)


def find_charmap(uml_path: str) -> dict | None:
    """같은 디렉토리에서 JSON 치환표를 자동 탐색"""
    dirpath = os.path.dirname(os.path.abspath(uml_path))
    for name in os.listdir(dirpath):
        if name.lower().endswith('.json'):
            candidate = os.path.join(dirpath, name)
            try:
                j = json.loads(open(candidate,'rb').read().decode('utf-8-sig'))
                if 'replace-table' in j:
                    return load_charmap(candidate)
            except Exception:
                pass
    return None


# ────────────────────────────────────────────────────────────
# 바이너리 파싱: 텍스트 섹션 → 인간이 읽을 수 있는 문자열
# ────────────────────────────────────────────────────────────
def parse_text_bytes(raw: bytes) -> str:
    """
    EUC-JP 텍스트 바이트열을 읽어서 컨트롤 코드를 태그로 치환한 문자열 반환.
    0x0D 0x00 (텍스트 종료 마커) 이후의 0x00 패딩은 버린다.
    """
    out = []
    i = 0
    while i < len(raw):
        b = raw[i]

        # 0x0C: 4바이트 스타일 태그
        if b == 0x0C and i + 3 < len(raw):
            tag3 = raw[i+1:i+4]
            out.append(f'<TAG:{tag3.hex().upper()}>')
            i += 4
            continue

        # 0x0D: 2바이트 섹션 마커 (0x02 등)
        if b == 0x0D and i + 1 < len(raw):
            out.append(f'<CTL:{b:02X}{raw[i+1]:02X}>')
            i += 2
            continue

        # 0x19: 2바이트 인라인 변수 마커
        if b == 0x19 and i + 1 < len(raw):
            out.append(f'<CTL:{b:02X}{raw[i+1]:02X}>')
            i += 2
            continue

        # @* 페이지 브레이크 (0x40 0x2A)
        if b == 0x40 and i + 1 < len(raw) and raw[i+1] == 0x2A:
            out.append('<PBR>')
            i += 2
            continue

        # EUC-JP 2바이트 문자
        if b >= 0xA1 and i + 1 < len(raw):
            pair = raw[i:i+2]
            try:
                ch = pair.decode(ENCODE_SRC)
                out.append(ch)
                i += 2
                continue
            except UnicodeDecodeError:
                pass

        # 그 외 ASCII / 제어문자
        if b < 0x80:
            # 줄바꿈은 편집 편의상 그대로 통과시키고, 나머지 제어문자는
            # 텍스트 에디터가 삼키거나 표시를 바꾸지 않도록 명시 태그로 보존한다.
            if b in (0x0A, 0x0D):
                out.append(chr(b))
            elif b < 0x20 or b == 0x7F:
                out.append(byte_tag(b))
            else:
                out.append(chr(b))
        else:
            # 해석 불가 바이트 → 단일 바이트 hex 이스케이프.
            # 예전처럼 <CTL:XX00>로 쓰면 뒤따르는 0x00까지 중복 삽입될 수 있다.
            out.append(byte_tag(b))
        i += 1

    return ''.join(out)


def split_header_body(text: str):
    """
    件名：<CTL:0D02>제목\n差出人：발신자\n 형태에서 제목/발신자/본문을 분리.
    """
    lines = text.split('\n')
    subject = ''
    sender  = ''
    body_start = 0

    for idx, line in enumerate(lines):
        # 件名：<CTL:0D02>...
        if line.startswith('件名：'):
            raw_subj = line[len('件名：'):]
            # <CTL:0D02> 마커 제거
            raw_subj = re.sub(r'<CTL:[0-9A-Fa-f]{4}>', '', raw_subj)
            subject = raw_subj.strip()
        elif line.startswith('差出人：'):
            sender = line[len('差出人：'):].strip()
            body_start = idx + 1
            break

    body = '\n'.join(lines[body_start:])
    return subject, sender, body


# ────────────────────────────────────────────────────────────
# 텍스트 → 바이너리 재조립
# ────────────────────────────────────────────────────────────
def encode_text(text: str, charmap: dict | None) -> bytes:
    """
    플레이스홀더 태그가 포함된 문자열을 다시 EUC-JP 바이너리로 변환.
    한글이 있으면 charmap으로 먼저 치환한 뒤 인코딩.
    """
    if charmap:
        text = apply_charmap(text, charmap)

    out = bytearray()
    i = 0
    while i < len(text):
        # <TAG:XXYYZZ>
        m = TAG_PATTERN.match(text, i)
        if m:
            out.append(0x0C)
            out.extend(bytes.fromhex(m.group(1)))
            i = m.end()
            continue

        # <CTL:XXYY>
        m = CTL_PATTERN.match(text, i)
        if m:
            pair = bytes.fromhex(m.group(1))
            # 하위 호환: 구버전 추출기는 해석 불가 단일 바이트 XX를
            # <CTL:XX00>로 표기했다. 0x0D/0x19가 아닌 CTL은 단일 바이트로 복원한다.
            if pair[0] in (0x0D, 0x19):
                out.extend(pair)
            else:
                out.append(pair[0])
            i = m.end()
            continue

        # <BYTE:XX>
        m = BYTE_PATTERN.match(text, i)
        if m:
            out.append(int(m.group(1), 16))
            i = m.end()
            continue

        # <NUL>, <BS> 등 단일 제어 바이트 별칭
        m = BYTE_NAME_PATTERN.match(text, i)
        if m:
            out.append(BYTE_NAME_TO_VALUE[m.group(1)])
            i = m.end()
            continue

        # <PBR>
        m = PBR_PATTERN.match(text, i)
        if m:
            out.extend(b'\x40\x2A')
            i = m.end()
            continue

        ch = text[i]
        enc = ch.encode(ENCODE_SRC, errors='replace')
        out.extend(enc)
        i += 1

    return bytes(out)


def rebuild_header_body(subject: str, sender: str, body: str, charmap: dict | None) -> bytes:
    """제목/발신자/본문을 원본 형식대로 바이너리로 조합"""
    # 件名：<CTL:0D02>제목\n差出人：발신자\n
    header_text = f'件名：<CTL:0D02>{subject}\n差出人：{sender}\n'
    full_text = header_text + body
    # 텍스트 종료 마커 <CTL:0D00> 이 body 끝에 있어야 함
    terminator_re = re.compile(r'<CTL:0D00>(?:\s|\x00|<NUL>|<BYTE:00>)*\Z')
    if not terminator_re.search(full_text):
        full_text = full_text.rstrip('\x00\n') + '<CTL:0D00>\n'
    return encode_text(full_text, charmap)


# ────────────────────────────────────────────────────────────
# UML 파일 파싱
# ────────────────────────────────────────────────────────────
class UMLFile:
    def __init__(self, path: str):
        self.path = path
        raw = open(path, 'rb').read()
        assert raw[:4] == MAGIC, f"매직 불일치: {raw[:4]}"

        self.raw         = raw
        self.header      = raw[:HEADER_SIZE]          # 0x00–0x5F
        self.text_end    = int.from_bytes(raw[0x20:0x24], 'little')  # JPEG 시작 = 텍스트 끝
        self.has_payload = TEXT_START <= self.text_end <= len(raw)

        if self.has_payload:
            self.text_bytes  = raw[TEXT_START:self.text_end]
            self.jpeg_and_tail = raw[self.text_end:]  # JPEG 블록 + 테일 패딩
        else:
            # 0062/0073처럼 빈 슬롯으로 보이는 UML이 있다.
            self.text_bytes = b''
            self.jpeg_and_tail = b''

        # JPEG 검증
        if self.jpeg_and_tail:
            assert self.jpeg_and_tail[:3] == b'\xff\xd8\xff', "JPEG 시그니처 없음"
        self.jpeg_streams = find_jpeg_streams(raw, self.text_end) if self.jpeg_and_tail else []

        self.text_str    = parse_text_bytes(self.text_bytes)
        self.subject, self.sender, self.body = split_header_body(self.text_str)

    # ── 내보내기 ──────────────────────────────────────────
    def export_txt(self, txt_path: str):
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(f'#SUBJECT:{self.subject}\n')
            f.write(f'#FROM:{self.sender}\n')
            f.write(self.body)
        print(f'  [TXT] {txt_path}')

    def export_jpg_parts(self, out_dir: str, basename: str):
        if not self.jpeg_streams:
            print('  [JPG-PART] 개별 JPEG 없음')
            return

        for idx, (start, end) in enumerate(self.jpeg_streams):
            part_path = os.path.join(out_dir, f'{basename}_{idx:02d}.jpg')
            with open(part_path, 'wb') as f:
                f.write(self.raw[start:end])
            print(f'  [JPG {idx:02d}] {part_path}  '
                  f'(offset 0x{start:x}, {end - start} bytes)')

    def build_jpeg_block_from_parts(self, src_dir: str, basename: str) -> bytes | None:
        if not self.jpeg_streams:
            return None

        block = bytearray(self.jpeg_and_tail)
        changed = False
        for idx, (start, end) in enumerate(self.jpeg_streams):
            part_path = os.path.join(src_dir, f'{basename}_{idx:02d}.jpg')
            if not os.path.exists(part_path):
                continue

            part = open(part_path, 'rb').read()
            slot_end = self.jpeg_streams[idx + 1][0] if idx + 1 < len(self.jpeg_streams) else end
            slot_len = slot_end - start
            if len(part) > slot_len:
                raise ValueError(
                    f'{part_path}가 원본 슬롯보다 큽니다: {len(part)} > {slot_len}. '
                    '개별 JPG 교체는 오프셋 보존을 위해 원본 이하 크기만 허용합니다.'
                )

            rel = start - self.text_end
            block[rel:rel + len(part)] = part
            if len(part) < slot_len:
                block[rel + len(part):rel + slot_len] = b'\x00' * (slot_len - len(part))
            changed = True

        return bytes(block) if changed else None

    # ── 재조립 ───────────────────────────────────────────
    def rebuild(self, new_text_bytes: bytes, new_jpeg: bytes | None = None) -> bytes:
        """
        new_text_bytes: 새 텍스트 바이너리 (0x60 이후 내용)
        new_jpeg      : None이면 원본 JPEG+테일 유지
        """
        if not self.has_payload:
            if new_text_bytes == self.text_bytes and new_jpeg is None:
                return self.raw
            raise ValueError('텍스트/JPEG payload가 없는 UML은 재조립할 수 없습니다.')

        jpeg_block = new_jpeg if new_jpeg is not None else self.jpeg_and_tail

        # The in-game mail viewer expects the JPEG block to stay on a stable
        # aligned boundary. If translated text becomes shorter than the original
        # section, preserve the original JPEG offset with NUL padding. If it
        # grows, align the new JPEG start to 16 bytes.
        orig_text_len = self.text_end - TEXT_START
        if len(new_text_bytes) <= orig_text_len:
            new_text_bytes += b'\x00' * (orig_text_len - len(new_text_bytes))
        else:
            pad = (- (TEXT_START + len(new_text_bytes))) % 16
            if pad:
                new_text_bytes += b'\x00' * pad

        new_text_end = TEXT_START + len(new_text_bytes)

        # 헤더 복사 후 0x20 값 업데이트
        hdr = bytearray(self.header)
        struct.pack_into('<I', hdr, 0x20, new_text_end)

        # 0x28 이후는 (JPEG 절대 오프셋, 표시 좌표) 쌍이다. 다중 이미지 UML은
        # 0x30/0x38/...에도 추가 포인터가 있으므로 텍스트 길이 변경분만큼 모두 옮긴다.
        delta = new_text_end - self.text_end
        for off in range(0x28, HEADER_SIZE, 8):
            image_ptr = struct.unpack_from('<I', hdr, off)[0]
            if self.text_end <= image_ptr < self.text_end + len(self.jpeg_and_tail):
                struct.pack_into('<I', hdr, off, image_ptr + delta)

        return bytes(hdr) + new_text_bytes + jpeg_block


# ────────────────────────────────────────────────────────────
# CLI 명령: extract
# ────────────────────────────────────────────────────────────
def cmd_extract(args):
    if len(args) < 1:
        print("사용법: extract <파일.uml> [출력디렉토리]")
        return

    uml_path = args[0]
    out_dir  = args[1] if len(args) > 1 else os.path.dirname(os.path.abspath(uml_path))
    os.makedirs(out_dir, exist_ok=True)

    basename = os.path.splitext(os.path.basename(uml_path))[0]
    txt_path = os.path.join(out_dir, basename + '.txt')

    print(f'[추출] {uml_path}')
    u = UMLFile(uml_path)
    u.export_txt(txt_path)
    u.export_jpg_parts(out_dir, basename)
    print(f'  제목  : {u.subject}')
    print(f'  발신자: {u.sender}')
    print(f'  본문길이: {len(u.body)} chars')


# ────────────────────────────────────────────────────────────
# CLI 명령: rebuild
# ────────────────────────────────────────────────────────────
def cmd_rebuild(args):
    if len(args) < 1:
        print("사용법: rebuild <원본.uml> [추출폴더/] [출력.uml]")
        print("  추출폴더 안의 <basename>.txt / <basename>_00.jpg... 를 자동으로 사용")
        print("  txt 없으면 원본 텍스트 유지, 개별 jpg 없으면 원본 이미지 유지")
        return

    uml_path  = args[0]
    src_dir   = args[1] if len(args) > 1 else None
    out_path  = args[2] if len(args) > 2 else uml_path.replace('.uml', '_new.uml')

    basename  = os.path.splitext(os.path.basename(uml_path))[0]

    # 폴더에서 txt / jpg 자동 탐색
    if src_dir and os.path.isdir(src_dir):
        txt_path = os.path.join(src_dir, basename + '.txt')
        if not os.path.exists(txt_path):
            txt_path = None
    else:
        # 하위 호환: 폴더 대신 txt 파일 직접 지정한 경우
        txt_path = src_dir if src_dir and os.path.isfile(src_dir) else None

    print(f'[재조립] {uml_path}')
    u = UMLFile(uml_path)

    # 치환표 로드
    charmap = find_charmap(uml_path)
    if charmap:
        print(f'  치환표 로드됨: {len(charmap)} 항목')
    else:
        print('  치환표 없음 – 한글 그대로 삽입 시도')

    print(f'  텍스트: {txt_path if txt_path else "원본 유지"}')
    print(f'  이미지: 개별 JPG 자동 탐색')

    if txt_path:
        # 번역 텍스트 읽기
        with open(txt_path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines(keepends=True)

        # #SUBJECT / #FROM 파싱
        subject = ''
        sender  = ''
        body_lines = []
        for line in lines:
            if line.startswith('#SUBJECT:'):
                subject = line[len('#SUBJECT:'):].rstrip('\n')
            elif line.startswith('#FROM:'):
                sender = line[len('#FROM:'):].rstrip('\n')
            else:
                body_lines.append(line)
        body = ''.join(body_lines)

        new_text_bytes = rebuild_header_body(subject, sender, body, charmap)
    else:
        # 원본 텍스트 그대로 roundtrip
        new_text_bytes = u.text_bytes
        print('  번역 파일 없음 – roundtrip 검증 모드')

    # 새 이미지 로드 (선택)
    new_jpeg = None
    if src_dir and os.path.isdir(src_dir):
        new_jpeg = u.build_jpeg_block_from_parts(src_dir, basename)
        if new_jpeg is not None:
            print('  새 이미지: 개별 JPG 슬롯 교체')

    result = u.rebuild(new_text_bytes, new_jpeg)

    with open(out_path, 'wb') as f:
        f.write(result)
    print(f'  → {out_path}  ({len(result)} bytes)')

    # roundtrip 검증
    if txt_path is None:
        if result == open(uml_path,'rb').read():
            print('  ✓ Roundtrip 완벽 일치')
        else:
            orig = open(uml_path,'rb').read()
            for i, (a,b) in enumerate(zip(result, orig)):
                if a != b:
                    print(f'  ✗ 첫 불일치 offset 0x{i:x}: got {a:02x}, expected {b:02x}')
                    break
            if len(result) != len(orig):
                print(f'  ✗ 크기 다름: {len(result)} vs {len(orig)}')


# ────────────────────────────────────────────────────────────
# CLI 명령: roundtrip (일괄 검증)
# ────────────────────────────────────────────────────────────
def cmd_roundtrip(args):
    """디렉토리 내 모든 .uml 파일 roundtrip 검증"""
    dirpath = args[0] if args else '.'
    import glob
    files = sorted(glob.glob(os.path.join(dirpath, '*.uml')))
    if not files:
        print('UML 파일 없음')
        return
    ok = 0
    fail = 0
    for uml_path in files:
        u = UMLFile(uml_path)
        result = u.rebuild(u.text_bytes)
        orig = open(uml_path,'rb').read()
        if result == orig:
            print(f'  ✓ {os.path.basename(uml_path)}')
            ok += 1
        else:
            print(f'  ✗ {os.path.basename(uml_path)}')
            fail += 1
    print(f'\n결과: {ok} 성공 / {fail} 실패')


# ────────────────────────────────────────────────────────────
# 진입점
# ────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd  = sys.argv[1].lower()
    args = sys.argv[2:]

    if cmd == 'extract':
        cmd_extract(args)
    elif cmd == 'rebuild':
        cmd_rebuild(args)
    elif cmd == 'roundtrip':
        cmd_roundtrip(args)
    else:
        print(f'알 수 없는 명령: {cmd}')
        print('명령: extract | rebuild | roundtrip')
        sys.exit(1)

if __name__ == '__main__':
    main()
