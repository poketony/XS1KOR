import copy
import unittest
from pathlib import Path

import pipeline as p
from think_vm import original, instruction, u16
from text_expand import expand

HERE = Path(__file__).resolve().parent


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = p.read_json(HERE/'translations.json')
        cls.table = p.read_json(p.DEFAULT_SOURCE/'XENOSAGA_KOR-JPN.json')['replace-table']
        cls.thinks, cls.report = p.build_thinks(cls.catalog,p.DEFAULT_SOURCE,cls.table)

    def test_catalog_matches_original_reset_txt(self):
        count = 0
        for name,file in self.catalog['files'].items():
            rows = {e['offset']:e for e in file['entries']}
            for offset,op,flag,text in p.legacy_rows(p.DEFAULT_SOURCE/name.replace('.bin','.txt')):
                if offset in rows:
                    self.assertEqual(rows[offset]['translation'],text)
                    count += 1
            parsed = original(p.DEFAULT_SOURCE/(name+'.ori'))
            for row in file['entries']:
                ins = instruction(self.thinks[name],row['offset'])
                expected = parsed.data[row['offset']+2:parsed.data.index(0,row['offset']+2)]
                if row['translation'] is not None and row['translation'] != row['source']:
                    expected = p.encode(row['translation'],self.table)
                self.assertTrue(ins.text.startswith(expected))
        self.assertEqual(count,1192)
        self.assertEqual(len(self.catalog['ignored_legacy_entries']),0)
        self.assertEqual(sum(len(f['entries']) for f in self.catalog['files'].values()),1192)

    def test_original_roundtrip_and_every_string_expansion(self):
        cases = 0
        for path in sorted(p.DEFAULT_SOURCE.glob('*.bin.ori')):
            parsed = original(path)
            texts = {o:i for o,i in (parsed.instructions|parsed.linear_candidates()).items() if i.text is not None}
            self.assertEqual(expand(parsed,{o:i.text for o,i in texts.items()})[0],parsed.data)
            for offset,ins in texts.items():
                for extra in (31,32):
                    raw = ins.text+b'X'*extra
                    output,changes = expand(parsed,{offset:raw})
                    self.assertEqual(u16(output,offset),1)
                    target = u16(output,offset+2)
                    moved = instruction(output,target)
                    self.assertEqual((moved.text,moved.args),(raw,ins.args))
                    back = instruction(output,moved.end)
                    self.assertEqual((back.op,back.args),(1,(ins.end,)))
                    self.assertEqual(output[:offset],parsed.data[:offset])
                    self.assertEqual(output[offset+4:len(parsed.data)],parsed.data[offset+4:])
                    cases += 1
        self.assertEqual(cases,2384)

    def test_long_korean_translation_through_catalog(self):
        catalog = copy.deepcopy(self.catalog)
        rows = catalog['files']['think053.bin']['entries']
        targets = [row for row in rows if row['opcode']==0x21][:2]
        for row in targets:
            row['translation'] += ' 원래 길이보다 긴 번역을 그대로 표시합니다．' * 4
        output, report = p.build_thinks(catalog,p.DEFAULT_SOURCE,self.table)
        self.assertEqual(len(report['think053.bin']['moved']),2)
        for row in targets:
            target = u16(output['think053.bin'],row['offset']+2)
            moved = instruction(output['think053.bin'],target)
            self.assertEqual(moved.text,p.encode(row['translation'],self.table))
        self.assertEqual(p.build_thinks(catalog,p.DEFAULT_SOURCE,self.table)[0],output)

    def test_comparison_groups_preserve_variants(self):
        catalog = {'files':{'think000.bin':{'entries':[
            {'source':'A\x19\n','translation':'B','offset':2,'opcode':33},
            {'source':'A\x19\n','translation':'B','offset':20,'opcode':33},
            {'source':'A\x19\n','translation':'C','offset':40,'opcode':26}]}}}
        text = p.comparison_text(catalog)
        self.assertEqual(text.count('[원문]'),1)
        self.assertEqual(text.count('[현재 번역 '),2)
        self.assertIn('A\\x19\n',text)
        for offset in ('0x0002','0x0014','0x0028'):
            self.assertIn(offset,text)

    def test_fail_closed(self):
        parsed = original(p.DEFAULT_SOURCE/'think053.bin.ori')
        offset = next(o for o,i in parsed.instructions.items() if i.text is not None)
        for replacements in ({0:b'x'},{offset:b'a\0b'},{offset:b'x'*16384}):
            with self.assertRaises(ValueError):expand(parsed,replacements)
        for mutation in ('source','duplicate','missing','hash'):
            catalog = copy.deepcopy(self.catalog)
            f = catalog['files']['think000.bin']
            if mutation=='source':f['entries'][0]['source']='wrong'
            elif mutation=='duplicate':f['entries'].append(f['entries'][0])
            elif mutation=='missing':f['entries'].pop()
            else:f['source_sha256']='wrong'
            with self.assertRaises(ValueError):p.build_thinks(catalog,p.DEFAULT_SOURCE,self.table)



if __name__ == '__main__':
    unittest.main()
