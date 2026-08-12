"""
repack.py — Xenosaga Episode I 고정 크기 아카이브 리패커.

원본 아카이브의 청크 크기는 절대 바꾸지 않는다. 수정 파일이 자신의 원래
섹터 수를 넘으면, 원본 위치 주변을 막는 작은 파일을 작업본에서 줄어든 빈
섹터로 옮겨 공간을 만든다. 모든 이동은 TOC에 기록되며 최종 배치가 원본
컨테이너 안에 들어가지 않으면 빌드를 중단한다.

사용법
------
    python repack.py <unpack 출력 디렉터리> <원본 TOC> <출력 디렉터리>
"""

from __future__ import annotations

import json
import shutil
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from xenoarc import SECTOR, BuildEntry, build_toc_stream


class RepackLayoutError(ValueError):
    """수정 파일을 원본 아카이브 크기 안에 안전하게 배치할 수 없음."""


@dataclass
class _Placement:
    index: int
    path: str
    original_lba: int
    original_sectors: int
    required_sectors: int
    lba: int

    @property
    def end(self) -> int:
        return self.lba + self.required_sectors


def _sectors(size: int) -> int:
    return (size + SECTOR - 1) // SECTOR


def _chunk_info(orig_toc: Path) -> tuple[int, list[Path], list[int]]:
    """(원본 TOC 크기, 청크 경로, 청크 크기) 수집."""
    group_digit = orig_toc.suffix[1]
    chunks: list[Path] = []
    sizes: list[int] = []
    for sub in range(1, 10):
        path = orig_toc.with_suffix(f".{group_digit}{sub}")
        if not path.exists():
            break
        chunks.append(path)
        sizes.append(path.stat().st_size)
    if not chunks:
        raise FileNotFoundError(f"No chunk siblings next to {orig_toc}")
    return orig_toc.stat().st_size, chunks, sizes


def _overlaps(start: int, end: int, item: _Placement) -> bool:
    return start < item.end and item.lba < end


def _free_gaps(
    placements: list[_Placement],
    active: set[int],
    data_start: int,
    total_sectors: int,
) -> list[tuple[int, int]]:
    intervals = sorted(
        (item.lba, item.end)
        for item in placements
        if item.index in active and item.required_sectors
    )
    gaps: list[tuple[int, int]] = []
    cursor = data_start
    for start, end in intervals:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_sectors:
        gaps.append((cursor, total_sectors))
    return gaps


def _plan_fixed_layout(
    entries_meta: list[dict],
    source_sizes: list[int],
    data_start: int,
    total_sectors: int,
) -> list[_Placement]:
    """원본 컨테이너 안에서 겹치지 않는 섹터 배치를 계산한다.

    우선 모든 파일을 원래 LBA에 둔다. 커진 파일은 원래 시작점을 유지한
    전방 확장과 원래 끝점을 유지한 후방 확장을 비교하고, 옮겨야 하는
    주변 파일의 총 섹터 수가 작은 쪽을 택한다. 밀려난 파일은 작업본의
    축소 파일이 만든 빈 구간에 best-fit으로 넣는다.
    """
    if len(entries_meta) != len(source_sizes):
        raise ValueError("entry/source size count mismatch")

    placements = [
        _Placement(
            index=index,
            path=meta["path"],
            original_lba=meta["lba"],
            original_sectors=_sectors(meta["size"]),
            required_sectors=_sectors(source_sizes[index]),
            lba=meta["lba"],
        )
        for index, meta in enumerate(entries_meta)
    ]
    active = {item.index for item in placements}
    relocated: set[int] = set()
    grown = {
        item.index
        for item in placements
        if item.required_sectors > item.original_sectors
    }

    for index in sorted(grown, key=lambda i: placements[i].original_lba):
        item = placements[index]
        starts = [item.original_lba]
        end_aligned = item.original_lba + item.original_sectors - item.required_sectors
        if end_aligned != item.original_lba:
            starts.append(end_aligned)

        candidates: list[tuple[tuple[int, int, int, int], int, list[int]]] = []
        for preference, start in enumerate(starts):
            end = start + item.required_sectors
            if start < data_start or end > total_sectors:
                continue
            blockers = [
                other.index
                for other in placements
                if other.index in active
                and other.index != index
                and other.required_sectors
                and _overlaps(start, end, other)
            ]
            # 커진 파일끼리 서로 밀어내는 배치는 불안정하므로 허용하지 않는다.
            if any(blocker in grown for blocker in blockers):
                continue
            score = (
                sum(placements[blocker].required_sectors for blocker in blockers),
                len(blockers),
                abs(start - item.original_lba),
                preference,
            )
            candidates.append((score, start, blockers))

        if not candidates:
            raise RepackLayoutError(
                f"{item.path}: cannot expand from {item.original_sectors} to "
                f"{item.required_sectors} sectors inside the original layout"
            )

        _, chosen_start, blockers = min(candidates, key=lambda candidate: candidate[0])
        item.lba = chosen_start
        for blocker in blockers:
            active.remove(blocker)
            relocated.add(blocker)

    # 고정된 항목이 서로 겹치면 위의 국소 확장만으로 해결할 수 없는 배치다.
    fixed = sorted(
        (item for item in placements if item.index in active and item.required_sectors),
        key=lambda item: item.lba,
    )
    for previous, current in zip(fixed, fixed[1:]):
        if previous.end > current.lba:
            raise RepackLayoutError(
                f"fixed layout overlap: {previous.path} -> {current.path}"
            )

    # 큰 파일부터 빈 구간에 넣어 작은 조각 때문에 배치가 실패하지 않게 한다.
    for index in sorted(
        relocated,
        key=lambda i: (-placements[i].required_sectors, placements[i].original_lba),
    ):
        item = placements[index]
        gaps = _free_gaps(placements, active, data_start, total_sectors)
        choices: list[tuple[tuple[int, int, int], int]] = []
        for gap_start, gap_end in gaps:
            gap_size = gap_end - gap_start
            if gap_size < item.required_sectors:
                continue
            near_start = min(
                max(item.original_lba, gap_start),
                gap_end - item.required_sectors,
            )
            score = (
                gap_size - item.required_sectors,
                abs(near_start - item.original_lba),
                near_start,
            )
            choices.append((score, near_start))
        if not choices:
            raise RepackLayoutError(
                f"{item.path}: no {item.required_sectors}-sector free range remains "
                "inside the original archive"
            )
        _, item.lba = min(choices, key=lambda choice: choice[0])
        active.add(index)

    ordered = sorted(
        (item for item in placements if item.required_sectors),
        key=lambda item: item.lba,
    )
    for item in ordered:
        if item.lba < data_start or item.end > total_sectors:
            raise RepackLayoutError(f"{item.path}: planned range is outside the archive")
    for previous, current in zip(ordered, ordered[1:]):
        if previous.end > current.lba:
            raise RepackLayoutError(
                f"planned overlap: {previous.path} -> {current.path}"
            )
    return placements


def repack(unpack_dir: Path, orig_toc: Path, out_dir: Path) -> None:
    manifest_path = unpack_dir / "manifest.json"
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)

    tree_dir = unpack_dir / "tree"
    if not tree_dir.is_dir():
        raise FileNotFoundError(f"Tree dir missing: {tree_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    orig_toc_size, orig_chunks, orig_chunk_sizes = _chunk_info(orig_toc)
    if orig_toc_size % SECTOR or any(size % SECTOR for size in orig_chunk_sizes):
        raise RepackLayoutError("original TOC/chunk size is not sector aligned")
    orig_n = orig_toc_size // SECTOR
    orig_virtual = orig_toc_size + sum(orig_chunk_sizes)
    total_sectors = orig_virtual // SECTOR

    entries_meta = sorted(manifest["entries"], key=lambda entry: entry["order"])
    sources = [tree_dir / entry["path"] for entry in entries_meta]
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"Missing source file: {source}")
    source_sizes = [source.stat().st_size for source in sources]

    placements = _plan_fixed_layout(entries_meta, source_sizes, orig_n, total_sectors)
    new_lbas = [item.lba for item in placements]
    moved = [item for item in placements if item.lba != item.original_lba]
    grown = [
        item for item in placements
        if item.required_sectors > item.original_sectors
    ]

    print(f"[plan] fixed archive size: {orig_virtual:,} bytes ({total_sectors} sectors)")
    print(f"[plan] original LBA kept: {len(placements) - len(moved)}, moved: {len(moved)}")
    for item in grown:
        print(
            f"  [grow] {item.path}: {item.original_sectors} -> "
            f"{item.required_sectors} sectors, LBA {item.original_lba} -> {item.lba}"
        )
    for item in moved:
        if item not in grown:
            print(f"  [move] {item.path}: LBA {item.original_lba} -> {item.lba}")

    build_entries: list[BuildEntry] = []
    for source, meta, item in zip(sources, entries_meta, placements):
        alt_lba = meta.get("alt_lba")
        if bool(meta["layer2"]) and item.lba != item.original_lba:
            alt_lba = item.lba
        build_entries.append(
            BuildEntry(
                path=meta["path"],
                source=source,
                layer2=bool(meta["layer2"]),
                alt_lba=alt_lba,
            )
        )

    real = build_toc_stream(build_entries, new_lbas)
    n_new = _sectors(len(real))
    if n_new != orig_n:
        raise RepackLayoutError(
            f"TOC sector count changed: {orig_n} -> {n_new}; fixed layout aborted"
        )
    real = bytes([orig_n]) + real[1:]
    orig_toc_bytes = orig_toc.read_bytes()
    if len(real) > len(orig_toc_bytes):
        raise RepackLayoutError("rebuilt TOC exceeds the original TOC allocation")

    out_toc = out_dir / orig_toc.name
    shutil.copy2(orig_toc, out_toc)
    with out_toc.open("r+b") as stream:
        stream.write(real)

    out_chunk_paths = [out_dir / path.name for path in orig_chunks]
    for source, destination in zip(orig_chunks, out_chunk_paths):
        shutil.copy2(source, destination)

    chunk_starts: list[int] = []
    cursor = 0
    for size in orig_chunk_sizes:
        chunk_starts.append(cursor)
        cursor += size

    with ExitStack() as stack:
        outputs = [stack.enter_context(path.open("r+b")) for path in out_chunk_paths]
        for source_path, item in zip(sources, placements):
            data_offset = item.lba * SECTOR - orig_toc_size
            remaining = source_path.stat().st_size
            position = data_offset
            with source_path.open("rb") as source:
                while remaining:
                    chunk_index = max(
                        index
                        for index, start in enumerate(chunk_starts)
                        if position >= start
                    )
                    local = position - chunk_starts[chunk_index]
                    room = orig_chunk_sizes[chunk_index] - local
                    take = min(4 * 1024 * 1024, remaining, room)
                    if take <= 0:
                        raise RepackLayoutError(f"write past final chunk: {item.path}")
                    data = source.read(take)
                    if len(data) != take:
                        raise EOFError(f"short source read: {source_path}")
                    outputs[chunk_index].seek(local)
                    outputs[chunk_index].write(data)
                    position += take
                    remaining -= take
        for output in outputs:
            output.flush()

    # 크기와 실제 기록 내용을 모두 확인한다.
    if out_toc.stat().st_size != orig_toc_size:
        raise RepackLayoutError("output TOC size changed")
    for output, expected in zip(out_chunk_paths, orig_chunk_sizes):
        if output.stat().st_size != expected:
            raise RepackLayoutError(f"output chunk size changed: {output.name}")

    with ExitStack() as stack:
        outputs = [stack.enter_context(path.open("rb")) for path in out_chunk_paths]
        for source_path, item in zip(sources, placements):
            position = item.lba * SECTOR - orig_toc_size
            remaining = source_path.stat().st_size
            with source_path.open("rb") as source:
                while remaining:
                    chunk_index = max(
                        index
                        for index, start in enumerate(chunk_starts)
                        if position >= start
                    )
                    local = position - chunk_starts[chunk_index]
                    take = min(
                        4 * 1024 * 1024,
                        remaining,
                        orig_chunk_sizes[chunk_index] - local,
                    )
                    expected = source.read(take)
                    outputs[chunk_index].seek(local)
                    actual = outputs[chunk_index].read(take)
                    if actual != expected:
                        raise RepackLayoutError(f"read-back mismatch: {item.path}")
                    position += take
                    remaining -= take

    report_path = out_dir / "repack_report.txt"
    with report_path.open("w", encoding="utf-8") as stream:
        stream.write(f"Source: {unpack_dir}\n")
        stream.write(f"Reference TOC: {orig_toc}\n")
        stream.write(f"Entries: {len(placements)}\n")
        stream.write(f"Fixed virtual size: {orig_virtual}\n")
        stream.write(f"Original TOC sectors: {orig_n}\n")
        stream.write(f"Moved entries: {len(moved)}\n")
        stream.write(f"Original chunk sizes: {orig_chunk_sizes}\n")
        stream.write(f"New chunk sizes: {orig_chunk_sizes}\n")
        if grown:
            stream.write("\nExpanded entries:\n")
            for item in grown:
                stream.write(
                    f"  {item.path}: {item.original_sectors} -> "
                    f"{item.required_sectors} sectors, "
                    f"LBA {item.original_lba} -> {item.lba}\n"
                )
        if moved:
            stream.write("\nMoved entries:\n")
            for item in moved:
                stream.write(f"  {item.path}: {item.original_lba} -> {item.lba}\n")

    print(f"[done] {report_path}")


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    repack(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
