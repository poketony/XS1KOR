"""
repack.py — Xenosaga Episode I 아카이브 리패커.

사용법
------
    python repack.py <unpack 출력 디렉터리> <원본 TOC> <출력 디렉터리>

    python repack.py ./out10 ../xenosaga/XENOSAGA.10 ./rebuilt10

전략
----
원본 DVD 레이아웃은 단순한 연속 팩이 아니라 파일들이 물리적으로 인터리브되어 있다
(스트리밍/시크 최적화). 그래서 "변하지 않은 파일"은 원본 LBA에 그대로 두고,
"크기가 커진 파일"만 데이터 영역 뒤쪽으로 재배치한다. 이 방식으로

  - 수정이 전혀 없을 때: 모든 파일이 원본 LBA 그대로 → 게임이 읽는 바이트 범위가 동일
  - 일부 파일만 수정되고 작아졌을 때: 원본 자리에 그대로 쓴다 (뒤 공간은 필러로 채움)
  - 커진 파일: 원본 자리 못 쓰고, 기존 max_lba 뒤에 새 LBA 할당해서 배치
  - TOC는 새 LBA 를 반영해 다시 인코딩되어 파일 이름 1개당 1엔트리로 저장

출력
----
    <out>/XENOSAGA.10        새 TOC (N 섹터, byte 0 = N)
    <out>/XENOSAGA.11..1N    새 청크들, 원본과 같은 개수/크기 목표
    <out>/repack_report.txt  요약

출력 청크 크기는 원본 크기를 기본값으로 쓰되, 총 데이터가 커지면 마지막 청크만 부풀린다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from xenoarc import (
    SECTOR,
    BuildEntry,
    align_up,
    build_toc_stream,
)


FILLER = b"MONOLITHSOFT Xenosaga Episode.1\x00"  # 32 bytes, 원본 패딩 패턴


def _fill_bytes(n: int) -> bytes:
    if n <= 0:
        return b""
    # FILLER 를 타일링
    q, r = divmod(n, len(FILLER))
    return FILLER * q + FILLER[:r]


def _pad_toc(toc_bytes: bytes, target_size: int) -> bytes:
    assert len(toc_bytes) <= target_size
    if len(toc_bytes) == target_size:
        return toc_bytes
    return toc_bytes + _fill_bytes(target_size - len(toc_bytes))


def _chunk_info(orig_toc: Path) -> tuple[int, list[Path], list[int]]:
    """(orig_toc_size, chunk paths, chunk sizes) 수집."""
    group_digit = orig_toc.suffix[1]
    chunks: list[Path] = []
    sizes: list[int] = []
    for sub in range(1, 10):
        p = orig_toc.with_suffix(f".{group_digit}{sub}")
        if p.exists():
            chunks.append(p)
            sizes.append(p.stat().st_size)
        else:
            break
    if not chunks:
        raise FileNotFoundError(f"No chunk siblings next to {orig_toc}")
    return orig_toc.stat().st_size, chunks, sizes


def repack(unpack_dir: Path, orig_toc: Path, out_dir: Path) -> None:
    manifest_path = unpack_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    tree_dir = unpack_dir / "tree"
    if not tree_dir.is_dir():
        raise FileNotFoundError(f"Tree dir missing: {tree_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    orig_toc_size, orig_chunks, orig_chunk_sizes = _chunk_info(orig_toc)
    orig_n = orig_toc_size // SECTOR

    # 원본 가상 파일 총 크기 = orig_toc_size + sum(chunk sizes)
    orig_virtual = orig_toc_size + sum(orig_chunk_sizes)

    # 엔트리 순서 (원본 TOC 순서) 복원
    entries_meta = sorted(manifest["entries"], key=lambda e: e["order"])

    # 원본 데이터 영역에서 "사용 중인 sector 범위" 집합을 만든다.
    # reservation = 리스트(정렬된 non-overlapping [start_sector, end_sector_exclusive))
    # 초기: 파일별로 원본 LBA..LBA+ceil(size/SECTOR)
    def _build_reservations(items):
        ranges = []
        for it in items:
            s = it["lba"]
            e = s + (it["size"] + SECTOR - 1) // SECTOR
            ranges.append((s, e))
        ranges.sort()
        # 병합
        merged = []
        for s, e in ranges:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    reservations = _build_reservations(entries_meta)
    max_used_sector = max(e["lba"] + (e["size"] + SECTOR - 1) // SECTOR for e in entries_meta)
    print(f"[plan] orig max used sector = {max_used_sector}")

    # 파일별 새 LBA 결정
    build_entries: list[BuildEntry] = []
    new_lbas: list[int] = []
    grown_files: list[tuple[str, int, int]] = []  # (path, old_size, new_size)

    # "커진 파일"은 데이터 영역 뒤쪽에 append. append 커서는 max_used_sector 뒤부터.
    append_cursor = max_used_sector

    # 원래 슬롯에 들어맞지 못한 파일을 표시
    kept_count = 0
    moved_count = 0

    for m in entries_meta:
        rel = m["path"]
        src = tree_dir / rel
        if not src.is_file():
            raise FileNotFoundError(f"Missing source file: {src}")
        new_size = src.stat().st_size
        orig_size = m["size"]
        orig_lba = m["lba"]
        # 원 슬롯의 용량 (sector 단위)
        orig_slot = (orig_size + SECTOR - 1) // SECTOR
        new_need = (new_size + SECTOR - 1) // SECTOR

        if new_need <= orig_slot:
            # 원 자리 그대로
            lba = orig_lba
            kept_count += 1
        else:
            # 뒤로 재배치
            lba = append_cursor
            append_cursor += new_need
            moved_count += 1
            grown_files.append((rel, orig_size, new_size))

        be = BuildEntry(
            path=rel,
            source=src,
            layer2=bool(m["layer2"]),
            alt_lba=None,
        )
        build_entries.append(be)
        new_lbas.append(lba)

    print(f"[plan] kept in place: {kept_count}, relocated: {moved_count}")
    if grown_files[:5]:
        print(f"[plan] first relocated files:")
        for p, o, n in grown_files[:5]:
            print(f"   {p}: {o} -> {n} bytes")
    final_end_sector = append_cursor  # 데이터 영역 끝 (섹터)
    print(f"[plan] final end sector = {final_end_sector}  ({final_end_sector * SECTOR} bytes)")

    # layer2 alt_lba 복원:
    #   - 원 자리 그대로인 파일: 원본 alt_lba 값을 그대로 사용
    #   - 재배치된 파일: 새 lba 와 동일 (안전한 fallback)
    for be, lba, m in zip(build_entries, new_lbas, entries_meta):
        if be.layer2:
            if lba == m["lba"] and m.get("alt_lba") is not None:
                be.alt_lba = m["alt_lba"]
            else:
                be.alt_lba = lba

    # 1차: 더미 LBA로 길이 측정
    stub = build_toc_stream(build_entries, [0] * len(build_entries))
    toc_byte_len = len(stub)
    n_new = (toc_byte_len + SECTOR - 1) // SECTOR
    print(f"[toc] byte length = {toc_byte_len} → N = {n_new} (orig N = {orig_n})")

    if n_new != orig_n:
        # 파일 경로/개수가 변했을 때만 발생
        print(f"[warn] TOC sector count changed ({orig_n} -> {n_new})")
        # LBA 들을 shift. 모든 파일 LBA 는 n_new 이상이어야 하는데, 원본 보존 LBA 는 orig_n 기준.
        # 보수적으로: shift = n_new - orig_n
        shift = n_new - orig_n
        if shift > 0:
            new_lbas = [lba + shift for lba in new_lbas]
            final_end_sector += shift

    # 2차: 실제 LBA 로 재빌드
    real = build_toc_stream(build_entries, new_lbas)
    assert len(real) == toc_byte_len
    real = bytes([n_new]) + real[1:]
    # TOC 크기가 원본과 같다면 패딩 영역을 원본 그대로 복사해서 바이트-동일성 유지
    orig_toc_bytes = orig_toc.read_bytes()
    target_size = n_new * SECTOR
    if target_size == len(orig_toc_bytes) and len(real) <= target_size:
        toc_padded = real + orig_toc_bytes[len(real):]
    else:
        toc_padded = _pad_toc(real, target_size)

    # TOC 쓰기
    out_toc = out_dir / orig_toc.name
    out_toc.write_bytes(toc_padded)
    print(f"[write] {out_toc.name} ({len(toc_padded)} bytes)")

    # 데이터 영역 총 바이트 = final_end_sector * SECTOR - n_new * SECTOR
    data_total = (final_end_sector - n_new) * SECTOR
    print(f"[write] data region = {data_total} bytes")

    # 청크 쓰기: 초기에는 원본 청크 크기를 유지, 부족하면 마지막 청크가 흡수
    # 청크 용량 배열
    chunk_count = len(orig_chunks)
    chunk_capacities = list(orig_chunk_sizes)

    # 필요한 총 용량
    needed = data_total
    # 원본 총 데이터 용량 (첫 TOC 넘어선 영역 포함)
    orig_budget = sum(orig_chunk_sizes) + (orig_toc_size - n_new * SECTOR)
    if needed > orig_budget:
        # 마지막 청크 확장
        chunk_capacities[-1] += needed - orig_budget
        print(f"[grow] last chunk +{needed - orig_budget} bytes")
    elif needed < orig_budget:
        # 여유분은 마지막 청크에서 줄임 (원본과 같은 크기 유지를 기본으로)
        # 사실 여기선 그냥 원본 크기를 유지해도 되는데, 용량이 남으면 filler 로 패딩된다.
        pass

    out_chunk_paths = [out_dir / p.name for p in orig_chunks]

    # 출력 청크 파일들: 미리 전체 크기로 생성 후 seek-write (sparse/filler로 초기화)
    # 간단히: 각 청크를 한번 열고 FILLER 로 채운 뒤 파일 쓰기를 통해 덮어씌움
    for cp, cap in zip(out_chunk_paths, chunk_capacities):
        with open(cp, "wb") as f:
            # FILLER 로 완전히 초기화
            remaining = cap
            block = _fill_bytes(min(remaining, 4 * 1024 * 1024))
            while remaining > 0:
                take = min(remaining, len(block))
                f.write(block[:take])
                remaining -= take
    print(f"[init] initialized {chunk_count} chunks with filler pattern")

    # 각 청크의 시작 byte (데이터 영역 기준)
    chunk_offsets = []
    acc = 0
    for cap in chunk_capacities:
        chunk_offsets.append(acc)
        acc += cap
    # 마지막 경계
    chunk_offsets.append(acc)

    def _write_at_data_offset(off: int, data: bytes):
        """데이터 영역 오프셋 off 에 data 를 쓴다. 청크 경계를 교차할 수 있다."""
        remaining = data
        pos = off
        while remaining:
            # 어느 청크인지
            ci = 0
            while ci < chunk_count and pos >= chunk_offsets[ci + 1]:
                ci += 1
            if ci >= chunk_count:
                raise RuntimeError(f"write past last chunk @ {pos}")
            local = pos - chunk_offsets[ci]
            space = chunk_capacities[ci] - local
            take = min(len(remaining), space)
            with open(out_chunk_paths[ci], "r+b") as f:
                f.seek(local)
                f.write(remaining[:take])
            remaining = remaining[take:]
            pos += take

    # 이제 파일별로 데이터 영역에 쓴다. lba 는 가상 파일 기준 (TOC 포함).
    # 데이터 영역 오프셋 = (lba - n_new) * SECTOR
    for be, lba in zip(build_entries, new_lbas):
        data_off = (lba - n_new) * SECTOR
        size = be.source.stat().st_size
        # 스트리밍 복사
        with open(be.source, "rb") as src:
            remaining_bytes = size
            cur = data_off
            while remaining_bytes > 0:
                buf = src.read(4 * 1024 * 1024)
                if not buf:
                    break
                _write_at_data_offset(cur, buf)
                cur += len(buf)
                remaining_bytes -= len(buf)

    # 리포트
    report_path = out_dir / "repack_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Source: {unpack_dir}\n")
        f.write(f"Reference TOC: {orig_toc}\n")
        f.write(f"Entries: {len(build_entries)}\n")
        f.write(f"Original N sectors: {orig_n}  New N sectors: {n_new}\n")
        f.write(f"Kept in place: {kept_count}\n")
        f.write(f"Relocated (grew): {moved_count}\n")
        f.write(f"Original chunk sizes: {orig_chunk_sizes}\n")
        f.write(f"New chunk sizes    : {chunk_capacities}\n")
        if grown_files:
            f.write("\nGrown files (relocated):\n")
            for p, o, n in grown_files:
                f.write(f"  {p}: {o} -> {n} (+{n - o})\n")

    print(f"[done] {report_path.name}")


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    repack(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
