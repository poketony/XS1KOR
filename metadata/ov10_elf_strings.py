#!/usr/bin/env python3
"""
OV10.OVL symbol-aware text extractor.

OV10 is an ELF32/MIPS overlay with a live symbol table. This extractor uses
symbol starts instead of blind EUC scans, so help-message control packets such
as 0c ... 0b 0d 00 are preserved as hex tokens instead of being decoded as
text.

Usage:
  python ov10_elf_strings.py extract OV10.OVL
  python ov10_elf_strings.py analyze OV10.OVL
  python ov10_elf_strings.py migrate OV10.OVL OV10_strings.txt
  python ov10_elf_strings.py rebuild OV10.OVL OV10_elf_strings.txt [out.OVL]
"""

import json
import os
import re
import struct
import sys
from collections import Counter, defaultdict

ENCODING = 'euc_jis_2004'
OV10_SECTION = 'ov10'
TEXT_TOKEN_RE = re.compile(r'(\{[^}]*\}|\\[rn]|\\x[0-9a-fA-F]{2})')
CTRL0C_TOKEN_RE = re.compile(r'\{CTRL0C:[^|}]*\|([^}]*)\}')
ANON_TEXT_SCAN_START = 0x4c5f8
ANON_TEXT_SCAN_END = 0x50410
KOR_UI_PATCHES = (
    # NewCMPGameModeExit uses fixed left-edge X positions for this menu:
    # ヘルプ=226, 進行設定=216, フェイズ終了=196, ゲーム終了=206.
    # Korean "진행　설정" is 5 fullwidth cells, so it needs the same X as
    # ゲーム終了 instead of the original 4-cell 進行設定 position.
    (0x3218, bytes.fromhex('d8 00 04 24'), bytes.fromhex('ce 00 04 24')),
    # Card sale result line is printed in fragments:
    #   <count> + count-suffix + <money> + sale-result-text.
    # Add a visual gap after the count suffix by moving the whole money block right.
    (0x1ea04, bytes.fromhex('68 00 04 24'), bytes.fromhex('70 00 04 24')),
    (0x1ea74, bytes.fromhex('7c 00 04 24'), bytes.fromhex('84 00 04 24')),
    (0x1eae0, bytes.fromhex('7c 00 04 24'), bytes.fromhex('84 00 04 24')),
    (0x1eb90, bytes.fromhex('90 00 04 24'), bytes.fromhex('98 00 04 24')),
    (0x1ebe0, bytes.fromhex('90 00 04 24'), bytes.fromhex('98 00 04 24')),
    (0x1ebf8, bytes.fromhex('a4 00 04 24'), bytes.fromhex('ac 00 04 24')),
)

# CardDeckDisp and CardListDisp copy each card name into a temporary stack
# buffer before drawing it. Convert only ASCII spaces in those two temporary
# buffers to the SLPS menu-spacing sentinel (0x7f). The source card data,
# counts, headers, effects, and every other OV10 text path remain unchanged.
# This requires the matching SLPS build produced by patch_slps_menu_spacing.py,
# whose font hook renders 0x7f as an exact blank glyph with an 8-pixel advance.
CARD_LIST_SPACE_PATCHES = (
    (
        'CardDeckDisp reserve s6 for the spacing sentinel',
        0x9a74,
        bytes.fromhex('20 00 b6 27'),
        bytes.fromhex('7f 00 16 24'),
    ),
    (
        'CardDeckDisp copy card name with 8-pixel ASCII spaces',
        0x9b10,
        bytes.fromhex(
            '08 00 a0 10 2d 30 00 00 21 10 26 02 21 20 a6 03 '
            '00 00 43 90 01 00 c6 24 2a 10 c5 00 fa ff 40 14 '
            '00 00 83 a0 21 10 a6 03 f0 ff 06 34 08 00 05 86 '
            '2d 38 a0 03 06 00 04 86 00 00 40 a0'
        ),
        bytes.fromhex(
            '09 00 a0 10 2d 20 a0 03 00 00 23 92 01 00 31 26 '
            '20 00 62 38 0a 18 c2 02 ff ff a5 24 00 00 83 a0 '
            'f9 ff a0 14 01 00 84 24 00 00 80 a0 f0 ff 06 34 '
            '08 00 05 86 2d 38 a0 03 06 00 04 86'
        ),
    ),
    (
        'CardDeckDisp recover count scratch pointer for sprintf',
        0x9ba0,
        bytes.fromhex('2d 20 c0 02'),
        bytes.fromhex('20 00 a4 27'),
    ),
    (
        'CardDeckDisp recover count scratch pointer for drawing',
        0x9bc8,
        bytes.fromhex('2d 38 c0 02'),
        bytes.fromhex('20 00 a7 27'),
    ),
    (
        'CardListDisp reserve s8 for the spacing sentinel',
        0xdccc,
        bytes.fromhex('a5 00 1e 3c'),
        bytes.fromhex('7f 00 1e 24'),
    ),
    (
        'CardListDisp make marker base available on both branch paths',
        0xdd3c,
        bytes.fromhex('08 00 64 56'),
        bytes.fromhex('08 00 64 16'),
    ),
    (
        'CardListDisp selected marker base before card-name copy',
        0xdd54,
        bytes.fromhex('b8 cb c7 27'),
        bytes.fromhex('b8 cb 47 24'),
    ),
    (
        'CardListDisp copy card name with 8-pixel ASCII spaces',
        0xdd78,
        bytes.fromhex(
            '08 00 a0 10 2d 30 00 00 21 10 26 02 21 20 a6 03 '
            '00 00 43 90 01 00 c6 24 2a 10 c5 00 fa ff 40 14 '
            '00 00 83 a0 21 10 a6 03 f0 ff 06 34 08 00 05 86 '
            '2d 38 a0 03 06 00 04 86 00 00 40 a0'
        ),
        bytes.fromhex(
            '09 00 a0 10 2d 20 a0 03 00 00 23 92 01 00 31 26 '
            '20 00 62 38 0a 18 c2 03 ff ff a5 24 00 00 83 a0 '
            'f9 ff a0 14 01 00 84 24 00 00 80 a0 f0 ff 06 34 '
            '08 00 05 86 2d 38 a0 03 06 00 04 86'
        ),
    ),
    (
        'CardListDisp selected count marker base',
        0xde14,
        bytes.fromhex('b8 cb c7 27'),
        bytes.fromhex('b8 cb 47 24'),
    ),
    (
        'CardListDisp selected unavailable-card marker base',
        0xde80,
        bytes.fromhex('b8 cb c7 27'),
        bytes.fromhex('b8 cb 67 24'),
    ),
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')


def zstr(buf, offset):
    end = buf.find(b'\x00', offset)
    if end < 0:
        end = len(buf)
    return buf[offset:end].decode('ascii', errors='replace')


def read_elf(path):
    data = open(path, 'rb').read()
    if data[:4] != b'\x7fELF' or data[4] != 1 or data[5] != 1:
        raise ValueError('expected little-endian ELF32')

    hdr = struct.unpack_from('<HHIIIIIHHHHHH', data, 16)
    keys = (
        'e_type', 'e_machine', 'e_version', 'e_entry', 'e_phoff', 'e_shoff',
        'e_flags', 'e_ehsize', 'e_phentsize', 'e_phnum', 'e_shentsize',
        'e_shnum', 'e_shstrndx',
    )
    elf = dict(zip(keys, hdr))

    phdrs = []
    for i in range(elf['e_phnum']):
        off = elf['e_phoff'] + i * elf['e_phentsize']
        vals = struct.unpack_from('<IIIIIIII', data, off)
        phdrs.append(dict(zip(
            ('p_type', 'p_offset', 'p_vaddr', 'p_paddr',
             'p_filesz', 'p_memsz', 'p_flags', 'p_align'),
            vals,
        )))

    shdrs = []
    for i in range(elf['e_shnum']):
        off = elf['e_shoff'] + i * elf['e_shentsize']
        vals = struct.unpack_from('<IIIIIIIIII', data, off)
        shdrs.append(dict(zip(
            ('sh_name', 'sh_type', 'sh_flags', 'sh_addr', 'sh_offset',
             'sh_size', 'sh_link', 'sh_info', 'sh_addralign', 'sh_entsize'),
            vals,
        )))

    shstr = shdrs[elf['e_shstrndx']]
    shstrtab = data[shstr['sh_offset']:shstr['sh_offset'] + shstr['sh_size']]
    for sh in shdrs:
        sh['name'] = zstr(shstrtab, sh['sh_name']) if sh['sh_name'] else ''

    return data, elf, phdrs, shdrs


def vaddr_to_offset(phdrs, vaddr):
    for ph in phdrs:
        if ph['p_type'] != 1:
            continue
        lo = ph['p_vaddr']
        hi = lo + ph['p_filesz']
        if lo <= vaddr < hi:
            return ph['p_offset'] + (vaddr - lo)
    return None


def read_symbols(data, phdrs, shdrs):
    symtab = next((s for s in shdrs if s['name'] == '.symtab'), None)
    strtab = next((s for s in shdrs if s['name'] == '.strtab'), None)
    if not symtab or not strtab:
        raise ValueError('ELF symbol/string table not found')

    names = data[strtab['sh_offset']:strtab['sh_offset'] + strtab['sh_size']]
    syms = []
    count = symtab['sh_size'] // symtab['sh_entsize']
    for i in range(count):
        off = symtab['sh_offset'] + i * symtab['sh_entsize']
        st_name, st_value, st_size, st_info, st_other, st_shndx = (
            struct.unpack_from('<IIIBBH', data, off)
        )
        if not st_value or st_shndx == 0:
            continue
        file_off = vaddr_to_offset(phdrs, st_value)
        if file_off is None:
            continue
        syms.append({
            'name': zstr(names, st_name),
            'vaddr': st_value,
            'offset': file_off,
            'size': st_size,
            'info': st_info,
            'shndx': st_shndx,
        })
    return sorted(syms, key=lambda s: (s['offset'], s['name']))


def is_text_symbol(sym):
    name = sym['name']
    return (
        (sym['info'] & 0x0f) == 1
        and sym['size'] == 0
        and (
            name.endswith('_txt')
            or 'Txt' in name
            or name in ('P1DeckLoadStr', 'P2DeckLoadStr')
        )
    )


def normal_text_start(data, start, limit):
    if data[start:start + 3] == b'\x0b\x0d\x00' and start + 3 < limit:
        return start + 3, data[start:start + 3]
    if data[start:start + 2] == b'\x0d\x00' and start + 2 < limit:
        return start + 2, data[start:start + 2]
    return start, b''


def prefix_display(prefix):
    if not prefix:
        return '-'
    return prefix.hex()


def is_stream_symbol(name):
    return name.startswith('Help') and name.endswith('_txt')


def should_extract_symbol(sym):
    name = sym['name']
    return (
        is_text_symbol(sym)
        or name in ('P1DeckLoadStr', 'P2DeckLoadStr')
    )


def next_symbol_offset(symbols, index, data_len):
    cur = symbols[index]['offset']
    for nxt in symbols[index + 1:]:
        if nxt['offset'] > cur:
            return nxt['offset']
    return data_len


def bytes_hex(raw):
    return raw.hex(' ')


def decode_euc_char(raw, pos):
    b = raw[pos]
    if b == 0x8f:
        size = 3
    elif b == 0x8e or 0xa1 <= b <= 0xfe:
        size = 2
    else:
        return None, 0

    chunk = raw[pos:pos + size]
    if len(chunk) != size:
        return None, 0
    try:
        return chunk.decode(ENCODING), size
    except UnicodeDecodeError:
        return None, 0


def strip_display_tokens(text):
    text = CTRL0C_TOKEN_RE.sub(lambda match: match.group(1), text)
    return TEXT_TOKEN_RE.sub('', text)


def has_display_text(text):
    plain = strip_display_tokens(text)
    return any(ord(ch) >= 0x80 for ch in plain)


def payload_to_display(raw):
    return raw_to_display(raw)


def record_payload_to_display(data, start, limit):
    out = []
    text = []
    i = start

    def flush_text():
        if text:
            out.append(''.join(text))
            text.clear()

    while i < limit:
        b = data[i]
        if b == 0x00:
            flush_text()
            return i, ''.join(out)

        if data[i:i + 2] == b'\x0d\x00' and i + 2 < limit and data[i + 2] == 0x0c:
            flush_text()
            out.append('{0D00}')
            i += 2
            continue

        if data[i] == 0x0d and i + 1 < limit and data[i + 1] in (0x02, 0x04):
            flush_text()
            out.append(f'{{0D{data[i + 1]:02X}}}')
            i += 2
            continue

        if b == 0x0c:
            flush_text()
            marker = data.find(b'\x0b\x0d\x00', i + 4, limit)
            first_nul = data.find(b'\x00', i + 1, limit)
            if marker >= 0 and (first_nul < 0 or first_nul >= marker + 2):
                header = data[i + 1:i + 4]
                payload = data[i + 4:marker]
                if len(header) == 3:
                    out.append(
                        '{CTRL0C:' + bytes_hex(header) + '|'
                        + payload_to_display(payload) + '}'
                    )
                    i = marker + 3
                    continue
            if first_nul >= 0 and first_nul >= i + 4:
                header = data[i + 1:i + 4]
                payload = data[i + 4:first_nul]
                if len(header) == 3:
                    out.append(
                        '{CTRL0C:' + bytes_hex(header) + '|'
                        + payload_to_display(payload) + '}'
                    )
                    i = first_nul
                    continue
            end = first_nul
            if end is None or end < 0:
                end = min(i + 4, limit)
            out.append('{CTRL:' + bytes_hex(data[i:end]) + '}')
            i = end
            continue

        if b == 0x0a:
            text.append('\\n')
            i += 1
            continue
        if b == 0x0d:
            text.append('\\r')
            i += 1
            continue
        if b == 0x0a:
            flush_text()
            out.append('\\n')
            i += 1
            continue
        if b == 0x0d:
            flush_text()
            out.append('\\r')
            i += 1
            continue
        if b < 0x20 or b == 0x7f or b == 0x80:
            flush_text()
            out.append(f'{{BYTE:{b:02x}}}')
            i += 1
            continue
        if b < 0x80:
            text.append(chr(b))
            i += 1
            continue

        ch, size = decode_euc_char(data, i)
        if ch is None:
            flush_text()
            out.append(f'{{BYTE:{b:02x}}}')
            i += 1
        else:
            text.append(ch)
            i += size

    flush_text()
    return limit, ''.join(out)


def parse_text_record(data, start, limit):
    text_start, prefix = normal_text_start(data, start, limit)
    end, display = record_payload_to_display(data, text_start, limit)
    if end <= text_start:
        return None
    return {
        'start': start,
        'text_start': text_start,
        'end': end,
        'prefix': prefix,
        'raw_len': end - text_start,
        'display': display,
    }


def plausible_symbol_text(sym, record):
    if record is None:
        return False
    name = sym['name']
    if should_extract_symbol(sym):
        return True
    if has_display_text(record['display']):
        return True
    return bool(record['prefix']) and any(key in name for key in ('Mess', 'Msg', 'Str', 'Txt'))


def ov10_bounds(shdrs, data_len):
    sec = next((s for s in shdrs if s['name'] == OV10_SECTION), None)
    if not sec:
        return 0, data_len
    return sec['sh_offset'], sec['sh_offset'] + sec['sh_size']


def zero_padding_after(data, end, limit):
    pos = end + 1
    while pos < limit and data[pos] == 0:
        pos += 1
    return pos - end - 1


def display_has_byte_escape(text):
    return '{BYTE:' in text or '\\x' in text


def should_scan_string_array_symbol(sym):
    if sym['size'] <= 0:
        return False
    name = sym['name']
    return any(key in name for key in ('Str', 'Name', 'Zenkaku'))


def text_record_candidate(data, start, limit):
    record = parse_text_record(data, start, limit)
    if record is None:
        return None
    if not has_display_text(record['display']):
        return None
    if record['raw_len'] > 220:
        return None
    if display_has_byte_escape(record['display']):
        return None
    return record


def possible_text_start(data, pos, range_start, include_plain):
    if data[pos:pos + 3] == b'\x0b\x0d\x00':
        return True
    if data[pos:pos + 2] == b'\x0d\x00':
        return True
    if not include_plain:
        return False
    if pos > range_start and data[pos - 1] != 0:
        return False
    ch, _size = decode_euc_char(data, pos)
    return ch is not None


def scan_text_records(data, start, limit, is_covered, include_plain=True):
    found = []
    pos = start
    while pos < limit:
        if is_covered(pos):
            pos += 1
            continue
        if not possible_text_start(data, pos, start, include_plain):
            pos += 1
            continue

        record = text_record_candidate(data, pos, limit)
        if record is None:
            pos += 1
            continue

        slack = zero_padding_after(data, record['end'], limit)
        record_limit = record['end'] + 1 + slack
        found.append((pos, record_limit, record))
        pos = record_limit
    return found


def filter_anonymous_candidates(candidates):
    kept = []
    for idx, (start, limit, record) in enumerate(candidates):
        prefix = bool(record['prefix'])
        prev_gap = start - candidates[idx - 1][1] if idx else 0x100000
        next_gap = candidates[idx + 1][0] - limit if idx + 1 < len(candidates) else 0x100000
        plain = strip_display_tokens(record['display'])
        if not prefix and record['raw_len'] <= 2 and len(plain) <= 1:
            continue
        placeholder = len(plain) >= 5 and set(plain) == {'？'}
        if prefix or prev_gap <= 32 or next_gap <= 32 or len(plain) >= 6 or placeholder:
            kept.append((start, limit, record))
    return kept


def collect_text_entries(data, symbols, shdrs):
    entries = []
    covered = []

    for idx, sym in enumerate(symbols):
        if (sym['info'] & 0x0f) != 1 or sym['size'] != 0:
            continue

        start = sym['offset']
        limit = next_symbol_offset(symbols, idx, len(data))
        if is_stream_symbol(sym['name']):
            entries.append({
                'kind': 'S',
                'offset': start,
                'name': sym['name'],
                'limit': limit,
                'raw': data[start:limit],
            })
            covered.append((start, limit))
            continue

        record = parse_text_record(data, start, limit)
        if not plausible_symbol_text(sym, record):
            continue

        record.update({
            'kind': 'N',
            'offset': start,
            'name': sym['name'],
            'limit': limit,
            'source': 'symbol',
        })
        entries.append(record)
        covered.append((start, limit))

    def is_covered(pos):
        return any(start <= pos < end for start, end in covered)

    for sym in symbols:
        if (sym['info'] & 0x0f) != 1 or not should_scan_string_array_symbol(sym):
            continue
        for start, limit, record in scan_text_records(
            data, sym['offset'], sym['offset'] + sym['size'], is_covered, True,
        ):
            record.update({
                'kind': 'N',
                'offset': start,
                'name': f"{sym['name']}+{start - sym['offset']:02x}",
                'limit': limit,
                'source': 'symbol-array',
            })
            entries.append(record)
            covered.append((start, limit))

    scan_start = max(ANON_TEXT_SCAN_START, ov10_bounds(shdrs, len(data))[0])
    scan_end = min(ANON_TEXT_SCAN_END, ov10_bounds(shdrs, len(data))[1])
    candidates = scan_text_records(data, scan_start, scan_end, is_covered, True)
    for start, limit, record in filter_anonymous_candidates(candidates):
        if is_covered(start):
            continue
        record.update({
            'kind': 'N',
            'offset': start,
            'name': f'AnonMess_{start:08x}',
            'limit': limit,
            'source': 'anonymous',
        })
        entries.append(record)
        covered.append((start, limit))

    return sorted(entries, key=lambda entry: (entry['offset'], entry['name']))


def stream_to_display(raw):
    out = []
    text = []
    i = 0

    def flush_text():
        if text:
            out.append(''.join(text))
            text.clear()

    while i < len(raw):
        if raw[i:i + 3] == b'\x0b\x0d\x00':
            flush_text()
            out.append('{0B0D00}')
            i += 3
            continue

        if raw[i] == 0x0c:
            flush_text()
            end = raw.find(b'\x0b\x0d\x00', i + 1)
            if end < 0 or end < i + 4:
                out.append('{CTRL:' + bytes_hex(raw[i:i + 1]) + '}')
                i += 1
                continue

            header = raw[i + 1:i + 4]
            payload = raw[i + 4:end]
            try:
                payload_text = raw_to_display(payload)
                out.append(
                    '{CTRL0C:' + bytes_hex(header) + '|' + payload_text + '}'
                )
            except Exception:
                out.append('{CTRL:' + bytes_hex(raw[i:end + 3]) + '}')
            i = end + 3
            continue

        b = raw[i]
        if b in (0x0a, 0x0d):
            text.append('\\n' if b == 0x0a else '\\r')
            i += 1
            continue
        if b < 0x20 or b == 0x7f or b == 0x80:
            flush_text()
            out.append(f'{{BYTE:{b:02x}}}')
            i += 1
            continue
        if b < 0x80:
            text.append(chr(b))
            i += 1
            continue

        ch, size = decode_euc_char(raw, i)
        if ch is None:
            flush_text()
            out.append(f'{{BYTE:{b:02x}}}')
            i += 1
        else:
            text.append(ch)
            i += size

    flush_text()
    return ''.join(out)


def read_legacy_strings(path):
    translations = {}
    line_re = re.compile(r'^([0-9a-fA-F]{8})\|[^|]*\|(.*)$')
    bad_fragments = ('\\x0c', '寸咤', '釘味', '釘達')
    with open(path, 'r', encoding='utf-8') as f:
        for line in f.read().splitlines():
            match = line_re.match(line)
            if not match:
                continue
            text = match.group(2)
            if any(fragment in text for fragment in bad_fragments):
                continue
            translations[int(match.group(1), 16)] = text
    return translations


def apply_translation(translations, applied, offset, fallback, strip_cr=False):
    text = translations.get(offset)
    if text is None:
        return fallback
    if strip_cr and text.endswith('\\r'):
        text = text[:-2]
    applied.add(offset)
    return text


def translated_record_payload(data, start, limit, translations, applied):
    out = []
    seg_start = None
    i = start

    def start_text():
        nonlocal seg_start
        if seg_start is None:
            seg_start = i

    def flush_text(strip_cr=False):
        nonlocal seg_start
        if seg_start is None:
            return
        fallback = raw_to_display(data[seg_start:i])
        out.append(apply_translation(
            translations, applied, seg_start, fallback, strip_cr,
        ))
        seg_start = None

    while i < limit:
        b = data[i]
        if b == 0x00:
            flush_text()
            return i, ''.join(out)

        if data[i:i + 2] == b'\x0d\x00' and i + 2 < limit and data[i + 2] == 0x0c:
            flush_text(strip_cr=True)
            out.append('{0D00}')
            i += 2
            continue

        if data[i] == 0x0d and i + 1 < limit and data[i + 1] in (0x02, 0x04):
            flush_text(strip_cr=True)
            out.append(f'{{0D{data[i + 1]:02X}}}')
            i += 2
            continue

        if b == 0x0c:
            flush_text()
            marker = data.find(b'\x0b\x0d\x00', i + 4, limit)
            first_nul = data.find(b'\x00', i + 1, limit)
            if marker >= 0 and (first_nul < 0 or first_nul >= marker + 2):
                header = data[i + 1:i + 4]
                payload_start = i + 4
                payload = data[payload_start:marker]
                payload_text = apply_translation(
                    translations, applied, payload_start,
                    payload_to_display(payload),
                )
                out.append(
                    '{CTRL0C:' + bytes_hex(header) + '|' + payload_text + '}'
                )
                i = marker + 3
                continue
            if first_nul >= 0 and first_nul >= i + 4:
                header = data[i + 1:i + 4]
                payload_start = i + 4
                payload = data[payload_start:first_nul]
                payload_text = apply_translation(
                    translations, applied, payload_start,
                    payload_to_display(payload),
                )
                out.append(
                    '{CTRL0C:' + bytes_hex(header) + '|' + payload_text + '}'
                )
                i = first_nul
                continue
            end = first_nul
            if end is None or end < 0:
                end = min(i + 4, limit)
            out.append('{CTRL:' + bytes_hex(data[i:end]) + '}')
            i = end
            continue

        if b < 0x20 or b == 0x7f or b == 0x80:
            flush_text()
            out.append(f'{{BYTE:{b:02x}}}')
            i += 1
            continue

        if b < 0x80:
            start_text()
            i += 1
            continue

        ch, size = decode_euc_char(data, i)
        if ch is None:
            flush_text()
            out.append(f'{{BYTE:{b:02x}}}')
            i += 1
        else:
            start_text()
            i += size

    flush_text()
    return limit, ''.join(out)


def translated_stream_to_display(raw, absolute_start, translations, applied):
    out = []
    seg_start = None
    i = 0

    def start_text():
        nonlocal seg_start
        if seg_start is None:
            seg_start = i

    def flush_text():
        nonlocal seg_start
        if seg_start is None:
            return
        abs_start = absolute_start + seg_start
        fallback = raw_to_display(raw[seg_start:i])
        out.append(apply_translation(translations, applied, abs_start, fallback))
        seg_start = None

    while i < len(raw):
        if raw[i:i + 3] == b'\x0b\x0d\x00':
            flush_text()
            out.append('{0B0D00}')
            i += 3
            continue

        if raw[i] == 0x0c:
            flush_text()
            end = raw.find(b'\x0b\x0d\x00', i + 1)
            if end < 0 or end < i + 4:
                out.append('{CTRL:' + bytes_hex(raw[i:i + 1]) + '}')
                i += 1
                continue

            header = raw[i + 1:i + 4]
            payload_start = i + 4
            payload = raw[payload_start:end]
            payload_text = apply_translation(
                translations, applied, absolute_start + payload_start,
                payload_to_display(payload),
            )
            out.append(
                '{CTRL0C:' + bytes_hex(header) + '|' + payload_text + '}'
            )
            i = end + 3
            continue

        b = raw[i]
        if b in (0x0a, 0x0d) or b < 0x20 or b == 0x7f or b == 0x80:
            flush_text()
            if b == 0x0a:
                out.append('\\n')
            elif b == 0x0d:
                out.append('\\r')
            else:
                out.append(f'{{BYTE:{b:02x}}}')
            i += 1
            continue
        if b < 0x80:
            start_text()
            i += 1
            continue

        ch, size = decode_euc_char(raw, i)
        if ch is None:
            flush_text()
            out.append(f'{{BYTE:{b:02x}}}')
            i += 1
        else:
            start_text()
            i += size

    flush_text()
    return ''.join(out)


def translated_entry_display(data, entry, translations, applied):
    if entry['kind'] == 'S':
        return translated_stream_to_display(
            entry['raw'], entry['offset'], translations, applied,
        )

    _end, display = translated_record_payload(
        data, entry['text_start'], entry['limit'], translations, applied,
    )
    return display


def raw_to_display(raw):
    out = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x0a:
            out.append('\\n')
            i += 1
            continue
        if b == 0x0d:
            out.append('\\r')
            i += 1
            continue
        if b < 0x20 or b == 0x7f or b == 0x80:
            out.append(f'\\x{b:02x}')
            i += 1
            continue
        if b < 0x80:
            out.append(chr(b))
            i += 1
            continue

        ch, size = decode_euc_char(raw, i)
        if ch is None:
            out.append(f'\\x{b:02x}')
            i += 1
        else:
            out.append(ch)
            i += size
    return ''.join(out)


def decode_raw_text(raw):
    return raw.decode(ENCODING)


def group_name(name):
    match = re.match(r'([A-Za-z]+)', name)
    return match.group(1) if match else name


def fullwidth_rows(text, width=23):
    return [text[i:i + width] for i in range(0, len(text), width)]


def collect_ctrl_packets(data, symbols):
    packets = []
    for idx, sym in enumerate(symbols):
        if not is_stream_symbol(sym['name']):
            continue
        raw = data[sym['offset']:next_symbol_offset(symbols, idx, len(data))]
        pos = 0
        while True:
            start = raw.find(b'\x0c', pos)
            if start < 0:
                break
            end = raw.find(b'\x0b\x0d\x00', start + 1)
            if end < 0:
                packets.append((sym, sym['offset'] + start, raw[start:start + 1], b'', False))
                break
            header = raw[start + 1:start + 4]
            payload = raw[start + 4:end]
            ok = True
            try:
                text = payload.decode(ENCODING)
            except UnicodeDecodeError:
                text = payload.decode(ENCODING, errors='replace')
                ok = False
            packets.append((sym, sym['offset'] + start, header, text, ok))
            pos = end + 3
    return packets


def analyze(path):
    data, elf, phdrs, shdrs = read_elf(path)
    symbols = read_symbols(data, phdrs, shdrs)
    entries = collect_text_entries(data, symbols, shdrs)
    out_path = os.path.join(
        os.path.dirname(os.path.abspath(path)),
        os.path.splitext(os.path.basename(path))[0] + '_format_report.txt',
    )

    text_symbols = [
        (idx, sym)
        for idx, sym in enumerate(symbols)
        if should_extract_symbol(sym)
    ]
    normal_rows = []
    groups = defaultdict(list)
    for entry in entries:
        if entry['kind'] != 'N' or not has_display_text(entry['display']):
            continue
        plain = strip_display_tokens(entry['display'])
        slot = entry['limit'] - entry['start']
        slack = max(0, entry['limit'] - entry['end'] - 1)
        groups[group_name(entry['name'])].append((
            entry['name'], slot, len(entry['prefix']),
            entry['raw_len'], slack, len(plain),
        ))
        if len(plain) >= 40:
            rows = fullwidth_rows(plain, 23)
            normal_rows.append((
                entry['name'], entry['start'], entry['raw_len'], len(plain), rows,
            ))

    packets = collect_ctrl_packets(data, symbols)
    text_by_vaddr = {
        sym['vaddr']: sym
        for _idx, sym in text_symbols
        if sym['size'] == 0
    }
    pointer_tables = []
    for sym in symbols:
        if sym['size'] < 4 or sym['size'] % 4:
            continue
        if not any(key in sym['name'] for key in ('Lst', 'List', 'Tbl')):
            continue
        targets = []
        for off in range(sym['offset'], sym['offset'] + sym['size'], 4):
            ptr = struct.unpack_from('<I', data, off)[0]
            target = text_by_vaddr.get(ptr)
            if target:
                targets.append(target['name'])
        if targets:
            pointer_tables.append((sym['name'], sym['offset'], sym['size'], targets))

    lines = [
        f'OV10 format report: {os.path.basename(path)}',
        '',
        'ELF',
        f"- machine: 0x{elf['e_machine']:x} (MIPS)",
        f"- load segment: file 0x{phdrs[0]['p_offset']:x}..0x{phdrs[0]['p_offset'] + phdrs[0]['p_filesz']:x} "
        f"-> vaddr 0x{phdrs[0]['p_vaddr']:x}",
        f"- extracted text entries: {len(entries)}",
        '',
        'Control Packet Rule',
        '- Help*_txt streams are not plain null-terminated strings.',
        '- Stream starts commonly contain 0b 0d 00.',
        '- Inline display packet observed in all sampled cases:',
        '  0c <3-byte header> <EUC-JIS-2004 payload text> 0b 0d 00',
        '- Some normal records use 0d 00/02/04 separators before 0c packets; these are kept as {0D00}/{0D02}/{0D04}.',
        '- The old XX/寸咤/釘味 artifacts are caused by decoding the 3-byte header as text.',
        '',
        '0x0c Header Distribution',
    ]

    for header, count in Counter(bytes_hex(p[2]) for p in packets).most_common():
        sample = next(p[3] for p in packets if bytes_hex(p[2]) == header)
        lines.append(f'- {header}: {count} packets; sample payload={sample!r}')

    bad = [p for p in packets if not p[4]]
    lines.extend([
        f'- payload decode failures after 3-byte header: {len(bad)}',
        '',
        'Pointer Tables',
    ])

    for name, off, size, targets in sorted(pointer_tables):
        sample = ', '.join(targets[:8])
        more = '' if len(targets) <= 8 else f', ... +{len(targets) - 8}'
        lines.append(
            f'- 0x{off:08x} {name}: {size // 4} entries, '
            f'{len(targets)} text refs [{sample}{more}]'
        )

    lines.extend([
        '',
        'Normal Text Slot Groups',
    ])

    for name, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        slot_counts = Counter(r[1] for r in rows).most_common(8)
        raw_counts = Counter(r[3] for r in rows).most_common(8)
        lines.append(
            f'- {name}: count={len(rows)} slot_bytes={slot_counts} raw_bytes={raw_counts}'
        )

    lines.extend([
        '',
        '23-Fullwidth-Cell Wrapping Candidates',
        '- Many long menu/help descriptions are authored as continuous 2-byte fullwidth text.',
        '- For these, the renderer appears to wrap every 23 fullwidth cells.',
        '- ASCII halfwidth bytes break the byte/cell assumption in some renderers; use fullwidth spaces/ASCII.',
    ])

    for name, off, raw_len, char_len, rows in normal_rows[:40]:
        lengths = [len(row) for row in rows]
        lines.append(
            f'- 0x{off:08x} {name}: raw={raw_len}B chars={char_len} row_chars={lengths}'
        )

    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))
    print(f'[OK] format report -> {out_path}')


def extract(path):
    data, _elf, phdrs, shdrs = read_elf(path)
    symbols = read_symbols(data, phdrs, shdrs)
    entries = collect_text_entries(data, symbols, shdrs)
    out_path = os.path.join(
        os.path.dirname(os.path.abspath(path)),
        os.path.splitext(os.path.basename(path))[0] + '_elf_strings.txt',
    )

    lines = [
        f'# {os.path.basename(path)} symbol-aware text dump',
        '# format:',
        '#   N|<offset>|<symbol>|<prefix_hex>|<orig>/<slack>|<text>',
        '#   S|<offset>|<symbol>|<stream_bytes>|<tokenized stream>',
        '# stream tokens preserve non-text control packets:',
        '#   {0B0D00}, {0D00}, {0D02}, {0D04}, {CTRL0C:hh hh hh|text}, {CTRL:...}, {BYTE:nn}',
        '',
    ]

    for entry in entries:
        if entry['kind'] == 'S':
            raw = entry['raw']
            lines.append(
                f"S|{entry['offset']:08x}|{entry['name']}|{len(raw)}|"
                f"{stream_to_display(raw)}"
            )
        else:
            slack = max(0, entry['limit'] - entry['end'] - 1)
            lines.append(
                f"N|{entry['offset']:08x}|{entry['name']}|"
                f"{prefix_display(entry['prefix'])}|"
                f"{entry['raw_len']}/{slack}|{entry['display']}"
            )

    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))
    print(f'[OK] {len(entries)} text entries -> {out_path}')


def migrate(path, legacy_path):
    data, _elf, phdrs, shdrs = read_elf(path)
    symbols = read_symbols(data, phdrs, shdrs)
    entries = collect_text_entries(data, symbols, shdrs)
    translations = read_legacy_strings(legacy_path)
    applied = set()
    out_path = os.path.join(
        os.path.dirname(os.path.abspath(path)),
        os.path.splitext(os.path.basename(path))[0] + '_elf_strings.txt',
    )

    lines = [
        f'# {os.path.basename(path)} symbol-aware text dump',
        f'# migrated translations from: {os.path.basename(legacy_path)}',
        '# format:',
        '#   N|<offset>|<symbol>|<prefix_hex>|<orig>/<slack>|<text>',
        '#   S|<offset>|<symbol>|<stream_bytes>|<tokenized stream>',
        '# stream tokens preserve non-text control packets:',
        '#   {0B0D00}, {0D00}, {0D02}, {0D04}, {CTRL0C:hh hh hh|text}, {CTRL:...}, {BYTE:nn}',
        '',
    ]

    for entry in entries:
        if entry['kind'] == 'S':
            raw = entry['raw']
            lines.append(
                f"S|{entry['offset']:08x}|{entry['name']}|{len(raw)}|"
                f"{translated_entry_display(data, entry, translations, applied)}"
            )
        else:
            slack = max(0, entry['limit'] - entry['end'] - 1)
            lines.append(
                f"N|{entry['offset']:08x}|{entry['name']}|"
                f"{prefix_display(entry['prefix'])}|"
                f"{entry['raw_len']}/{slack}|"
                f"{translated_entry_display(data, entry, translations, applied)}"
            )

    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))

    missed = sorted(set(translations) - applied)
    print(f'[OK] {len(entries)} text entries -> {out_path}')
    print(f'[OK] migrated {len(applied)}/{len(translations)} legacy offsets')
    if missed:
        sample = ', '.join(f'{off:08x}' for off in missed[:40])
        more = '' if len(missed) <= 40 else f', ... +{len(missed) - 40}'
        print(f'[WARN] unmigrated legacy offsets: {sample}{more}')


def load_replace_table(bin_path):
    folder = os.path.dirname(os.path.abspath(bin_path))
    json_path = os.path.join(folder, 'XENOSAGA_KOR-JPN.json')
    if not os.path.exists(json_path):
        print(f'[INFO] {json_path} 없음 - 한글 치환 없이 진행')
        return {}
    with open(json_path, encoding='utf-8-sig') as f:
        data = json.load(f)
    table = data.get('replace-table', {})
    print(f'[INFO] replace-table 로드: {len(table)}개 ({json_path})')
    return table


def apply_replace_table(text, table):
    if not table:
        return text
    return ''.join(table.get(ch, ch) for ch in text)


def encode_literal(text, table):
    converted = apply_replace_table(text, table)
    return converted.encode(ENCODING)


def display_to_raw(text, table, ctrl0c_stream_marker=False, ctrl0c_markers=None):
    out = bytearray()
    literal = []
    hexdigits = '0123456789abcdefABCDEF'
    ctrl0c_index = 0

    def flush_literal():
        if not literal:
            return
        out.extend(encode_literal(''.join(literal), table))
        literal.clear()

    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '{':
            end = text.find('}', i + 1)
            if end >= 0:
                token = text[i + 1:end]
                if token == '0B0D00':
                    flush_literal()
                    out.extend(b'\x0b\x0d\x00')
                    i = end + 1
                    continue
                if token in ('0D00', '0D02', '0D04'):
                    flush_literal()
                    out.extend(bytes.fromhex(token))
                    i = end + 1
                    continue
                if token.upper().startswith('BYTE:'):
                    flush_literal()
                    out.append(int(token[5:], 16))
                    i = end + 1
                    continue
                if token.upper().startswith('CTRL:'):
                    flush_literal()
                    out.extend(bytes.fromhex(token[5:]))
                    i = end + 1
                    continue
                if token.upper().startswith('CTRL0C:') and '|' in token:
                    flush_literal()
                    header_hex, payload = token[7:].split('|', 1)
                    out.append(0x0c)
                    out.extend(bytes.fromhex(header_hex))
                    out.extend(encode_literal(payload, table))
                    use_marker = ctrl0c_stream_marker
                    if ctrl0c_markers is not None:
                        use_marker = (
                            ctrl0c_index < len(ctrl0c_markers)
                            and ctrl0c_markers[ctrl0c_index]
                        )
                    ctrl0c_index += 1
                    if use_marker:
                        out.extend(b'\x0b\x0d\x00')
                    i = end + 1
                    continue

        if ch == '\\' and i + 1 < len(text):
            esc = text[i + 1]
            if esc == 'n':
                flush_literal()
                out.append(0x0a)
                i += 2
                continue
            if esc == 'r':
                flush_literal()
                out.append(0x0d)
                i += 2
                continue
            if esc in ('x', 'X') and i + 3 < len(text):
                hx = text[i + 2:i + 4]
                if all(c in hexdigits for c in hx):
                    flush_literal()
                    out.append(int(hx, 16))
                    i += 4
                    continue

        literal.append(ch)
        i += 1

    flush_literal()
    return bytes(out)


def parse_elf_string_file(path):
    records = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip('\n')
            if line.endswith('\r'):
                line = line[:-1]
            if not line or line.startswith('#'):
                continue
            if line.startswith('N|'):
                parts = line.split('|', 5)
                if len(parts) != 6:
                    raise ValueError(f'{path}:{lineno}: malformed N record')
                orig, slack = parts[4].split('/', 1)
                records.append({
                    'line': lineno,
                    'kind': 'N',
                    'offset': int(parts[1], 16),
                    'symbol': parts[2],
                    'prefix': b'' if parts[3] == '-' else bytes.fromhex(parts[3]),
                    'orig': int(orig),
                    'slack': int(slack),
                    'text': parts[5],
                })
            elif line.startswith('S|'):
                parts = line.split('|', 4)
                if len(parts) != 5:
                    raise ValueError(f'{path}:{lineno}: malformed S record')
                records.append({
                    'line': lineno,
                    'kind': 'S',
                    'offset': int(parts[1], 16),
                    'symbol': parts[2],
                    'size': int(parts[3]),
                    'text': parts[4],
                })
            else:
                raise ValueError(f'{path}:{lineno}: unknown record kind')
    return records


def normal_record_ctrl0c_markers(raw):
    markers = []
    i = 0
    limit = len(raw)
    while i < limit:
        if raw[i] == 0x00:
            break
        if raw[i:i + 2] == b'\x0d\x00' and i + 2 < limit and raw[i + 2] == 0x0c:
            i += 2
            continue
        if raw[i] == 0x0d and i + 1 < limit and raw[i + 1] in (0x02, 0x04):
            i += 2
            continue
        if raw[i] != 0x0c:
            i += 1
            continue

        marker = raw.find(b'\x0b\x0d\x00', i + 4, limit)
        first_nul = raw.find(b'\x00', i + 1, limit)
        if marker >= 0 and (first_nul < 0 or first_nul >= marker + 2):
            markers.append(True)
            i = marker + 3
            continue

        if first_nul >= 0 and first_nul >= i + 4:
            markers.append(False)
            i = first_nul
            continue

        markers.append(False)
        i += 1
    return markers


def rebuild_from_elf_strings(bin_path, txt_path, out_path=None):
    data = bytearray(open(bin_path, 'rb').read())
    table = load_replace_table(bin_path)
    records = parse_elf_string_file(txt_path)
    if out_path is None:
        base, ext = os.path.splitext(bin_path)
        out_path = base + '_patched' + ext

    patched = 0
    skipped = 0
    for rec in records:
        if rec['kind'] == 'S':
            raw = display_to_raw(
                rec['text'], table, ctrl0c_stream_marker=True,
            ).rstrip(b'\x00')
            cap = rec['size']
            write_start = rec['offset']
        else:
            payload_start = rec['offset'] + len(rec['prefix'])
            original_payload = data[payload_start:payload_start + rec['orig'] + 1]
            ctrl0c_markers = normal_record_ctrl0c_markers(original_payload)
            payload = display_to_raw(
                rec['text'], table, ctrl0c_stream_marker=False,
                ctrl0c_markers=ctrl0c_markers,
            )
            max_payload = rec['orig'] + rec['slack']
            if len(payload) > max_payload:
                print(
                    f"[여유 공간 초과하여 미적용] line {rec['line']} "
                    f"0x{rec['offset']:08x} {rec['symbol']} "
                    f"원본={rec['orig']}B 여유={rec['slack']}B 신규={len(payload)}B"
                )
                skipped += 1
                continue
            raw = rec['prefix'] + payload
            cap = len(rec['prefix']) + max_payload + 1
            write_start = rec['offset']

        if len(raw) > cap:
            print(
                f"[여유 공간 초과하여 미적용] line {rec['line']} "
                f"0x{rec['offset']:08x} {rec['symbol']} "
                f"슬롯={cap}B 신규={len(raw)}B"
            )
            skipped += 1
            continue

        data[write_start:write_start + cap] = raw + b'\x00' * (cap - len(raw))
        patched += 1

    ui_patched = 0
    for off, expected, replacement in KOR_UI_PATCHES:
        actual = bytes(data[off:off + len(expected)])
        if actual == expected:
            data[off:off + len(expected)] = replacement
            ui_patched += 1
        elif actual != replacement:
            print(
                f"[WARN] UI patch skipped at 0x{off:08x}: "
                f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
            )

    for label, off, expected, replacement in CARD_LIST_SPACE_PATCHES:
        if len(expected) != len(replacement):
            raise ValueError(f'{label}: patch changes the OV10 file size')
        actual = bytes(data[off:off + len(expected)])
        if actual == replacement:
            continue
        if actual != expected:
            raise ValueError(
                f"{label}: unexpected code at 0x{off:08x}; "
                f"expected {expected.hex(' ')}, found {actual.hex(' ')}"
            )
        data[off:off + len(expected)] = replacement
        ui_patched += 1

    with open(out_path, 'wb') as f:
        f.write(data)
    print(f'[OK] patched={patched}, skipped={skipped}, ui_patched={ui_patched} -> {out_path}')


def usage():
    print(__doc__)
    sys.exit(1)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        usage()
    cmd = sys.argv[1].lower()
    if cmd in ('extract', 'analyze') and len(sys.argv) != 3:
        usage()
    if cmd == 'migrate' and len(sys.argv) != 4:
        usage()
    if cmd == 'rebuild' and len(sys.argv) not in (4, 5):
        usage()

    if cmd == 'extract':
        extract(sys.argv[2])
    elif cmd == 'analyze':
        analyze(sys.argv[2])
    elif cmd == 'migrate':
        migrate(sys.argv[2], sys.argv[3])
    elif cmd == 'rebuild':
        out_path = sys.argv[4] if len(sys.argv) == 5 else None
        rebuild_from_elf_strings(sys.argv[2], sys.argv[3], out_path)
    else:
        usage()
