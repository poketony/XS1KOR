#!/usr/bin/env python3
"""
xtx_tool.py - Xenosaga 1 .xtx texture extract/import tool

Usage:
  python xtx_tool.py extract <file.xtx> [--out DIR] [--fix-alpha]
  python xtx_tool.py import  <file.xtx> <folder>   [--out FILE] [--fix-alpha]

Extract:
  각 서브이미지를 독립 atlas에 배치 후 PS2 unswizzle 적용,
  <out_dir>/<n>_1.png, _2.png ... (grayscale) 로 저장.

Import:
  <folder> 안의 <n>_1.png, _2.png ... (편집된 grayscale) 를 읽어
  swizzle 적용 후 대응 서브이미지 픽셀 영역에 반영.

Alpha 정책:
  unswizzled grayscale 편집 기반이므로 alpha는 별도 처리 없음.
  --fix-alpha: extract 시 원본 RGBA도 _1_rgba.png 로 함께 저장 (참고용)
"""

import os
import re
import struct
import argparse
import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# ARX decompression
# ---------------------------------------------------------------------------

def decompress_arx(data: bytes) -> bytes:
    size_orig = struct.unpack_from('<I', data, 4)[0]
    lut = list(struct.unpack_from('<30I', data, 16))
    out = bytearray(size_orig)
    out_pos = 0
    fp_pos  = 136
    buf: int = 0
    buf_len: int = 0
    STATE_DATA, STATE_MARKER, STATE_LUT = 0, 1, 2
    state = STATE_DATA
    lut_val = lut_idx = lut_len = 0

    def read_u32():
        nonlocal fp_pos
        if fp_pos + 4 > len(data): return None
        v = struct.unpack_from('<I', data, fp_pos)[0]; fp_pos += 4; return v

    def write_u32(v):
        nonlocal out_pos
        if out_pos + 4 <= len(out):
            struct.pack_into('<I', out, out_pos, v & 0xFFFFFFFF)
        out_pos += 4

    while True:
        val = read_u32()
        if val is None: break
        buf |= (val << (32 - buf_len)) & 0xFFFFFFFFFFFFFFFF
        buf_len += 32
        while buf_len > 0:
            bit = (buf >> 63) & 1
            if state == STATE_DATA:
                if bit: state = STATE_MARKER
                else:
                    v = read_u32()
                    if v is None: buf_len = 0; break
                    write_u32(v)
                buf = (buf << 1) & 0xFFFFFFFFFFFFFFFF; buf_len -= 1
            elif state == STATE_MARKER:
                lut_val = lut_idx = lut_len = 0; state = STATE_LUT
                buf = (buf << 1) & 0xFFFFFFFFFFFFFFFF; buf_len -= 1
            elif state == STATE_LUT:
                lut_val = ((lut_val << 1) | bit) & 0xFF
                if lut_idx == 0: lut_len = 4 if bit else 2
                if lut_idx == 1 and lut_len == 4 and bit: lut_len = 6
                if lut_idx == 2 and lut_len == 6 and bit: lut_len = 8
                lut_idx += 1
                buf = (buf << 1) & 0xFFFFFFFFFFFFFFFF; buf_len -= 1
                if lut_idx == lut_len:
                    state = STATE_DATA
                    if lut_len == 2:   idx = lut_val
                    elif lut_len == 4: idx = 2 + (lut_val & 0x7)
                    elif lut_len == 6: idx = 6 + (lut_val & 0xF)
                    else:              idx = 14 + (lut_val & 0x1F)
                    write_u32(lut[idx] if idx < len(lut) else 0)
    return bytes(out)


# ---------------------------------------------------------------------------
# XTX header parsing
# ---------------------------------------------------------------------------

def parse_xtx_headers(data: bytes) -> list:
    if data[0:4] != b'XTX\x00':
        raise ValueError(f"Not XTX magic: {data[0:4]}")
    count = struct.unpack_from('<I', data, 8)[0]
    haddr = struct.unpack_from('<I', data, 12)[0]
    images = []
    for i in range(count):
        base     = haddr + i * 20
        width    = struct.unpack_from('<H', data, base + 0)[0]
        bw       = struct.unpack_from('<H', data, base + 2)[0]
        height   = struct.unpack_from('<H', data, base + 4)[0]
        offset   = struct.unpack_from('<I', data, base + 8)[0]
        img_size = struct.unpack_from('<I', data, base + 12)[0]
        img_addr = struct.unpack_from('<I', data, base + 16)[0]
        bw_eff   = bw if bw else 8
        block    = offset // 4096
        x0       = (block % (bw_eff // 2)) * 64
        y0       = (block // (bw_eff // 2)) * 32
        pstart   = img_addr + 32
        pend     = pstart + width * height * 4
        valid    = (width > 0) and (height > 0) and (pend <= len(data))
        images.append({
            'index': i, 'width': width, 'height': height,
            'bw': bw, 'bw_eff': bw_eff, 'offset': offset,
            'img_size': img_size, 'img_addr': img_addr,
            'x0': x0, 'y0': y0,
            'pstart': pstart, 'pend': pend, 'valid': valid,
        })
    return images


# ---------------------------------------------------------------------------
# Swizzle / Unswizzle (Sparky's Swizzle8to32)
# ---------------------------------------------------------------------------

def unswizzle8(b: bytes, width: int, height: int) -> bytes:
    """swizzled atlas -> linear (extract용)"""
    ret = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            bl = (y & ~0xf) * width + (x & ~0xf) * 2
            ss = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * width * 2 + ((x + ss) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            si = bl + cl + bn
            ret[y * width + x] = b[si] if si < len(b) else 0
    return bytes(ret)


def swizzle8(b: bytes, width: int, height: int) -> bytes:
    """linear -> swizzled atlas (import용, unswizzle8의 역방향)"""
    ret = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            bl = (y & ~0xf) * width + (x & ~0xf) * 2
            ss = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * width * 2 + ((x + ss) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            dst = bl + cl + bn
            if dst < len(ret):
                ret[dst] = b[y * width + x]
    return bytes(ret)


# ---------------------------------------------------------------------------
# Per-image unswizzle / swizzle (독립 atlas 방식)
# ---------------------------------------------------------------------------

ATLAS_STRIDE = 512   # RGBA pixels per row
ATLAS_SIZE   = ATLAS_STRIDE * ATLAS_STRIDE * 4  # bytes (= 1024*1024)

def img_to_unsw(data: bytes, img: dict) -> np.ndarray:
    """서브이미지 하나를 독립 atlas에 배치 -> unswizzle -> 크롭. 반환: (h*2, w*2) L array"""
    w, h, x0, y0 = img['width'], img['height'], img['x0'], img['y0']
    pdata = data[img['pstart']:img['pend']]
    atlas = bytearray(ATLAS_SIZE)
    for y in range(h):
        for x in range(w):
            src = (y * w + x) * 4
            dst = ((y0 + y) * ATLAS_STRIDE + (x0 + x)) * 4
            if dst + 4 <= len(atlas):
                atlas[dst:dst+4] = pdata[src:src+4]
    unsw = unswizzle8(bytes(atlas), 1024, 1024)
    arr  = np.frombuffer(unsw, dtype=np.uint8).reshape(1024, 1024)
    ux, uy = x0 * 2, y0 * 2
    uw, uh = w  * 2, h  * 2
    return arr[uy:min(uy+uh, 1024), ux:min(ux+uw, 1024)]


def unsw_to_pdata(unsw_arr: np.ndarray, img: dict) -> bytes:
    """편집된 unswizzled grayscale -> swizzle -> 서브이미지 픽셀 바이트 반환"""
    w, h, x0, y0 = img['width'], img['height'], img['x0'], img['y0']
    ux, uy = x0 * 2, y0 * 2
    uw, uh = w  * 2, h  * 2

    # 편집된 이미지를 1024x1024 canvas에 붙이기
    canvas = np.zeros((1024, 1024), dtype=np.uint8)
    ph, pw = unsw_arr.shape
    canvas[uy:uy+ph, ux:ux+pw] = unsw_arr[:ph, :pw]

    # swizzle -> atlas bytes
    swz   = swizzle8(canvas.tobytes(), 1024, 1024)
    atlas = bytearray(swz)

    # atlas에서 서브이미지 픽셀 추출
    pdata = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            src = ((y0 + y) * ATLAS_STRIDE + (x0 + x)) * 4
            dst = (y * w + x) * 4
            if src + 4 <= len(atlas):
                pdata[dst:dst+4] = atlas[src:src+4]
    return bytes(pdata)


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def cmd_extract(xtx_path: str, out_dir: str, fix_alpha: bool = False):
    data  = open(xtx_path, 'rb').read()
    magic = data[0:4]

    if magic == b'ARX\x00':
        print(f"[ARX] decompressing {xtx_path} ...")
        data = decompress_arx(data)
        if data[0:4] != b'XTX\x00':
            print("ERROR: ARX payload is not XTX"); return
        print(f"[ARX] decompressed -> {len(data)} bytes")

    if data[0:4] != b'XTX\x00':
        print(f"ERROR: unknown magic {data[0:4]}"); return

    images    = parse_xtx_headers(data)
    os.makedirs(out_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(xtx_path))[0]

    saved = 0
    for img in images:
        if not img['valid']:
            print(f"  [{img['index']}] {img['width']}x{img['height']}"
                  f" - SKIP (img_addr {hex(img['img_addr'])} out of range)")
            continue

        w, h = img['width'], img['height']

        # unswizzled grayscale (편집 + import 대상)
        crop     = img_to_unsw(data, img)
        out_path = os.path.join(out_dir, f"{base_name}_{saved + 1}.png")
        Image.fromarray(crop, 'L').save(out_path)
        print(f"  [{img['index']}] {w}x{h} -> {out_path}")

        # --fix-alpha: 원본 RGBA도 참고용으로 저장
        if fix_alpha:
            pdata    = data[img['pstart']:img['pend']]
            arr      = np.frombuffer(pdata, dtype=np.uint8).reshape(h, w, 4)
            rgba_out = arr.copy()
            rgba_out[:, :, 3] = np.clip(
                255.0 * (arr[:, :, 3].astype(np.float32) / 128.0), 0, 255
            ).astype(np.uint8)
            ref_path = os.path.join(out_dir, f"{base_name}_{saved + 1}_rgba.png")
            Image.fromarray(rgba_out, 'RGBA').save(ref_path)
            print(f"           rgba ref -> {ref_path}")

        saved += 1

    if saved == 0:
        print("  No valid sub-images found.")
    else:
        print(f"\n  {saved} image(s) extracted to: {out_dir}/")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def cmd_import(xtx_path: str, folder: str, out_path: str, fix_alpha: bool = False):
    data   = open(xtx_path, 'rb').read()
    is_arx = data[0:4] == b'ARX\x00'

    if is_arx:
        print(f"[ARX] decompressing {xtx_path} ...")
        xtx_data = decompress_arx(data)
        if xtx_data[0:4] != b'XTX\x00':
            print("ERROR: ARX payload is not XTX"); return
    else:
        xtx_data = data

    images       = parse_xtx_headers(xtx_data)
    valid_images = [img for img in images if img['valid']]

    if not valid_images:
        print("ERROR: no valid sub-images in XTX"); return

    base_name = os.path.splitext(os.path.basename(xtx_path))[0]
    pattern   = re.compile(rf'^{re.escape(base_name)}_(\d+)\.png$', re.IGNORECASE)
    entries   = []
    for fname in os.listdir(folder):
        m = pattern.match(fname)
        if m:
            entries.append((int(m.group(1)), os.path.join(folder, fname)))
    entries.sort(key=lambda x: x[0])

    if not entries:
        print(f"ERROR: no matching PNGs ({base_name}_1.png ...) found in {folder}/")
        return

    count = min(len(entries), len(valid_images))
    print(f"Replacing {count} sub-image(s) "
          f"({len(entries)} PNG(s) found, {len(valid_images)} valid slot(s))")

    xtx_out = bytearray(xtx_data)

    for i in range(count):
        num, png_path = entries[i]
        slot          = valid_images[i]
        w, h          = slot['width'], slot['height']
        uw, uh        = w * 2, h * 2

        png = Image.open(png_path).convert('L')
        if png.size != (uw, uh):
            print(f"  [{i+1}] WARNING: resizing {png.size} -> ({uw}, {uh})")
            png = png.resize((uw, uh), Image.LANCZOS)

        unsw_arr = np.array(png, dtype=np.uint8)
        pdata    = unsw_to_pdata(unsw_arr, slot)
        xtx_out[slot['pstart']:slot['pend']] = pdata
        print(f"  [{i+1}] {w}x{h} (unsw {uw}x{uh}) <- {png_path}")

    if count < len(entries):
        print(f"  WARNING: {len(entries)-count} PNG(s) ignored")
    if count < len(valid_images):
        print(f"  WARNING: {len(valid_images)-count} slot(s) not replaced")

    if is_arx:
        print("[ARX] WARNING: ARX re-compression not yet supported. Saving as raw XTX.")

    open(out_path, 'wb').write(xtx_out)
    print(f"\nSaved -> {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Xenosaga 1 XTX texture tool')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_ex = sub.add_parser('extract', help='XTX -> unswizzled grayscale PNG(s)')
    p_ex.add_argument('xtx', help='.xtx file path')
    p_ex.add_argument('--out', default=None, help='output directory')
    p_ex.add_argument('--fix-alpha', action='store_true',
                      help='also save RGBA reference PNG (_rgba.png)')

    p_im = sub.add_parser('import', help='edited unswizzled PNG(s) -> XTX')
    p_im.add_argument('xtx', help='original .xtx file path')
    p_im.add_argument('folder', help='folder with edited <n>_1.png, _2.png ...')
    p_im.add_argument('--out', default=None, help='output .xtx path')
    p_im.add_argument('--fix-alpha', action='store_true', help='(reserved)')

    args = parser.parse_args()

    if args.cmd == 'extract':
        out_dir = args.out or (os.path.splitext(args.xtx)[0] + '_extracted')
        cmd_extract(args.xtx, out_dir, args.fix_alpha)
    elif args.cmd == 'import':
        out_path = args.out or (os.path.splitext(args.xtx)[0] + '_imported.xtx')
        cmd_import(args.xtx, args.folder, out_path, args.fix_alpha)


if __name__ == '__main__':
    main()
