"""Original-based think extraction, translation migration and binary build."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from think_vm import original
from text_expand import expand, LIMIT

ENCODING = 'euc_jis_2004'
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = ROOT / '0.xenosaga0/yamamoto/think'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def legacy_rows(path):
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    rows = []; i = 0
    pattern = r'^>>> \[(\d+)\] op=0x([0-9A-Fa-f]+)\(\w+\) flag=0x([0-9A-Fa-f]+) offset=0x([0-9A-Fa-f]+)$'
    while i < len(lines):
        line = lines[i]; match = re.fullmatch(pattern, line)
        if line.startswith('>>>') and not match:
            raise ValueError(f'{path}:{i+1}: Invalid entry header')
        if match:
            text = []; i += 1
            while i < len(lines) and lines[i] != '<<<':
                text.append(lines[i]); i += 1
            if i == len(lines):
                raise ValueError(f'{path}: Missing terminator')
            raw = '\n'.join(text)
            raw = re.sub(r'\\x([0-9a-fA-F]{2})',lambda m:chr(int(m[1],16)),raw)
            rows.append((int(match[4],16),int(match[2],16),int(match[3],16),raw))
        i += 1
    return rows


def extract(source):
    files = {}; ignored = []
    for path in sorted(source.glob('think*.bin.ori')):
        parsed = original(path)
        texts = {o:i for o,i in (parsed.instructions | parsed.linear_candidates()).items() if i.text is not None}
        rows = {}
        legacy = path.with_name(path.name.replace('.bin.ori','.txt'))
        if legacy.exists():
            for offset, op, flag, text in legacy_rows(legacy):
                if offset in rows:
                    raise ValueError(f'{legacy}: Duplicate offset {offset:#x}')
                if offset not in texts:
                    # Only accept the five proven, unchanged legacy false positives.
                    known = {'think036.bin.ori':{0x18},'think040.bin.ori':{0x2e},
                             'think048.bin.ori':{0x10},'think057.bin.ori':{0x38,0x42}}
                    end = parsed.data.find(b'\0',offset+2)
                    if offset not in known.get(path.name,set()) or end < 0 or text != parsed.data[offset+2:end].decode(ENCODING):
                        raise ValueError(f'{legacy}: Unrecognized/edited non-text entry {offset:#x}')
                    ignored.append({'file':legacy.name,'offset':offset,'text':text})
                    continue
                if flag or texts[offset].op != op:
                    raise ValueError(f'{legacy}: Opcode mismatch at {offset:#x}')
                rows[offset] = text
        name = path.name.removesuffix('.ori')
        files[name] = {'source_sha256':digest(parsed.data),'source_size':len(parsed.data),
            'entries':[{'offset':o,'opcode':i.op,'source':i.text.decode(ENCODING),
                        'translation':rows.get(o),'rooted':o in parsed.instructions}
                       for o,i in sorted(texts.items())]}
    if not files:
        raise ValueError('No original think files')
    return {'version':1,'files':files,'ignored_legacy_entries':ignored}


def encode(text, table):
    for a,b in sorted(table.items(), key=lambda pair:-len(pair[0])):
        text = text.replace(a,b)
    return text.encode(ENCODING)


def build_thinks(catalog, source, table):
    if catalog['version'] != 1:
        raise ValueError('Unsupported catalog version')
    output = {}; report = {}
    for name, file in catalog['files'].items():
        if not re.fullmatch(r'think[0-9]{3}\.bin',name):
            raise ValueError('Invalid think filename')
        parsed = original(source/(name+'.ori'))
        if digest(parsed.data) != file['source_sha256']:
            raise ValueError(f'{name}: Original hash mismatch')
        texts = {o:i for o,i in (parsed.instructions | parsed.linear_candidates()).items() if i.text is not None}
        replacements = {}; seen = set()
        for row in file['entries']:
            o = row['offset']
            if o in seen or o not in texts or row['opcode'] != texts[o].op or row['source'] != texts[o].text.decode(ENCODING):
                raise ValueError(f'{name}: Invalid/duplicate/source-edited entry {o:#x}')
            seen.add(o)
            if row['translation'] is not None and row['translation'] != row['source']:
                replacements[o] = encode(row['translation'],table)
        if seen != texts.keys():
            raise ValueError(f'{name}: Missing catalog entries')
        try:
            output[name], changes = expand(parsed,replacements)
        except ValueError as error:
            raise ValueError(f'{name}: {error}') from error
        if len(output[name]) > LIMIT:
            raise ValueError(f'{name}: File exceeds 16KB')
        report[name] = {'size':len(output[name]), 'growth':len(output[name])-len(parsed.data),
                       'moved':[c for c in changes if c['offset'] != c['destination']]}
    return output, report


def comparison_text(catalog):
    """One source block, with distinct translation variants and all locations."""
    groups = {}
    total = 0
    for name, file in catalog['files'].items():
        for row in file['entries']:
            variants = groups.setdefault(row['source'], {})
            locations = variants.setdefault(row['translation'], [])
            kind = 'MSG' if row['opcode'] == 0x21 else 'PRINT'
            locations.append(f"{name} @0x{row['offset']:04X} {kind}")
            total += 1

    def visible(text):
        return ''.join(f'\\x{ord(c):02x}' if ord(c) < 0x20 and c != '\n' else c for c in text)

    def layout(text):
        return visible(text).replace(' ', '[SP]').replace('\u3000', '[FWSP]').replace('\n', '[NL]\n')

    def counts(text):
        return f"반각 {text.count(' ')} / 전각 {text.count(chr(0x3000))} / 개행 {text.count(chr(10))}"

    lines = ['think 원문 ↔ 현재 번역 대응표',
             f'전체 {total}개 위치 / 중복 제거 원문 {len(groups)}개',
             '동일 원문은 한 번만 기재. 번역이 다르면 번역별로 사용 위치를 나눔.',
             '공백·개행·제어문자가 다른 원문은 별도 항목으로 보존함.',
             '기술명뿐 아니라 대사·튜토리얼·상태 메시지·PRINT까지 모두 수록.',
             '배치 확인: [SP]=반각 공백, [FWSP]=전각 공백, [NL]=개행. 이 표시는 편집용 번역이 아님.',
             r'제어문자는 \xNN으로 표시. 개행과 전각/반각 공백은 원문 그대로.',
             'PRINT(0x1A)는 분석한 retail 경로에서 화면 출력을 하지 않음.',
             '번역 없음은 기존 TXT에 없던 항목. BIN 빌드 시 원문을 유지함.', '']
    for index, (source, variants) in enumerate(groups.items(), 1):
        lines += [f'===== [{index:04d}] =====', '[원문]', visible(source), '[/원문]',
                  f'[원문 배치: {counts(source)}]', layout(source), '[/원문 배치]']
        for number, (translation, locations) in enumerate(variants.items(), 1):
            lines += [f'[현재 번역 {number}]',
                      visible(translation) if translation is not None else '(번역 없음 — 원문 유지)',
                      '[/현재 번역]']
            if translation is not None:
                lines += [f'[번역 배치: {counts(translation)}]', layout(translation), '[/번역 배치]']
            lines += ['[사용 위치]', *locations, '[/사용 위치]']
        lines.append('')
    return '\n'.join(lines)+'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['extract','build','compare'])
    parser.add_argument('--source',type=Path,default=DEFAULT_SOURCE)
    parser.add_argument('--catalog',type=Path,default=Path(__file__).with_name('translations.json'))
    parser.add_argument('--table',type=Path,default=DEFAULT_SOURCE/'XENOSAGA_KOR-JPN.json')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--check',action='store_true',help='Validate entire build without writing output')
    args = parser.parse_args()
    if args.command == 'compare':
        if args.output is None or args.output.exists():
            raise ValueError('Specify a new .txt path with --output')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(comparison_text(read_json(args.catalog)), encoding='utf-8-sig')
        print(f'Comparison written: {args.output}')
        return
    if args.command == 'extract':
        if args.catalog.exists():
            raise ValueError('Extraction never overwrites an existing translation catalog; choose fresh paths')
        catalog = extract(args.source)
        save_json(args.catalog,catalog)
        print(f"Extracted {len(catalog['files'])} thinks")
        return
    thinks, report = build_thinks(read_json(args.catalog),args.source,read_json(args.table)['replace-table'])
    if not args.check:
        if args.output is None:
            raise ValueError('Specify --output (a new directory) or --check')
        out = args.output.resolve()
        source = args.source.resolve()
        if out.exists() or out == source or source in out.parents or out in source.parents:
            raise ValueError('Output must be a new directory separate from the original inputs')
        out.mkdir(parents=True)
        for name,data in thinks.items():
            (out/name).write_bytes(data)
        save_json(out/'build-report.json',{'think':report,
                  'catalog_sha256':digest(args.catalog.read_bytes()),
                  'table_sha256':digest(args.table.read_bytes())})
    print(f"PASS: {len(thinks)} thinks; {sum(len(r['moved']) for r in report.values())} expanded strings")


if __name__ == '__main__':
    main()
