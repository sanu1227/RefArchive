import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import analysis_tools
from archive import ArchiveError, Library, clean_text_rule
from test_archive import picture
from test_documents_dates import office
from test_features import pdf


class TextRulesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library(Path(self.temporary.name) / 'data')

    def rule(self, name='회의', mode='auto', fields=None, terms=None, match='any'):
        self.library.taxonomy({'kind': 'tags', 'name': name})
        self.library.text_rule({'name': name, 'rule': {'mode': mode, 'fields': fields or ['title', 'content'],
            'terms': ['회의'] if terms is None else terms, 'match': match}})

    def document(self, title='회의록', body='안건을 논의했습니다.', **metadata):
        return self.library.add_file(body.encode(), {'filename': 'test.txt', 'title': title, **metadata})['id']

    def item(self, key):
        return next(item for item in self.library.state()['items'] if item['id'] == key)

    def wait(self):
        deadline = time.monotonic() + 10
        while self.library.job['running'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.library.job['running'])
        self.assertEqual(self.library.job['errors'], [])

    def test_office_body_and_title_without_model_or_training_examples(self):
        self.rule(name='참고', fields=['content'], terms=['한글'])
        with patch.object(analysis_tools.ENGINE, 'vector', side_effect=AssertionError('Text must not use CLIP')):
            for extension in ('.docx', '.xlsx', '.pptx'):
                key = self.library.add_file(office(extension), {'filename': '자료' + extension})['id']
                self.library.classify(key)
                self.assertEqual(self.item(key)['tags'], ['참고'] if extension != '.pptx' else [])
            self.rule()
            key = self.document()
            self.library.queue_import(key); self.wait()
        self.assertEqual(self.item(key)['autoTags'], ['회의'])
        tag = next(tag for tag in self.library.state()['tags'] if tag['name'] == '회의')
        self.assertEqual(tag['examples'], 0)
        self.assertFalse(tag['eligible'])
        prediction = next(p for p in self.library.state()['predictions'] if p['item'] == key)
        self.assertEqual(prediction['source'], 'text')
        self.assertEqual(prediction['matches'], [{'term': '회의', 'field': 'title', 'page': 0}])

    def test_all_terms_across_fields_unicode_spaces_and_bundle(self):
        self.rule(fields=['title', 'filename', 'note', 'content'], terms=['회의 결과', 'REPORT', '예산'], match='all')
        key = self.document(title='회의\t결과', body='예산안', filename='Ｒｅｐｏｒｔ.txt')
        self.library.classify(key)
        self.assertEqual(self.item(key)['tags'], ['회의'])
        second = self.document(title='자료', body='다른 내용')
        self.library.classify(second)
        self.assertEqual(self.item(second)['tags'], [])
        bundle = self.library.bundle({'action': 'create', 'ids': [key, second], 'title': '공동 제목'})['id']
        self.library.update({'id': bundle, 'title': '공동 제목', 'note': '공통 메모', 'tags': [], 'categories': []})
        self.rule(name='묶음', fields=['title', 'note'], terms=['공동', '공통'], match='all')
        self.library.classify(second)
        self.assertIn('묶음', self.item(second)['tags'])

    def test_reclassification_mode_changes_and_manual_decisions(self):
        self.rule(mode='suggest')
        key = self.document()
        self.library.classify(key)
        self.assertEqual(self.item(key)['tags'], [])
        self.rule(mode='auto'); self.library.classify(key)
        self.assertEqual(self.item(key)['tags'], ['회의'])
        self.rule(terms=['일치하지 않음']); self.library.classify(key)
        self.assertEqual(self.item(key)['tags'], [])
        self.assertEqual(self.library.state()['predictions'], [])
        self.rule(); self.library.classify(key)
        self.rule(mode='off'); self.library.classify(key)
        self.assertEqual(self.item(key)['tags'], [])
        self.rule(); self.library.classify(key)
        self.library.review({'id': key, 'tag': '회의', 'action': 'accept'})
        self.rule(terms=['다른 문구']); self.library.classify(key)
        self.assertEqual(self.item(key)['tags'], ['회의'])
        self.assertEqual(self.item(key)['autoTags'], [])
        rejected = self.document(body='거절할 문서')
        self.rule(); self.library.classify(rejected)
        self.library.review({'id': rejected, 'tag': '회의', 'action': 'reject'})
        self.library.classify(rejected)
        self.assertEqual(self.item(rejected)['tags'], [])
        manual = self.document(body='수동 분류', tags=['회의'])
        self.library.classify(manual)
        self.assertEqual(self.item(manual)['tags'], ['회의'])
        self.assertEqual(self.item(manual)['autoTags'], [])

    def test_pdf_extract_ocr_completion_and_corrected_content(self):
        self.rule(fields=['content'], terms=['LIGHTING'])
        key = self.library.add_pdf(pdf(), {'filename': 'reference.pdf'})['id']
        original = self.library.media(key)[0]
        self.library.queue_import(key); self.wait()
        self.assertEqual(self.item(key)['tags'], ['회의'])
        self.assertFalse(self.item(key)['analysis']['summarized'])
        self.assertEqual(self.library.state()['predictions'][0]['matches'][0]['page'], 2)
        self.library.analysis_edit({'id': key, 'page': 2, 'text': '수정한 본문'})
        self.library.classify(key)
        self.assertEqual(self.item(key)['tags'], [])
        self.assertEqual(self.library.media(key)[0], original)
        image = self.library.add_image(picture(), {'filename': 'scan.png'})['id']
        with patch.object(analysis_tools, 'ocr', return_value='LIGHTING'):
            self.library.start_job({'operation': 'text', 'ids': [image], 'ocr': True}); self.wait()
        self.assertEqual(self.item(image)['tags'], ['회의'])
        self.assertEqual(self.item(image)['autoTags'], ['회의'])

    def test_text_priority_and_image_fallback_preserve_independent_settings(self):
        self.rule(mode='suggest', fields=['title'])
        for number in range(10):
            self.library.add_image(picture((number * 20, 0, 0)), {'filename': f'{number}.png', 'tags': ['회의']})
        with patch.object(analysis_tools, 'MODEL', Path(__file__)):
            self.library.mode({'name': '회의', 'mode': 'auto'})
        self.assertEqual(self.library.state()['tags'][0]['textRule']['mode'], 'suggest')
        key = self.library.add_image(picture('blue'), {'filename': '회의.png'})['id']
        with patch.object(analysis_tools.ENGINE, 'vector', return_value=np.array([1., 0.])) as vector:
            self.library.classify(key)
            vector.assert_not_called()
            self.assertEqual(self.library.state()['predictions'][0]['status'], 'pending')
            self.rule(mode='off'); self.library.classify(key)
        self.assertEqual(self.item(key)['autoTags'], ['회의'])
        self.assertNotEqual(self.library.state()['predictions'][0].get('source'), 'text')

    def test_backup_restore_rename_and_duplicate_manual_confirmation(self):
        self.rule()
        key = self.document()
        self.library.classify(key)
        self.library.taxonomy({'kind': 'tags', 'action': 'rename', 'name': '회의', 'target': '회의록'})
        backup = io.BytesIO(); self.library.backup(backup)
        other = Library(Path(self.temporary.name) / 'restored')
        other.restore(backup.getvalue())
        for field in ('items', 'tags', 'predictions'):
            self.assertEqual(other.state()[field], self.library.state()[field])
        self.assertEqual(other.media(key), self.library.media(key))
        self.library.add_file('안건을 논의했습니다.'.encode(), {'filename': 'copy.txt', 'tags': ['회의록']})
        self.assertEqual(self.item(key)['autoTags'], [])
        self.assertEqual(self.library.state()['predictions'][0]['status'], 'accepted')
        self.library.taxonomy({'kind': 'tags', 'action': 'delete', 'name': '회의록'})
        self.assertEqual(self.library.state()['predictions'], [])
        self.assertEqual(self.library.media(key)[0], '안건을 논의했습니다.'.encode())

    def test_scope_documents_links_bundles_type_and_classified_history(self):
        self.rule()
        first, second = self.document(), self.document(body='두번째 문서')
        link = self.library.add_link({'title': '회의 링크', 'source': 'https://example.com'})['id']
        bundle = self.library.bundle({'ids': [first, second], 'title': '회의 묶음'})['id']
        self.assertEqual(set(self.library.classification_ids({'type': 'document'})), {first, second})
        self.assertEqual(set(self.library.classification_ids({'mode': 'ids', 'ids': [bundle, link]})), {first, second, link})
        self.library.start_job({'operation': 'classify', 'scope': {'mode': 'new', 'type': 'document'}}); self.wait()
        self.assertEqual(self.library.job['total'], 2)
        self.assertEqual(self.library.classification_ids({'mode': 'new', 'type': 'document'}), [])
        self.assertEqual(self.item(link)['tags'], [])
        self.library.batch({'action': 'trash', 'ids': [bundle]})
        self.assertEqual(self.library.classification_ids({'type': 'document'}), [])

    def test_invalid_rules_and_backup_validation(self):
        valid = {'mode': 'auto', 'fields': ['title'], 'match': 'any', 'terms': ['회의']}
        for change in ({'mode': 'bad'}, {'fields': []}, {'fields': ['unknown']}, {'terms': []}, {'terms': [' ']},
                       {'terms': ['x' * 101]}, {'terms': ['x'] * 31}, {'terms': '회의'}, {'match': 'bad'}, {'other': True}):
            with self.subTest(change=change), self.assertRaises(ArchiveError):
                clean_text_rule({**valid, **change})
        self.rule()
        self.library.job['running'] = True
        with self.assertRaises(ArchiveError): self.rule(mode='suggest')
        self.library.job['running'] = False
        self.assertEqual(self.library.state()['tags'][0]['textRule']['mode'], 'auto')
        with self.library.db() as connection:
            self.library.put_extra(connection, 'mode', '회의', {'mode': 'off', 'threshold': .9, 'textRule': {**valid, 'fields': ['bad']}})
        backup = io.BytesIO(); self.library.backup(backup)
        other = Library(Path(self.temporary.name) / 'existing')
        kept = other.add_link({'source': 'https://example.com', 'title': '보존'})['id']
        with self.assertRaises(ArchiveError): other.restore(backup.getvalue())
        self.assertEqual(other.state()['items'][0]['id'], kept)


if __name__ == '__main__':
    unittest.main()
