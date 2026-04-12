import struct, sys, os, argparse
import numpy as np
from PIL import Image

# ── PS2 GS 스위즐 테이블 ──────────────────────────────────────────────────────

BLOCK32 = [
     0,  1,  4,  5, 16, 17, 20, 21,
     2,  3,  6,  7, 18, 19, 22, 23,
     8,  9, 12, 13, 24, 25, 28, 29,
    10, 11, 14, 15, 26, 27, 30, 31,
]
COLUMN_WORD32 = [
    0,  1,  4,  5,  8,  9, 12, 13,
    2,  3,  6,  7, 10, 11, 14, 15,
]
BLOCK4 = [
     0,  2,  8, 10,
     1,  3,  9, 11,
     4,  6, 12, 14,
     5,  7, 13, 15,
    16, 18, 24, 26,
    17, 19, 25, 27,
    20, 22, 28, 30,
    21, 23, 29, 31,
]
COLUMN_WORD4 = [
    [  # column & 1 == 0
         0,  1,  4,  5,  8,  9, 12, 13,   0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,   0,  1,  4,  5,  8,  9, 12, 13,
         2,  3,  6,  7, 10, 11, 14, 15,   2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,   2,  3,  6,  7, 10, 11, 14, 15,
         8,  9, 12, 13,  0,  1,  4,  5,   8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,   8,  9, 12, 13,  0,  1,  4,  5,
        10, 11, 14, 15,  2,  3,  6,  7,  10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,  10, 11, 14, 15,  2,  3,  6,  7,
    ],
    [  # column & 1 == 1
         8,  9, 12, 13,  0,  1,  4,  5,   8,  9, 12, 13,  0,  1,  4,  5,
         8,  9, 12, 13,  0,  1,  4,  5,   8,  9, 12, 13,  0,  1,  4,  5,
        10, 11, 14, 15,  2,  3,  6,  7,  10, 11, 14, 15,  2,  3,  6,  7,
        10, 11, 14, 15,  2,  3,  6,  7,  10, 11, 14, 15,  2,  3,  6,  7,
         0,  1,  4,  5,  8,  9, 12, 13,   0,  1,  4,  5,  8,  9, 12, 13,
         0,  1,  4,  5,  8,  9, 12, 13,   0,  1,  4,  5,  8,  9, 12, 13,
         2,  3,  6,  7, 10, 11, 14, 15,   2,  3,  6,  7, 10, 11, 14, 15,
         2,  3,  6,  7, 10, 11, 14, 15,   2,  3,  6,  7, 10, 11, 14, 15,
    ],
]
COLUMN_BYTE4 = [
    0, 0, 0, 0, 0, 0, 0, 0,  2, 2, 2, 2, 2, 2, 2, 2,
    4, 4, 4, 4, 4, 4, 4, 4,  6, 6, 6, 6, 6, 6, 6, 6,
    0, 0, 0, 0, 0, 0, 0, 0,  2, 2, 2, 2, 2, 2, 2, 2,
    4, 4, 4, 4, 4, 4, 4, 4,  6, 6, 6, 6, 6, 6, 6, 6,
    1, 1, 1, 1, 1, 1, 1, 1,  3, 3, 3, 3, 3, 3, 3, 3,
    5, 5, 5, 5, 5, 5, 5, 5,  7, 7, 7, 7, 7, 7, 7, 7,
    1, 1, 1, 1, 1, 1, 1, 1,  3, 3, 3, 3, 3, 3, 3, 3,
    5, 5, 5, 5, 5, 5, 5, 5,  7, 7, 7, 7, 7, 7, 7, 7,
]


# ── 상수 ─────────────────────────────────────────────────────────────────────

BLOCK_SIZE      = 0x3C000
HEADER_SIZE     = 0x20
OUT_W           = 640
TILE_H          = 128
TILES_PER_BLOCK = 6
SHEET_H         = TILE_H * TILES_PER_BLOCK   # 768
GRAY            = [0, 85, 170, 255]


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


# ── 추출 ─────────────────────────────────────────────────────────────────────

def extract_font(tex_path):
    if not os.path.exists(tex_path):
        return None
        
    with open(tex_path, 'rb') as f:
        raw = f.read()

    # 4개의 개별 시트를 위한 배열 초기화
    sheets = [np.zeros((SHEET_H, OUT_W), dtype=np.uint8) for _ in range(4)]

    for m in range(2):
        gsmem  = [0] * (1024 * 1024)
        offset = BLOCK_SIZE * m + HEADER_SIZE * (m + 1)
        
        # 파일 크기 체크
        if offset + BLOCK_SIZE > len(raw):
            continue
            
        chunk  = raw[offset : offset + BLOCK_SIZE]

        # PSMCT32 언스위즐 → GS 메모리 적재
        for y in range(192):
            for x in range(320):
                pos = _ct32_pos(x, y)
                gsmem[pos] = struct.unpack_from('<I', chunk, (y * 320 + x) * 4)[0]

        # PSMT4 언스위즐 → 시트 분리
        for n in range(TILES_PER_BLOCK):
            dbp = (n * OUT_W) // 4
            for row in range(TILE_H):
                for col in range(OUT_W):
                    pos, cb = _4bpp_pos(col, row, dbp)
                    word  = gsmem[pos]
                    byte  = (word >> ((cb >> 1) * 8)) & 0xFF
                    nib   = (byte >> 4) & 0xF if (cb & 1) else byte & 0xF

                    y_out = n * TILE_H + row
                    
                    # 블록 m과 nibble 비트를 조합해 시트 1~4 생성
                    sheets[m*2 + 0][y_out, col] = GRAY[nib & 3]
                    sheets[m*2 + 1][y_out, col] = GRAY[nib >> 2]

    return sheets


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Xenosaga Episode I 폰트 시트 추출기 (4시트 분리)')
    parser.add_argument('texfiles', nargs='+', help='.tex 파일 경로')
    parser.add_argument('-o', '--outdir', default='.', help='출력 폴더')
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    for tex_path in args.texfiles:
        if not os.path.isfile(tex_path):
            print(f'[오류] 파일을 찾을 수 없음: {tex_path}')
            continue

        base = os.path.splitext(os.path.basename(tex_path))[0]
        print(f'처리 중: {tex_path}')

        all_sheets = extract_font(tex_path)
        if all_sheets is None:
            continue

        for i, sheet_data in enumerate(all_sheets):
            out_filename = f'{base}_sheet{i+1}.png'
            out_path = os.path.join(args.outdir, out_filename)
            Image.fromarray(sheet_data).save(out_path)
            print(f'  → {out_filename} 저장 완료')

    print('모든 작업이 완료되었습니다.')


if __name__ == '__main__':
    main()