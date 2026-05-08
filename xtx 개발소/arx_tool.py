#!/usr/bin/env python3
"""
arx_tool.py - Xenosaga Episode I ARX 압축해제/재압축 툴

Usage:
  python arx_tool.py decompress <input.arx>  [--out <output>]
  python arx_tool.py compress   <input>       [--out <output.arx>]
  python arx_tool.py roundtrip  <input.arx>   # 라운드트립 검증

ARX 포맷:
  Header (136 bytes):
    [0x00] magic     char[4]  = "ARX\x00"
    [0x04] size_orig uint32   = 압축 해제 후 크기 (u32 정렬)
    [0x08] size_comp uint32   = 압축 데이터 크기
    [0x0C] unk0      uint32   = 0
    [0x10] lut[30]   uint32*30 = 빈도 상위 30개 u32 값
  Body (offset 136~):
    비트스트림 제어와 raw u32가 인터리브된 스트림
"""

import sys
import os
import struct
from collections import Counter


# ---------------------------------------------------------------------------
# 압축 해제 (C 소스 xeno_arx.c 기반)
# ---------------------------------------------------------------------------

def decompress_arx(data: bytes) -> bytes:
    """ARX 압축 해제. 반환값: 압축 해제된 원본 bytes."""
    if data[:4] != b'ARX\x00':
        raise ValueError(f"ARX magic 불일치: {data[:4]!r}")

    size_orig = struct.unpack_from('<I', data, 4)[0]
    lut       = list(struct.unpack_from('<30I', data, 16))

    out     = bytearray(size_orig)
    out_pos = 0
    fp_pos  = 136           # 헤더 다음부터
    buf     = 0
    buf_len = 0
    top     = 1 << 63

    # 상태: 0=DATA, 1=MARKER, 2=LUT
    s = 0
    lut_val = lut_idx = lut_len = 0

    def read_u32():
        nonlocal fp_pos
        if fp_pos + 4 > len(data):
            return None
        v = struct.unpack_from('<I', data, fp_pos)[0]
        fp_pos += 4
        return v

    while True:
        val = read_u32()
        if val is None:
            break
        buf |= (val << (32 - buf_len)) & 0xFFFFFFFFFFFFFFFF
        buf_len += 32

        while buf_len:
            bit = (buf & top) >> 63

            if s == 0:  # DATA
                if bit:
                    s = 1   # → MARKER (bit 소비 안 함, MARKER에서 소비)
                else:
                    v = read_u32()
                    if v is None:
                        buf_len = 0
                        break
                    if out_pos + 4 <= len(out):
                        struct.pack_into('<I', out, out_pos, v)
                    out_pos += 4
                    buf = (buf << 1) & 0xFFFFFFFFFFFFFFFF
                    buf_len -= 1

            elif s == 1:  # MARKER
                lut_val = lut_idx = lut_len = 0
                s = 2
                buf = (buf << 1) & 0xFFFFFFFFFFFFFFFF
                buf_len -= 1

            elif s == 2:  # LUT
                lut_val = ((lut_val << 1) | bit) & 0xFF
                if lut_idx == 0: lut_len = 4 if bit else 2
                if lut_idx == 1 and lut_len == 4 and bit: lut_len = 6
                if lut_idx == 2 and lut_len == 6 and bit: lut_len = 8
                lut_idx += 1

                if lut_idx == lut_len:
                    s = 0
                    if   lut_len == 2: idx = lut_val
                    elif lut_len == 4: idx = 2  + (lut_val & 0x7)
                    elif lut_len == 6: idx = 6  + (lut_val & 0xF)
                    else:              idx = 14 + (lut_val & 0x1F)
                    v = lut[idx] if idx < len(lut) else 0
                    if out_pos + 4 <= len(out):
                        struct.pack_into('<I', out, out_pos, v)
                    out_pos += 4

                buf = (buf << 1) & 0xFFFFFFFFFFFFFFFF
                buf_len -= 1

    return bytes(out)


# ---------------------------------------------------------------------------
# 재압축
# ---------------------------------------------------------------------------

def _lut_idx_to_bits(idx: int) -> list:
    """
    LUT 인덱스 → 비트 시퀀스 (DATA bit=1 포함).

    해제기 동작:
      DATA  bit=1 → MARKER (shift 없음)
      MARKER       → 이 bit(=1)을 소비하고 LUT로 전환
      LUT          → 가변 비트로 인덱스 결정

    따라서 전체 비트 = [1(DATA)] + [LUT bits (MARKER가 1 소비)]
    """
    if idx <= 1:
        # lut_len=2: LUT[0]=0(→len=2), LUT[1]=idx
        return [1, 0, idx]
    elif idx <= 5:
        val = idx - 2  # 0~3
        # LUT[0]=1(→len=4), LUT[1]=0(→stay4), LUT[2..3]=val(2비트)
        return [1, 1, 0, (val >> 1) & 1, val & 1]
    elif idx <= 13:
        val = idx - 6  # 0~7
        # LUT[0]=1, LUT[1]=1(→len=6), LUT[2]=0(→stay6), LUT[3..5]=val(3비트)
        return [1, 1, 1, 0, (val >> 2) & 1, (val >> 1) & 1, val & 1]
    else:
        val = idx - 14  # 0~15
        # LUT[0]=1, LUT[1]=1, LUT[2]=1(→len=8), LUT[3]=0, LUT[4..7]=val(4비트)
        return [1, 1, 1, 1, 0, (val >> 3) & 1, (val >> 2) & 1, (val >> 1) & 1, val & 1]


def compress_arx(data: bytes) -> bytes:
    """
    ARX 압축.

    인터리브 규칙:
      해제기는 ctrl u32와 raw u32를 같은 fp에서 읽음.
      ctrl u32의 bit=0 처리 시 즉시 fp에서 raw u32를 읽음.
      → raw word는 해당 bit=0이 속한 ctrl word 바로 뒤에 배치.
      → flush 시 [ctrl_u32][raw0][raw1]... 순서로 출력.
      → raw를 ctrl_bits에 bit 추가 전에 ctrl_raws에 등록해야 함.
    """
    if len(data) % 4 != 0:
        raise ValueError(f"데이터 크기가 4바이트 정렬되지 않음: {len(data)}")

    words = [struct.unpack_from('<I', data, i)[0] for i in range(0, len(data), 4)]

    # LUT: 빈도 상위 30개
    cnt = Counter(words)
    lut = [v for v, _ in cnt.most_common(30)]
    while len(lut) < 30:
        lut.append(0)
    lut_map = {v: i for i, v in enumerate(lut)}

    result    = []
    ctrl_bits = []  # 현재 ctrl word 비트 누적
    ctrl_raws = []  # 이 ctrl word에 딸린 raw u32들

    def flush():
        bits = ctrl_bits[:32]
        while len(bits) < 32:
            bits.append(0)
        w = 0
        for b in bits:
            w = (w << 1) | b
        result.append(struct.pack('<I', w))
        for r in ctrl_raws:
            result.append(struct.pack('<I', r))
        ctrl_bits.clear()
        ctrl_raws.clear()

    for word in words:
        if word in lut_map:
            bits = _lut_idx_to_bits(lut_map[word])
            raw  = None
        else:
            bits = [0]
            raw  = word

        # raw는 bit 추가 전에 등록 (bit=0이 ctrl word 경계에 걸려도 올바른 위치 보장)
        if raw is not None:
            ctrl_raws.append(raw)

        for bit in bits:
            ctrl_bits.append(bit)
            if len(ctrl_bits) == 32:
                flush()

    if ctrl_bits or ctrl_raws:
        flush()

    body = b''.join(result)
    header = (
        b'ARX\x00'
        + struct.pack('<I', len(data))   # size_orig
        + struct.pack('<I', len(body))   # size_comp
        + struct.pack('<I', 0)           # unk0
        + struct.pack('<30I', *lut)
    )
    return header + body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_decompress(src: str, dst: str):
    data = open(src, 'rb').read()
    out  = decompress_arx(data)
    open(dst, 'wb').write(out)
    ratio = len(data) / len(out) * 100 if out else 0
    print(f"압축 해제: {src}")
    print(f"  {len(data):,} bytes → {len(out):,} bytes  (압축률 {ratio:.1f}%)")
    print(f"  출력: {dst}")


def cmd_compress(src: str, dst: str):
    data = open(src, 'rb').read()
    # 4바이트 정렬
    if len(data) % 4 != 0:
        data += b'\x00' * (4 - len(data) % 4)
    out  = compress_arx(data)
    open(dst, 'wb').write(out)
    ratio = len(out) / len(data) * 100 if data else 0
    print(f"압축: {src}")
    print(f"  {len(data):,} bytes → {len(out):,} bytes  (압축률 {ratio:.1f}%)")
    print(f"  출력: {dst}")


def cmd_roundtrip(src: str):
    data   = open(src, 'rb').read()
    decomp = decompress_arx(data)
    recomp = compress_arx(decomp)
    decomp2 = decompress_arx(recomp)
    match = decomp == decomp2
    print(f"라운드트립 검증: {src}")
    print(f"  원본 압축 크기    : {len(data):,} bytes")
    print(f"  압축 해제 크기    : {len(decomp):,} bytes")
    print(f"  재압축 크기       : {len(recomp):,} bytes")
    print(f"  결과              : {'✓ 일치' if match else '✗ 불일치'}")
    if not match:
        diffs = [(i*4, decomp[i*4:i*4+4].hex(), decomp2[i*4:i*4+4].hex())
                 for i in range(min(len(decomp), len(decomp2)) // 4)
                 if decomp[i*4:i*4+4] != decomp2[i*4:i*4+4]]
        print(f"  차이 위치 (처음 5개): {[(hex(a), b, c) for a, b, c in diffs[:5]]}")
    return match


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    src = sys.argv[2]

    if cmd == 'decompress':
        dst = sys.argv[4] if len(sys.argv) >= 5 and sys.argv[3] == '--out' \
              else os.path.splitext(src)[0] + '.bin'
        cmd_decompress(src, dst)

    elif cmd == 'compress':
        dst = sys.argv[4] if len(sys.argv) >= 5 and sys.argv[3] == '--out' \
              else src + '.arx'
        cmd_compress(src, dst)

    elif cmd == 'roundtrip':
        ok = cmd_roundtrip(src)
        sys.exit(0 if ok else 1)

    else:
        print(f"알 수 없는 명령: {cmd}")
        print("사용법: decompress / compress / roundtrip")
        sys.exit(1)


if __name__ == '__main__':
    main()
