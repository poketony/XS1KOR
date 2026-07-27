import os
import unittest

try:
    from . import ov11_elf_strings as ov11
except ImportError:
    import ov11_elf_strings as ov11


HERE = os.path.dirname(os.path.abspath(__file__))
OV11 = os.path.join(HERE, "OV11.OVL")


class Ov11ElfStringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.records = ov11.discover_records(OV11)

    def test_discovers_only_referenced_plain_text_and_complete_streams(self):
        plain = [record for record in self.records if record.kind == "N"]
        streams = [record for record in self.records if record.kind == "S"]

        self.assertEqual(len(plain), 18)
        self.assertEqual(len(streams), 60)
        self.assertEqual(streams[0].offset, 0xD680)
        self.assertEqual(streams[-1].slot_end, 0xE9F8)

    def test_old_header_fragments_are_not_records(self):
        offsets = {record.offset for record in self.records}

        self.assertNotIn(0xD7DB, offsets)
        self.assertNotIn(0xDC53, offsets)
        self.assertNotIn(0xDF3B, offsets)
        self.assertIn(0xD7D8, offsets)

    def test_title_keeps_zero_valued_control_operand(self):
        record = next(item for item in self.records if item.offset == 0xD7D8)
        display = ov11.record_to_display(record)

        self.assertIn("シオン", display)
        self.assertIn("\\x19\\x00", display)
        parsed = ov11.euc_scan.parse_control_aware_string(record.raw + b"\x00", 0)
        self.assertEqual(parsed.terminator, len(record.raw))

    def test_source_dump_round_trips_without_byte_changes(self):
        for record in self.records:
            encoded = ov11.encode_record_text(
                record, ov11.record_to_display(record), {},
            )
            ov11.validate_encoded_record(record, encoded)
            self.assertEqual(encoded, record.raw)


if __name__ == "__main__":
    unittest.main()
