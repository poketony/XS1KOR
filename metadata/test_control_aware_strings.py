import io
import unittest
from contextlib import redirect_stdout

try:
    from . import euc_scan
except ImportError:
    import euc_scan


def encoded(text):
    return text.encode(euc_scan.ENCODING)


def edit(offset, text, original_length, line=1):
    return euc_scan.TranslationEdit(offset, text, original_length, line)


class ControlAwareStringTests(unittest.TestCase):
    def test_fixed_control_zero_operand_is_not_terminator(self):
        source = encoded("前") + b"\x19\x00" + encoded("後") + b"\x00\x00"
        slots = euc_scan.build_string_slots(source)

        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].raw, source[:-2])
        self.assertEqual(
            euc_scan.raw_to_display(slots[0].raw),
            "前\\x19\\x00後",
        )

    def test_color_packet_can_contain_two_zero_operands(self):
        source = (
            encoded("前")
            + b"\x0c\x80\x00\x00"
            + encoded("後")
            + b"\x00"
        )
        parsed = euc_scan.parse_control_aware_string(source, 0)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.terminator, len(source) - 1)
        self.assertEqual(len(parsed.embedded_zero_offsets), 2)

    def test_variable_08_packet_uses_runtime_flag_widths(self):
        source = (
            encoded("前")
            + b"\x08\x88\x00\x08"
            + encoded("後")
            + b"\x00"
        )
        parsed = euc_scan.parse_control_aware_string(source, 0)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.terminator, len(source) - 1)
        self.assertIn(b"\x08\x88\x00\x08", parsed.control_packets)

    def test_variable_15_packet_uses_length_operand(self):
        source = (
            encoded("前")
            + b"\x15\x02\x00\x08\x20"
            + encoded("後")
            + b"\x00"
        )
        parsed = euc_scan.parse_control_aware_string(source, 0)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.terminator, len(source) - 1)
        self.assertIn(b"\x15\x02\x00\x08\x20", parsed.control_packets)

    def test_emessage_1e_consumes_its_id_byte(self):
        source = b"\x1e\x03" + encoded("決定") + b"\x00"
        parsed = euc_scan.parse_control_aware_string(source, 0)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.terminator, len(source) - 1)
        self.assertEqual(
            euc_scan.raw_to_display(source[:-1]),
            "\\x1e\\x03決定",
        )

    def test_legacy_fragments_are_compacted_as_one_string(self):
        left = encoded("前")
        right = encoded("後")
        source = bytearray(left + b"\x19\x00" + right + b"\x00\x00\x00")
        right_offset = len(left) + 2
        edits = {
            0: edit(0, "短\\x19", len(left) + 1),
            right_offset: edit(right_offset, "続", len(right), 2),
        }

        stats = euc_scan.apply_grouped_translations(
            source,
            [(0, len(source), False)],
            edits,
            {},
            label="test",
        )
        expected = encoded("短") + b"\x19\x00" + encoded("続")

        self.assertEqual(stats.patched_groups, 1)
        self.assertEqual(stats.invalid, 0)
        self.assertEqual(source[:len(expected)], expected)
        parsed = euc_scan.parse_control_aware_string(source, 0)
        self.assertEqual(parsed.terminator, len(expected))

    def test_exposed_legacy_operands_do_not_replace_source_operands(self):
        prefix = encoded("質問")
        suffix = encoded("○")
        logical = prefix + b"\x0c\x80\x20\x20" + suffix
        source = bytearray(logical + b"\x00\x00")
        declared = len(prefix) + 1
        edits = {0: edit(0, "返答\\x0c ", declared)}

        stats = euc_scan.apply_grouped_translations(
            source,
            [(0, len(source), False)],
            edits,
            {},
            label="test",
        )
        translated_prefix = encoded("返答")

        self.assertEqual(stats.patched_groups, 1)
        self.assertEqual(
            source[len(translated_prefix):len(translated_prefix) + 4],
            b"\x0c\x80\x20\x20",
        )

    def test_emessage_fragments_keep_color_and_1e_bridge(self):
        left = encoded("決定")
        right = encoded("取消")
        bridge = b"\x0c\x80\x80\x80\x1e\x03"
        source = bytearray(left + bridge + right + b"\x00\x00")
        right_offset = len(left) + len(bridge)
        edits = {
            0: edit(0, "選択\\x0c", len(left) + 1),
            right_offset: edit(right_offset, "中止", len(right), 2),
        }

        stats = euc_scan.apply_grouped_translations(
            source,
            [(0, len(source), False)],
            edits,
            {},
            label="test",
        )
        expected = encoded("選択") + bridge + encoded("中止")

        self.assertEqual(stats.patched_groups, 1)
        self.assertEqual(source[:len(expected)], expected)
        parsed = euc_scan.parse_control_aware_string(source, 0)
        self.assertEqual(parsed.terminator, len(expected))

    def test_full_logical_record_can_encode_zero_operand(self):
        logical = encoded("前") + b"\x19\x00" + encoded("後")
        source = bytearray(logical + b"\x00\x00")
        edits = {0: edit(0, "短\\x19\\x00続", len(logical))}

        stats = euc_scan.apply_grouped_translations(
            source,
            [(0, len(source), False)],
            edits,
            {},
            label="test",
        )

        self.assertEqual(stats.patched_groups, 1)
        parsed = euc_scan.parse_control_aware_string(source, 0)
        self.assertEqual(parsed.terminator, len(encoded("短")) + 2 + len(encoded("続")))

    def test_overflow_skips_the_complete_logical_group(self):
        logical = encoded("前") + b"\x19\x00" + encoded("後")
        source = bytearray(logical + b"\x00")
        original = bytes(source)
        edits = {0: edit(0, "非常に長い文\\x19", len(encoded("前")) + 1)}

        with redirect_stdout(io.StringIO()):
            stats = euc_scan.apply_grouped_translations(
                source,
                [(0, len(source), False)],
                edits,
                {},
                label="test",
            )

        self.assertEqual(stats.overflow, 1)
        self.assertEqual(bytes(source), original)


if __name__ == "__main__":
    unittest.main()
