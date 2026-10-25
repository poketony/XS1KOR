"""Preserve original VM addresses while expanding inline strings."""
import struct
from think_vm import instruction

LIMIT = 0x4000


def expand(parsed, replacements):
    data = parsed.data
    instructions = parsed.instructions | parsed.linear_candidates()
    output = bytearray(data)
    changes = []
    for offset, raw in sorted(replacements.items()):
        ins = instructions.get(offset)
        if ins is None or ins.text is None:
            raise ValueError(f'Not a structurally decoded text command: {offset:#x}')
        if b'\0' in raw:
            raise ValueError('Embedded NUL truncates a VM string')
        if raw == ins.text:
            continue
        # Keep original string storage and command end for shorter translations.
        if len(raw) <= len(ins.text):
            padding = len(ins.text)-len(raw)
            padded = raw + b'\xa1\xa1' * (padding // 2) + b' ' * (padding % 2)
            output[offset+2:offset+2+len(ins.text)] = padded
            changes.append({'offset':offset, 'destination':offset, 'resume':ins.end})
            continue
        if ins.end-offset < 4:
            raise ValueError('No room for a GO instruction')
        for target, role in parsed.references.values():
            if offset < (target & ~1) < ins.end:
                raise ValueError(f'Interior {role} reference into text at {offset:#x}')
        destination = len(output)
        command = struct.pack('<H', ins.op) + raw + b'\0'
        command += b'\0' * (len(command) & 1)
        if ins.op == 0x21:
            command += struct.pack('<H', ins.args[0])
        command += struct.pack('<HH', 1, ins.end)
        if destination + len(command) > LIMIT:
            raise ValueError('Expanded think exceeds the 0x4000-byte runtime buffer')
        output += command
        struct.pack_into('<HH', output, offset, 1, destination)
        changes.append({'offset':offset, 'destination':destination, 'resume':ins.end})
    return bytes(output), changes

