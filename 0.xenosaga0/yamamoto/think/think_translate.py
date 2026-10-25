"""Translate shared think strings and build adjacent thinkNNN.bin from .bin.ori."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from think_vm import original
from text_expand import expand, LIMIT

ENCODING = 'euc_jis_2004'
HERE = Path(__file__).resolve().parent

def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


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

def expand_catalog(shared, source=HERE):
    if shared['version'] != 2:
        raise ValueError('Expected shared translation format version 2')
    originals = {p.name.removesuffix('.ori') for p in source.glob('think*.bin.ori')}
    if set(shared['original_sha256']) != originals:
        raise ValueError('Original file list mismatch')
    files = {}
    expected = {}
    for name, sha in shared['original_sha256'].items():
        if not re.fullmatch(r'think[0-9]{3}\.bin', name):
            raise ValueError('Invalid think filename')
        parsed = original(source/(name+'.ori'))
        if digest(parsed.data) != sha:
            raise ValueError(f'{name}: Original hash mismatch')
        files[name] = {'source_sha256':sha, 'entries':[]}
        for offset, ins in (parsed.instructions | parsed.linear_candidates()).items():
            if ins.text is not None:
                key = f'{name}:0x{offset:04X}'
                expected[key] = (name, offset, ins.op, ins.text.decode(ENCODING))
    seen = set(); ids = set(); sources = set()
    for row in shared['strings']:
        if row['id'] in ids or row['source'] in sources:
            raise ValueError('Duplicate string ID or source')
        ids.add(row['id']); sources.add(row['source'])
        overrides = row.get('overrides', {})
        if not set(overrides).issubset(row['locations']):
            raise ValueError(f"{row['id']}: Override outside its locations")
        for key in row['locations']:
            if key in seen or key not in expected:
                raise ValueError(f'Duplicate/invalid location: {key}')
            seen.add(key)
            name, offset, op, text = expected[key]
            if text != row['source']:
                raise ValueError(f'{key}: Source mismatch')
            files[name]['entries'].append({'offset':offset, 'opcode':op, 'source':text,
                'translation':overrides.get(key,row['translation'])})
    if seen != expected.keys():
        raise ValueError('Missing text locations')
    return {'version':1, 'files':files}


def build(shared):
    # Always rebuild from originals; never use previously patched BIN as input.
    return build_thinks(expand_catalog(shared), HERE,
                        read_json(HERE/'XENOSAGA_KOR-JPN.json')['replace-table'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check','build'])
    args = parser.parse_args()
    outputs, report = build(read_json(HERE/'translations.json'))
    if args.command == 'build':
        # All files have been validated and encoded before touching any output.
        for name, data in outputs.items():
            (HERE/name).write_bytes(data)
        save_json(HERE.parents[2]/'codex-lab/think-reports/think_build_report.json', report)
    print(f"PASS: {len(outputs)} BIN, {sum(len(r['moved']) for r in report.values())} expanded strings")


if __name__ == '__main__':
    main()
