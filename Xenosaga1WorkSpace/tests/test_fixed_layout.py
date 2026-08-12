from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tsuru"))

from isobuild import IsoLayoutError, SECTOR, parse_iso_layout, rebuild_iso  # noqa: E402
from repack import RepackLayoutError, _plan_fixed_layout  # noqa: E402


def _both_u16(value: int) -> bytes:
    return struct.pack("<H", value) + struct.pack(">H", value)


def _both_u32(value: int) -> bytes:
    return struct.pack("<I", value) + struct.pack(">I", value)


def _record(name: bytes, lba: int, size: int, flags: int = 0) -> bytes:
    length = 33 + len(name)
    if length % 2:
        length += 1
    record = bytearray(length)
    record[0] = length
    record[2:10] = _both_u32(lba)
    record[10:18] = _both_u32(size)
    record[25] = flags
    record[28:32] = _both_u16(1)
    record[32] = len(name)
    record[33 : 33 + len(name)] = name
    return bytes(record)


def _minimal_iso(path: Path) -> bytes:
    image = bytearray(64 * SECTOR)
    root_record = _record(b"\x00", 20, SECTOR, flags=2)
    pvd = memoryview(image)[16 * SECTOR : 17 * SECTOR]
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[80:88] = _both_u32(64)
    pvd[128:132] = _both_u16(SECTOR)
    pvd[156 : 156 + len(root_record)] = root_record

    root = memoryview(image)[20 * SECTOR : 21 * SECTOR]
    dot = _record(b"\x00", 20, SECTOR, flags=2)
    dotdot = _record(b"\x01", 20, SECTOR, flags=2)
    file_record = _record(b"DATA.BIN;1", 30, SECTOR)
    position = 0
    for record in (dot, dotdot, file_record):
        root[position : position + len(record)] = record
        position += len(record)
    image[30 * SECTOR : 31 * SECTOR] = b"A" * SECTOR
    data = bytes(image)
    path.write_bytes(data)
    return data


class FixedArchivePlannerTests(unittest.TestCase):
    def test_grown_files_borrow_local_sectors_and_move_small_blockers(self):
        specs = [
            ("shrink1", 1, 2, 1),
            ("shrink2", 3, 2, 1),
            ("shrink3", 5, 2, 1),
            ("cf0", 7, 1, 1),
            ("st1", 8, 2, 3),
            ("cf1", 10, 1, 1),
            ("st2", 11, 2, 3),
            ("cf2", 13, 1, 1),
            ("middle", 14, 3, 3),
            ("mini", 17, 1, 1),
            ("card", 18, 2, 3),
            ("large", 20, 5, 5),
        ]
        entries = [
            {"path": name, "lba": lba, "size": old * SECTOR}
            for name, lba, old, _ in specs
        ]
        sizes = [required * SECTOR for _, _, _, required in specs]
        plan = _plan_fixed_layout(entries, sizes, data_start=1, total_sectors=25)
        by_name = {item.path: item for item in plan}

        self.assertEqual(by_name["st1"].lba, 8)
        self.assertEqual(by_name["st2"].lba, 11)
        self.assertEqual(by_name["card"].lba, 17)
        moved_small = {by_name[name].lba for name in ("cf1", "cf2", "mini")}
        self.assertEqual(moved_small, {2, 4, 6})

    def test_no_free_sector_fails_instead_of_growing_container(self):
        entries = [
            {"path": "grown", "lba": 1, "size": 2 * SECTOR},
            {"path": "blocker", "lba": 3, "size": SECTOR},
        ]
        with self.assertRaises(RepackLayoutError):
            _plan_fixed_layout(
                entries,
                [3 * SECTOR, SECTOR],
                data_start=1,
                total_sectors=4,
            )


class FixedIsoTests(unittest.TestCase):
    def test_replacement_changes_only_original_extent(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp:
            temp_dir = Path(temp)
            source = temp_dir / "source.iso"
            output = temp_dir / "output.iso"
            replacement = temp_dir / "DATA.BIN"
            original = _minimal_iso(source)
            replacement.write_bytes(b"B" * SECTOR)

            layout = parse_iso_layout(source)
            rebuild_iso(
                source,
                output,
                {"DATA.BIN;1": replacement},
                layout,
                progress=lambda _message: None,
            )
            rebuilt = output.read_bytes()
            start = 30 * SECTOR
            end = start + SECTOR
            self.assertEqual(len(rebuilt), len(original))
            self.assertEqual(rebuilt[:start], original[:start])
            self.assertEqual(rebuilt[start:end], b"B" * SECTOR)
            self.assertEqual(rebuilt[end:], original[end:])

    def test_oversize_root_replacement_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp:
            temp_dir = Path(temp)
            source = temp_dir / "source.iso"
            output = temp_dir / "output.iso"
            replacement = temp_dir / "DATA.BIN"
            _minimal_iso(source)
            replacement.write_bytes(b"B" * (SECTOR + 1))

            with self.assertRaises(IsoLayoutError):
                rebuild_iso(
                    source,
                    output,
                    {"DATA.BIN;1": replacement},
                    progress=lambda _message: None,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
