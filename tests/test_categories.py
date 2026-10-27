import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import analysis_tools
import category_tools
from archive import ArchiveError, Library
from test_features import pdf


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library(Path(self.temporary.name) / 'data')

    def rule(self, name='회의', **changes):
        self.library.taxonomy({'kind': 'categories', 'name': name})
        self.library.category_rule({'name': name, 'rule': {**category_tools.DEFAULT, **changes}})

    def document(self, title='회의록', body='결정 사항을 기록합니다.', **metadata):
        return self.library.add_file(body.encode(), {'filename': '자료.txt', 'title': title, **metadata})['id']

    def item(self, key):
        return next(item for item in self.library.state()['items'] if item['id'] == key)

    def decisions(self, key):
        return [row for row in self.library.state()['categoryPredictions'] if row['item'] == key]

    def wait(self):
        deadline = time.monotonic() + 10
        while self.library.job['running'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.library.job['running'])
        self.assertEqual(self.library.job['errors'], [])

    def test_category_name_only_upload_queue_no_model_and_date_preserved(self):
        self.library.taxonomy({'kind': 'categories', 'name': '회의'})
        key = self.document(material_date='2024-02-29')
        with patch.object(analysis_tools.ENGINE, 'vector', side_effect=AssertionError('No model needed')):
            self.library.queue_import(key); self.wait()
        self.assertEqual(self.item(key)['autoCategories'], ['회의'])
        self.assertEqual(self.item(key)['material_date'], '2024-02-29')
        self.assertEqual(self.item(key)['tags'], [])
        self.assertEqual(self.library.state()['categoryRules'][0]['examples'], 0)
        self.assertEqual(self.decisions(key)[0]['reasons'][0]['field'], 'title')

    def test_collision_exclusion_and_weak_body_abstain(self):
        self.rule('회의'); self.rule('예산')
        key = self.document('회의 예산안')
        self.library.classify(key)
        self.assertEqual(self.item(key)['categories'], [])
        self.assertEqual({row['status'] for row in self.decisions(key)}, {'pending'})
        self.rule('예산', exclude=['회의'])
        self.library.classify(key)
        self.assertEqual(self.item(key)['autoCategories'], ['회의'])
        other = self.document('주간 기록', '이번 주 회의에서 안건을 논의했다.')
        self.library.classify(other)
        self.assertEqual(self.item(other)['categories'], [])
        self.assertEqual(self.decisions(other)[0]['status'], 'pending')

    def test_explicit_body_phrases_and_pdf_extraction(self):
        self.rule('재무', include=['예산', '결산'])
        key = self.document('일반 자료', '예산 및 결산의 세부 수치')
        self.library.classify(key)
        self.assertEqual(self.item(key)['autoCategories'], ['재무'])
        self.rule('reference', include=['lighting', 'shadows'])
        key = self.library.add_file(pdf(), {'filename': 'plain.pdf'})['id']
        self.library.classify(key)
        self.assertIsNotNone(self.item(key)['analysis'])
        self.assertEqual(self.item(key)['autoCategories'], ['reference'])
        # Real PDF contents, without OCR or a model download.
        text = ' '.join(page['text'] for page in self.item(key)['analysis']['pages'])
        self.assertTrue(text.strip())

    def test_reclassification_modes_and_manual_decisions(self):
        self.rule(mode='suggest'); key = self.document()
        self.library.classify(key); self.assertEqual(self.item(key)['categories'], [])
        self.rule(); self.library.classify(key); self.assertEqual(self.item(key)['autoCategories'], ['회의'])
        self.rule(exclude=['회의']); self.library.classify(key); self.assertEqual(self.item(key)['categories'], [])
        self.rule(); self.library.classify(key)
        self.library.category_review({'id': key, 'name': '회의', 'action': 'accept'})
        self.rule(mode='off'); self.library.classify(key)
        self.assertEqual(self.item(key)['categories'], ['회의'])
        self.assertEqual(self.item(key)['autoCategories'], [])
        self.assertEqual(self.library.state()['categoryRules'][0]['examples'], 1)
        rejected = self.document(body='다른 회의 내용')
        self.rule(); self.library.classify(rejected)
        self.library.category_review({'id': rejected, 'name': '회의', 'action': 'reject'})
        self.library.classify(rejected)
        self.assertEqual(self.item(rejected)['categories'], [])
        self.assertEqual(self.decisions(rejected)[0]['status'], 'rejected')

    def test_manual_assignment_and_batch_removal_preserved(self):
        self.rule(); key = self.document(categories=['개인'])
        self.library.classify(key)
        self.assertEqual(self.item(key)['categories'], ['개인'])
        self.library.batch({'ids': [key], 'action': 'add', 'categories': ['회의']})
        self.library.classify(key)
        self.assertEqual(self.item(key)['autoCategories'], [])
        self.library.batch({'ids': [key], 'action': 'remove', 'categories': ['회의']})
        self.library.classify(key)
        self.assertNotIn('회의', self.item(key)['categories'])
        manual = self.document(body='처음부터 직접 분류한 자료', categories=['회의'])
        self.library.update({'id': manual, 'title': '회의록', 'categories': [], 'tags': []})
        self.library.classify(manual)
        self.assertEqual(self.item(manual)['categories'], [])
        self.assertEqual(self.decisions(manual)[0]['status'], 'rejected')

    def test_examples_confirmed_only_independent_bundles_and_no_self_learning(self):
        self.rule('업무')
        body = '협업 프로젝트 개발 진행 상황을 공유하고 다음 작업의 담당자와 마감 일정을 결정했습니다.'
        ids = [self.document(f'기록 {n}', body + f' 기록 번호 {n}', categories=['업무']) for n in range(3)]
        key = self.document('새 기록', body + ' 추가 사항')
        self.library.classify(key)
        self.assertEqual(self.item(key)['autoCategories'], ['업무'])
        self.assertEqual(set(self.decisions(key)[0]['evidence']), set(ids))
        self.assertEqual(self.library.state()['categoryRules'][0]['examples'], 3)
        self.library.bundle({'ids': ids, 'title': '하나의 회의'})
        self.library.classify(key)
        self.assertEqual(self.item(key)['categories'], [])
        self.assertEqual(self.library.state()['categoryRules'][0]['examples'], 1)

    def test_duplicate_upload_confirmation_and_rename_merge_delete(self):
        self.rule(); key = self.document()
        self.library.classify(key)
        self.library.taxonomy({'kind': 'categories', 'action': 'rename', 'name': '회의', 'target': '회의록'})
        self.assertEqual(self.item(key)['autoCategories'], ['회의록'])
        self.library.add_file('결정 사항을 기록합니다.'.encode(), {'filename': 'copy.txt', 'categories': ['회의록']})
        self.assertEqual(self.item(key)['autoCategories'], [])
        self.rule('기록', include=['회의록']); self.library.classify(key)
        self.library.taxonomy({'kind': 'categories', 'action': 'merge', 'name': '기록', 'target': '회의록'})
        self.assertEqual(self.item(key)['autoCategories'], [])
        self.library.taxonomy({'kind': 'categories', 'action': 'delete', 'name': '회의록'})
        self.assertEqual(self.decisions(key), [])
        self.assertEqual(self.library.media(key)[0], '결정 사항을 기록합니다.'.encode())

    def test_auto_merge_into_manual_and_rejected_target(self):
        self.rule(); key = self.document()
        self.library.classify(key)
        self.library.batch({'ids': [key], 'action': 'add', 'categories': ['업무']})
        self.library.taxonomy({'kind': 'categories', 'action': 'merge', 'name': '회의', 'target': '업무'})
        self.assertEqual(self.item(key)['autoCategories'], [])
        self.rule('업무', mode='off'); self.library.classify(key)
        self.assertEqual(self.item(key)['categories'], ['업무'])

    def test_scope_saved_filters_and_original_backup_roundtrip(self):
        self.rule(); key = self.document(); blank = self.document('잡동사니', '산책 사진')
        self.library.classify(key)
        self.assertEqual(self.library.classification_ids({'mode': 'uncategorized'}), [blank])
        self.library.saved({'kind': 'search', 'name': '미지정', 'filters': {'view': 'uncategorized', 'group': 'registered-day'}})
        backup = io.BytesIO(); self.library.backup(backup)
        restored = Library(Path(self.temporary.name) / 'restored'); restored.restore(backup.getvalue())
        for field in ('items', 'categoryRules', 'categoryPredictions', 'searches'):
            self.assertEqual(restored.state()[field], self.library.state()[field])
        self.assertEqual(restored.media(key), self.library.media(key))
        self.library.batch({'ids': [key], 'action': 'trash'})
        self.library.start_job({'operation': 'classify', 'scope': {'mode': 'all'}}); self.wait()
        self.assertEqual(self.library.job['total'], 1)

    def test_invalid_rule_and_backup_rejected_before_replacing_library(self):
        self.rule(); key = self.document(); self.library.classify(key)
        for change in ({'mode': 'bad'}, {'include': 'a'}, {'exclude': ['']}, {'include': ['a'] * 31}, {'description': 3}, {'include': ['ABC'], 'exclude': ['abc']}):
            with self.subTest(change=change), self.assertRaises(ArchiveError):
                self.rule(**change)
        self.library.job['running'] = True
        with self.assertRaises(ArchiveError): self.rule(mode='off')
        self.library.job['running'] = False
        with self.library.db() as connection:
            record = self.library.get_extra(connection, 'preferences', 'category:' + key)
            record['decisions'][0]['evidence'] = ['0' * 32]
            self.library.put_extra(connection, 'preferences', 'category:' + key, record)
        backup = io.BytesIO(); self.library.backup(backup)
        restored = Library(Path(self.temporary.name) / 'existing')
        kept = restored.add_link({'title': '보존', 'source': 'https://example.com'})['id']
        with self.assertRaises(ArchiveError): restored.restore(backup.getvalue())
        self.assertEqual(restored.state()['items'][0]['id'], kept)

    def test_title_boundary_and_description_is_only_support(self):
        self.rule('art')
        key = self.document('party', 'party invitation')
        self.library.classify(key); self.assertEqual(self.decisions(key), [])
        self.rule('자료', description='프로젝트 개발 진행 상황 공유 담당자 일정 결정')
        key = self.document('기록', '프로젝트 개발 진행 상황 공유 담당자 일정 결정', filename='plain.txt')
        self.library.classify(key)
        self.assertEqual(self.item(key)['categories'], [])
        self.assertEqual(self.decisions(key)[0]['status'], 'pending')

    def test_overlapping_and_case_duplicate_phrases_are_not_two_signals(self):
        self.rule('기록', include=['MEETING', 'meeting', 'meeting notes'])
        key = self.document('파일', 'meeting notes from today', filename='plain.txt')
        self.library.classify(key)
        self.assertEqual(self.item(key)['categories'], [])
        self.assertEqual(self.decisions(key)[0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
