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

ATLAS_STRIDE = 512   # RGBA pixels per row; configured from XTX buffer_width
ATLAS_SIZE   = ATLAS_STRIDE * ATLAS_STRIDE * 4

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
    unsw = unswizzle8(bytes(atlas), FULL_INDEX_W, FULL_INDEX_H)
    arr  = np.frombuffer(unsw, dtype=np.uint8).reshape(FULL_INDEX_H, FULL_INDEX_W)
    ux, uy = x0 * 2, y0 * 2
    uw, uh = w  * 2, h  * 2
    return arr[uy:min(uy+uh, FULL_INDEX_H), ux:min(ux+uw, FULL_INDEX_W)]


def unsw_to_pdata(unsw_arr: np.ndarray, img: dict) -> bytes:
    """편집된 unswizzled grayscale -> swizzle -> 서브이미지 픽셀 바이트 반환"""
    w, h, x0, y0 = img['width'], img['height'], img['x0'], img['y0']
    ux, uy = x0 * 2, y0 * 2
    uw, uh = w  * 2, h  * 2

    # 편집된 이미지를 full index canvas에 붙이기
    canvas = np.zeros((FULL_INDEX_H, FULL_INDEX_W), dtype=np.uint8)
    ph, pw = unsw_arr.shape
    canvas[uy:uy+ph, ux:ux+pw] = unsw_arr[:ph, :pw]

    # swizzle -> atlas bytes
    swz   = swizzle8(canvas.tobytes(), FULL_INDEX_W, FULL_INDEX_H)
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
# Material-based LEX palette support
# ---------------------------------------------------------------------------

FULL_INDEX_W = 1024
FULL_INDEX_H = 1024
RGBA_ATLAS_W = 512
RGBA_ATLAS_H = 512


def configure_texture_dimensions(images: list):
    """Configure atlas dimensions from XTX buffer_width.

    Xenosaga XTX stores an RGBA atlas whose bytes are also treated as an
    8-bit indexed texture and unswizzled at twice the RGBA width/height.
    buffer_width=4 => 256x256 RGBA / 512x512 indexed.
    buffer_width=8 => 512x512 RGBA / 1024x1024 indexed.
    """
    global FULL_INDEX_W, FULL_INDEX_H, RGBA_ATLAS_W, RGBA_ATLAS_H, ATLAS_STRIDE, ATLAS_SIZE
    bw = 8
    for img in images:
        if img.get('valid'):
            bw = img.get('bw_eff') or img.get('bw') or 8
            break
    if bw <= 0:
        bw = 8
    FULL_INDEX_W = FULL_INDEX_H = int(bw) * 128
    RGBA_ATLAS_W = RGBA_ATLAS_H = FULL_INDEX_W // 2
    ATLAS_STRIDE = RGBA_ATLAS_W
    ATLAS_SIZE = ATLAS_STRIDE * ATLAS_STRIDE * 4


def build_rgba_atlas(data: bytes, images: list) -> np.ndarray:
    """Return raw XTX RGBA atlas as (512,512,4)."""
    atlas = np.zeros((RGBA_ATLAS_H, RGBA_ATLAS_W, 4), dtype=np.uint8)
    for img in images:
        if not img['valid']:
            continue
        w, h, x0, y0 = img['width'], img['height'], img['x0'], img['y0']
        pdata = data[img['pstart']:img['pend']]
        arr = np.frombuffer(pdata, dtype=np.uint8).reshape(h, w, 4)
        atlas[y0:y0+h, x0:x0+w, :] = arr
    return atlas


def build_index_atlas(data: bytes, images: list) -> np.ndarray:
    """Return full unswizzled 8-bit index atlas as (1024,1024)."""
    rgba_atlas = build_rgba_atlas(data, images)
    unsw = unswizzle8(rgba_atlas.tobytes(), FULL_INDEX_W, FULL_INDEX_H)
    return np.frombuffer(unsw, dtype=np.uint8).reshape(FULL_INDEX_H, FULL_INDEX_W).copy()


def index_atlas_to_xtx_pdata(index_atlas: np.ndarray, img: dict) -> bytes:
    """Full unswizzled index atlas -> one XTX subimage swizzled pdata."""
    w, h, x0, y0 = img['width'], img['height'], img['x0'], img['y0']
    swz = swizzle8(index_atlas.astype(np.uint8).tobytes(), FULL_INDEX_W, FULL_INDEX_H)
    rgba_atlas_bytes = swz
    pdata = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            src = ((y0 + y) * RGBA_ATLAS_W + (x0 + x)) * 4
            dst = (y * w + x) * 4
            pdata[dst:dst+4] = rgba_atlas_bytes[src:src+4]
    return bytes(pdata)


def _ps2_alpha_to_png(a: np.ndarray) -> np.ndarray:
    a16 = a.astype(np.uint16)
    if a16.max(initial=0) <= 128 and np.any(a16 > 0):
        return np.clip(a16 * 255 // 128, 0, 255).astype(np.uint8)
    return a.astype(np.uint8)


def get_palette_from_rgba_atlas(rgba_atlas: np.ndarray, palx: int, paly: int, reorder=True) -> np.ndarray:
    """Read a 16x16 CLUT from the raw XTX RGBA atlas.

    xenotool's original C code indexes the RGBA atlas as a flat array with
    offset = (paly + y) * raw_width + palx + x.  That means palx == raw_width
    is not invalid; it intentionally starts at the next row.  Several BG*.lex
    files use palx=256 with a 256-wide raw atlas, so a strict 2-D bounds check
    incorrectly rejects valid palettes.
    """
    raw_h, raw_w = rgba_atlas.shape[:2]
    if palx < 0 or paly < 0:
        return None
    flat = rgba_atlas.reshape(raw_h * raw_w, 4)
    start = paly * raw_w + palx
    idxs = []
    for y in range(16):
        row = start + y * raw_w
        idxs.extend(range(row, row + 16))
    if not idxs or min(idxs) < 0 or max(idxs) >= len(flat):
        return None
    pal = flat[np.array(idxs, dtype=np.int64)].copy()
    if reorder:
        # Same as C code: for each 32-color group, swap entries 8..15 with 16..23.
        for i in range(8):
            a = i * 32 + 8
            b = i * 32 + 16
            tmp = pal[a:a+8].copy()
            pal[a:a+8] = pal[b:b+8]
            pal[b:b+8] = tmp
    pal[:, 3] = _ps2_alpha_to_png(pal[:, 3])
    return pal


def _bits(v, start, length):
    return (v >> start) & ((1 << length) - 1)


def parse_uvinfo(buf: bytes, off: int):
    t = buf[off]
    b = buf[off+1:off+16]
    if len(b) < 15:
        return None
    if t == 0x00:
        return None
    if t == 0xff:
        # C bitfield layout, little-endian LSB first.
        uvw = _bits(b[0], 0, 4)
        x1  = _bits(b[1], 3, 1)
        uvx = _bits(b[1], 4, 4)
        uvh = _bits(b[2], 4, 4)
        y1  = _bits(b[3], 7, 1)
        uvy = _bits(b[4], 0, 4)
        umin = uvx * 64 + x1 * 32
        vmin = uvy * 64 + y1 * 32
        umax = umin + (uvw + 1) * 16
        vmax = vmin + (uvh + 1) * 16
        return umin, vmin, umax, vmax, t
    # type 0x0a and unknowns are parsed like xeno_lex.c fallback parse_materialraw_0a.
    uvx = _bits(b[0], 0, 6) << 4
    uvx2 = _bits(b[0], 6, 2)
    uvx1 = b[1]
    uvy = b[2]
    uvy2 = _bits(b[3], 2, 6)
    uvy1 = b[4]
    umin = uvx
    vmin = uvy
    umax = ((uvx1 << 2) | uvx2) + 1
    vmax = ((uvy1 << 6) | uvy2) + 1
    return umin, vmin, umax, vmax, t


def parse_paletteinfo(buf: bytes, off: int):
    if off + 16 > len(buf):
        return None
    pal2 = buf[off + 4]
    pal = buf[off + 5]
    pal_hi = pal >> 4
    pal_lo = pal & 0x0f

    # Corrected CLUT position formula.
    # The xenotool C code used:
    #   x = (pal_hi % 2) * 256 + (pal_lo / 2) * 32 + (pal2 >> 7) * 16
    #   y = (pal_hi / 2) * 32 + (pal_lo % 2) * 16
    # That places BG1 pal=0x60 at (0,96), producing a very dark/broken image.
    # The actual Xenosaga BG samples place the same CLUT at (0,192):
    #   pal=0x60 -> (0,192)  BG1/BG2/BG4
    #   pal=0x70 -> (0,224)  BG3/BG5
    #   pal=0x6E,pal2=0x86 -> (240,192) BG6
    # So pal_hi is a direct vertical tile index, not halved, and does not add
    # a 256-pixel horizontal page. pal_lo selects the 32x16 subcell.
    palx = (pal_lo // 2) * 32 + (pal2 >> 7) * 16
    paly = pal_hi * 32 + (pal_lo % 2) * 16
    return pal, pal2, palx, paly


def make_material(buf: bytes, pal_off: int, uv_off: int, source: str):
    pi = parse_paletteinfo(buf, pal_off)
    uv = parse_uvinfo(buf, uv_off)
    if pi is None or uv is None:
        return None
    pal, pal2, palx, paly = pi
    umin, vmin, umax, vmax, uvtype = uv
    if pal == 0xff:
        return None
    # Clamp clearly bogus rectangles but keep small/odd valid ones.
    umin = max(0, min(FULL_INDEX_W, int(umin)))
    umax = max(0, min(FULL_INDEX_W, int(umax)))
    vmin = max(0, min(FULL_INDEX_H, int(vmin)))
    vmax = max(0, min(FULL_INDEX_H, int(vmax)))
    if umax <= umin or vmax <= vmin:
        return None
    return {
        'source': source, 'pal': pal, 'pal2': pal2, 'palx': palx, 'paly': paly,
        'umin': umin, 'vmin': vmin, 'umax': umax, 'vmax': vmax, 'uvtype': uvtype,
    }


def _dedupe_materials(mats):
    seen = set(); out = []
    for m in mats:
        key = (m['pal'], m['pal2'], m['palx'], m['paly'], m['umin'], m['vmin'], m['umax'], m['vmax'])
        if key not in seen:
            seen.add(key); out.append(m)
    return out


def parse_lex_materials(lex_path: str, verbose=False, scan_blocks=False):
    """Best-effort parser for LEX mesh-header materials.

    scan_blocks=True also scans for embedded MaterialBlock-like records, but that can
    produce false positives, so it is intentionally optional.
    """
    data = open(lex_path, 'rb').read()
    if len(data) < 0xb0 or data[:3].lower() != b'lex':
        print(f"[LEX] WARNING: unexpected magic {data[:4]!r}; trying best-effort parse")
    mats = []

    # Mesh headers: LexHeader is 0xb0, followed by nmesh uint32 mesh addresses.
    try:
        nmesh = struct.unpack_from('<I', data, 0x44)[0]
        addr_table = 0xb0
        for i in range(min(nmesh, 4096)):
            if addr_table + i*4 + 4 > len(data): break
            maddr = struct.unpack_from('<I', data, addr_table + i*4)[0]
            if 0 <= maddr and maddr + 0x140 <= len(data):
                m = make_material(data, maddr + 0x120, maddr + 0x130, f'mesh_header[{i}]@0x{maddr:X}')
                if m: mats.append(m)
    except Exception as e:
        if verbose: print(f"[LEX] mesh-header parse failed: {e}")

    if scan_blocks:
        # Experimental fallback: scan for MaterialBlock / MaterialBlockSmall-like records.
        # This can generate false positives on arbitrary geometry bytes, so keep it off by default.
        for off in range(0, max(0, len(data) - 64), 16):
            m = make_material(data, off + 32, off + 16, f'scan_block@0x{off:X}')
            if m:
                area = (m['umax'] - m['umin']) * (m['vmax'] - m['vmin'])
                if 16 <= area <= FULL_INDEX_W * FULL_INDEX_H:
                    mats.append(m)

    mats = _dedupe_materials(mats)
    # Sort smaller/specific regions later so they override broad regions in map application.
    mats.sort(key=lambda m: (m['vmin'], m['umin'], (m['vmax']-m['vmin'])*(m['umax']-m['umin'])))
    print(f"[LEX] material palette regions: {len(mats)}")
    if verbose:
        for i, m in enumerate(mats[:200]):
            print(f"  mat {i:03d}: pal={m['pal']:02X} pal2={m['pal2']:02X} palxy=({m['palx']},{m['paly']}) "
                  f"uv=({m['umin']},{m['vmin']})-({m['umax']},{m['vmax']}) type={m['uvtype']:02X} {m['source']}")
        if len(mats) > 200: print(f"  ... {len(mats)-200} more")
    return mats


def build_material_maps(mats, rgba_atlas, ps2_reorder=True):
    mat_map = np.full((FULL_INDEX_H, FULL_INDEX_W), -1, dtype=np.int32)
    palettes = []
    valid_mats = []
    for m in mats:
        pal = get_palette_from_rgba_atlas(rgba_atlas, m['palx'], m['paly'], ps2_reorder)
        if pal is None:
            continue
        # Skip entirely empty palettes; these are usually false positives or unused material refs.
        if np.count_nonzero(pal[:, :3]) == 0 and np.count_nonzero(pal[:, 3]) == 0:
            continue
        idx = len(valid_mats)
        valid_mats.append(m)
        palettes.append(pal)
        mat_map[m['vmin']:m['vmax'], m['umin']:m['umax']] = idx
    print(f"[LEX] usable palette regions: {len(valid_mats)}")
    return mat_map, palettes, valid_mats


def colorize_index_atlas(index_atlas, mat_map, palettes):
    out = np.zeros((FULL_INDEX_H, FULL_INDEX_W, 4), dtype=np.uint8)
    for mi, pal in enumerate(palettes):
        mask = (mat_map == mi)
        if np.any(mask):
            out[mask] = pal[index_atlas[mask]]
    return out

def choose_palette_mode(requested: str, mat_map, palettes):
    """Resolve palette application mode.

    material: use UV/material rectangles from LEX.
    global: apply the first usable LEX palette to the whole indexed atlas.
    auto: if LEX material coverage is low, use global. This is important for
          BG*.lex where the mesh header only describes half the image, while
          the paired XTX is a single full-screen indexed image using one CLUT.
    """
    if not palettes:
        return 'none', 0.0
    coverage = 0.0
    if mat_map is not None:
        coverage = float(np.mean(mat_map >= 0))
    if requested == 'global':
        return 'global', coverage
    if requested == 'material':
        return 'material', coverage
    # auto
    if len(palettes) == 1 and coverage < 0.75:
        return 'global', coverage
    if coverage < 0.50:
        return 'global', coverage
    return 'material', coverage


def colorize_with_mode(index_atlas, mat_map, palettes, mode: str):
    if not palettes:
        return None
    if mode == 'global':
        return palettes[0][index_atlas]
    return colorize_index_atlas(index_atlas, mat_map, palettes)


def nearest_palette_indices(rgba_arr: np.ndarray, palette: np.ndarray) -> np.ndarray:
    rgba = rgba_arr.reshape(-1, 4).astype(np.int16)
    pal = palette.astype(np.int16)
    out = np.empty((rgba.shape[0],), dtype=np.uint8)
    transparent = rgba[:, 3] < 8
    if np.any(transparent):
        out[transparent] = int(np.argmin(pal[:, 3]))
    opaque_idx = np.where(~transparent)[0]
    if len(opaque_idx):
        q = rgba[opaque_idx]
        best = np.empty((len(q),), dtype=np.uint8)
        chunk = 32768
        for s in range(0, len(q), chunk):
            e = min(s + chunk, len(q))
            diff = q[s:e, None, :3] - pal[None, :, :3]
            dist = np.sum(diff * diff, axis=2)
            da = q[s:e, None, 3] - pal[None, :, 3]
            dist = dist + (da * da) // 8
            best[s:e] = np.argmin(dist, axis=1).astype(np.uint8)
        out[opaque_idx] = best
    return out.reshape(rgba_arr.shape[:2])


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def cmd_extract(xtx_path: str, out_dir: str, fix_alpha: bool = False, lex_path: str = None,
                lex_verbose: bool = False, pal_no_ps2_reorder: bool = False,
                save_full: bool = False, lex_scan: bool = False, palette_mode: str = 'auto'):
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
    configure_texture_dimensions(images)
    os.makedirs(out_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(xtx_path))[0]

    rgba_atlas = None
    index_atlas = None
    mat_map = palettes = valid_mats = None
    color_atlas = None
    if lex_path:
        rgba_atlas = build_rgba_atlas(data, images)
        index_atlas = build_index_atlas(data, images)
        mats = parse_lex_materials(lex_path, lex_verbose, lex_scan)
        mat_map, palettes, valid_mats = build_material_maps(mats, rgba_atlas, not pal_no_ps2_reorder)
        if not palettes:
            print("[LEX] WARNING: no usable palettes found in this XTX/LEX pair; falling back to grayscale extraction")
        else:
            resolved_mode, coverage = choose_palette_mode(palette_mode, mat_map, palettes)
            color_atlas = colorize_with_mode(index_atlas, mat_map, palettes, resolved_mode)
            print(f"[LEX] palette apply mode: {resolved_mode} (material coverage {coverage*100:.1f}%)")
        if save_full:
            if color_atlas is not None:
                Image.fromarray(color_atlas, 'RGBA').save(os.path.join(out_dir, f"{base_name}_full_palette.png"))
            Image.fromarray(index_atlas, 'L').save(os.path.join(out_dir, f"{base_name}_full_index.png"))

    saved = 0
    for img in images:
        if not img['valid']:
            print(f"  [{img['index']}] {img['width']}x{img['height']} - SKIP")
            continue

        w, h = img['width'], img['height']
        ux, uy = img['x0'] * 2, img['y0'] * 2
        uw, uh = w * 2, h * 2
        out_path = os.path.join(out_dir, f"{base_name}_{saved + 1}.png")

        if color_atlas is not None:
            crop = color_atlas[uy:uy+uh, ux:ux+uw, :]
            Image.fromarray(crop, 'RGBA').save(out_path)
            covered = int(np.mean(mat_map[uy:uy+uh, ux:ux+uw] >= 0) * 100) if mat_map is not None else 0
            print(f"  [{img['index']}] {w}x{h} -> {out_path} (RGBA, material coverage {covered}%)")
        else:
            crop = img_to_unsw(data, img)
            Image.fromarray(crop, 'L').save(out_path)
            print(f"  [{img['index']}] {w}x{h} -> {out_path}")

        if fix_alpha:
            pdata    = data[img['pstart']:img['pend']]
            arr      = np.frombuffer(pdata, dtype=np.uint8).reshape(h, w, 4)
            rgba_out = arr.copy()
            rgba_out[:, :, 3] = _ps2_alpha_to_png(arr[:, :, 3])
            ref_path = os.path.join(out_dir, f"{base_name}_{saved + 1}_rgba_atlas_ref.png")
            Image.fromarray(rgba_out, 'RGBA').save(ref_path)
            print(f"           rgba atlas ref -> {ref_path}")

        saved += 1

    print(f"\n  {saved} image(s) extracted to: {out_dir}/")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def cmd_import(xtx_path: str, folder: str, out_path: str, fix_alpha: bool = False, lex_path: str = None,
               lex_verbose: bool = False, pal_no_ps2_reorder: bool = False, lex_scan: bool = False, palette_mode: str = 'auto'):
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
    configure_texture_dimensions(images)
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
    print(f"Replacing {count} sub-image(s) ({len(entries)} PNG(s), {len(valid_images)} valid slot(s))")

    if lex_path:
        rgba_atlas = build_rgba_atlas(xtx_data, images)
        index_atlas = build_index_atlas(xtx_data, images)
        mats = parse_lex_materials(lex_path, lex_verbose, lex_scan)
        mat_map, palettes, valid_mats = build_material_maps(mats, rgba_atlas, not pal_no_ps2_reorder)
        if not palettes:
            print("[LEX] WARNING: no usable palettes found; color import cannot quantize. Use grayscale mode or check the XTX/LEX pair.")
            return
        resolved_mode, coverage = choose_palette_mode(palette_mode, mat_map, palettes)
        print(f"[LEX] palette import mode: {resolved_mode} (material coverage {coverage*100:.1f}%)")
    else:
        index_atlas = None
        mat_map = palettes = None

    xtx_out = bytearray(xtx_data)

    for i in range(count):
        num, png_path = entries[i]
        slot          = valid_images[i]
        w, h          = slot['width'], slot['height']
        uw, uh        = w * 2, h * 2
        ux, uy        = slot['x0'] * 2, slot['y0'] * 2

        src_png = Image.open(png_path)
        if src_png.size != (uw, uh):
            print(f"  [{i+1}] WARNING: resizing {src_png.size} -> ({uw}, {uh})")
            src_png = src_png.resize((uw, uh), Image.LANCZOS)

        if lex_path:
            rgba = np.array(src_png.convert('RGBA'), dtype=np.uint8)
            if resolved_mode == 'global':
                out_crop = nearest_palette_indices(rgba, palettes[0])
                converted = out_crop.size
                index_atlas[uy:uy+uh, ux:ux+uw] = out_crop
                mode_note = f"RGBA->indexed by global palette ({converted} px)"
            else:
                local_map = mat_map[uy:uy+uh, ux:ux+uw]
                out_crop = index_atlas[uy:uy+uh, ux:ux+uw].copy()
                converted = 0
                for mi, pal in enumerate(palettes):
                    mask = (local_map == mi)
                    if not np.any(mask):
                        continue
                    # Convert bounding subset for speed.
                    ys, xs = np.where(mask)
                    y0, y1 = ys.min(), ys.max() + 1
                    x0, x1 = xs.min(), xs.max() + 1
                    submask = mask[y0:y1, x0:x1]
                    subrgba = rgba[y0:y1, x0:x1, :]
                    idxs = nearest_palette_indices(subrgba, pal)
                    tmp = out_crop[y0:y1, x0:x1]
                    tmp[submask] = idxs[submask]
                    out_crop[y0:y1, x0:x1] = tmp
                    converted += int(np.count_nonzero(submask))
                if converted == 0:
                    print(f"  [{i+1}] WARNING: no material-covered pixels; image left unchanged unless grayscale fallback used")
                index_atlas[uy:uy+uh, ux:ux+uw] = out_crop
                mode_note = f"RGBA->indexed by material palettes ({converted} px)"
        else:
            png = src_png.convert('L')
            index_crop = np.array(png, dtype=np.uint8)
            # Use old per-image route for grayscale mode.
            pdata = unsw_to_pdata(index_crop, slot)
            xtx_out[slot['pstart']:slot['pend']] = pdata
            print(f"  [{i+1}] {w}x{h} (unsw {uw}x{uh}, grayscale index) <- {png_path}")
            continue

        print(f"  [{i+1}] {w}x{h} (unsw {uw}x{uh}, {mode_note}) <- {png_path}")

    if lex_path:
        # Rebuild all valid XTX slots from the modified full index atlas. This preserves untouched areas too.
        for slot in valid_images:
            pdata = index_atlas_to_xtx_pdata(index_atlas, slot)
            xtx_out[slot['pstart']:slot['pend']] = pdata

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
    parser = argparse.ArgumentParser(description='Xenosaga 1 XTX texture tool with LEX material palette support')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_ex = sub.add_parser('extract', help='XTX -> PNG(s); with --lex, colorized by material palettes')
    p_ex.add_argument('xtx', help='.xtx file path')
    p_ex.add_argument('--out', default=None, help='output directory')
    p_ex.add_argument('--fix-alpha', action='store_true', help='also save raw RGBA atlas reference PNGs')
    p_ex.add_argument('--lex', default=None, help='paired .lex file; enables colorized material-palette extract')
    p_ex.add_argument('--lex-verbose', action='store_true', help='print parsed material palette regions')
    p_ex.add_argument('--pal-no-ps2-reorder', action='store_true', help='disable PS2 8bpp CLUT 8/16 swap reorder')
    p_ex.add_argument('--save-full', action='store_true', help='also save full color/index atlases')
    p_ex.add_argument('--lex-scan', action='store_true', help='experimental: scan LEX for extra material blocks; may produce false positives')
    p_ex.add_argument('--palette-mode', choices=['auto','material','global'], default='auto', help='how to apply LEX palette: auto/global/material')

    p_im = sub.add_parser('import', help='edited PNG(s) -> XTX; with --lex, RGB/RGBA is quantized to material palettes')
    p_im.add_argument('xtx', help='original .xtx file path')
    p_im.add_argument('folder', help='folder with edited <base>_1.png, _2.png ...')
    p_im.add_argument('--out', default=None, help='output .xtx path')
    p_im.add_argument('--fix-alpha', action='store_true', help='reserved')
    p_im.add_argument('--lex', default=None, help='paired .lex file; enables color image import')
    p_im.add_argument('--lex-verbose', action='store_true', help='print parsed material palette regions')
    p_im.add_argument('--pal-no-ps2-reorder', action='store_true', help='disable PS2 8bpp CLUT 8/16 swap reorder')
    p_im.add_argument('--lex-scan', action='store_true', help='experimental: scan LEX for extra material blocks; may produce false positives')
    p_im.add_argument('--palette-mode', choices=['auto','material','global'], default='auto', help='how to quantize color PNGs: auto/global/material')

    args = parser.parse_args()

    if args.cmd == 'extract':
        out_dir = args.out or (os.path.splitext(args.xtx)[0] + '_extracted')
        cmd_extract(args.xtx, out_dir, args.fix_alpha, args.lex, args.lex_verbose, args.pal_no_ps2_reorder, args.save_full, args.lex_scan, args.palette_mode)
    elif args.cmd == 'import':
        out_path = args.out or (os.path.splitext(args.xtx)[0] + '_imported.xtx')
        cmd_import(args.xtx, args.folder, out_path, args.fix_alpha, args.lex, args.lex_verbose, args.pal_no_ps2_reorder, args.lex_scan, args.palette_mode)


if __name__ == '__main__':
    main()
