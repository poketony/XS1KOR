#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


BLOCK32 = [
    0, 1, 4, 5, 16, 17, 20, 21,
    2, 3, 6, 7, 18, 19, 22, 23,
    8, 9, 12, 13, 24, 25, 28, 29,
    10, 11, 14, 15, 26, 27, 30, 31,
]
COLUMN_WORD32 = [
    0, 1, 4, 5, 8, 9, 12, 13,
    2, 3, 6, 7, 10, 11, 14, 15,
]
BLOCK4 = [
    0, 2, 8, 10,
    1, 3, 9, 11,
    4, 6, 12, 14,
    5, 7, 13, 15,
    16, 18, 24, 26,
    17, 19, 25, 27,
    20, 22, 28, 30,
    21, 23, 29, 31,
]
COLUMN_WORD4 = [
    [
        0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
        0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
        2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15,
        2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15,
        8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
        8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
        10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7,
        10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7,
    ],
    [
        8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
        8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
        10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7,
        10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7,
        0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
        0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
        2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15,
        2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15,
    ],
]
COLUMN_BYTE4 = [
    0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2,
    4, 4, 4, 4, 4, 4, 4, 4, 6, 6, 6, 6, 6, 6, 6, 6,
    0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2,
    4, 4, 4, 4, 4, 4, 4, 4, 6, 6, 6, 6, 6, 6, 6, 6,
    1, 1, 1, 1, 1, 1, 1, 1, 3, 3, 3, 3, 3, 3, 3, 3,
    5, 5, 5, 5, 5, 5, 5, 5, 7, 7, 7, 7, 7, 7, 7, 7,
    1, 1, 1, 1, 1, 1, 1, 1, 3, 3, 3, 3, 3, 3, 3, 3,
    5, 5, 5, 5, 5, 5, 5, 5, 7, 7, 7, 7, 7, 7, 7, 7,
]


@dataclass
class NbxxEntry:
    index: int
    name: str
    offset: int
    end: int
    size: int


def u32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 4], "little")


def zstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def parse_nbxx(data: bytes) -> tuple[dict, list[NbxxEntry]]:
    if data[:4] != b"NBXX":
        raise ValueError(f"not NBXX: {data[:4]!r}")
    name_size = u32(data, 4)
    off_table = u32(data, 8)
    count = u32(data, 20)
    offsets = [u32(data, off_table + i * 4) for i in range(count)]
    ends = offsets[1:] + [len(data)]
    entries = []
    for i, (off, end) in enumerate(zip(offsets, ends)):
        name = zstr(data[0x20 + i * name_size : 0x20 + (i + 1) * name_size])
        entries.append(NbxxEntry(i, name, off, end, end - off))
    return {"name_size": name_size, "offset_table": off_table, "count": count}, entries


def ct32_pos(x: int, y: int, dbw: int) -> int:
    page = ((y >> 5) * (dbw >> 6)) + (x >> 6)
    px, py = x & 63, y & 31
    block = BLOCK32[(px >> 3) + (py >> 3) * 8]
    bx, by = px & 7, py & 7
    col = by >> 1
    cw = COLUMN_WORD32[bx + (by & 1) * 8]
    return page * 2048 + block * 64 + col * 16 + cw


def psmt4_pos(x: int, y: int, dbp: int, dbw: int) -> tuple[int, int]:
    dbw2 = dbw >> 1
    start = dbp * 64
    page = ((y >> 7) * ((dbw2 + 127) >> 7)) + (x >> 7)
    px, py = x & 127, y & 127
    block = BLOCK4[(px >> 5) + (py >> 4) * 4]
    bx, by = px & 31, py & 15
    col = by >> 2
    cx, cy = bx, by & 3
    cw = COLUMN_WORD4[col & 1][cx + cy * 32]
    cb = COLUMN_BYTE4[cx + cy * 32]
    return start + page * 2048 + block * 64 + col * 16 + cw, cb


def build_gsmem_words(xtx_data: bytes, images: list[dict], dbw32: int) -> np.ndarray:
    gsmem = np.zeros(1024 * 1024, dtype=np.uint32)
    for img in images:
        if not img["valid"]:
            continue
        w, h, x0, y0 = img["width"], img["height"], img["x0"], img["y0"]
        payload = xtx_data[img["pstart"] : img["pend"]]
        words = np.frombuffer(payload, dtype="<u4").reshape(h, w)
        for y in range(h):
            for x in range(w):
                pos = ct32_pos(x0 + x, y0 + y, dbw32)
                gsmem[pos] = words[y, x]
    return gsmem


def decode_psmt4(gsmem: np.ndarray, width: int, height: int, dbp: int, dbw4: int) -> np.ndarray:
    out = np.zeros((height, width), dtype=np.uint8)
    for y in range(height):
        for x in range(width):
            pos, cb = psmt4_pos(x, y, dbp, dbw4)
            word = int(gsmem[pos]) if 0 <= pos < len(gsmem) else 0
            byte = (word >> ((cb >> 1) * 8)) & 0xFF
            out[y, x] = ((byte >> 4) & 0xF) if (cb & 1) else (byte & 0xF)
    return out
