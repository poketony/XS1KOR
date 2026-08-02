import struct
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import patch_slps_menu_spacing as patcher
import slps_strings


class SlpsLoadTextRelocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_path = HERE / "slps_290.02"
        cls.original = cls.source_path.read_bytes()
        cls.translations = patcher.parse_translations(
            HERE / "slps_290_strings.KOR.txt"
        )
        cls.replace_table = slps_strings.load_replace_table(str(cls.source_path))

    def test_relocation_preserves_sbss_and_updates_all_references(self):
        data = bytearray(self.original)
        before_sbss_padding = bytes(data[0x002D7980:0x002D8000])

        patcher.apply_load_text_relocations(
            data,
            self.original,
            self.translations,
            lambda text: slps_strings.encode_display(text, self.replace_table),
        )
        patcher.apply_spacing_fixes(data, self.translations)

        for pointer_offset, (_expected, replacement) in (
            patcher.LOAD_TEXT_POINTER_PATCHES.items()
        ):
            self.assertEqual(
                struct.unpack_from("<I", data, pointer_offset)[0],
                replacement,
            )

        self.assertEqual(
            struct.unpack_from("<I", data, patcher.LOAD_TEXT_PHDR_FILESZ_OFFSET)[0],
            patcher.LOAD_TEXT_PHDR_FILESZ[1],
        )
        self.assertEqual(
            struct.unpack_from("<I", data, patcher.LOAD_TEXT_SDATA_SH_SIZE_OFFSET)[0],
            patcher.LOAD_TEXT_SDATA_SH_SIZE[1],
        )

        for source_offset, (destination, span) in (
            patcher.LOAD_TEXT_RELOCATIONS.items()
        ):
            encoded = slps_strings.encode_display(
                self.translations[source_offset], self.replace_table
            )
            self.assertEqual(bytes(data[destination:destination + len(encoded)]), encoded)
            self.assertEqual(data[destination + len(encoded)], 0)
            self.assertEqual(
                bytes(data[destination + len(encoded):destination + span]),
                b"\0" * (span - len(encoded)),
            )

        first, second = patcher.LOAD_TEXT_COMPOUND_PARTS
        bridge_start, bridge_end = patcher.LOAD_TEXT_COMPOUND_BRIDGE
        compound = (
            slps_strings.encode_display(self.translations[first], self.replace_table)
            + self.original[bridge_start:bridge_end]
            + slps_strings.encode_display(self.translations[second], self.replace_table)
        )
        destination, span = patcher.LOAD_TEXT_COMPOUND_DESTINATION
        self.assertEqual(bytes(data[destination:destination + len(compound)]), compound)
        self.assertEqual(data[destination + len(compound)], 0)
        self.assertEqual(
            bytes(data[destination + len(compound):destination + span]),
            b"\0" * (span - len(compound)),
        )
        self.assertEqual(bytes(data[0x002D7980:0x002D8000]), before_sbss_padding)
        self.assertEqual(patcher.visible_cells(self.translations[0x002D6060]), 4)
        self.assertEqual(data[0x002D6070:0x002D6075], bytes.fromhex("02 04 06 06 06"))

    def test_status_centering_uses_actual_pixel_width_only_for_status_window(self):
        data = bytearray(self.original)
        patcher._apply_scoped_menu_half_spaces(data)
        cave, addresses = patcher._build_menu_space_patch()

        hook = addresses["status_center_hook"]
        self.assertLessEqual(len(cave), patcher.MENU_SPACE_CAVE_SIZE)
        self.assertEqual(
            struct.unpack_from("<I", cave, hook - patcher.MENU_SPACE_CAVE_VA + 16)[0],
            patcher._mips_j(0x03, 0x0021AE20),
        )

        scope = addresses["scope_entry"] - patcher.MENU_SPACE_CAVE_VA
        self.assertEqual(
            struct.unpack_from("<I", cave, scope + 8)[0],
            patcher._mips_i(
                0x0F, 0, 8, patcher.MENU_SPACE_SCOPE_FLAG_VA >> 16
            ),
        )
        self.assertEqual(
            struct.unpack_from("<I", cave, scope + 16)[0],
            patcher._mips_i(
                0x2B, 8, 9, patcher.MENU_SPACE_SCOPE_FLAG_VA & 0xFFFF
            ),
        )
        self.assertEqual(
            struct.unpack_from("<I", data, patcher.MENU_SPACE_SBSS_SIZE_OFFSET)[0],
            patcher.MENU_SPACE_SBSS_SIZE[1],
        )

        patch_offset = 0x0027DE98 - patcher.TEXT_VA_DELTA
        self.assertEqual(
            struct.unpack_from("<I", data, patch_offset)[0],
            patcher._mips_j(0x03, hook),
        )
        self.assertEqual(
            bytes(data[patch_offset + 8:patch_offset + 16]),
            struct.pack(
                "<2I",
                patcher._mips_i(0x04, 0, 0, 9),
                patcher._mips_i(0x2B, 9, 3, 4),
            ),
        )

        for va in (0x0027DDD0, 0x0027DFF0, 0x0027E7A0, 0x0027E7E4, 0x0027E9F0):
            offset = va - patcher.TEXT_VA_DELTA
            self.assertEqual(data[offset:offset + 4], self.original[offset:offset + 4])

        scope_call = struct.pack("<I", patcher._mips_j(0x03, addresses["scope_entry"]))
        for va in (
            0x00287D9C,
            0x00287F0C,
            0x002A01C4,
            0x002A8670,
            0x002A8718,
            0x002A87D4,
            0x002A8878,
            0x002A892C,
        ):
            offset = va - patcher.TEXT_VA_DELTA
            self.assertEqual(bytes(data[offset:offset + 4]), scope_call)

        force_call = struct.pack("<I", patcher._mips_j(0x03, addresses["force_entry"]))
        force_offset = 0x0029C0B8 - patcher.TEXT_VA_DELTA
        self.assertEqual(bytes(data[force_offset:force_offset + 4]), force_call)


if __name__ == "__main__":
    unittest.main()
