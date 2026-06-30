#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


PACK_MAGIC = b"\x00\x00\x01\xBA"
END_MAGIC = b"\x00\x00\x01\xB9"
PREFIX = b"\x00\x00\x01"
PRIVATE_STREAM_1 = 0xBD
TAIL_PACKS = 4
MAX_TAIL_PACKS = 48
DEFAULT_ORIGINAL_ROOT = Path("E:\\\uc81c\ub178\uc0ac\uac001 \uc601\uc0c1 \uc791\uc5c5\uc18c\\original")
NO_PES_HEADER = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}
VIDEO_STREAM_0 = 0xE0
PICTURE_START_CODE = 0x00
GOP_START_CODE = 0xB8


@dataclass(frozen=True)
class Packet:
    offset: int
    end: int
    stream_id: int
    packet_length: int | None
    payload_offset: int | None
    payload_length: int | None

    @property
    def shape(self) -> tuple[int, int | None, int | None, int | None]:
        return (
            self.stream_id,
            self.packet_length,
            self.payload_length,
            None if self.payload_offset is None else self.payload_offset - self.offset,
        )


@dataclass(frozen=True)
class AudioStats:
    ads_len: int
    ssbd_size: int | None
    ads_delta: int | None
    bad_frames: int
    first_bad_frame: int | None
    last_bad_frame: int | None
    sha256: str


def u16be(buf: bytes | bytearray, off: int) -> int:
    return (buf[off] << 8) | buf[off + 1]


def u32le(buf: bytes | bytearray, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


def pack_header_end(buf: bytes | bytearray, off: int) -> int:
    if off + 14 > len(buf):
        return min(len(buf), off + 4)
    if (buf[off + 4] & 0xC0) == 0x40:
        return min(len(buf), off + 14 + (buf[off + 13] & 0x07))
    return min(len(buf), off + 12)


def find_next_prefix(buf: bytes | bytearray, start: int) -> int:
    found = buf.find(PREFIX, start)
    return len(buf) if found < 0 else found


def parse_pes_payload_offset(buf: bytes | bytearray, off: int, end: int, stream_id: int) -> int:
    payload_off = off + 6
    if stream_id in NO_PES_HEADER or end <= payload_off:
        return payload_off
    if payload_off + 3 <= end and (buf[payload_off] & 0xC0) == 0x80:
        return min(end, payload_off + 3 + buf[payload_off + 2])

    # MPEG-1 fallback. This keeps the script usable with older PSS variants.
    p = payload_off
    while p < end and buf[p] == 0xFF:
        p += 1
    if p + 2 <= end and (buf[p] & 0xC0) == 0x40:
        p += 2
    if p + 5 <= end and (buf[p] & 0xF0) == 0x20:
        p += 5
    elif p + 10 <= end and (buf[p] & 0xF0) == 0x30:
        p += 10
    return min(end, p)


def iter_packets(buf: bytes | bytearray) -> list[Packet]:
    packets: list[Packet] = []
    pos = 0
    size = len(buf)
    while pos < size:
        if pos + 4 > size or buf[pos : pos + 3] != PREFIX:
            next_pos = find_next_prefix(buf, pos + 1)
            if next_pos >= size:
                break
            pos = next_pos
            continue

        stream_id = buf[pos + 3]
        if stream_id == 0xBA:
            end = pack_header_end(buf, pos)
            packets.append(Packet(pos, end, stream_id, None, None, None))
            pos = end
            continue
        if stream_id == 0xB9:
            end = pos + 4
            packets.append(Packet(pos, end, stream_id, None, None, None))
            pos = end
            continue
        if pos + 6 > size:
            break

        packet_length = u16be(buf, pos + 4)
        end = find_next_prefix(buf, pos + 6) if packet_length == 0 else min(size, pos + 6 + packet_length)
        payload_off = pos + 6 if stream_id == 0xBB else parse_pes_payload_offset(buf, pos, end, stream_id)
        packets.append(Packet(pos, end, stream_id, packet_length, payload_off, max(0, end - payload_off)))
        pos = end
    return packets


def pack_offsets(buf: bytes | bytearray) -> list[int]:
    offsets: list[int] = []
    pos = 0
    while True:
        found = buf.find(PACK_MAGIC, pos)
        if found < 0:
            return offsets
        offsets.append(found)
        pos = found + 1


def tail_start(buf: bytes | bytearray, tail_packs: int) -> int:
    offsets = pack_offsets(buf)
    if len(offsets) < tail_packs:
        raise ValueError(f"Need at least {tail_packs} pack starts, found {len(offsets)}")
    return offsets[-tail_packs]


def packets_from(buf: bytes | bytearray, start: int) -> list[Packet]:
    return [packet for packet in iter_packets(buf) if packet.offset >= start]


def extract_ads(buf: bytes | bytearray) -> bytes:
    out = bytearray()
    for packet in iter_packets(buf):
        if packet.stream_id != PRIVATE_STREAM_1:
            continue
        if packet.payload_offset is None or packet.payload_length is None or packet.payload_length <= 4:
            continue
        # ps2str PSS ADPCM private stream starts with substream id FF A1 00 00.
        start = packet.payload_offset + 4
        out.extend(buf[start : packet.payload_offset + packet.payload_length])
    return bytes(out)


def audio_stats(buf: bytes | bytearray) -> AudioStats:
    ads = extract_ads(buf)
    digest = hashlib.sha256(ads).hexdigest()
    if len(ads) < 0x28 or ads[:4] != b"SShd" or ads[0x20:0x24] != b"SSbd":
        return AudioStats(len(ads), None, None, 0, None, None, digest)

    ssbd_size = u32le(ads, 0x24)
    expected_len = 0x28 + ssbd_size
    audio_end = min(len(ads), expected_len)
    bad_frames = 0
    first_bad = None
    last_bad = None
    for frame_index in range(max(0, audio_end - 0x28) // 16):
        frame_off = 0x28 + frame_index * 16
        header = ads[frame_off]
        predictor = header >> 4
        shift = header & 0x0F
        if predictor > 4 or shift > 12:
            bad_frames += 1
            if first_bad is None:
                first_bad = frame_index
            last_bad = frame_index
    return AudioStats(len(ads), ssbd_size, len(ads) - expected_len, bad_frames, first_bad, last_bad, digest)


def original_name_from_translated(translated: Path) -> str:
    stem = translated.stem
    lower = stem.lower()
    for marker in ("_lastsector_fixed", "_safe_tail", "_kor", "_fixed"):
        idx = lower.find(marker)
        if idx >= 0:
            stem = stem[:idx]
            lower = stem.lower()
    return f"{stem}.pss"


def find_original(translated: Path, root: Path) -> Path:
    original_name = original_name_from_translated(translated)
    matches = [path for path in root.rglob("*.pss") if path.name.lower() == original_name.lower()]
    if not matches:
        raise FileNotFoundError(f"Original PSS not found under {root}: {original_name}")
    if len(matches) > 1:
        sample = "\n".join(f"  {path}" for path in matches[:20])
        raise ValueError(f"Multiple original PSS matches found for {original_name}:\n{sample}")
    return matches[0]


def output_path_for(translated: Path) -> Path:
    return translated.with_name(f"{translated.stem}_safe_tail{translated.suffix}")


def check_tail_layout(original_tail: list[Packet], translated_tail: list[Packet]) -> None:
    original_shapes = [(p.offset - original_tail[0].offset, p.shape) for p in original_tail]
    translated_shapes = [(p.offset - translated_tail[0].offset, p.shape) for p in translated_tail]
    if original_shapes != translated_shapes:
        for index, (left, right) in enumerate(zip(original_shapes, translated_shapes)):
            if left != right:
                raise ValueError(
                    "Original and translated tail packet layouts differ at tail packet "
                    f"{index}: original={left}, translated={right}"
                )
        raise ValueError(
            "Original and translated tail packet counts differ: "
            f"{len(original_tail)} != {len(translated_tail)}"
        )


def make_padding_packet(total_size: int) -> bytes:
    if total_size < 6:
        raise ValueError(f"Cannot create MPEG padding packet smaller than 6 bytes: {total_size}")
    packet_length = total_size - 6
    if packet_length > 0xFFFF:
        raise ValueError(f"Padding packet too large: {total_size}")
    return PREFIX + b"\xBE" + packet_length.to_bytes(2, "big") + (b"\x00" * packet_length)


def packet_bytes(buf: bytes | bytearray, packet: Packet) -> bytes:
    return bytes(buf[packet.offset : packet.end])


def bd_payload_parts(buf: bytes | bytearray, packet: Packet) -> tuple[bytes, bytes]:
    if packet.payload_offset is None or packet.payload_length is None or packet.payload_length < 4:
        raise ValueError(f"BD packet has no PS2 private subheader at offset 0x{packet.offset:X}")
    payload_end = packet.payload_offset + packet.payload_length
    subheader = bytes(buf[packet.payload_offset : packet.payload_offset + 4])
    audio_data = bytes(buf[packet.payload_offset + 4 : payload_end])
    return subheader, audio_data


def collect_bd_audio_data(buf: bytes | bytearray, packets: list[Packet]) -> tuple[bytes, bytes, int]:
    data = bytearray()
    subheader: bytes | None = None
    packet_count = 0
    for packet in packets:
        if packet.stream_id != PRIVATE_STREAM_1:
            continue
        packet_subheader, packet_data = bd_payload_parts(buf, packet)
        if subheader is None:
            subheader = packet_subheader
        elif packet_subheader != subheader:
            raise ValueError(
                "Mixed BD private subheaders in translated tail: "
                f"{subheader.hex()} != {packet_subheader.hex()} at 0x{packet.offset:X}"
            )
        data.extend(packet_data)
        packet_count += 1
    return bytes(data), (subheader or b"\xFF\xA1\x00\x00"), packet_count


def bd_data_capacity(packet: Packet) -> int:
    if packet.payload_length is None:
        raise ValueError(f"BD packet has no payload length at offset 0x{packet.offset:X}")
    return max(0, packet.payload_length - 4)


def build_bd_packet_from_template(
    *,
    buf: bytes | bytearray,
    packet: Packet,
    subheader: bytes,
    audio_data: bytes,
) -> bytes:
    if packet.payload_offset is None:
        raise ValueError(f"BD packet has no payload offset at 0x{packet.offset:X}")
    packet_size = packet.end - packet.offset
    header_size = packet.payload_offset - packet.offset
    total_size = header_size + len(subheader) + len(audio_data)
    if total_size > packet_size:
        raise ValueError(
            f"Rebuilt BD packet is larger than original slot at 0x{packet.offset:X}: "
            f"{total_size} > {packet_size}"
        )
    if total_size < 6:
        raise ValueError(f"Rebuilt BD packet is too small at 0x{packet.offset:X}: {total_size}")

    header = bytearray(buf[packet.offset : packet.payload_offset])
    header[4:6] = (total_size - 6).to_bytes(2, "big")
    return bytes(header) + subheader + audio_data


def build_audio_preserving_tail(
    *,
    original_buf: bytes | bytearray,
    translated_buf: bytes | bytearray,
    original_tail_start: int,
    translated_tail_start: int,
    original_tail_packets: list[Packet],
    translated_tail_packets: list[Packet],
) -> tuple[bytes, int, int]:
    """Return original tail bytes with translated BD audio data re-packetized.

    The old whole-tail copy corrupts ADPCM when the original and translated
    tail contain BD packets at different relative positions. This function
    keeps original video/end packets, removes original BD/BE packet contents,
    then repartitions translated tail ADPCM data into the original BD slots.
    """
    tail = bytearray(original_buf[original_tail_start:])
    translated_audio_data, subheader, translated_bd_packets = collect_bd_audio_data(
        translated_buf,
        translated_tail_packets,
    )
    remaining = memoryview(translated_audio_data)
    rebuilt_bd_packets = 0

    original_bd_capacity = sum(
        bd_data_capacity(packet)
        for packet in original_tail_packets
        if packet.stream_id == PRIVATE_STREAM_1
    )
    if len(translated_audio_data) > original_bd_capacity:
        raise ValueError(
            "Translated tail ADPCM data does not fit in original tail BD capacity: "
            f"{len(translated_audio_data)} > {original_bd_capacity}"
        )

    original_bd_packets = [packet for packet in original_tail_packets if packet.stream_id == PRIVATE_STREAM_1]
    for bd_index, packet in enumerate(original_bd_packets):
        rel = packet.offset - original_tail_start
        slot_size = packet.end - packet.offset
        if not remaining:
            tail[rel : rel + slot_size] = make_padding_packet(slot_size)
            continue

        chunk_size = min(bd_data_capacity(packet), len(remaining))
        bd_packet = build_bd_packet_from_template(
            buf=original_buf,
            packet=packet,
            subheader=subheader,
            audio_data=remaining[:chunk_size].tobytes(),
        )
        leftover = slot_size - len(bd_packet)
        if 0 < leftover < 6:
            need_to_move = 6 - leftover
            if chunk_size <= need_to_move or bd_index + 1 >= len(original_bd_packets):
                raise ValueError(
                    "Could not split translated tail ADPCM on a legal padding boundary at "
                    f"original tail BD index {bd_index}: leftover={leftover}"
                )
            chunk_size -= need_to_move
            bd_packet = build_bd_packet_from_template(
                buf=original_buf,
                packet=packet,
                subheader=subheader,
                audio_data=remaining[:chunk_size].tobytes(),
            )
            leftover = slot_size - len(bd_packet)

        tail[rel : rel + slot_size] = bd_packet + (make_padding_packet(leftover) if leftover else b"")
        remaining = remaining[chunk_size:]
        rebuilt_bd_packets += 1

    if remaining:
        raise ValueError(f"Translated tail ADPCM data left over after rebuild: {len(remaining)} bytes")

    for packet in original_tail_packets:
        if packet.stream_id != 0xBE:
            continue
        rel = packet.offset - original_tail_start
        slot_size = packet.end - packet.offset
        tail[rel : rel + slot_size] = make_padding_packet(slot_size)

    return bytes(tail), rebuilt_bd_packets, translated_bd_packets


def last_gop_info(buf: bytes | bytearray, max_tail_packs: int) -> tuple[int, int] | None:
    offsets = pack_offsets(buf)
    if not offsets:
        return None

    scan_tail_packs = min(max_tail_packs, len(offsets))
    scan_start = offsets[-scan_tail_packs]
    packets = packets_from(buf, scan_start)
    last_gop_offset: int | None = None

    for packet in packets:
        if packet.stream_id != VIDEO_STREAM_0 or packet.payload_offset is None:
            continue
        payload = buf[packet.payload_offset : packet.end]
        pos = 0
        while True:
            found = payload.find(PREFIX, pos)
            if found < 0:
                break
            if found + 3 < len(payload) and payload[found + 3] == GOP_START_CODE:
                last_gop_offset = packet.payload_offset + found
            pos = found + 1

    if last_gop_offset is None:
        return None

    pack_index = bisect.bisect_right(offsets, last_gop_offset) - 1
    if pack_index < 0:
        return None
    return len(offsets) - pack_index, last_gop_offset


def build_video_packet_from_template(
    *,
    buf: bytes | bytearray,
    packet: Packet,
    start: int,
    end: int,
) -> bytes:
    if packet.payload_offset is None:
        raise ValueError(f"Video packet has no payload offset at 0x{packet.offset:X}")
    header_size = packet.payload_offset - packet.offset
    if start + header_size > end:
        raise ValueError(f"Video packet split is too small at 0x{packet.offset:X}")
    header = bytearray(buf[packet.offset : packet.payload_offset])
    packet_size = end - start
    header[4:6] = (packet_size - 6).to_bytes(2, "big")
    return bytes(header) + bytes(buf[start + header_size : end])


def build_video_packet_with_payload(
    *,
    buf: bytes | bytearray,
    packet: Packet,
    payload: bytes,
) -> bytes:
    if packet.payload_offset is None:
        raise ValueError(f"Video packet has no payload offset at 0x{packet.offset:X}")
    header = bytearray(buf[packet.offset : packet.payload_offset])
    packet_size = len(header) + len(payload)
    if packet_size < 6:
        raise ValueError(f"Video packet is too small at 0x{packet.offset:X}: {packet_size}")
    header[4:6] = (packet_size - 6).to_bytes(2, "big")
    return bytes(header) + payload


def collect_original_video_from_gop(
    *,
    original_buf: bytes | bytearray,
    original_tail_packets: list[Packet],
    gop_file_offset: int,
) -> bytes:
    video = bytearray()
    for packet in original_tail_packets:
        if packet.stream_id != VIDEO_STREAM_0 or packet.payload_offset is None:
            continue
        start = max(packet.payload_offset, gop_file_offset)
        if start < packet.end:
            video.extend(original_buf[start : packet.end])
    return bytes(video)


def translated_video_insert_index(
    *,
    translated_packets: list[Packet],
    video_data_len: int,
) -> int | None:
    video_packets = [
        (index, packet)
        for index, packet in enumerate(translated_packets)
        if packet.stream_id == VIDEO_STREAM_0 and packet.payload_offset is not None
    ]
    capacity = 0
    for index, packet in reversed(video_packets):
        capacity += packet.payload_length or 0
        if capacity >= video_data_len:
            return index
    return None


def build_translated_map_video_tail(
    *,
    original_buf: bytes | bytearray,
    translated_buf: bytes | bytearray,
    original_tail_packets: list[Packet],
    translated_tail_start: int,
    translated_tail_packets: list[Packet],
    gop_file_offset: int,
) -> tuple[bytes, int, int]:
    tail = bytearray(translated_buf[translated_tail_start:])
    original_video = collect_original_video_from_gop(
        original_buf=original_buf,
        original_tail_packets=original_tail_packets,
        gop_file_offset=gop_file_offset,
    )
    insert_index = translated_video_insert_index(
        translated_packets=translated_tail_packets,
        video_data_len=len(original_video),
    )
    if insert_index is None:
        translated_capacity = sum(
            packet.payload_length or 0
            for packet in translated_tail_packets
            if packet.stream_id == VIDEO_STREAM_0 and packet.payload_offset is not None
        )
        raise ValueError(
            "Original GOP video does not fit in translated tail E0 capacity: "
            f"{len(original_video)} > {translated_capacity}"
        )

    remaining = memoryview(original_video)
    rebuilt_video_packets = 0
    translated_video_packets = sum(
        1
        for packet in translated_tail_packets[insert_index:]
        if packet.stream_id == VIDEO_STREAM_0 and packet.payload_offset is not None
    )

    for packet in translated_tail_packets[insert_index:]:
        if packet.stream_id != VIDEO_STREAM_0 or packet.payload_offset is None:
            continue

        rel = packet.offset - translated_tail_start
        slot_size = packet.end - packet.offset
        capacity = packet.payload_length or 0
        if not remaining:
            tail[rel : rel + slot_size] = make_padding_packet(slot_size)
            continue

        chunk_size = min(capacity, len(remaining))
        video_packet = build_video_packet_with_payload(
            buf=translated_buf,
            packet=packet,
            payload=remaining[:chunk_size].tobytes(),
        )
        leftover = slot_size - len(video_packet)
        if 0 < leftover < 6:
            need_to_move = 6 - leftover
            if chunk_size <= need_to_move:
                raise ValueError(
                    "Could not split original GOP video on a legal padding boundary at "
                    f"translated E0 offset 0x{packet.offset:X}: leftover={leftover}"
                )
            chunk_size -= need_to_move
            video_packet = build_video_packet_with_payload(
                buf=translated_buf,
                packet=packet,
                payload=remaining[:chunk_size].tobytes(),
            )
            leftover = slot_size - len(video_packet)

        tail[rel : rel + slot_size] = video_packet + (make_padding_packet(leftover) if leftover else b"")
        remaining = remaining[chunk_size:]
        rebuilt_video_packets += 1

    if remaining:
        raise ValueError(f"Original GOP video left over after translated-map rebuild: {len(remaining)} bytes")

    return bytes(tail), rebuilt_video_packets, translated_video_packets


def suppress_video_before_gop(
    *,
    tail_bytes: bytes,
    original_buf: bytes | bytearray,
    original_tail_start: int,
    original_tail_packets: list[Packet],
    gop_file_offset: int | None,
) -> bytes:
    if gop_file_offset is None:
        return tail_bytes

    tail = bytearray(tail_bytes)
    for packet in original_tail_packets:
        if packet.stream_id != VIDEO_STREAM_0:
            continue
        rel = packet.offset - original_tail_start
        size = packet.end - packet.offset
        if packet.end <= gop_file_offset:
            tail[rel : rel + size] = make_padding_packet(size)
            continue
        if packet.offset < gop_file_offset < packet.end:
            if packet.payload_offset is None:
                raise ValueError(f"GOP is inside a video packet without payload offset at 0x{packet.offset:X}")
            header_size = packet.payload_offset - packet.offset
            new_packet_start = gop_file_offset - header_size
            if new_packet_start <= packet.offset:
                return bytes(tail)
            padding_size = new_packet_start - packet.offset
            if padding_size < 6:
                raise ValueError(
                    "Cannot suppress original pre-GOP video without an illegal tiny padding packet: "
                    f"padding_size={padding_size}"
                )
            rel_padding_end = rel + padding_size
            tail[rel:rel_padding_end] = make_padding_packet(padding_size)
            tail[rel_padding_end : rel + size] = build_video_packet_from_template(
                buf=original_buf,
                packet=packet,
                start=new_packet_start,
                end=packet.end,
            )
            return bytes(tail)
        return bytes(tail)

    return bytes(tail)


def choose_tail_layout(
    *,
    original_buf: bytes | bytearray,
    translated_buf: bytes | bytearray,
    min_tail_packs: int,
    max_tail_packs: int,
    sync_to_gop: bool,
) -> tuple[int, int | None, int | None, int, int, bytes, list[Packet], list[Packet]]:
    if max_tail_packs < min_tail_packs:
        raise ValueError(f"--max-tail-packs must be >= --tail-packs: {max_tail_packs} < {min_tail_packs}")

    gop_info = last_gop_info(original_buf, max_tail_packs) if sync_to_gop else None
    gop_tail_packs = None if gop_info is None else gop_info[0]
    gop_file_offset = None if gop_info is None else gop_info[1]
    first_tail_packs = max(min_tail_packs, gop_tail_packs or min_tail_packs)
    rejected: list[str] = []
    for candidate_tail_packs in range(first_tail_packs, max_tail_packs + 1):
        original_tail_start = tail_start(original_buf, candidate_tail_packs)
        translated_tail_start = tail_start(translated_buf, candidate_tail_packs)
        original_tail_bytes = original_buf[original_tail_start:]
        translated_tail_size = len(translated_buf) - translated_tail_start
        if len(original_tail_bytes) != translated_tail_size:
            rejected.append(
                f"{candidate_tail_packs}: tail size {len(original_tail_bytes)} != {translated_tail_size}"
            )
            continue

        original_tail_packets = packets_from(original_buf, original_tail_start)
        translated_tail_packets = packets_from(translated_buf, translated_tail_start)
        translated_audio_data, _subheader, _packet_count = collect_bd_audio_data(
            translated_buf,
            translated_tail_packets,
        )
        original_bd_capacity = sum(
            bd_data_capacity(packet)
            for packet in original_tail_packets
            if packet.stream_id == PRIVATE_STREAM_1
        )
        if len(translated_audio_data) <= original_bd_capacity:
            return (
                candidate_tail_packs,
                gop_tail_packs,
                gop_file_offset,
                original_tail_start,
                translated_tail_start,
                bytes(original_tail_bytes),
                original_tail_packets,
                translated_tail_packets,
            )

        rejected.append(
            f"{candidate_tail_packs}: translated tail ADPCM {len(translated_audio_data)} "
            f"> original BD capacity {original_bd_capacity}"
        )

    raise ValueError(
        "Translated tail ADPCM data does not fit in original tail BD capacity within auto-expand limit. "
        f"Tried tail packs {first_tail_packs}..{max_tail_packs}: " + "; ".join(rejected)
    )


def choose_translated_map_layout(
    *,
    original_buf: bytes | bytearray,
    translated_buf: bytes | bytearray,
    min_tail_packs: int,
    max_tail_packs: int,
) -> tuple[int, int, int, int, int, bytes, list[Packet], list[Packet]]:
    gop_info = last_gop_info(original_buf, max_tail_packs)
    if gop_info is None:
        raise ValueError("Cannot use translated-map fallback because no original GOP was found.")

    gop_tail_packs, gop_file_offset = gop_info
    first_tail_packs = max(min_tail_packs, gop_tail_packs)
    rejected: list[str] = []
    for candidate_tail_packs in range(first_tail_packs, max_tail_packs + 1):
        original_tail_start = tail_start(original_buf, candidate_tail_packs)
        translated_tail_start = tail_start(translated_buf, candidate_tail_packs)
        original_tail_size = len(original_buf) - original_tail_start
        translated_tail_size = len(translated_buf) - translated_tail_start
        if original_tail_size != translated_tail_size:
            rejected.append(f"{candidate_tail_packs}: tail size {original_tail_size} != {translated_tail_size}")
            continue

        original_tail_packets = packets_from(original_buf, original_tail_start)
        translated_tail_packets = packets_from(translated_buf, translated_tail_start)
        original_video = collect_original_video_from_gop(
            original_buf=original_buf,
            original_tail_packets=original_tail_packets,
            gop_file_offset=gop_file_offset,
        )
        insert_index = translated_video_insert_index(
            translated_packets=translated_tail_packets,
            video_data_len=len(original_video),
        )
        if insert_index is not None:
            return (
                candidate_tail_packs,
                gop_tail_packs,
                gop_file_offset,
                original_tail_start,
                translated_tail_start,
                bytes(translated_buf[translated_tail_start:]),
                original_tail_packets,
                translated_tail_packets,
            )

        translated_capacity = sum(
            packet.payload_length or 0
            for packet in translated_tail_packets
            if packet.stream_id == VIDEO_STREAM_0 and packet.payload_offset is not None
        )
        rejected.append(
            f"{candidate_tail_packs}: original GOP video {len(original_video)} "
            f"> translated E0 capacity {translated_capacity}"
        )

    raise ValueError(
        "Original GOP video does not fit in translated tail E0 capacity within auto-expand limit. "
        f"Tried tail packs {first_tail_packs}..{max_tail_packs}: " + "; ".join(rejected)
    )


def safe_tail_graft(
    original: Path,
    translated: Path,
    output: Path,
    *,
    tail_packs: int,
    max_tail_packs: int,
    sync_to_gop: bool,
    overwrite: bool,
    allow_oversize_input: bool,
    allow_bad_input_audio: bool,
) -> dict[str, object]:
    if original.resolve() == translated.resolve():
        raise ValueError("Original and translated paths are the same file.")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists. Use --overwrite: {output}")

    original_buf = bytearray(original.read_bytes())
    translated_buf = bytearray(translated.read_bytes())
    original_size = len(original_buf)
    translated_size = len(translated_buf)
    if translated_size > original_size and not allow_oversize_input:
        raise ValueError(
            "Translated PSS is larger than original. Refusing to create an unrebuildable output: "
            f"{translated_size} > {original_size}. Re-encode/remux smaller, or pass --allow-oversize-input "
            "only for diagnostics."
        )

    translated_audio_before = audio_stats(translated_buf)
    if translated_audio_before.bad_frames and not allow_bad_input_audio:
        raise ValueError(
            "Translated input already has invalid ADPCM tail frames. Run this script on the ps2str mux output "
            "before the old last-sector patch, or pass --allow-bad-input-audio for diagnostics. "
            f"bad_frames={translated_audio_before.bad_frames}"
        )

    graft_mode = "original-tail-audio-rebuilt"
    fallback_reason: str | None = None
    rebuilt_video_packets: int | None = None
    translated_video_packets: int | None = None

    try:
        (
            selected_tail_packs,
            gop_tail_packs,
            gop_file_offset,
            original_tail_start,
            translated_tail_start,
            original_tail_bytes,
            original_tail_packets,
            translated_tail_packets,
        ) = choose_tail_layout(
            original_buf=original_buf,
            translated_buf=translated_buf,
            min_tail_packs=tail_packs,
            max_tail_packs=max_tail_packs,
            sync_to_gop=sync_to_gop,
        )
        if not original_tail_bytes.endswith(END_MAGIC):
            raise ValueError("Original tail does not end with 00 00 01 B9.")

        rebuilt_tail_bytes, inserted_bd_packets, translated_tail_bd_packets = build_audio_preserving_tail(
            original_buf=original_buf,
            translated_buf=translated_buf,
            original_tail_start=original_tail_start,
            translated_tail_start=translated_tail_start,
            original_tail_packets=original_tail_packets,
            translated_tail_packets=translated_tail_packets,
        )
        if sync_to_gop:
            rebuilt_tail_bytes = suppress_video_before_gop(
                tail_bytes=rebuilt_tail_bytes,
                original_buf=original_buf,
                original_tail_start=original_tail_start,
                original_tail_packets=original_tail_packets,
                gop_file_offset=gop_file_offset,
            )
    except ValueError as primary_error:
        if not sync_to_gop:
            raise
        fallback_reason = str(primary_error)
        graft_mode = "translated-map-video-rebuilt"
        (
            selected_tail_packs,
            gop_tail_packs,
            gop_file_offset,
            original_tail_start,
            translated_tail_start,
            original_tail_bytes,
            original_tail_packets,
            translated_tail_packets,
        ) = choose_translated_map_layout(
            original_buf=original_buf,
            translated_buf=translated_buf,
            min_tail_packs=tail_packs,
            max_tail_packs=max_tail_packs,
        )
        if not original_tail_bytes.endswith(END_MAGIC):
            raise ValueError("Translated tail does not end with 00 00 01 B9.")
        rebuilt_tail_bytes, rebuilt_video_packets, translated_video_packets = build_translated_map_video_tail(
            original_buf=original_buf,
            translated_buf=translated_buf,
            original_tail_packets=original_tail_packets,
            translated_tail_start=translated_tail_start,
            translated_tail_packets=translated_tail_packets,
            gop_file_offset=gop_file_offset,
        )
        inserted_bd_packets = sum(1 for packet in translated_tail_packets if packet.stream_id == PRIVATE_STREAM_1)
        translated_tail_bd_packets = inserted_bd_packets

    output_buf = bytearray(translated_buf)
    output_buf[translated_tail_start:] = rebuilt_tail_bytes

    if len(output_buf) > original_size:
        raise ValueError(
            f"Output would be larger than original: {len(output_buf)} > {original_size}. "
            "No file was written."
        )
    if not output_buf.endswith(END_MAGIC):
        raise ValueError("Output does not end with 00 00 01 B9 after safe tail graft.")

    translated_audio_after = audio_stats(output_buf)
    if translated_audio_before.sha256 != translated_audio_after.sha256:
        raise ValueError("ADPCM stream changed during safe tail graft. Refusing output.")
    if translated_audio_after.bad_frames and not allow_bad_input_audio:
        raise ValueError(f"Output ADPCM contains invalid frames: {translated_audio_after.bad_frames}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(output_buf)

    return {
        "original": str(original),
        "translated": str(translated),
        "output": str(output),
        "original_size": original_size,
        "translated_size": translated_size,
        "output_size": len(output_buf),
        "requested_tail_packs": tail_packs,
        "tail_packs": selected_tail_packs,
        "max_tail_packs": max_tail_packs,
        "gop_tail_packs": gop_tail_packs,
        "gop_file_offset": gop_file_offset,
        "gop_tail_offset": None if gop_file_offset is None else gop_file_offset - original_tail_start,
        "sync_to_gop": sync_to_gop,
        "original_tail_start": original_tail_start,
        "translated_tail_start": translated_tail_start,
        "tail_size": len(original_tail_bytes),
        "inserted_bd_packets": inserted_bd_packets,
        "translated_tail_bd_packets": translated_tail_bd_packets,
        "rebuilt_video_packets": rebuilt_video_packets,
        "translated_video_packets": translated_video_packets,
        "graft_mode": graft_mode,
        "fallback_reason": fallback_reason,
        "audio_sha256": translated_audio_after.sha256,
        "audio_ads_len": translated_audio_after.ads_len,
        "audio_ssbd_size": translated_audio_after.ssbd_size,
        "audio_ads_delta": translated_audio_after.ads_delta,
        "audio_bad_frames": translated_audio_after.bad_frames,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copy the original final PSS tail for video/end behavior while preserving translated ADPCM packets. "
            "Use this instead of the old whole-tail copy step."
        )
    )
    parser.add_argument(
        "paths",
        type=Path,
        nargs="+",
        help="Either TRANSLATED.pss, or ORIGINAL.pss TRANSLATED.pss.",
    )
    parser.add_argument("--original-root", type=Path, default=DEFAULT_ORIGINAL_ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--tail-packs", type=int, default=TAIL_PACKS)
    parser.add_argument(
        "--max-tail-packs",
        type=int,
        default=MAX_TAIL_PACKS,
        help="Auto-expand the final graft window up to this many packs when the default tail has too little BD capacity.",
    )
    parser.add_argument(
        "--no-gop-sync",
        action="store_true",
        help="Do not expand the final graft window to include the last original MPEG GOP start.",
    )
    parser.add_argument(
        "--allow-oversize-input",
        action="store_true",
        help="Diagnostic only. Output is still refused if it would exceed original size.",
    )
    parser.add_argument(
        "--allow-bad-input-audio",
        action="store_true",
        help="Diagnostic only. Correct pipeline input should not already have invalid ADPCM frames.",
    )
    parser.add_argument("--json-report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.paths) == 1:
        translated = args.paths[0].resolve()
        original = find_original(translated, args.original_root.resolve()).resolve()
    elif len(args.paths) == 2:
        original = args.paths[0].resolve()
        translated = args.paths[1].resolve()
    else:
        raise ValueError("Expected either TRANSLATED.pss or ORIGINAL.pss TRANSLATED.pss.")

    output = args.out.resolve() if args.out else output_path_for(translated).resolve()
    report = safe_tail_graft(
        original,
        translated,
        output,
        tail_packs=args.tail_packs,
        max_tail_packs=args.max_tail_packs,
        sync_to_gop=not args.no_gop_sync,
        overwrite=args.overwrite,
        allow_oversize_input=args.allow_oversize_input,
        allow_bad_input_audio=args.allow_bad_input_audio,
    )

    print(f"Original      : {report['original']}")
    print(f"Translated    : {report['translated']}")
    print(f"Output        : {report['output']}")
    print(f"Size          : {report['output_size']} / original {report['original_size']} bytes")
    print(f"Mode          : {report['graft_mode']}")
    print(f"Tail graft    : last {report['tail_packs']} packs, {report['tail_size']} bytes")
    if report["gop_tail_packs"]:
        print(f"Video sync    : last GOP within {report['gop_tail_packs']} packs")
    if report["gop_tail_offset"] is not None:
        print(f"GOP offset    : tail + {report['gop_tail_offset']} bytes")
    if report["tail_packs"] != report["requested_tail_packs"]:
        print(f"Tail expanded : {report['requested_tail_packs']} -> {report['tail_packs']} packs")
    print(f"BD rebuilt    : {report['inserted_bd_packets']} / {report['translated_tail_bd_packets']} packet(s)")
    if report["rebuilt_video_packets"] is not None:
        print(f"Video rebuilt : {report['rebuilt_video_packets']} / {report['translated_video_packets']} packet(s)")
    print(f"Audio hash    : unchanged ({str(report['audio_sha256'])[:16]}...)")
    print(f"ADPCM check   : bad_frames={report['audio_bad_frames']}, ads_delta={report['audio_ads_delta']}")
    print("End code      : verified 00 00 01 B9")

    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON report   : {args.json_report.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
