"""Original-backed checks for the memory-card first-line-only patch."""

from pathlib import Path
import struct
import unittest

import patch_slps_menu_spacing as tool


class SaveTitlePrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "slps_290.02").read_bytes()

    def test_only_reviewed_bytes_change_and_payload_is_loaded(self):
        built = bytearray(self.source)
        tool.apply_save_title_prefix(built)
        regions = ((0x64, 8), (0x134CB0, 0x50), (0x20830, 4),
                   (0x20854, 4), (0x20868, 4), (0x20878, 4))
        restored = bytearray(built)
        for start, size in regions:
            restored[start:start + size] = self.source[start:start + size]
        self.assertEqual(restored, self.source)
        self.assertEqual(len(built), len(self.source))
        self.assertEqual(struct.unpack_from("<II", built, 0x64), (0x133D00, 0x133D00))
        self.assertEqual(struct.unpack_from("<I", built, 0x78)[0], 0x134D00)
        self.assertEqual(built[0x134CE0:0x134CFD], bytes.fromhex(
            "82778285828e828f82938281828782818264826f8268817c824f824f00"))
        self.assertEqual(struct.unpack_from("<I", built, 0x20854)[0], 0x248900DC)
        # Converter, location composer, file-name builder and their data stay intact.
        self.assertEqual(built[0x20610:0x20830], self.source[0x20610:0x20830])
        self.assertEqual(built[0x208B0:0x20A00], self.source[0x208B0:0x20A00])

    def test_bad_inputs_are_rejected_before_any_mutation(self):
        for offset in (0x64, 0x134CB0, 0x20830, 0x20854, 0x20868, 0x20878):
            damaged = bytearray(self.source)
            damaged[offset] ^= 1
            before = bytes(damaged)
            with self.subTest(offset=hex(offset)), self.assertRaises(ValueError):
                tool.apply_save_title_prefix(damaged)
            self.assertEqual(damaged, before)
        built = bytearray(self.source)
        tool.apply_save_title_prefix(built)
        with self.assertRaises(ValueError):
            tool.apply_save_title_prefix(built)


if __name__ == "__main__":
    unittest.main()
