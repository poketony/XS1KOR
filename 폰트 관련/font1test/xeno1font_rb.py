import struct, sys, os, argparse
import numpy as np
from PIL import Image

# ── PS2 GS 스위즐 테이블 (기존과 동일) ──────────────────────────────────────────
BLOCK32 = [0, 1, 4, 5, 16, 17, 20, 21, 2, 3, 6, 7, 18, 19, 22, 23, 8, 9, 12, 13, 24, 25, 28, 29, 10, 11, 14, 15, 26, 27, 30, 31]
COLUMN_WORD32 = [0, 1, 4, 5, 8, 9, 12, 13, 2, 3, 6, 7, 10, 11, 14, 15]
BLOCK4 = [0, 2, 8, 10, 1, 3, 9, 11, 4, 6, 12, 14, 5, 7, 13, 15, 16, 18, 24, 26, 17, 19, 25, 27, 20, 22, 28, 30, 21, 23, 29, 31]
COLUMN_WORD4 = [
    [0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7],
    [8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15]
]
COLUMN_BYTE4 = [0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2, 4, 4, 4, 4, 4, 4, 4, 4, 6, 6, 6, 6, 6, 6, 6, 6, 0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2, 4, 4, 4, 4, 4, 4, 4, 4, 6, 6, 6, 6, 6, 6, 6, 6, 1, 1, 1, 1, 1, 1, 1, 1, 3, 3, 3, 3, 3, 3, 3, 3, 5, 5, 5, 5, 5, 5, 5, 5, 7, 7, 7, 7, 7, 7, 7, 7, 1, 1, 1, 1, 1, 1, 1, 1, 3, 3, 3, 3, 3, 3, 3, 3, 5, 5, 5, 5, 5, 5, 5, 5, 7, 7, 7, 7, 7, 7, 7, 7]

# ── 상수 ─────────────────────────────────────────────────────────────────────
BLOCK_SIZE      = 0x3C000
HEADER_SIZE     = 0x20
OUT_W           = 640
TILE_H          = 128
TILES_PER_BLOCK = 6
SHEET_H         = TILE_H * TILES_PER_BLOCK   # 768

# ── GS 메모리 연산 ────────────────────────────────────────────────────────────
def _ct32_pos(x, y, dbw=0x140):
    page  = ((y >> 5) * (dbw >> 6)) + (x >> 6)
    px, py = x & 63, y & 31
    block  = BLOCK32[(px >> 3) + (py >> 3) * 8]
    bx, by = px & 7, py & 7
    col    = by >> 1
    cw     = COLUMN_WORD32[bx + (by & 1) * 8]
    return page * 2048 + block * 64 + col * 16 + cw

def _4bpp_pos(x, y, dbp, dbw=64):
    dbw2   = dbw >> 1
    start  = dbp * 64
    page   = ((y >> 7) * ((dbw2 + 127) >> 7)) + (x >> 7)
    px, py = x & 127, y & 127
    block  = BLOCK4[(px >> 5) + (py >> 4) * 4]
    bx, by = px & 31, py & 15
    col    = by >> 2
    cx, cy = bx, by & 3
    cw     = COLUMN_WORD4[col & 1][cx + cy * 32]
    cb     = COLUMN_BYTE4[cx + cy * 32]
    return start + page * 2048 + block * 64 + col * 16 + cw, cb

# ── 유틸리티 ──────────────────────────────────────────────────────────────────
def _quantize(arr):
    lv = np.zeros(arr.shape, dtype=np.uint8)
    lv[arr >  42] = 1
    lv[arr > 127] = 2
    lv[arr > 212] = 3
    return lv

def load_sheet(path):
    img = Image.open(path).convert('L')
    arr = np.array(img)
    if arr.shape != (SHEET_H, OUT_W):
        raise ValueError(f"이미지 {path}의 크기가 640x768이어야 합니다. 현재: {arr.shape[1]}x{arr.shape[0]}")
    return _quantize(arr)

# ── 리빌드 메인 ───────────────────────────────────────────────────────────────
def rebuild_tex(sheets, original_data):
    out = bytearray(original_data)

    for m in range(2):
        gsmem  = bytearray(1024 * 1024 * 4) # 4MB GS memory buffer
        
        # 블록 m에 해당하는 시트 데이터 (m=0: 1&2, m=1: 3&4)
        sheet_low = sheets[m * 2]     # 하위 2비트용
        sheet_high = sheets[m * 2 + 1] # 상위 2비트용

        for n in range(TILES_PER_BLOCK):
            dbp = (n * OUT_W) // 4
            for row in range(TILE_H):
                y_in = n * TILE_H + row
                for col in range(OUT_W):
                    # 두 시트를 합쳐서 4비트 nibble 생성
                    nib = (sheet_high[y_in, col] << 2) | sheet_low[y_in, col]
                    
                    pos, cb = _4bpp_pos(col, row, dbp)
                    baddr = pos * 4 + (cb >> 1)
                    
                    if cb & 1:
                        gsmem[baddr] = (gsmem[baddr] & 0x0F) | ((nib << 4) & 0xF0)
                    else:
                        gsmem[baddr] = (gsmem[baddr] & 0xF0) | (nib & 0x0F)

        # GS 메모리 → PSMCT32 역스위즐
        offset = BLOCK_SIZE * m + HEADER_SIZE * (m + 1)
        chunk  = bytearray(BLOCK_SIZE)
        for y in range(192):
            for x in range(320):
                pos = _ct32_pos(x, y)
                word = struct.unpack_from('<I', gsmem, pos * 4)[0]
                struct.pack_into('<I', chunk, (y * 320 + x) * 4, word)

        out[offset : offset + BLOCK_SIZE] = chunk
        print(f'  블록 {m} 리빌드 완료 (Sheet {m*2+1}, {m*2+2} 포함)')

    return bytes(out)

def main():
    parser = argparse.ArgumentParser(description='Xenosaga Episode I 폰트 시트(4개) → .tex 리빌더')
    parser.add_argument('-s', '--sheets', nargs=4, required=True,
                        help='입력 시트 PNG 4개 (순서대로 sheet1 sheet2 sheet3 sheet4)')
    parser.add_argument('-r', '--reference', required=True, help='원본 .tex 파일')
    parser.add_argument('-o', '--output', required=True, help='출력 .tex 경로')
    args = parser.parse_args()

    # 파일 존재 확인
    for p in args.sheets + [args.reference]:
        if not os.path.isfile(p):
            print(f'[오류] 파일 없음: {p}'); return

    # 시트 로드 및 양자화
    print("시트 로딩 중...")
    try:
        loaded_sheets = [load_sheet(s) for s in args.sheets]
    except Exception as e:
        print(f"[오류] {e}"); return

    # 원본 파일 로드
    with open(args.reference, 'rb') as f:
        original = f.read()

    # 리빌드
    print("리빌드 진행 중...")
    result = rebuild_tex(loaded_sheets, original)

    # 저장
    with open(args.output, 'wb') as f:
        f.write(result)
    print(f"저장 완료: {args.output}")

if __name__ == '__main__':
    main()