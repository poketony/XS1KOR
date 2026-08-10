"""
xenoarc.py — Xenosaga Episode I (PS2) archive library.

아카이브 구조 요약 (reverse-engineered from SLPS_290.02):
- TOC 파일(xenosaga.10, .20)과 데이터 청크(.11/.12/.13, .21/.22/.23/.24)가 한 세트.
- 게임은 DVD의 raw 섹터로 접근하므로, TOC파일+청크들을 순서대로 이어붙이면
  LBA * 0x800 = 가상 연결 파일 내 바이트 오프셋이 성립한다.
- TOC는 prefix-trie 인코딩 + 24비트 LBA + 32비트 size.

엔트리 형식
-----------
cmd = 1바이트:
  bit 7 = 1 → 디렉터리 push 명령
  bit 6 = 1 → 이 파일은 layer2 alt LBA를 추가로 가짐 (듀얼레이어)
  bits 0-5 = name_len

디렉터리 push (bit7=1):
  cmd | back_byte | name[name_len - 2]
  파서 스택: iVar14 = (iVar14 - back) + 1, apcStack_40[iVar14] = end+1
    back=0  → 서브디렉터리로 진입
    back=1  → 형제 디렉터리
    back=N  → N-1 단계 pop 후 push

파일 (bit7=0):
  cmd | name[name_len - 1] | lba(u24 LE) | size(u32 LE) | [alt_lba(u24 LE) if bit6]

스트림 끝 = cmd == 0x00
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator


SECTOR = 0x800


@dataclass
class Entry:
    path: str            # '/' 구분, 앞에 슬래시 없음. 예: 'sound/vda/s195001.vds'
    lba: int             # 가상 연결 파일에서의 섹터 오프셋
    size: int            # 바이트
    alt_lba: int | None  # bit 0x40 플래그가 있었던 경우 layer2 alt LBA

    @property
    def byte_offset(self) -> int:
        return self.lba * SECTOR


@dataclass
class ArchiveSet:
    """한 세트(TOC + 데이터 청크들)."""
    toc_path: Path
    chunk_paths: list[Path]           # TOC 뒤로 이어지는 청크들, 순서 중요
    header_sectors: int               # TOC 첫 바이트의 N값
    entries: list[Entry]

    @property
    def virtual_size(self) -> int:
        return self.toc_path.stat().st_size + sum(p.stat().st_size for p in self.chunk_paths)


# ---------------------------------------------------------------------------
# TOC 파서
# ---------------------------------------------------------------------------

def _u24(buf: bytes, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16)


def _u32(buf: bytes, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


def parse_toc(toc_bytes: bytes) -> list[Entry]:
    """
    TOC 바이너리 스트림을 파싱해 모든 파일 엔트리를 반환.

    SLPS_290.02의 xglCdGetFilePosSub을 그대로 역추적한 구현이다.
    """
    # 0번 바이트는 헤더 섹터 카운트(N)이고, 실제 트리 스트림은 1번 바이트부터 시작한다.
    p = 1
    end = len(toc_bytes)

    # 디렉터리 이름 스택. 문자열 리스트로 유지하면 경로 재구성이 간단하다.
    dir_stack: list[str] = []
    # iVar14 역할: "현재 깊이 인덱스". dir_stack의 길이와 일치.

    results: list[Entry] = []

    while p < end:
        cmd = toc_bytes[p]
        if cmd == 0:
            break  # 스트림 종료

        layer2flag = (cmd & 0x40) != 0
        name_len = cmd & 0x3F

        if cmd & 0x80:
            # 디렉터리 push 명령
            if p + 1 >= end:
                raise ValueError(f"TOC truncated at dir cmd @ 0x{p:x}")
            back = toc_bytes[p + 1]
            raw_name_len = name_len - 2  # cmd 와 back 바이트를 제외
            if raw_name_len < 0:
                raise ValueError(f"Bad dir name_len {name_len} @ 0x{p:x}")
            name_start = p + 2
            name_end = name_start + raw_name_len
            if name_end > end:
                raise ValueError(f"TOC truncated in dir name @ 0x{p:x}")
            name = toc_bytes[name_start:name_end].decode("ascii", errors="replace")

            # 스택 조작: pop (back - 1)번 한 뒤 push 1회
            # iVar14 new = iVar14 - back + 1 → 새 depth 값
            # back == 0 → pop 0, 그대로 push → 깊이 +1 (서브디렉터리)
            # back == 1 → pop 1, push → 같은 깊이 (형제)
            # back == N → pop N-1, push → 위로 N-1단 올라가 sibling
            new_depth = len(dir_stack) - back + 1
            if new_depth < 1:
                raise ValueError(
                    f"Dir stack underflow @ 0x{p:x} (depth={len(dir_stack)}, back={back})"
                )
            del dir_stack[new_depth - 1 :]
            dir_stack.append(name)

            p = name_end
            continue

        # 파일 엔트리
        raw_name_len = name_len - 1
        if raw_name_len <= 0:
            raise ValueError(f"Bad file name_len {name_len} @ 0x{p:x}")
        name_start = p + 1
        name_end = name_start + raw_name_len
        meta_start = name_end
        meta_end = meta_start + (10 if layer2flag else 7)
        if meta_end > end:
            raise ValueError(f"TOC truncated in file meta @ 0x{p:x}")

        name = toc_bytes[name_start:name_end].decode("ascii", errors="replace")
        lba = _u24(toc_bytes, meta_start)
        size = _u32(toc_bytes, meta_start + 3)
        alt = _u24(toc_bytes, meta_start + 7) if layer2flag else None

        full_path = "/".join([*dir_stack, name]) if dir_stack else name
        results.append(Entry(path=full_path, lba=lba, size=size, alt_lba=alt))

        p = meta_end

    return results


# ---------------------------------------------------------------------------
# 가상 연결 파일 리더
# ---------------------------------------------------------------------------

class VirtualChain(BinaryIO):
    """
    TOC 파일 + 데이터 청크들을 하나의 연속 바이너리처럼 읽게 해주는 래퍼.
    seek/read 만 지원. 대용량 파일 고려해서 메모리에 전체를 올리지 않는다.
    """

    def __init__(self, files: list[Path]):
        self._files = files
        self._sizes = [p.stat().st_size for p in files]
        self._offsets: list[int] = []
        running = 0
        for s in self._sizes:
            self._offsets.append(running)
            running += s
        self._total = running
        self._pos = 0
        self._fps: list[BinaryIO | None] = [None] * len(files)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for fp in self._fps:
            if fp is not None:
                fp.close()
        self._fps = [None] * len(self._files)

    @property
    def total_size(self) -> int:
        return self._total

    def _get_fp(self, i: int) -> BinaryIO:
        fp = self._fps[i]
        if fp is None:
            fp = open(self._files[i], "rb")
            self._fps[i] = fp
        return fp

    def seek(self, offset: int, whence: int = 0) -> int:  # type: ignore[override]
        if whence == 0:
            new = offset
        elif whence == 1:
            new = self._pos + offset
        elif whence == 2:
            new = self._total + offset
        else:
            raise ValueError(f"bad whence {whence}")
        if new < 0:
            raise ValueError("negative seek")
        self._pos = new
        return new

    def tell(self) -> int:  # type: ignore[override]
        return self._pos

    def read(self, n: int = -1) -> bytes:  # type: ignore[override]
        if n < 0:
            n = self._total - self._pos
        if n <= 0 or self._pos >= self._total:
            return b""
        out = bytearray()
        remaining = min(n, self._total - self._pos)
        while remaining > 0:
            i = self._which_file(self._pos)
            local = self._pos - self._offsets[i]
            take = min(remaining, self._sizes[i] - local)
            fp = self._get_fp(i)
            fp.seek(local)
            chunk = fp.read(take)
            if not chunk:
                break
            out.extend(chunk)
            self._pos += len(chunk)
            remaining -= len(chunk)
        return bytes(out)

    def _which_file(self, pos: int) -> int:
        # 청크 개수가 적으니 선형 탐색
        for i in range(len(self._files) - 1, -1, -1):
            if pos >= self._offsets[i]:
                return i
        return 0


# ---------------------------------------------------------------------------
# 세트 열기 / 청크 발견
# ---------------------------------------------------------------------------

def discover_set(toc_path: Path) -> ArchiveSet:
    """
    toc_path 가 xenosaga.10 (혹은 .20) 같은 TOC 파일이라고 가정.
    같은 prefix 를 공유하는 청크들을 자동 발견한다.

    예) XENOSAGA.10 → [XENOSAGA.11, XENOSAGA.12, XENOSAGA.13]
        XENOSAGA.20 → [XENOSAGA.21, XENOSAGA.22, XENOSAGA.23, XENOSAGA.24]
    """
    toc_path = Path(toc_path)
    stem = toc_path.stem            # "XENOSAGA"
    suffix = toc_path.suffix        # ".10"
    if len(suffix) != 3 or not suffix.startswith(".") or not suffix[1:].isdigit():
        raise ValueError(f"Unexpected TOC suffix: {suffix!r}")

    # 앞자리(1,2,...)를 고정하고 뒷자리를 1,2,3,... 로 증가시키며 탐색
    group_digit = suffix[1]         # "1", "2"
    chunks: list[Path] = []
    for sub in range(1, 10):
        candidate = toc_path.with_suffix(f".{group_digit}{sub}")
        if candidate.exists():
            chunks.append(candidate)
        else:
            break
    if not chunks:
        raise FileNotFoundError(
            f"No chunk files found next to {toc_path}. "
            f"Expected e.g. {toc_path.with_suffix(f'.{group_digit}1')}"
        )

    # TOC 로드
    with open(toc_path, "rb") as f:
        first = f.read(SECTOR)
        if not first:
            raise ValueError(f"Empty TOC: {toc_path}")
        n = first[0]
        total = n * SECTOR
        if toc_path.stat().st_size < total:
            raise ValueError(
                f"TOC header claims N={n} sectors ({total} bytes) "
                f"but file is only {toc_path.stat().st_size} bytes"
            )
        rest = f.read(total - SECTOR) if n > 1 else b""
    toc_bytes = first + rest

    entries = parse_toc(toc_bytes)

    return ArchiveSet(
        toc_path=toc_path,
        chunk_paths=chunks,
        header_sectors=n,
        entries=entries,
    )


def open_virtual(arc: ArchiveSet) -> VirtualChain:
    """TOC 파일 + 모든 청크를 이어붙인 가상 파일 리더를 연다."""
    return VirtualChain([arc.toc_path, *arc.chunk_paths])


# ---------------------------------------------------------------------------
# TOC 빌더 (리팩 시)
# ---------------------------------------------------------------------------

@dataclass
class BuildEntry:
    """리빌드 시 사용. 순서를 원본과 일치시키기 위한 최소 정보."""
    path: str                # '/' 구분
    source: Path             # 디스크 상의 소스 파일
    layer2: bool = False     # bit 0x40 유지 여부. 원본에서 세워져 있었다면 True.
    alt_lba: int | None = None  # 보통 0 또는 원본값. 리빌드에서는 0 으로 둬도 동작상 무해.


def _encode_dir_cmd(name: str, back: int) -> bytes:
    name_bytes = name.encode("ascii")
    name_len = len(name_bytes) + 2
    if name_len > 0x3F:
        raise ValueError(f"Dir name too long: {name!r}")
    return bytes([0x80 | name_len, back & 0xFF]) + name_bytes


def _encode_file_cmd(name: str, lba: int, size: int, alt_lba: int | None) -> bytes:
    name_bytes = name.encode("ascii")
    name_len = len(name_bytes) + 1
    if name_len > 0x3F:
        raise ValueError(f"File name too long: {name!r}")
    cmd = name_len
    meta = bytes([lba & 0xFF, (lba >> 8) & 0xFF, (lba >> 16) & 0xFF])
    meta += bytes([
        size & 0xFF,
        (size >> 8) & 0xFF,
        (size >> 16) & 0xFF,
        (size >> 24) & 0xFF,
    ])
    if alt_lba is not None:
        cmd |= 0x40
        meta += bytes([alt_lba & 0xFF, (alt_lba >> 8) & 0xFF, (alt_lba >> 16) & 0xFF])
    return bytes([cmd]) + name_bytes + meta


def build_toc_stream(entries: list[BuildEntry], lbas: list[int]) -> bytes:
    """
    주어진 순서·LBA 배치로 TOC 바이너리를 생성.

    엔트리 순서는 원본의 DFS 순서를 그대로 보존해야 한다 (manifest에 저장).
    파서 호환을 위해 디렉터리 전환은 0x80 cmd 로 처리한다.
    """
    assert len(entries) == len(lbas)
    out = bytearray()
    # 첫 바이트는 헤더 섹터 카운트(N) — 이 시점에 N을 모르므로 placeholder 0 으로 두고
    # 호출측에서 나중에 덮어쓴다.
    out.append(0)

    current_dirs: list[str] = []
    for ent, lba in zip(entries, lbas):
        parts = ent.path.split("/")
        target_dirs = parts[:-1]
        name = parts[-1]

        # 현재 디렉터리 스택을 target_dirs로 맞추기
        # 공통 prefix 길이를 구해 pop 수와 push 수를 결정
        common = 0
        while (
            common < len(current_dirs)
            and common < len(target_dirs)
            and current_dirs[common] == target_dirs[common]
        ):
            common += 1
        # pop 할 레벨 수
        pops_needed = len(current_dirs) - common
        # push 할 새 디렉터리들
        pushes = target_dirs[common:]

        if not pushes:
            # 같은 디렉터리 계속
            pass
        else:
            # 파서 수식: new_depth = current_depth - back + 1
            #   → 첫 push의 back = current_depth - common   (= pops_needed, 스택 위로 그만큼 올라감)
            #   → 이후 push는 서브디렉터리 진입이므로 back = 0
            for i, d in enumerate(pushes):
                back = pops_needed if i == 0 else 0
                out += _encode_dir_cmd(d, back)
            current_dirs = list(target_dirs)

        out += _encode_file_cmd(
            name,
            lba,
            _source_size(ent.source),
            ent.alt_lba if ent.layer2 else None,
        )

    out.append(0)  # 종단
    return bytes(out)


def _source_size(p: Path) -> int:
    return p.stat().st_size


def align_up(n: int, a: int = SECTOR) -> int:
    return (n + a - 1) & ~(a - 1)
