"""Evidence-driven, XS1 think structural parser."""
from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
import struct

LAB = Path(__file__).resolve().parent
OPCODES = {r['op']: r for r in json.loads((LAB / 'opcodes.json').read_text())}


def u16(data, offset):
    if offset < 0 or offset + 2 > len(data) or offset & 1:
        raise ValueError(f'Invalid word offset {offset:#x}')
    return struct.unpack_from('<H', data, offset)[0]


@dataclass
class Instruction:
    offset: int
    op: int
    end: int
    args: tuple
    text: bytes | None = None


def instruction(data, offset):
    op = u16(data, offset)
    if op not in OPCODES:
        raise ValueError(f'Unknown/null opcode {op:#x} at {offset:#x}')
    if op in (0x1a, 0x21):
        end = data.find(b'\0', offset + 2)
        if end < 0:
            raise ValueError(f'Unterminated text at {offset:#x}')
        after = (end + 2) & ~1
        args = (u16(data, after),) if op == 0x21 else ()
        return Instruction(offset, op, after + len(args)*2, args, data[offset+2:end])
    count = OPCODES[op]['args']
    if op in (5, 6):
        count = u16(data, offset + 4)
        if count & 0x8000:
            raise ValueError(f'Dynamic jump table size at {offset:#x}')
        count += 2  # selector, count, count inline targets (VM check also accepts count!)
    args = tuple(u16(data, offset + 2 + i*2) for i in range(count))
    return Instruction(offset, op, offset + 2 + count*2, args)


class Think:
    def __init__(self, data, extra_units=()):
        self.data = data
        self.instructions = {}
        self.formations = []
        self.units = {}
        self.references = {}  # operand file offset -> (target, role)
        self.dynamic = []
        self.owners = {}
        self.pending = deque()
        self.claim(0, 2, 'header')
        top = u16(data, 0)
        self.reference(0, 'formation-table')
        offset = top
        while u16(data, offset):
            self.claim(offset, offset + 10, f'formation:{offset:x}')
            row = tuple(u16(data, offset+i*2) for i in range(5))
            self.formations.append((offset, row))
            self.reference(offset, 'unit-list')
            unit = row[0]
            for i in range(5):
                if not u16(data, unit):
                    self.claim(unit, unit+2, f'unit-end:{unit:x}')
                    break
                self.unit(unit)
                unit += 8
            for field in (4, 6, 8):
                self.reference(offset+field, 'code', optional=True)
            offset += 10
        self.claim(offset, offset+2, 'formation-end')
        for offset in extra_units:
            self.unit(offset)
        while self.pending:
            start = self.pending.popleft()
            if start in self.instructions:
                continue
            ins = instruction(data, start)
            self.claim(start, ins.end, f'code:{start:x}')
            self.instructions[start] = ins
            op = ins.op
            if op in (1, 2):
                self.reference(start+2, 'code', operand=True)
            elif op == 4:
                self.reference(start+8, 'code', operand=True)
            elif op in (5, 6):
                for field in range(start+6, ins.end, 2):
                    self.reference(field, 'code', operand=True)
            elif op == 0x50:
                self.reference(start+4, 'code', operand=True)
            elif op == 0x30:
                self.reference(start+8, 'code', operand=True)
            elif op == 0x2a:
                target = self.reference(start+4, 'unit-record', operand=True)
                if target is not None:
                    self.unit(target)
            # Known direct data-buffer/table operands; their contents can be mutable.
            for arg in {0x17:(1,), 0x18:(1,), 0x29:(1,), 0x2c:(1,),
                        0x2d:(0,), 0x2e:(0,), 0x2f:(0,), 0x30:(1,2)}.get(op, ()):
                self.reference(start+2+arg*2, 'data', operand=True)
            if op not in (0, 1, 3, 5):
                self.pending.append(ins.end)

    def claim(self, start, end, owner):
        if start < 0 or end > len(self.data) or start & 1 or end & 1:
            raise ValueError(f'Out-of-range {owner}: {start:#x}..{end:#x}')
        for off in range(start, end):
            previous = self.owners.get(off)
            if previous is not None and previous != owner:
                raise ValueError(f'Overlap {owner} and {previous} at {off:#x}')
            self.owners[off] = owner

    def reference(self, field, role, optional=False, operand=False):
        target = u16(self.data, field)
        if optional and target == 0:
            return None
        if operand and target & 0x8000:
            self.dynamic.append((field, target, role))
            return None
        if operand and target & 0x4000:
            raise ValueError(f'Negative immediate address at {field:#x}')
        # thinkAdrGet masks bit 0, but preserve the raw field in evidence.
        address = target & ~1
        if address >= len(self.data):
            raise ValueError(f'Out-of-file {role} {target:#x} at {field:#x}')
        self.references[field] = (target, role)
        if role == 'code':
            self.pending.append(address)
        return address

    def unit(self, offset):
        if offset in self.units:
            return
        self.claim(offset, offset+8, f'unit:{offset:x}')
        self.units[offset] = tuple(u16(self.data, offset+i*2) for i in range(4))
        self.reference(offset+4, 'code', optional=True)
        self.reference(offset+6, 'code', optional=True)

    def gaps(self):
        result = []
        start = None
        for i in range(len(self.data)+1):
            free = i < len(self.data) and i not in self.owners
            if free and start is None:
                start = i
            elif not free and start is not None:
                result.append((start, i))
                start = None
        return result

    def linear_candidates(self):
        """Syntactic decoding of unrooted regions, not a reachability claim.

        Zero words may be mutable storage or EXIT; never reclaim these as free space.
        """
        result = {}
        for start, end in self.gaps():
            while start < end:
                ins = instruction(self.data, start)
                if ins.end > end:
                    raise ValueError(f'Candidate crosses classified boundary at {start:#x}')
                result[start] = ins
                start = ins.end
        return result


def original(path):
    # Dormant cmdUnitCreate operands identify these additional 8-byte unit records.
    # Verify the referring instruction bytes before using each corpus hint.
    data = path.read_bytes()
    hints = {'think000.bin.ori': [(0x2512, 0x24b2)],
             'think098.bin.ori': [(0x57c, 0x554)]}.get(path.name, [])
    for source, target in hints:
        ins = instruction(data, source)
        if ins.op != 0x2a or ins.args[1] != target:
            raise ValueError('Dormant unit record evidence mismatch')
    return Think(data, extra_units=[target for _, target in hints])


