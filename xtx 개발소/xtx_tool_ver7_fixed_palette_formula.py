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
import hashlib
import importlib.util
import json
import sys
import numpy as np
from PIL import Image


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_local_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


psmt4_tool = load_local_module("xs1kor_xtx_psmt4_codec", os.path.join(SCRIPT_DIR, "xtx_psmt4_codec.py"))


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
INVALID_PALETTE_SCORE = -1.0e30

# GS facts used by PCSX2's local-memory layout:
# PSMT8 pages are 128x64 pixels, PSMT4 pages are 128x128 pixels, and CLUT
# sizes are 256 entries for PSMT8 / 16 entries for PSMT4.  The Xenosaga LEX
# fields below do not store a ready-made PNG palette coordinate; they encode a
# GS-style CLUT page/subcell choice.  We derive the plausible coordinates from
# those fields, then validate them against the actual XTX index usage.
GS_LAYOUT_FACTS = {
    'PSMT8_PAGE': [128, 64],
    'PSMT4_PAGE': [128, 128],
    'PSMT8_CLUT_ENTRIES': 256,
    'PSMT4_CLUT_ENTRIES': 16,
}


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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def texture_magic(data: bytes) -> str:
    if data[:4] == b'XTX\x00':
        return 'XTX'
    if data[:4] == b'ARX\x00':
        return 'ARX'
    return 'UNKNOWN'


def imported_default_path(path: str) -> str:
    stem, ext = os.path.splitext(path)
    return stem + '_imported' + (ext if ext else '.xtx')


def file_sha256(path: str) -> str:
    with open(path, 'rb') as f:
        return sha256(f.read())


def make_grayscale_palette(levels: int) -> list[int]:
    palette = []
    for i in range(256):
        if levels == 16:
            v = 0 if i == 0 else 96 + round((i & 0x0F) * (159 / 15))
        else:
            v = i
        palette.extend([v, v, v])
    return palette


def write_index_png(arr: np.ndarray, path: str, levels: int) -> None:
    image = Image.fromarray(arr.astype(np.uint8), 'P')
    image.putpalette(make_grayscale_palette(levels))
    image.save(path)


def read_index_png(path: str, expected_size: tuple[int, int], max_value: int) -> np.ndarray:
    image = Image.open(path)
    if image.mode == 'P':
        arr = np.array(image, dtype=np.uint8)
    elif image.mode in ('L', 'I;16', 'I'):
        arr = np.array(image.convert('L'), dtype=np.uint8)
    else:
        raise ValueError(
            f"{path} must be an indexed/grayscale PNG. "
            "RGBA/RGB images are rejected to avoid color quantization mistakes."
        )
    got = (int(arr.shape[1]), int(arr.shape[0]))
    if got != expected_size:
        raise ValueError(f"{path} size must be {expected_size}, got {got}")
    if int(arr.max(initial=0)) > max_value:
        raise ValueError(f"{path} contains values above {max_value}")
    return arr.astype(np.uint8)


def psmt4_config(xtx_path: str) -> dict:
    stem = os.path.splitext(os.path.basename(str(xtx_path)))[0].lower()
    dbw4 = 1024 if stem in ('help_01_01', 'help_03_01') else 64
    profile = 'full_atlas_psmt4'
    if stem == 'help_01_01':
        profile = 'help_01_01_psmt4_full_width'
    elif stem == 'help_03_01':
        profile = 'help_03_01_full_atlas_tbw16'
    return {
        'profile': profile,
        'width': int(FULL_INDEX_W),
        'height': int(FULL_INDEX_H),
        'dbp': 0,
        'dbw4': int(dbw4),
        'dbw32': max(1, int(FULL_INDEX_W) // 2),
        'gs_layout_facts': GS_LAYOUT_FACTS,
        'notes': [
            'PSMT4 is a logical GS interpretation of the same XTX upload bytes.',
            'PCSX2 GS layout basis: PSMT4 page=128x128, PSMT8 page=128x64, CLUT entries are 16/256.',
            'Edit exact index values 0..15 only.',
        ],
    }


def decode_psmt4_png(xtx_data: bytes, images: list, cfg: dict) -> np.ndarray:
    gsmem = psmt4_tool.build_gsmem_words(xtx_data, images, int(cfg['dbw32']))
    return psmt4_tool.decode_psmt4(
        gsmem,
        int(cfg['width']),
        int(cfg['height']),
        int(cfg['dbp']),
        int(cfg['dbw4']),
    ).astype(np.uint8)


def set_psmt4_pixel(gsmem: np.ndarray, x: int, y: int, value: int, dbp: int, dbw4: int) -> None:
    pos, cb = psmt4_tool.psmt4_pos(x, y, dbp, dbw4)
    if not (0 <= pos < len(gsmem)):
        return
    word = int(gsmem[pos])
    shift = (cb >> 1) * 8
    byte = (word >> shift) & 0xFF
    if cb & 1:
        byte = (byte & 0x0F) | ((int(value) & 0x0F) << 4)
    else:
        byte = (byte & 0xF0) | (int(value) & 0x0F)
    mask = 0xFF << shift
    gsmem[pos] = (word & ~mask) | (byte << shift)


def gsmem_to_xtx_payloads(gsmem: np.ndarray, images: list, dbw32: int) -> dict[int, bytes]:
    out = {}
    for img in images:
        if not img.get('valid'):
            continue
        w, h, x0, y0 = int(img['width']), int(img['height']), int(img['x0']), int(img['y0'])
        words = np.zeros((h, w), dtype='<u4')
        for y in range(h):
            for x in range(w):
                pos = psmt4_tool.ct32_pos(x0 + x, y0 + y, dbw32)
                words[y, x] = int(gsmem[pos]) if 0 <= pos < len(gsmem) else 0
        out[int(img['index'])] = words.tobytes()
    return out


def rebuild_xtx_from_psmt4(original_xtx: bytes, images: list, cfg: dict, index4: np.ndarray) -> bytes:
    gsmem = psmt4_tool.build_gsmem_words(original_xtx, images, int(cfg['dbw32']))
    for y in range(index4.shape[0]):
        for x in range(index4.shape[1]):
            set_psmt4_pixel(gsmem, x, y, int(index4[y, x]), int(cfg['dbp']), int(cfg['dbw4']))
    payloads = gsmem_to_xtx_payloads(gsmem, images, int(cfg['dbw32']))
    modified = bytearray(original_xtx)
    for img in images:
        idx = int(img['index'])
        if idx not in payloads:
            continue
        pstart, pend = int(img['pstart']), int(img['pend'])
        pdata = payloads[idx]
        if len(pdata) != pend - pstart:
            raise ValueError('PSMT4 payload size mismatch')
        modified[pstart:pend] = pdata
    return bytes(modified)


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
    candidates = lex_clut_coordinate_candidates(pal, pal2)
    return pal, pal2, candidates


def lex_clut_coordinate_candidates(pal: int, pal2: int) -> dict[str, tuple[int, int]]:
    """Return LEX-derived CLUT coordinate candidates.

    The names describe observed layout families, not file-specific cases:

    primary:
        Xenosaga BG/card-style layout where pal_hi is a direct vertical page.
    legacy:
        xenotool's older interpretation, kept because some actor/card assets
        still resolve there.
    page:
        128-pixel CLUT page-row layout.
    halfpage:
        128-pixel page-row layout with 16-pixel PSMT4 CLUT subcells.  This is
        the layout used by the `carddata/help` h_* and help5/help6 families.
    """
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

    # Some LEX files use different CLUT page layouts. Keep candidate
    # coordinates and select the one that resolves to real CLUT data once the
    # paired XTX atlas is available.
    alt_palx = (pal_hi % 2) * 256 + (pal_lo // 2) * 32 + (pal2 >> 7) * 16
    alt_paly = (pal_hi // 2) * 32 + (pal_lo % 2) * 16
    page_palx = pal_hi * 128 + (pal_lo // 2) * 32 + (pal2 >> 7) * 16
    page_paly = (pal_lo % 2) * 16
    halfpage_palx = pal_hi * 128 + (pal_lo // 2) * 16 + (pal2 >> 7) * 16
    halfpage_paly = (pal_lo % 2) * 16
    return {
        'primary': (palx, paly),
        'legacy': (alt_palx, alt_paly),
        'page': (page_palx, page_paly),
        'halfpage': (halfpage_palx, halfpage_paly),
    }


def make_material(buf: bytes, pal_off: int, uv_off: int, source: str):
    pi = parse_paletteinfo(buf, pal_off)
    uv = parse_uvinfo(buf, uv_off)
    if pi is None or uv is None:
        return None
    pal, pal2, candidates = pi
    palx, paly = candidates['primary']
    alt_palx, alt_paly = candidates['legacy']
    page_palx, page_paly = candidates['page']
    halfpage_palx, halfpage_paly = candidates['halfpage']
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
        'alt_palx': alt_palx, 'alt_paly': alt_paly, 'pal_source': 'primary',
        'page_palx': page_palx, 'page_paly': page_paly,
        'halfpage_palx': halfpage_palx, 'halfpage_paly': halfpage_paly,
        'clut_candidates': {k: [int(v[0]), int(v[1])] for k, v in candidates.items()},
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
            extras = []
            if (m.get('alt_palx'), m.get('alt_paly')) != (m['palx'], m['paly']):
                extras.append(f"legacy=({m['alt_palx']},{m['alt_paly']})")
            if (m.get('page_palx'), m.get('page_paly')) not in ((m['palx'], m['paly']), (m.get('alt_palx'), m.get('alt_paly'))):
                extras.append(f"page=({m['page_palx']},{m['page_paly']})")
            if (m.get('halfpage_palx'), m.get('halfpage_paly')) not in (
                (m['palx'], m['paly']),
                (m.get('alt_palx'), m.get('alt_paly')),
                (m.get('page_palx'), m.get('page_paly')),
            ):
                extras.append(f"halfpage=({m['halfpage_palx']},{m['halfpage_paly']})")
            extra = f" {' '.join(extras)}" if extras else ''
            print(f"  mat {i:03d}: pal={m['pal']:02X} pal2={m['pal2']:02X} palxy=({m['palx']},{m['paly']}){extra} "
                  f"uv=({m['umin']},{m['vmin']})-({m['umax']},{m['vmax']}) type={m['uvtype']:02X} {m['source']}")
        if len(mats) > 200: print(f"  ... {len(mats)-200} more")
    return mats


def palette_candidate_score(palette: np.ndarray, index_crop: np.ndarray | None) -> float:
    if palette is None:
        return INVALID_PALETTE_SCORE
    if np.count_nonzero(palette[:, :3]) == 0 and np.count_nonzero(palette[:, 3]) == 0:
        return INVALID_PALETTE_SCORE
    if index_crop is None or index_crop.size == 0:
        alpha = palette[:, 3].astype(np.int16)
        visible_unique = len({tuple(v) for v in palette[alpha > 8].tolist()})
        return float(visible_unique * 16 + int(np.count_nonzero(alpha > 8)))

    used = np.bincount(index_crop.reshape(-1), minlength=256).astype(np.float64)
    used_idx = np.where(used > 0)[0]
    if len(used_idx) == 0:
        return INVALID_PALETTE_SCORE

    alpha = palette[:, 3].astype(np.float64)
    used_alpha = alpha[used_idx]
    used_weight = used[used_idx]
    total = float(np.sum(used_weight))
    visible = float(np.sum(used_weight[used_alpha > 8]))
    transparent = total - visible
    low_alpha = float(np.sum(used_weight[(used_alpha > 8) & (used_alpha < 96)]))
    mean_alpha = float(np.average(used_alpha, weights=used_weight))
    used_unique = len({tuple(v) for v in palette[used_idx].tolist()})

    score = visible * 3.0
    score -= transparent * 8.0
    score -= low_alpha * 0.75
    score += (mean_alpha / 128.0) * total
    score += min(used_unique, 128) * 8.0
    return float(score)


def collect_palette_sources(xtx_path: str, current_rgba_atlas: np.ndarray, palette_xtx_paths: list[str] | None = None,
                            auto_sources: bool = True) -> list[dict]:
    def palette_source_name(path: str) -> str:
        name = os.path.basename(path)
        parent = os.path.basename(os.path.dirname(path))
        if name.lower() == 'original.xtx' and parent:
            return f"{parent}/{name}"
        return name

    def palette_source_priority(path: str, explicit: bool = False, current: bool = False) -> float:
        if explicit:
            return 20000.0
        name = palette_source_name(path).lower()
        if current:
            return 5000.0
        if 'base' in name or 'grap' in name:
            return 5000.0
        if name.endswith('/original.xtx'):
            return 1000.0
        return -10000.0

    def is_likely_auto_palette_source(path: str) -> bool:
        name = palette_source_name(path).lower()
        return 'base' in name or 'grap' in name

    sources = [{
        'name': palette_source_name(xtx_path),
        'path': os.path.abspath(xtx_path),
        'rgba_atlas': current_rgba_atlas,
        'current': True,
        'priority': palette_source_priority(xtx_path, current=True),
    }]
    seen = {os.path.abspath(xtx_path).lower()}
    candidates = []
    explicit_candidates = set()
    if palette_xtx_paths:
        for path in palette_xtx_paths:
            candidates.append(path)
            explicit_candidates.add(os.path.abspath(path).lower())
    if auto_sources:
        folder = os.path.dirname(os.path.abspath(xtx_path))
        scan_folders = [folder]
        if os.path.basename(folder).lower().endswith('_out'):
            parent = os.path.dirname(folder)
            if parent and parent not in scan_folders:
                scan_folders.append(parent)
        try:
            for scan_folder in scan_folders:
                for fname in os.listdir(scan_folder):
                    path = os.path.join(scan_folder, fname)
                    if os.path.isfile(path):
                        if is_likely_auto_palette_source(path):
                            candidates.append(path)
                    elif os.path.isdir(path) and fname.lower().endswith('_out'):
                        extracted_original = os.path.join(path, 'original.xtx')
                        if os.path.isfile(extracted_original) and is_likely_auto_palette_source(extracted_original):
                            candidates.append(extracted_original)
        except OSError:
            pass

    for path in candidates:
        abspath = os.path.abspath(path)
        key = abspath.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            with open(abspath, 'rb') as f:
                data = f.read()
            if texture_magic(data) == 'ARX':
                data = decompress_arx(data)
            if data[:4] != b'XTX\x00':
                continue
            images = parse_xtx_headers(data)
            bw = next((int(img.get('bw_eff') or img.get('bw') or 8) for img in images if img.get('valid')), 8)
            if bw * 64 != RGBA_ATLAS_W:
                continue
            sources.append({
                'name': palette_source_name(abspath),
                'path': abspath,
                'rgba_atlas': build_rgba_atlas(data, images),
                'current': False,
                'priority': palette_source_priority(abspath, key in explicit_candidates, False),
            })
        except Exception:
            continue
    return sources


def build_material_maps(mats, rgba_atlas, ps2_reorder=True, index_atlas=None, palette_sources=None):
    mat_map = np.full((FULL_INDEX_H, FULL_INDEX_W), -1, dtype=np.int32)
    palettes = []
    valid_mats = []
    if not palette_sources:
        palette_sources = [{
            'name': 'current',
            'path': None,
            'rgba_atlas': rgba_atlas,
            'current': True,
        }]
    for m in mats:
        pal = None
        chosen = None
        coords = [
            ('primary', m['palx'], m['paly']),
            ('legacy', m.get('alt_palx', m['palx']), m.get('alt_paly', m['paly'])),
            ('page', m.get('page_palx', m['palx']), m.get('page_paly', m['paly'])),
            ('halfpage', m.get('halfpage_palx', m['palx']), m.get('halfpage_paly', m['paly'])),
        ]
        seen_coords = set()
        index_crop = None
        if index_atlas is not None:
            index_crop = index_atlas[m['vmin']:m['vmax'], m['umin']:m['umax']]
        best_score = INVALID_PALETTE_SCORE
        for pal_source in palette_sources:
            src_rgba = pal_source['rgba_atlas']
            for source, palx, paly in coords:
                key = (pal_source['path'], source, palx, paly)
                if key in seen_coords:
                    continue
                seen_coords.add(key)
                candidate = get_palette_from_rgba_atlas(src_rgba, palx, paly, ps2_reorder)
                raw_score = palette_candidate_score(candidate, index_crop)
                if raw_score <= INVALID_PALETTE_SCORE / 2:
                    continue
                score = raw_score + float(pal_source.get('priority', 0.0))
                if score > best_score:
                    pal = candidate
                    chosen = (
                        source, palx, paly, pal_source['name'], pal_source['path'],
                        score, pal_source.get('current', False), raw_score,
                        float(pal_source.get('priority', 0.0)),
                    )
                    best_score = score
        if pal is None:
            continue
        idx = len(valid_mats)
        selected = dict(m)
        if chosen:
            selected['pal_source'], selected['palx'], selected['paly'] = chosen[0], chosen[1], chosen[2]
            selected['palette_xtx_name'] = chosen[3]
            selected['palette_xtx_path'] = chosen[4]
            selected['palette_score'] = float(chosen[5])
            selected['palette_raw_score'] = float(chosen[7])
            selected['palette_source_priority'] = float(chosen[8])
            if chosen[0] != 'primary' or not chosen[6]:
                print(
                    f"[LEX] palette fallback: pal={m['pal']:02X} pal2={m['pal2']:02X} "
                    f"primary=({m['palx']},{m['paly']}) -> {chosen[3]}:{chosen[0]}=({chosen[1]},{chosen[2]}) "
                    f"score={chosen[5]:.1f} raw={chosen[7]:.1f} priority={chosen[8]:.1f}"
                )
        valid_mats.append(selected)
        palettes.append(pal)
        mat_map[selected['vmin']:selected['vmax'], selected['umin']:selected['umax']] = idx
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


def save_clean_edit_set(out_dir: str, index_atlas: np.ndarray, valid_mats: list, palettes: list) -> list:
    edit_items = []
    for i, (mat, pal) in enumerate(zip(valid_mats, palettes), 1):
        umin, vmin = int(mat['umin']), int(mat['vmin'])
        umax, vmax = int(mat['umax']), int(mat['vmax'])
        umin = max(0, min(FULL_INDEX_W, umin))
        umax = max(0, min(FULL_INDEX_W, umax))
        vmin = max(0, min(FULL_INDEX_H, vmin))
        vmax = max(0, min(FULL_INDEX_H, vmax))
        if umax <= umin or vmax <= vmin:
            continue

        crop_idx = index_atlas[vmin:vmax, umin:umax]
        crop_rgba = pal[crop_idx]
        rel_path = f"edit_{i:03d}.png"
        out_path = os.path.join(out_dir, rel_path)
        Image.fromarray(crop_rgba, 'RGBA').save(out_path)
        edit_items.append({
            'path': rel_path,
            'sha256': file_sha256(out_path),
            'rect': [umin, vmin, umax, vmax],
            'size': [umax - umin, vmax - vmin],
            'palette_rgba': pal.astype(np.uint8).tolist(),
            'material': dict(mat),
        })
        print(
            f"  [EDIT {i:03d}] rect=({umin},{vmin})-({umax},{vmax}) "
            f"palette={mat.get('pal_source', 'primary')}({mat['palx']},{mat['paly']}) -> {out_path}"
        )
    return edit_items


def read_psmt4_palette_row(rgba_atlas: np.ndarray, x: int, y: int) -> np.ndarray | None:
    if y < 0 or y >= rgba_atlas.shape[0] or x < 0 or x + 16 > rgba_atlas.shape[1]:
        return None
    pal = rgba_atlas[y, x:x+16, :].copy()
    pal[:, 3] = _ps2_alpha_to_png(pal[:, 3])
    return pal.astype(np.uint8)


def sanitize_display_palette(palette: np.ndarray) -> np.ndarray:
    pal = palette.astype(np.uint8).copy()
    pal[pal[:, 3] <= 8, :3] = 0
    return pal


def score_psmt4_palette(palette: np.ndarray, hist: list[int]) -> float:
    total_nonzero = sum(hist[1:16])
    if total_nonzero <= 0:
        return -1.0
    alpha0 = int(palette[0, 3])
    if hist[0] > total_nonzero and alpha0 > 16:
        return -1.0
    opaque_used = 0
    for i in range(1, 16):
        if int(palette[i, 3]) > 32:
            opaque_used += int(hist[i])
    opaque_ratio = opaque_used / total_nonzero
    if opaque_ratio < 0.10:
        return -1.0

    rgb = palette[:, :3].astype(np.int16)
    alpha = palette[:, 3].astype(np.int16)
    has_white = bool(np.any((np.all(rgb > 220, axis=1)) & (alpha > 160)))
    has_black = bool(np.any((np.sum(rgb, axis=1) < 48) & (alpha > 160)))
    visible_unique = len({tuple(v) for v in palette[alpha > 32].tolist()})
    if visible_unique > 8:
        return -1.0

    score = opaque_ratio * 1000 + visible_unique * 20
    if alpha0 <= 16:
        score += 2000
    else:
        score -= alpha0 * 8
    if has_white:
        score += 250
    if has_black:
        score += 250
    return score


def psmt4_palette_search_regions(images: list, rgba_shape: tuple[int, int, int], valid_mats: list) -> list[dict]:
    regions = []
    for img in images:
        if not img.get('valid'):
            continue
        w, h = int(img['width']), int(img['height'])
        if w >= 16 and w <= 32 and h <= 16:
            regions.append({
                'source': f"small_slot[{img['index']}]",
                'x0': int(img['x0']),
                'y0': int(img['y0']),
                'x1': min(int(rgba_shape[1]) - 15, int(img['x0']) + w),
                'y1': min(int(rgba_shape[0]), int(img['y0']) + h),
                'base_palx': int(img['x0']),
                'base_paly': int(img['y0']),
                'base_pal_source': f"small_slot[{img['index']}]",
            })
    if regions:
        return regions

    for mat in valid_mats:
        base_x, base_y = int(mat['palx']), int(mat['paly'])
        regions.append({
            'source': mat.get('pal_source', 'primary'),
            'x0': base_x,
            'y0': base_y,
            'x1': min(base_x + 32, int(rgba_shape[1]) - 15),
            'y1': min(base_y + 16, int(rgba_shape[0])),
            'base_palx': base_x,
            'base_paly': base_y,
            'base_pal_source': mat.get('pal_source', 'primary'),
        })
    return regions


def find_psmt4_palette(rgba_atlas: np.ndarray, psmt4_index: np.ndarray, valid_mats: list, images: list) -> dict | None:
    if not valid_mats:
        return None
    hist = np.bincount(psmt4_index.reshape(-1), minlength=16).astype(np.int64).tolist()
    best = None
    regions = psmt4_palette_search_regions(images, rgba_atlas.shape, valid_mats)
    for region in regions:
        for y in range(region['y0'], region['y1']):
            for x in range(region['x0'], region['x1']):
                pal = read_psmt4_palette_row(rgba_atlas, x, y)
                if pal is None:
                    continue
                score = score_psmt4_palette(pal, hist)
                if best is None or score > best['score']:
                    display_pal = sanitize_display_palette(pal)
                    best = {
                        'score': float(score),
                        'palx': int(x),
                        'paly': int(y),
                        'base_palx': int(region['base_palx']),
                        'base_paly': int(region['base_paly']),
                        'base_pal_source': region['base_pal_source'],
                        'search_source': region['source'],
                        'palette_rgba': display_pal.tolist(),
                        'raw_palette_rgba': pal.tolist(),
                    }
    if best is None or best['score'] < 0:
        return None
    return best


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
                save_full: bool = False, lex_scan: bool = False, palette_mode: str = 'auto',
                edit_only: bool = False, palette_xtx_paths: list[str] | None = None,
                auto_palette_sources: bool = True):
    data  = open(xtx_path, 'rb').read()
    magic = texture_magic(data)

    if magic == 'ARX':
        print(f"[ARX] decompressing {xtx_path} ...")
        data = decompress_arx(data)
        if data[0:4] != b'XTX\x00':
            print("ERROR: ARX payload is not XTX"); return
        print(f"[ARX] decompressed -> {len(data)} bytes")

    if magic not in ('XTX', 'ARX') or data[0:4] != b'XTX\x00':
        print(f"ERROR: unsupported file magic {data[0:4]!r}; expected XTX\\0 or ARX\\0")
        return

    images    = parse_xtx_headers(data)
    configure_texture_dimensions(images)
    os.makedirs(out_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(xtx_path))[0]

    rgba_atlas = None
    index_atlas = build_index_atlas(data, images)
    original_xtx_path = os.path.join(out_dir, 'original.xtx')
    with open(original_xtx_path, 'wb') as f:
        f.write(data)

    meta = {
        'tool': 'xtx_tool_ver7_fixed_palette_formula',
        'version': 10,
        'source_xtx_name': os.path.basename(xtx_path),
        'source_xtx_path': os.path.abspath(xtx_path),
        'xtx_size': len(data),
        'xtx_sha256': sha256(data),
        'image_count': len(images),
        'images': [dict(img) for img in images],
        'gs_layout_facts': GS_LAYOUT_FACTS,
        'metadata_runtime_evidence': [
            'OV12.OVL strings reference rg_help.euc.c, help.npr, help_*.bxx, RgBxxGetXtx, pXtxData, and pLexData.',
            'This supports the container -> XTX/LEX -> GS indexed texture pipeline.',
        ],
        'edit_files': {},
        'clean_edit_files': [],
        'subimage_edit_files': [],
        'full_reference_files': {},
        'rebuild_rule': [
            'PSMT4.png, PSMT8.png, and edit_*.png are separate edit pipelines.',
            '<source>_N.png subimage files are also edit inputs when changed.',
            '<source>_full_index.png is a PSMT8 full-index reference/edit input.',
            'Preferred workflow: keep extracted originals untouched and place edited *_KOR.png files beside them.',
            'Example: edit_001_KOR.png replaces edit_001.png; <source>_1_KOR.png replaces <source>_1.png.',
            'Edit only one pipeline before import.',
            'PSMT4 pixels must be exact index values 0..15.',
            'PSMT8 pixels must be exact index values 0..255.',
            'edit_*.png files are palette-applied material edit inputs.',
            'If several files in one pipeline changed, they are applied together.',
            'If files from multiple pipelines changed, rebuild stops as ambiguous.',
        ],
    }

    psmt8_path = os.path.join(out_dir, 'PSMT8.png')
    write_index_png(index_atlas, psmt8_path, 256)

    p4_cfg = psmt4_config(xtx_path)
    psmt4 = decode_psmt4_png(data, images, p4_cfg)
    psmt4_path = os.path.join(out_dir, 'PSMT4.png')
    write_index_png(psmt4, psmt4_path, 16)

    meta['edit_files'] = {
        'PSMT4': {
            'path': 'PSMT4.png',
            'sha256': file_sha256(psmt4_path),
            'size': [int(psmt4.shape[1]), int(psmt4.shape[0])],
            'max_index': 15,
            'config': p4_cfg,
        },
        'PSMT8': {
            'path': 'PSMT8.png',
            'sha256': file_sha256(psmt8_path),
            'size': [int(index_atlas.shape[1]), int(index_atlas.shape[0])],
            'max_index': 255,
        },
    }

    mat_map = palettes = valid_mats = None
    color_atlas = None
    if lex_path:
        rgba_atlas = build_rgba_atlas(data, images)
        mats = parse_lex_materials(lex_path, lex_verbose, lex_scan)
        palette_sources = collect_palette_sources(xtx_path, rgba_atlas, palette_xtx_paths, auto_palette_sources)
        if len(palette_sources) > 1:
            names = ', '.join(src['name'] for src in palette_sources[:12])
            if len(palette_sources) > 12:
                names += f", ... +{len(palette_sources)-12}"
            print(f"[LEX] palette source candidates: {names}")
        mat_map, palettes, valid_mats = build_material_maps(
            mats, rgba_atlas, not pal_no_ps2_reorder, index_atlas, palette_sources
        )
        if not palettes:
            print("[LEX] WARNING: no usable palettes found in this XTX/LEX pair; falling back to grayscale extraction")
        else:
            resolved_mode, coverage = choose_palette_mode(palette_mode, mat_map, palettes)
            color_atlas = colorize_with_mode(index_atlas, mat_map, palettes, resolved_mode)
            print(f"[LEX] palette apply mode: {resolved_mode} (material coverage {coverage*100:.1f}%)")
            meta['clean_edit_files'] = save_clean_edit_set(out_dir, index_atlas, valid_mats, palettes)
            p4_palette = find_psmt4_palette(rgba_atlas, psmt4, valid_mats, images)
            if p4_palette:
                p4_rgba = np.array(p4_palette['palette_rgba'], dtype=np.uint8)[psmt4]
                p4_rgba_path = os.path.join(out_dir, 'PSMT4_RGBA.png')
                Image.fromarray(p4_rgba, 'RGBA').save(p4_rgba_path)
                meta['edit_files']['PSMT4_RGBA'] = {
                    'path': 'PSMT4_RGBA.png',
                    'sha256': file_sha256(p4_rgba_path),
                    'size': [int(psmt4.shape[1]), int(psmt4.shape[0])],
                    'config': p4_cfg,
                    'palette_rgba': p4_palette['palette_rgba'],
                    'raw_palette_rgba': p4_palette.get('raw_palette_rgba'),
                    'palette_source': {
                        'palx': p4_palette['palx'],
                        'paly': p4_palette['paly'],
                        'base_palx': p4_palette['base_palx'],
                        'base_paly': p4_palette['base_paly'],
                        'base_pal_source': p4_palette['base_pal_source'],
                        'search_source': p4_palette.get('search_source'),
                        'score': p4_palette['score'],
                    },
                }
                print(
                    f"[PSMT4] palette row=({p4_palette['palx']},{p4_palette['paly']}) "
                    f"base=({p4_palette['base_palx']},{p4_palette['base_paly']}) "
                    f"score={p4_palette['score']:.1f} -> {p4_rgba_path}"
                )
        if save_full:
            if color_atlas is not None:
                full_palette_path = os.path.join(out_dir, f"{base_name}_full_palette.png")
                Image.fromarray(color_atlas, 'RGBA').save(full_palette_path)
                meta['full_reference_files']['LEX_FULL_RGBA'] = {
                    'path': os.path.basename(full_palette_path),
                    'sha256': file_sha256(full_palette_path),
                    'size': [int(color_atlas.shape[1]), int(color_atlas.shape[0])],
                    'requires_lex': True,
                }
            full_index_path = os.path.join(out_dir, f"{base_name}_full_index.png")
            Image.fromarray(index_atlas, 'L').save(full_index_path)
            meta['full_reference_files']['PSMT8_FULL_INDEX'] = {
                'path': os.path.basename(full_index_path),
                'sha256': file_sha256(full_index_path),
                'size': [int(index_atlas.shape[1]), int(index_atlas.shape[0])],
                'max_index': 255,
            }

    if edit_only and meta.get('clean_edit_files'):
        with open(os.path.join(out_dir, 'xtx_meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"\n  {len(meta['clean_edit_files'])} clean edit image(s) extracted to: {out_dir}/")
        return

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

        meta['subimage_edit_files'].append({
            'path': os.path.basename(out_path),
            'sha256': file_sha256(out_path),
            'slot_order': int(saved),
            'image_index': int(img['index']),
            'rect': [int(ux), int(uy), int(ux + uw), int(uy + uh)],
            'size': [int(uw), int(uh)],
            'requires_lex': bool(color_atlas is not None),
        })

        if fix_alpha:
            pdata    = data[img['pstart']:img['pend']]
            arr      = np.frombuffer(pdata, dtype=np.uint8).reshape(h, w, 4)
            rgba_out = arr.copy()
            rgba_out[:, :, 3] = _ps2_alpha_to_png(arr[:, :, 3])
            ref_path = os.path.join(out_dir, f"{base_name}_{saved + 1}_rgba_atlas_ref.png")
            Image.fromarray(rgba_out, 'RGBA').save(ref_path)
            print(f"           rgba atlas ref -> {ref_path}")

        saved += 1

    with open(os.path.join(out_dir, 'xtx_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"\n  {saved} image(s) extracted to: {out_dir}/")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def kor_variant_path(path: str) -> str:
    stem, ext = os.path.splitext(path)
    return stem + '_KOR' + ext


def edit_input_path(folder: str, item: dict, label: str = 'edit file') -> tuple[str, bool]:
    path = os.path.join(folder, item['path'])
    kor_path = kor_variant_path(path)
    if os.path.exists(kor_path):
        return kor_path, True
    if not os.path.exists(path):
        raise ValueError(f"missing {label}: {path}")
    return path, False


def edit_item_changed(folder: str, item: dict, label: str = 'edit file') -> dict | None:
    path, is_kor = edit_input_path(folder, item, label)
    if is_kor or file_sha256(path) != item['sha256']:
        changed = dict(item)
        changed['_input_path'] = path
        changed['_input_is_kor'] = is_kor
        return changed
    return None


def edit_item_path(item: dict, folder: str) -> str:
    return item.get('_input_path') or edit_input_path(folder, item)[0]


def changed_rgba_mask(folder: str, item: dict, rgba: np.ndarray) -> np.ndarray | None:
    ref_path = os.path.join(folder, item['path'])
    if not os.path.exists(ref_path):
        return None
    try:
        ref = np.array(Image.open(ref_path).convert('RGBA'), dtype=np.uint8)
    except Exception:
        return None
    if ref.shape != rgba.shape:
        return None
    return np.any(ref != rgba, axis=2)


def infer_legacy_meta_edit_files(folder: str, meta: dict) -> dict:
    """Add edit metadata for older extract folders that lack v10 fields.

    This keeps folders such as existing Tu_000_out usable with the new _KOR
    convention, without treating already-present untracked reference PNGs as
    modified edits.
    """
    meta = dict(meta)
    if 'subimage_edit_files' not in meta:
        base = os.path.splitext(meta.get('source_xtx_name') or 'image')[0]
        items = []
        for slot_order, img in enumerate([x for x in meta.get('images', []) if x.get('valid')]):
            path = f"{base}_{slot_order + 1}.png"
            full = os.path.join(folder, path)
            if not os.path.exists(full):
                continue
            w, h = int(img['width']) * 2, int(img['height']) * 2
            x0, y0 = int(img['x0']) * 2, int(img['y0']) * 2
            try:
                mode = Image.open(full).mode
            except Exception:
                mode = 'L'
            items.append({
                'path': path,
                'sha256': file_sha256(full),
                'slot_order': int(slot_order),
                'image_index': int(img['index']),
                'rect': [x0, y0, x0 + w, y0 + h],
                'size': [w, h],
                'requires_lex': mode not in ('1', 'L', 'P', 'I;16', 'I'),
                'inferred_from_legacy_meta': True,
            })
        meta['subimage_edit_files'] = items
    if 'full_reference_files' not in meta:
        base = os.path.splitext(meta.get('source_xtx_name') or 'image')[0]
        refs = {}
        full_index = os.path.join(folder, f"{base}_full_index.png")
        if os.path.exists(full_index):
            size = Image.open(full_index).size
            refs['PSMT8_FULL_INDEX'] = {
                'path': os.path.basename(full_index),
                'sha256': file_sha256(full_index),
                'size': [int(size[0]), int(size[1])],
                'max_index': 255,
                'inferred_from_legacy_meta': True,
            }
        full_palette = os.path.join(folder, f"{base}_full_palette.png")
        if os.path.exists(full_palette):
            size = Image.open(full_palette).size
            refs['LEX_FULL_RGBA'] = {
                'path': os.path.basename(full_palette),
                'sha256': file_sha256(full_palette),
                'size': [int(size[0]), int(size[1])],
                'requires_lex': True,
                'inferred_from_legacy_meta': True,
            }
        meta['full_reference_files'] = refs
    return meta


def choose_full_atlas_edit_mode(folder: str, meta: dict) -> str | None:
    changed = []
    for mode in ('PSMT4', 'PSMT8', 'PSMT4_RGBA'):
        item = meta.get('edit_files', {}).get(mode)
        if not item:
            continue
        changed_item = edit_item_changed(folder, item)
        if changed_item:
            changed.append(mode)
    if not changed:
        return None
    if len(changed) > 1:
        raise ValueError(f"multiple full-atlas edit files changed in {folder}: {changed}; edit only one")
    return changed[0]


def changed_clean_edit_items(folder: str, meta: dict) -> list:
    changed = []
    for item in meta.get('clean_edit_files', []):
        changed_item = edit_item_changed(folder, item, 'clean edit file')
        if changed_item:
            changed.append(changed_item)
    return changed


def changed_full_atlas_modes(folder: str, meta: dict) -> list:
    return [mode for mode, item in changed_full_atlas_items(folder, meta)]


def changed_full_atlas_items(folder: str, meta: dict) -> list:
    changed = []
    for mode in ('PSMT4', 'PSMT8', 'PSMT4_RGBA'):
        item = meta.get('edit_files', {}).get(mode)
        if not item:
            continue
        changed_item = edit_item_changed(folder, item)
        if changed_item:
            changed.append((mode, changed_item))
    return changed


def changed_full_reference_items(folder: str, meta: dict) -> list:
    changed = []
    for mode, item in meta.get('full_reference_files', {}).items():
        changed_item = edit_item_changed(folder, item, 'full reference file')
        if changed_item:
            changed.append((mode, changed_item))
    return changed


def changed_subimage_edit_items(folder: str, meta: dict) -> list:
    changed = []
    for item in meta.get('subimage_edit_files', []):
        changed_item = edit_item_changed(folder, item, 'subimage edit file')
        if changed_item:
            changed.append(changed_item)
    return changed


def changed_meta_edit_groups(folder: str, meta: dict) -> dict:
    groups = {}
    clean = changed_clean_edit_items(folder, meta)
    if clean:
        groups['clean'] = clean
    subimages = changed_subimage_edit_items(folder, meta)
    if subimages:
        groups['subimage'] = subimages
    for mode, item in changed_full_atlas_items(folder, meta):
        groups[mode] = [item]
    for mode, item in changed_full_reference_items(folder, meta):
        groups[mode] = [item]
    return groups


def quantize_rgba_full_atlas(xtx_data: bytes, xtx_path: str, rgba: np.ndarray, lex_path: str,
                             lex_verbose: bool, pal_no_ps2_reorder: bool, lex_scan: bool,
                             palette_mode: str, palette_xtx_paths: list[str] | None,
                             auto_palette_sources: bool) -> np.ndarray:
    if not lex_path:
        raise ValueError('RGBA full/subimage import requires --lex so the tool can quantize by LEX palettes')
    images = parse_xtx_headers(xtx_data)
    configure_texture_dimensions(images)
    rgba_atlas = build_rgba_atlas(xtx_data, images)
    index_atlas = build_index_atlas(xtx_data, images)
    mats = parse_lex_materials(lex_path, lex_verbose, lex_scan)
    palette_sources = collect_palette_sources(xtx_path, rgba_atlas, palette_xtx_paths, auto_palette_sources)
    mat_map, palettes, valid_mats = build_material_maps(
        mats, rgba_atlas, not pal_no_ps2_reorder, index_atlas, palette_sources
    )
    if not palettes:
        raise ValueError('no usable LEX palettes found; cannot quantize RGBA edit image')
    resolved_mode, coverage = choose_palette_mode(palette_mode, mat_map, palettes)
    print(f"[META] LEX quantize mode: {resolved_mode} (material coverage {coverage*100:.1f}%)")
    if resolved_mode == 'global':
        return nearest_palette_indices(rgba, palettes[0])

    out = index_atlas.copy()
    for mi, pal in enumerate(palettes):
        mask = (mat_map == mi)
        if not np.any(mask):
            continue
        ys, xs = np.where(mask)
        y0, y1 = ys.min(), ys.max() + 1
        x0, x1 = xs.min(), xs.max() + 1
        submask = mask[y0:y1, x0:x1]
        idxs = nearest_palette_indices(rgba[y0:y1, x0:x1, :], pal)
        tmp = out[y0:y1, x0:x1]
        tmp[submask] = idxs[submask]
        out[y0:y1, x0:x1] = tmp
    return out


def rebuild_xtx_from_psmt8_index(xtx_data: bytes, images: list, index8: np.ndarray) -> bytes:
    modified = bytearray(xtx_data)
    for slot in images:
        if not slot.get('valid'):
            continue
        pdata = index_atlas_to_xtx_pdata(index8, slot)
        modified[slot['pstart']:slot['pend']] = pdata
    return bytes(modified)


def apply_meta_subimage_edits(xtx_data: bytes, xtx_path: str, folder: str, items: list, lex_path: str | None,
                              lex_verbose: bool, pal_no_ps2_reorder: bool, lex_scan: bool,
                              palette_mode: str, palette_xtx_paths: list[str] | None,
                              auto_palette_sources: bool) -> bytes:
    images = parse_xtx_headers(xtx_data)
    configure_texture_dimensions(images)
    index_atlas = build_index_atlas(xtx_data, images)
    lex_state = None

    print(f"[META] applying {len(items)} subimage edit file(s)")
    for item in items:
        path = edit_item_path(item, folder)
        expected_size = tuple(int(v) for v in item['size'])
        image = Image.open(path)
        if image.size != expected_size:
            raise ValueError(f"{path} size must be {expected_size}, got {image.size}")
        umin, vmin, umax, vmax = [int(v) for v in item['rect']]
        if item.get('requires_lex'):
            if lex_state is None:
                if not lex_path:
                    raise ValueError(f"{item['path']} is RGBA/LEX-based; pass --lex for import")
                rgba_atlas = build_rgba_atlas(xtx_data, images)
                base_index = build_index_atlas(xtx_data, images)
                mats = parse_lex_materials(lex_path, lex_verbose, lex_scan)
                palette_sources = collect_palette_sources(xtx_path, rgba_atlas, palette_xtx_paths, auto_palette_sources)
                mat_map, palettes, valid_mats = build_material_maps(
                    mats, rgba_atlas, not pal_no_ps2_reorder, base_index, palette_sources
                )
                if not palettes:
                    raise ValueError('no usable LEX palettes found; cannot quantize RGBA subimage edits')
                resolved_mode, coverage = choose_palette_mode(palette_mode, mat_map, palettes)
                print(f"[META] LEX quantize mode: {resolved_mode} (material coverage {coverage*100:.1f}%)")
                lex_state = (mat_map, palettes, resolved_mode)
            mat_map, palettes, resolved_mode = lex_state
            rgba = np.array(image.convert('RGBA'), dtype=np.uint8)
            pixel_changed = changed_rgba_mask(folder, item, rgba)
            if pixel_changed is not None and not np.any(pixel_changed):
                print(f"  [META] {os.path.basename(path)} (_KOR) has no pixel differences; keeping original indices")
                continue
            if resolved_mode == 'global':
                q = nearest_palette_indices(rgba, palettes[0])
                indices = index_atlas[vmin:vmax, umin:umax].copy()
                if pixel_changed is None:
                    indices[:, :] = q
                else:
                    indices[pixel_changed] = q[pixel_changed]
            else:
                local_map = mat_map[vmin:vmax, umin:umax]
                indices = index_atlas[vmin:vmax, umin:umax].copy()
                for mi, pal in enumerate(palettes):
                    mask = (local_map == mi)
                    if pixel_changed is not None:
                        mask = mask & pixel_changed
                    if np.any(mask):
                        q = nearest_palette_indices(rgba, pal)
                        indices[mask] = q[mask]
        else:
            indices = np.array(image.convert('L'), dtype=np.uint8)
        index_atlas[vmin:vmax, umin:umax] = indices
        suffix = ' (_KOR)' if item.get('_input_is_kor') else ''
        print(f"  [META] {os.path.basename(path)}{suffix} -> rect=({umin},{vmin})-({umax},{vmax})")
    return rebuild_xtx_from_psmt8_index(xtx_data, images, index_atlas)


def try_meta_driven_import(xtx_data: bytes, xtx_path: str, folder: str, lex_path: str | None,
                           lex_verbose: bool, pal_no_ps2_reorder: bool, lex_scan: bool,
                           palette_mode: str, palette_xtx_paths: list[str] | None,
                           auto_palette_sources: bool) -> bytes | None:
    meta_path = os.path.join(folder, 'xtx_meta.json')
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    meta = infer_legacy_meta_edit_files(folder, meta)
    if sha256(xtx_data) != meta.get('xtx_sha256'):
        raise ValueError('input XTX hash does not match xtx_meta.json; import from the original XTX used for extract')

    groups = changed_meta_edit_groups(folder, meta)
    has_new_meta = 'subimage_edit_files' in meta or 'full_reference_files' in meta
    if not groups:
        if has_new_meta:
            print('[META] no edit image changes detected; output will match input XTX')
            return bytes(xtx_data)
        return None
    if len(groups) > 1:
        raise ValueError(f"multiple edit pipelines changed in {folder}: {sorted(groups)}; edit only one pipeline")

    mode, items = next(iter(groups.items()))
    images = parse_xtx_headers(xtx_data)
    configure_texture_dimensions(images)
    print(f"[META] detected changed pipeline: {mode}")
    if mode == 'clean':
        return try_clean_edit_import(xtx_data, folder)
    if mode == 'subimage':
        return apply_meta_subimage_edits(
            xtx_data, xtx_path, folder, items, lex_path, lex_verbose,
            pal_no_ps2_reorder, lex_scan, palette_mode, palette_xtx_paths,
            auto_palette_sources,
        )
    if mode == 'PSMT8':
        item = items[0]
        size = tuple(int(x) for x in item['size'])
        index8 = read_index_png(edit_item_path(item, folder), size, 255)
        return rebuild_xtx_from_psmt8_index(xtx_data, images, index8)
    if mode == 'PSMT4':
        item = items[0]
        size = tuple(int(x) for x in item['size'])
        index4 = read_index_png(edit_item_path(item, folder), size, 15)
        return rebuild_xtx_from_psmt4(xtx_data, images, item['config'], index4)
    if mode == 'PSMT4_RGBA':
        item = items[0]
        size = tuple(int(x) for x in item['size'])
        path = edit_item_path(item, folder)
        image = Image.open(path).convert('RGBA')
        if image.size != size:
            raise ValueError(f"{path} size must be {size}, got {image.size}")
        rgba = np.array(image, dtype=np.uint8)
        palette = np.array(item['palette_rgba'], dtype=np.uint8)
        q = nearest_palette_indices(rgba, palette)
        pixel_changed = changed_rgba_mask(folder, item, rgba)
        if pixel_changed is not None:
            if not np.any(pixel_changed):
                print(f"[META] {os.path.basename(path)} (_KOR) has no pixel differences; keeping original indices")
                return bytes(xtx_data)
            base_index4 = decode_psmt4_png(xtx_data, images, item['config'])
            index4 = base_index4.copy()
            index4[pixel_changed] = q[pixel_changed]
        else:
            index4 = q
        return rebuild_xtx_from_psmt4(xtx_data, images, item['config'], index4)
    if mode == 'PSMT8_FULL_INDEX':
        item = items[0]
        size = tuple(int(x) for x in item['size'])
        index8 = read_index_png(edit_item_path(item, folder), size, 255)
        return rebuild_xtx_from_psmt8_index(xtx_data, images, index8)
    if mode == 'LEX_FULL_RGBA':
        item = items[0]
        size = tuple(int(x) for x in item['size'])
        path = edit_item_path(item, folder)
        image = Image.open(path).convert('RGBA')
        if image.size != size:
            raise ValueError(f"{path} size must be {size}, got {image.size}")
        q = quantize_rgba_full_atlas(
            xtx_data, xtx_path, np.array(image, dtype=np.uint8), lex_path,
            lex_verbose, pal_no_ps2_reorder, lex_scan, palette_mode,
            palette_xtx_paths, auto_palette_sources,
        )
        rgba = np.array(image, dtype=np.uint8)
        pixel_changed = changed_rgba_mask(folder, item, rgba)
        if pixel_changed is not None:
            if not np.any(pixel_changed):
                print(f"[META] {os.path.basename(path)} (_KOR) has no pixel differences; keeping original indices")
                return bytes(xtx_data)
            index8 = build_index_atlas(xtx_data, images)
            index8[pixel_changed] = q[pixel_changed]
        else:
            index8 = q
        return rebuild_xtx_from_psmt8_index(xtx_data, images, index8)
    raise ValueError(f"unsupported changed edit pipeline: {mode}")


def try_clean_edit_import(xtx_data: bytes, folder: str) -> bytes | None:
    meta_path = os.path.join(folder, 'xtx_meta.json')
    if not os.path.exists(meta_path):
        return None

    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    if not meta.get('clean_edit_files'):
        return None

    if sha256(xtx_data) != meta.get('xtx_sha256'):
        raise ValueError('input XTX hash does not match xtx_meta.json; import from the original XTX used for extract')

    clean_changed = changed_clean_edit_items(folder, meta)
    full_changed = changed_full_atlas_modes(folder, meta)
    if not clean_changed:
        if full_changed:
            return None
        print('[CLEAN] no clean edit changes detected; output will match input XTX')
        return bytes(xtx_data)

    if full_changed:
        raise ValueError(f"clean edit PNGs and {full_changed} changed together; edit only one pipeline")

    images = parse_xtx_headers(xtx_data)
    configure_texture_dimensions(images)
    index_atlas = build_index_atlas(xtx_data, images)

    print(f"[CLEAN] applying {len(clean_changed)} clean edit image(s)")
    for item in clean_changed:
        path = edit_item_path(item, folder)
        image = Image.open(path).convert('RGBA')
        expected_size = tuple(int(v) for v in item['size'])
        if image.size != expected_size:
            raise ValueError(f"{path} size must be {expected_size}, got {image.size}")
        rgba = np.array(image, dtype=np.uint8)
        pixel_changed = changed_rgba_mask(folder, item, rgba)
        if pixel_changed is not None and not np.any(pixel_changed):
            print(f"  [CLEAN] {os.path.basename(path)} (_KOR) has no pixel differences; keeping original indices")
            continue
        palette = np.array(item['palette_rgba'], dtype=np.uint8)
        umin, vmin, umax, vmax = [int(v) for v in item['rect']]
        indices = index_atlas[vmin:vmax, umin:umax].copy()
        q = nearest_palette_indices(rgba, palette)
        if pixel_changed is None:
            indices[:, :] = q
        else:
            indices[pixel_changed] = q[pixel_changed]
        index_atlas[vmin:vmax, umin:umax] = indices
        suffix = ' (_KOR)' if item.get('_input_is_kor') else ''
        print(f"  [CLEAN] {os.path.basename(path)}{suffix} -> rect=({umin},{vmin})-({umax},{vmax})")

    modified = bytearray(xtx_data)
    for slot in images:
        if not slot.get('valid'):
            continue
        pdata = index_atlas_to_xtx_pdata(index_atlas, slot)
        modified[slot['pstart']:slot['pend']] = pdata
    return bytes(modified)


def try_full_atlas_import(xtx_data: bytes, xtx_path: str, folder: str) -> bytes | None:
    meta_path = os.path.join(folder, 'xtx_meta.json')
    if not os.path.exists(meta_path):
        return None

    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)

    if sha256(xtx_data) != meta.get('xtx_sha256'):
        raise ValueError('input XTX hash does not match xtx_meta.json; import from the original XTX used for extract')

    images = parse_xtx_headers(xtx_data)
    configure_texture_dimensions(images)
    mode = choose_full_atlas_edit_mode(folder, meta)
    if mode is None:
        print('[FULL] no PSMT4/PSMT8 changes detected; output will match input XTX')
        return bytes(xtx_data)

    print(f'[FULL] applying {mode}.png full-atlas edit')
    if mode == 'PSMT8':
        item = meta['edit_files']['PSMT8']
        size = tuple(int(x) for x in item['size'])
        index8 = read_index_png(edit_item_path(item, folder), size, 255)
        modified = bytearray(xtx_data)
        for slot in images:
            if not slot.get('valid'):
                continue
            pdata = index_atlas_to_xtx_pdata(index8, slot)
            modified[slot['pstart']:slot['pend']] = pdata
        return bytes(modified)

    if mode == 'PSMT4':
        item = meta['edit_files']['PSMT4']
        size = tuple(int(x) for x in item['size'])
        index4 = read_index_png(edit_item_path(item, folder), size, 15)
        return rebuild_xtx_from_psmt4(xtx_data, images, item['config'], index4)

    item = meta['edit_files']['PSMT4_RGBA']
    size = tuple(int(x) for x in item['size'])
    path = edit_item_path(item, folder)
    image = Image.open(path).convert('RGBA')
    if image.size != size:
        raise ValueError(f"{path} size must be {size}, got {image.size}")
    rgba = np.array(image, dtype=np.uint8)
    palette = np.array(item['palette_rgba'], dtype=np.uint8)
    index4 = nearest_palette_indices(rgba, palette)
    return rebuild_xtx_from_psmt4(xtx_data, images, item['config'], index4)


def cmd_import(xtx_path: str, folder: str, out_path: str, fix_alpha: bool = False, lex_path: str = None,
               lex_verbose: bool = False, pal_no_ps2_reorder: bool = False, lex_scan: bool = False, palette_mode: str = 'auto',
               palette_xtx_paths: list[str] | None = None, auto_palette_sources: bool = True):
    data   = open(xtx_path, 'rb').read()
    magic = texture_magic(data)
    is_arx = magic == 'ARX'

    if is_arx:
        print(f"[ARX] decompressing {xtx_path} ...")
        xtx_data = decompress_arx(data)
        if xtx_data[0:4] != b'XTX\x00':
            print("ERROR: ARX payload is not XTX"); return
    elif magic == 'XTX':
        xtx_data = data
    else:
        print(f"ERROR: unsupported file magic {data[0:4]!r}; expected XTX\\0 or ARX\\0")
        return

    meta_edit_out = try_meta_driven_import(
        xtx_data, xtx_path, folder, lex_path, lex_verbose, pal_no_ps2_reorder,
        lex_scan, palette_mode, palette_xtx_paths, auto_palette_sources,
    )
    if meta_edit_out is not None:
        if is_arx:
            print("[ARX] WARNING: ARX re-compression not yet supported. Saving as raw XTX.")
        open(out_path, 'wb').write(meta_edit_out)
        print(f"\nSaved -> {out_path}")
        return

    clean_edit_out = try_clean_edit_import(xtx_data, folder)
    if clean_edit_out is not None:
        if is_arx:
            print("[ARX] WARNING: ARX re-compression not yet supported. Saving as raw XTX.")
        open(out_path, 'wb').write(clean_edit_out)
        print(f"\nSaved -> {out_path}")
        return

    full_atlas_out = try_full_atlas_import(xtx_data, xtx_path, folder)
    if full_atlas_out is not None:
        if is_arx:
            print("[ARX] WARNING: ARX re-compression not yet supported. Saving as raw XTX.")
        open(out_path, 'wb').write(full_atlas_out)
        print(f"\nSaved -> {out_path}")
        return

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
        palette_sources = collect_palette_sources(xtx_path, rgba_atlas, palette_xtx_paths, auto_palette_sources)
        if len(palette_sources) > 1:
            names = ', '.join(src['name'] for src in palette_sources[:12])
            if len(palette_sources) > 12:
                names += f", ... +{len(palette_sources)-12}"
            print(f"[LEX] palette source candidates: {names}")
        mat_map, palettes, valid_mats = build_material_maps(
            mats, rgba_atlas, not pal_no_ps2_reorder, index_atlas, palette_sources
        )
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

    p_ex = sub.add_parser('extract', help='XTX/ARX -> PNG(s); with --lex, colorized by material palettes')
    p_ex.add_argument('xtx', help='XTX/ARX file path; extension is ignored, magic is used')
    p_ex.add_argument('--out', default=None, help='output directory')
    p_ex.add_argument('--fix-alpha', action='store_true', help='also save raw RGBA atlas reference PNGs')
    p_ex.add_argument('--lex', default=None, help='paired .lex file; enables colorized material-palette extract')
    p_ex.add_argument('--lex-verbose', action='store_true', help='print parsed material palette regions')
    p_ex.add_argument('--pal-no-ps2-reorder', action='store_true', help='disable PS2 8bpp CLUT 8/16 swap reorder')
    p_ex.add_argument('--save-full', action='store_true', help='also save full color/index atlases')
    p_ex.add_argument('--edit-only', action='store_true', help='with --lex, extract only clean edit_*.png material images')
    p_ex.add_argument('--palette-xtx', action='append', default=[], help='extra XTX/BIN file to use as a LEX CLUT source')
    p_ex.add_argument('--no-auto-palette-sources', action='store_true', help='do not scan sibling XTX/BIN files as LEX CLUT sources')
    p_ex.add_argument('--lex-scan', action='store_true', help='experimental: scan LEX for extra material blocks; may produce false positives')
    p_ex.add_argument('--palette-mode', choices=['auto','material','global'], default='auto', help='how to apply LEX palette: auto/global/material')

    p_im = sub.add_parser('import', help='edited PNG(s) -> XTX/ARX payload; with --lex, RGB/RGBA is quantized to material palettes')
    p_im.add_argument('xtx', help='original XTX/ARX file path; extension is ignored, magic is used')
    p_im.add_argument('folder', help='folder with edited <base>_1.png, _2.png ...')
    p_im.add_argument('--out', default=None, help='output path; defaults to preserving input extension')
    p_im.add_argument('--fix-alpha', action='store_true', help='reserved')
    p_im.add_argument('--lex', default=None, help='paired .lex file; enables color image import')
    p_im.add_argument('--lex-verbose', action='store_true', help='print parsed material palette regions')
    p_im.add_argument('--pal-no-ps2-reorder', action='store_true', help='disable PS2 8bpp CLUT 8/16 swap reorder')
    p_im.add_argument('--palette-xtx', action='append', default=[], help='extra XTX/BIN file to use as a LEX CLUT source')
    p_im.add_argument('--no-auto-palette-sources', action='store_true', help='do not scan sibling XTX/BIN files as LEX CLUT sources')
    p_im.add_argument('--lex-scan', action='store_true', help='experimental: scan LEX for extra material blocks; may produce false positives')
    p_im.add_argument('--palette-mode', choices=['auto','material','global'], default='auto', help='how to quantize color PNGs: auto/global/material')

    args = parser.parse_args()

    if args.cmd == 'extract':
        out_dir = args.out or (os.path.splitext(args.xtx)[0] + '_extracted')
        cmd_extract(
            args.xtx, out_dir, args.fix_alpha, args.lex, args.lex_verbose,
            args.pal_no_ps2_reorder, args.save_full, args.lex_scan,
            args.palette_mode, args.edit_only, args.palette_xtx,
            not args.no_auto_palette_sources,
        )
    elif args.cmd == 'import':
        out_path = args.out or imported_default_path(args.xtx)
        cmd_import(
            args.xtx, args.folder, out_path, args.fix_alpha, args.lex,
            args.lex_verbose, args.pal_no_ps2_reorder, args.lex_scan,
            args.palette_mode, args.palette_xtx, not args.no_auto_palette_sources,
        )


if __name__ == '__main__':
    main()
