import io
from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import analysis_tools
from archive import ArchiveError, Library, SCHEMA_V1
import test_archive
from test_archive import picture, multipart


def pdf():
    # Minimal PDF fixture with real page text, no development-only PDF dependency.
    lines = [b'Archive search stores useful reference documents. Reference documents contain searchable text.',
             b'Color and lighting are important in illustration. Lighting explains how shadows appear in a drawing.']
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>']
    for index, text in enumerate(lines):
        stream = b'BT /F1 12 Tf 50 720 Td (' + text + b') Tj ET'
        objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents {4 + index * 2} 0 R >>'.encode(),
                    f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream']
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    output, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(output)
    output.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temporary.name) / 'data')

    def tearDown(self):
        deadline = time.monotonic() + 10
        while self.library.job['running'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.temporary.cleanup()

    def add(self, color='red', **metadata):
        return self.library.add_image(picture(color), {'filename': 'test.png', **metadata})['id']

    def examples(self, tag='갑옷'):
        return [self.add((index, 20, 30), tags=[tag]) for index in range(10)]

    def test_saved_search_collection_persistence_rename_and_restore(self):
        item = self.add(tags=['숲'], categories=['환경'])
        filters = {'query': '역광', 'tags': ['숲'], 'exclude': ['사진'], 'category': '환경', 'type': 'image', 'match': 'any'}
        search = self.library.saved({'kind': 'search', 'name': '작업 참고', 'filters': filters})['id']
        collection = self.library.saved({'kind': 'collection', 'name': '프로젝트', 'ids': [item, item]})['id']
        self.library.taxonomy({'kind': 'tags', 'name': '숲', 'target': '자연', 'action': 'rename'})
        state = Library(self.library.data).state()
        self.assertEqual(state['searches'][0]['filters']['tags'], ['자연'])
        self.assertEqual(state['collections'][0]['ids'], [item])
        self.library.taxonomy({'kind': 'tags', 'name': '자연', 'action': 'delete'})
        # A removed predicate remains a predicate; silently widening the saved search is incorrect.
        self.assertEqual(self.library.state()['searches'][0]['filters']['tags'], ['자연'])
        with self.assertRaises(ArchiveError):
            self.library.saved({'kind': 'search', 'name': '작업 참고', 'filters': {}})
        with self.assertRaises(ArchiveError):
            self.library.saved({'kind': 'collection', 'name': '오류', 'ids': ['f' * 32]})
        output = io.BytesIO(); self.library.backup(output)
        other = Library(Path(self.temporary.name) / 'other'); other.restore(output.getvalue())
        self.assertEqual(other.state()['searches'], self.library.state()['searches'])
        self.assertEqual(other.state()['collections'], self.library.state()['collections'])
        other.saved({'kind': 'collection', 'id': collection, 'action': 'delete'})
        other.saved({'kind': 'search', 'id': search, 'action': 'delete'})
        self.assertEqual(other.media(item)[0], picture())

    def test_pdf_original_pages_summary_correction_and_backup(self):
        with self.assertRaises(ValueError): self.library.add_pdf(picture(), {'filename': 'fake.pdf'})
        content = pdf()
        item = self.library.add_file(content, {'filename': 'reference.pdf', 'tags': ['강의']})['id']
        self.assertEqual(self.library.media(item)[0], content)
        self.assertEqual(self.library.state()['items'][0]['type'], 'pdf')
        result = self.library.analyze_text(item)
        self.assertEqual(len(result['pages']), 2)
        self.assertIn('lighting', result['pages'][1]['text'])
        self.assertTrue(result['summary'])
        self.assertTrue(all(sentence['text'] in result['pages'][sentence['page'] - 1]['text'] for sentence in result['summary']))
        self.library.analysis_edit({'id': item, 'page': 2, 'text': 'Corrected lighting explains the shadows clearly.'})
        self.assertEqual(self.library.state()['items'][0]['analysis']['pages'][1]['method'], 'corrected')
        duplicate = self.library.add_pdf(content, {'filename': 'copy.pdf', 'tags': ['PDF']})
        self.assertTrue(duplicate['duplicate'])
        output = io.BytesIO(); self.library.backup(output)
        other = Library(Path(self.temporary.name) / 'other'); other.restore(output.getvalue())
        self.assertEqual(other.state()['items'], self.library.state()['items'])
        self.assertEqual(other.media(item)[0], content)

    def test_old_database_and_backup_upgrade_preserve_original(self):
        item = self.add(tags=['이전'])
        old = Path(self.temporary.name) / 'v1.sqlite3'
        with closing(sqlite3.connect(old)) as target, self.library.db() as source:
            target.executescript(SCHEMA_V1)
            for table in ('items', 'tags', 'categories', 'item_tags', 'item_categories'):
                columns = ','.join(row[1] for row in target.execute(f'PRAGMA table_info({table})'))
                rows = list(source.execute(f'SELECT {columns} FROM {table}'))
                if rows: target.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in rows[0])})", [tuple(row) for row in rows])
            target.commit()
        old_content = old.read_bytes()
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('manifest.json', json.dumps({'format': 'RefArchive', 'version': 1}))
            archive.writestr('archive.sqlite3', old_content)
            archive.writestr(f'files/{item}.png', picture())
            archive.writestr(f'thumbs/{item}.jpg', self.library.media(item, True)[0])
        other = Library(Path(self.temporary.name) / 'restored'); other.restore(output.getvalue())
        self.assertEqual(other.media(item)[0], picture())
        self.assertEqual(other.state()['searches'], [])
        (other.root / 'archive.sqlite3').write_bytes(old_content)
        reopened = Library(other.data)
        self.assertEqual(reopened.state()['items'][0]['id'], item)
        self.assertTrue(list((other.data / 'backups').glob('migration-v1-*.sqlite3')))

    @patch.object(analysis_tools.ENGINE, 'vector', return_value=np.array([1., 0.]))
    def test_classification_modes_confirmation_rejection_and_threshold(self, vector):
        ids = self.examples()
        self.library.mode({'name': '갑옷', 'mode': 'suggest', 'threshold': .8})
        target = self.add('blue')
        self.library.classify(target)
        result = self.library.state()
        self.assertEqual(result['predictions'][0]['status'], 'pending')
        self.assertEqual(next(item for item in result['items'] if item['id'] == target)['tags'], [])
        self.library.review({'id': target, 'tag': '갑옷', 'action': 'reject'})
        self.library.classify(target)
        self.assertEqual(self.library.state()['predictions'][0]['status'], 'rejected')
        self.library.mode({'name': '갑옷', 'mode': 'auto'})
        auto = self.add('green')
        self.library.classify(auto)
        state = self.library.state()
        self.assertEqual(state['tags'][0]['examples'], 10)
        self.assertEqual(next(item for item in state['items'] if item['id'] == auto)['autoTags'], ['갑옷'])
        self.library.review({'id': auto, 'tag': '갑옷', 'action': 'accept'})
        self.assertEqual(self.library.state()['tags'][0]['examples'], 11)
        self.library.batch({'ids': ids[:2], 'action': 'trash'})
        self.assertFalse(self.library.state()['tags'][0]['eligible'])
        with self.assertRaises(ArchiveError): self.library.mode({'name': '갑옷', 'mode': 'auto'})
        waiting = self.add('yellow'); self.library.classify(waiting)
        self.assertEqual(next(item for item in self.library.state()['items'] if item['id'] == waiting)['tags'], [])
        self.library.mode({'name': '갑옷', 'mode': 'off'})
        self.assertEqual(next(item for item in self.library.state()['items'] if item['id'] == auto)['tags'], ['갑옷'])

    @patch.object(analysis_tools.ENGINE, 'vector', return_value=np.array([1., 0.]))
    def test_auto_manual_edit_and_rejected_results_survive_backup(self, vector):
        self.examples(); self.library.mode({'name': '갑옷', 'mode': 'auto'})
        item = self.add('blue'); self.library.classify(item)
        self.library.update({'id': item, 'title': '제목만 수정', 'tags': ['갑옷']})
        self.assertEqual(self.library.state()['tags'][0]['examples'], 10)
        self.assertEqual(next(value for value in self.library.state()['items'] if value['id'] == item)['autoTags'], ['갑옷'])
        self.library.batch({'ids': [item], 'action': 'remove', 'tags': ['갑옷']})
        self.library.classify(item)
        current = next(value for value in self.library.state()['items'] if value['id'] == item)
        self.assertEqual(current['autoTags'], [])
        self.assertEqual(current['tags'], [])
        self.assertEqual(self.library.state()['predictions'][0]['status'], 'rejected')
        self.library.update({'id': item, 'title': '수동 수정', 'tags': ['갑옷']})
        self.assertEqual(self.library.state()['tags'][0]['examples'], 11)
        output = io.BytesIO(); self.library.backup(output)
        other = Library(Path(self.temporary.name) / 'other'); other.restore(output.getvalue())
        self.assertEqual(other.state()['items'], self.library.state()['items'])
        self.assertEqual(other.state()['predictions'], self.library.state()['predictions'])

    def test_reclassification_updates_mode_threshold_and_preserves_manual_data(self):
        self.examples()
        target = self.add('blue', tags=['수동'], categories=['참고'], note='유지할 메모')
        original = self.library.media(target)[0]
        def vector(content):
            return np.array([.86, np.sqrt(1 - .86 ** 2)]) if content == original else np.array([1., 0.])
        def current():
            state = self.library.state()
            return next(item for item in state['items'] if item['id'] == target), [entry for entry in state['predictions'] if entry['item'] == target]
        with patch.object(analysis_tools.ENGINE, 'vector', side_effect=vector):
            self.library.mode({'name': '갑옷', 'mode': 'suggest', 'threshold': .8})
            self.library.classify(target)
            self.assertEqual(current()[1][0]['status'], 'pending')
            self.library.mode({'name': '갑옷', 'mode': 'auto', 'threshold': .8})
            self.library.classify(target)
            self.assertEqual(current()[0]['autoTags'], ['갑옷'])
            self.library.mode({'name': '갑옷', 'mode': 'suggest', 'threshold': .8})
            self.library.classify(target)
            self.assertEqual(current()[0]['tags'], ['수동'])
            self.assertEqual(current()[1][0]['status'], 'pending')
            self.library.mode({'name': '갑옷', 'mode': 'auto', 'threshold': .95})
            self.library.classify(target)
            self.assertEqual(current()[1], [])
            self.library.mode({'name': '갑옷', 'mode': 'auto', 'threshold': .8})
            self.library.classify(target)
            self.library.mode({'name': '갑옷', 'mode': 'auto', 'threshold': .95})
            self.library.classify(target)
            self.assertEqual(current()[0]['autoTags'], [])
            self.assertEqual(current()[1], [])
            self.library.mode({'name': '갑옷', 'mode': 'auto', 'threshold': .8})
            self.library.classify(target)
            self.library.mode({'name': '갑옷', 'mode': 'off'})
            self.library.classify(target)
        item, predictions = current()
        self.assertEqual(item['tags'], ['수동'])
        self.assertEqual(item['categories'], ['참고'])
        self.assertEqual(item['note'], '유지할 메모')
        self.assertEqual(item['autoTags'], [])
        self.assertEqual(predictions, [])
        self.assertTrue(item['classifiedAt'] and item['hasClassification'])
        self.assertEqual(self.library.media(target)[0], original)
        backup = io.BytesIO(); self.library.backup(backup)
        other = Library(Path(self.temporary.name) / 'scoped-restore')
        other.restore(backup.getvalue())
        self.assertEqual(other.state()['items'], self.library.state()['items'])
        self.assertNotIn(target, other.classification_ids({'mode': 'new'}))

    @patch.object(analysis_tools.ENGINE, 'vector', return_value=np.array([1., 0.]))
    def test_reclassification_preserves_reviewed_results_and_failed_analysis(self, vector):
        self.examples(); self.library.mode({'name': '갑옷', 'mode': 'auto'})
        accepted, rejected, waiting = self.add('blue'), self.add('green'), self.add('yellow')
        for item in (accepted, rejected, waiting): self.library.classify(item)
        self.library.review({'id': accepted, 'tag': '갑옷', 'action': 'accept'})
        self.library.review({'id': rejected, 'tag': '갑옷', 'action': 'reject'})
        self.library.mode({'name': '갑옷', 'mode': 'suggest'})
        before = self.library.state()
        with patch.object(analysis_tools.ENGINE, 'vector', side_effect=RuntimeError('모델 분석 실패')):
            with self.assertRaises(RuntimeError): self.library.classify(waiting)
        self.assertEqual(self.library.state(), before)
        self.library.mode({'name': '갑옷', 'mode': 'off'})
        for item in (accepted, rejected, waiting): self.library.classify(item)
        state = self.library.state()
        results = {entry['item']: entry['status'] for entry in state['predictions']}
        self.assertEqual(results, {accepted: 'accepted', rejected: 'rejected'})
        self.assertEqual(next(item for item in state['items'] if item['id'] == accepted)['tags'], ['갑옷'])
        self.assertEqual(next(item for item in state['items'] if item['id'] == rejected)['tags'], [])
        self.library.mode({'name': '갑옷', 'mode': 'auto'})
        self.library.classify(rejected)
        self.assertEqual(next(entry for entry in self.library.state()['predictions'] if entry['item'] == rejected)['status'], 'rejected')

    def test_classification_scopes_combine_conditions_and_exclude_non_images(self):
        unfiled = self.add('red')
        categorized = self.add('blue', categories=['참고'])
        tagged = self.add('green', tags=['자료'], categories=['참고'])
        trashed = self.add('yellow'); self.library.batch({'ids': [trashed], 'action': 'trash'})
        document = self.library.add_pdf(pdf(), {'filename': 'scope.pdf'})['id']
        link = self.library.add_link({'source': 'https://example.com'})['id']
        with self.library.db() as connection:
            self.library.put_extra(connection, 'preferences', 'classified:' + unfiled, datetime.now(timezone.utc).isoformat())
            connection.execute('UPDATE items SET created=? WHERE id=?', ((datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(), tagged))
        targets = lambda scope: set(self.library.classification_ids({'type': 'image', **scope}))
        self.assertEqual(targets({'mode': 'all'}), {unfiled, categorized, tagged})
        self.assertEqual(targets({'mode': 'unorganized'}), {unfiled})
        self.assertEqual(targets({'mode': 'unfiled'}), {unfiled, categorized})
        self.assertEqual(targets({'mode': 'new'}), {categorized, tagged})
        self.assertEqual(targets({'tag': '자료'}), {tagged})
        self.assertEqual(targets({'category': '참고'}), {categorized, tagged})
        self.assertEqual(targets({'hours': 1}), {unfiled, categorized})
        self.assertEqual(targets({'mode': 'new', 'category': '참고', 'hours': 1}), {categorized})
        self.assertEqual(targets({'mode': 'ids', 'ids': [unfiled, document, link, unfiled]}), {unfiled})
        self.assertEqual(targets({'mode': 'ids', 'ids': [trashed, document, link]}), set())
        for scope in ({'mode': 'bad'}, {'tag': '없음'}, {'category': '없음'}, {'hours': True}, {'hours': float('nan')}, {'hours': 0}, {'hours': 8761}, {'mode': 'ids', 'ids': ['없는 자료']}, {'ids': []}, {'unexpected': True}):
            with self.subTest(scope=scope), self.assertRaises(ArchiveError): targets(scope)

    @patch.object(analysis_tools.ENGINE, 'vector', return_value=np.array([1., 0.]))
    def test_scoped_job_changes_only_confirmed_targets_and_counts_images(self, vector):
        self.examples(); self.library.mode({'name': '갑옷', 'mode': 'auto'})
        included = self.add('blue', categories=['시험'])
        excluded = self.add('green', categories=['보존'])
        original = self.library.media(included)[0]
        with patch.object(self.library, 'classify', wraps=self.library.classify) as classify:
            self.library.start_job({'operation': 'classify', 'scope': {'category': '시험'}})
            deadline = time.monotonic() + 5
            while self.library.job['running'] and time.monotonic() < deadline: time.sleep(.01)
            self.assertEqual([call.args[0] for call in classify.call_args_list], [included])
        state = self.library.state()
        self.assertEqual(state['job']['total'], 1)
        self.assertEqual(state['job']['done'], 1)
        self.assertEqual(state['job']['errors'], [])
        self.assertEqual(next(item for item in state['items'] if item['id'] == included)['tags'], ['갑옷'])
        self.assertFalse(next(item for item in state['items'] if item['id'] == excluded)['hasClassification'])
        self.assertEqual(self.library.media(included)[0], original)
        self.library.batch({'ids': [included], 'action': 'trash'})
        self.assertNotIn(included, self.library.classification_ids({}))
        self.library.batch({'ids': [included], 'action': 'restore'})
        self.assertIn(included, self.library.classification_ids({}))
        self.assertEqual(self.library.media(included)[0], original)
        with self.assertRaises(ArchiveError): self.library.start_job({'operation': 'text', 'scope': {}})
        with self.assertRaises(ArchiveError): self.library.start_job({'operation': 'classify', 'scope': {}, 'ids': []})

    def test_setting_changes_are_blocked_during_analysis(self):
        self.examples()
        self.library.job['running'] = True
        try:
            with self.assertRaises(ArchiveError): self.library.mode({'name': '갑옷', 'mode': 'suggest'})
            self.assertEqual(self.library.state()['tags'][0]['mode'], 'off')
        finally:
            self.library.job['running'] = False

    def test_classification_history_backup_validation_and_legacy_predictions(self):
        image = self.add('blue')
        link = self.library.add_link({'source': 'https://example.com'})['id']
        with self.library.db() as connection:
            self.library.labels(connection, image, ['자료'], [])
            self.library.put_extra(connection, 'prediction', image + ':자료', {'item': image, 'tag': '자료', 'score': .9, 'evidence': [], 'status': 'rejected'})
            self.library.validate_extras(connection)
        self.assertNotIn(image, self.library.classification_ids({'mode': 'new'}))
        with self.library.db() as connection:
            self.library.put_extra(connection, 'preferences', 'classified:' + link, datetime.now(timezone.utc).isoformat())
            self.library.validate_extras(connection)
        for key, value in [('classified:' + image, 'bad-date'), ('classified:' + image, 12), ('classified:' + 'f' * 32, datetime.now(timezone.utc).isoformat())]:
            with self.subTest(key=key, value=value), self.library.db() as connection:
                self.library.put_extra(connection, 'preferences', key, value)
                with self.assertRaises(ArchiveError): self.library.validate_extras(connection)
                connection.execute("DELETE FROM extras WHERE kind='preferences' AND key=?", (key,))

    def test_analysis_failure_does_not_lose_saved_item_and_restore_is_blocked(self):
        item = self.add()
        entered, finish = threading.Event(), threading.Event()
        def fail(*args):
            entered.set(); finish.wait(5); raise RuntimeError('분석 테스트 실패')
        with patch.object(self.library, 'classify', side_effect=fail):
            self.library.start_job({'operation': 'classify', 'ids': [item]})
            self.assertTrue(entered.wait(3))
            with self.assertRaises(ArchiveError): self.library.restore(b'not a backup')
            finish.set()
            deadline = time.monotonic() + 5
            while self.library.job['running'] and time.monotonic() < deadline: time.sleep(.01)
        self.assertFalse(self.library.job['running'])
        self.assertEqual(self.library.job['errors'][0]['message'], '분석 테스트 실패')
        self.assertEqual(self.library.media(item)[0], picture())

    def test_link_download_opt_in_and_original_storage(self):
        with self.assertRaises(ArchiveError): self.library.inspect_link({'source': 'https://example.com'})
        self.library.preferences({'downloads': True})
        with patch.object(analysis_tools, 'public_get', return_value=(b'<img src="/image.png"><meta property="og:image" content="/cover.png">', 'text/html', 'https://example.com/page')):
            result = self.library.inspect_link({'source': 'https://example.com/page'})
            self.assertEqual(result['images'], ['https://example.com/image.png', 'https://example.com/cover.png'])
        with patch.object(analysis_tools, 'public_get', return_value=(picture(), 'image/png', 'https://example.com/image.png')):
            item = self.library.import_link({'image': result['images'][0], 'source': result['source'], 'tags': ['배경']})['id']
        self.assertEqual(self.library.media(item)[0], picture())
        self.assertEqual(self.library.state()['items'][0]['source'], 'https://example.com/page')

    def test_private_addresses_and_redirects_are_blocked_before_connect(self):
        for address in ('http://127.0.0.1/image', 'http://[::1]/image', 'http://10.0.0.1/image', 'http://169.254.169.254/image', 'http://224.0.0.1/image', 'http://example.com:8080/image', 'http://example.com:0/image'):
            with self.subTest(address=address), patch.object(analysis_tools.socket, 'create_connection') as connect, self.assertRaises(ValueError):
                analysis_tools.public_get(address)
            connect.assert_not_called()
        response = Mock(status=302)
        response.getheader.return_value = 'http://127.0.0.1/private'
        connection = Mock(); connection.getresponse.return_value = response
        with patch.object(analysis_tools.socket, 'getaddrinfo', side_effect=[[(2, 1, 6, '', ('93.184.216.34', 80))], [(2, 1, 6, '', ('127.0.0.1', 80))]]), patch.object(analysis_tools.socket, 'create_connection') as connect, patch.object(analysis_tools.http.client, 'HTTPConnection', return_value=connection), self.assertRaises(ValueError):
            analysis_tools.public_get('http://example.com/image')
        self.assertEqual(connect.call_count, 1)

    @unittest.skipUnless(analysis_tools.MODEL.is_file(), 'CLIP model not installed')
    def test_actual_clip_resized_image_similarity(self):
        content = picture('red')
        with Image.open(io.BytesIO(content)) as image:
            output = io.BytesIO(); image.resize((128, 96)).save(output, 'JPEG')
        first, second = analysis_tools.ENGINE.vector(content), analysis_tools.ENGINE.vector(output.getvalue())
        self.assertEqual(first.shape, (512,))
        self.assertAlmostEqual(float(np.linalg.norm(first)), 1, places=4)
        self.assertGreater(float(first @ second), .95)
        a = self.add('red'); b = self.library.add_image(output.getvalue(), {'filename': 'resized.jpg'})['id']
        self.assertEqual(self.library.similar({'id': a})['items'][0]['id'], b)

    def test_actual_windows_ocr_and_note_summary(self):
        if not Path('C:/Windows/Fonts/arial.ttf').is_file(): self.skipTest('Windows OCR test')
        image = Image.new('RGB', (1100, 220), 'white')
        ImageDraw.Draw(image).text((30, 50), 'ARCHIVE SEARCH TEST', font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 60), fill='black')
        output = io.BytesIO(); image.save(output, 'PNG')
        self.assertIn('ARCHIVE SEARCH TEST', analysis_tools.ocr(output.getvalue()))
        image = Image.new('RGB', (1100, 220), 'white')
        ImageDraw.Draw(image).text((30, 50), '자료 보관 키워드 검색', font=ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 60), fill='black')
        output = io.BytesIO(); image.save(output, 'PNG')
        self.assertIn('키워드', analysis_tools.ocr(output.getvalue()))
        link = self.library.add_link({'source': 'https://example.com', 'note': '자료 정리는 검색 시간을 줄이는 작업입니다. 키워드는 자료의 구체적인 특징을 설명합니다.'})['id']
        self.assertTrue(self.library.analyze_text(link)['summary'])

    def test_automatic_summary_settings_and_queued_imports(self):
        item = self.library.add_pdf(pdf(), {'filename': 'auto.pdf'})['id']
        self.library.queue_import(item)
        self.assertFalse(self.library.job['running'])
        self.library.preferences({'downloads': False, 'summaries': True, 'ocr': False})
        self.library.queue_import(item)
        deadline = time.monotonic() + 5
        while self.library.job['running'] and time.monotonic() < deadline: time.sleep(.01)
        current = next(value for value in self.library.state()['items'] if value['id'] == item)
        self.assertTrue(current['analysis']['summary'])
        self.assertTrue(current['analysis']['summarized'])
        self.library.preferences({'downloads': False, 'summaries': False, 'ocr': True})
        image = self.add()
        with patch.object(analysis_tools, 'ocr', return_value='Recognized reference text has enough words for a sentence.'):
            self.library.queue_import(image)
            deadline = time.monotonic() + 5
            while self.library.job['running'] and time.monotonic() < deadline: time.sleep(.01)
        current = next(value for value in self.library.state()['items'] if value['id'] == image)
        self.assertEqual(current['analysis']['summary'], [])
        self.assertFalse(current['analysis']['summarized'])

    @patch.object(analysis_tools.ENGINE, 'vector', return_value=np.array([1., 0.]))
    def test_merge_preserves_existing_manual_tag_origin(self, vector):
        self.examples(); self.library.mode({'name': '갑옷', 'mode': 'auto'})
        item = self.add('blue', tags=['의상']); self.library.classify(item)
        self.library.taxonomy({'kind': 'tags', 'name': '갑옷', 'target': '의상', 'action': 'merge'})
        current = next(value for value in self.library.state()['items'] if value['id'] == item)
        self.assertEqual(current['tags'], ['의상'])
        self.assertEqual(current['autoTags'], [])
        output = io.BytesIO(); self.library.backup(output)
        self.library.restore(output.getvalue())

    def test_invalid_extra_backup_preserves_current_data(self):
        item = self.add()
        search = self.library.saved({'kind': 'search', 'name': '정상', 'filters': {}})['id']
        output = io.BytesIO(); self.library.backup(output)
        database = Path(self.temporary.name) / 'tampered.sqlite3'
        corrupt = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as source:
            database.write_bytes(source.read('archive.sqlite3'))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE extras SET value=? WHERE kind='search'", (json.dumps({'id': search, 'name': '잘못된 조건', 'filters': {'type': 'unknown'}}),))
                connection.commit()
            with zipfile.ZipFile(corrupt, 'w') as target:
                for name in source.namelist(): target.writestr(name, database.read_bytes() if name == 'archive.sqlite3' else source.read(name))
        with self.assertRaises(ArchiveError): self.library.restore(corrupt.getvalue())
        self.assertEqual(self.library.media(item)[0], picture())
        self.assertEqual(self.library.state()['searches'][0]['name'], '정상')


class FeatureHTTPTests(test_archive.HTTPTests):
    def test_classification_scope_http_validation_and_authorization(self):
        image = self.server.library.add_image(picture(), {'filename': 'scope.png'})['id']
        self.server.library.add_link({'source': 'https://example.com'})
        self.server.library.add_pdf(pdf(), {'filename': 'scope.pdf'})
        request = json.dumps({'mode': 'all', 'type': 'image'}).encode()
        self.assertEqual(self.request('POST', '/api/classification/scope', request)[0], 403)
        authorized = {'X-RefArchive-Token': self.server.token}
        status, _, body = self.request('POST', '/api/classification/scope', request, authorized)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ids': [image], 'count': 1, 'limit': 5000})
        rule = json.dumps({'name': '규칙', 'rule': {'mode': 'auto', 'fields': ['title'], 'match': 'any', 'terms': ['회의']}}).encode()
        self.server.library.taxonomy({'kind': 'tags', 'name': '규칙'})
        self.assertEqual(self.request('POST', '/api/text-rule', rule)[0], 403)
        self.assertEqual(self.request('POST', '/api/text-rule', rule, authorized)[0], 200)
        self.assertEqual(self.request('POST', '/api/text-rule', b'{}', authorized)[0], 400)
        self.assertEqual(self.request('POST', '/api/classification/scope', b'{"hours":0}', authorized)[0], 400)

    def test_saved_pdf_job_and_shutdown_guards(self):
        authorized = {'X-RefArchive-Token': self.server.token}
        content, headers = multipart(pdf(), {'filename': 'document.pdf'})
        status, _, body = self.request('POST', '/api/files', content, {**headers, **authorized})
        self.assertEqual(status, 201)
        item = json.loads(body)['id']
        self.assertEqual(self.request('GET', '/media/' + item)[1]['Content-Type'], 'application/pdf')
        request = json.dumps({'kind': 'search', 'name': 'PDF', 'filters': {'type': 'pdf'}}).encode()
        self.assertEqual(self.request('POST', '/api/saved', request, authorized)[0], 200)
        self.assertEqual(self.request('POST', '/api/shutdown', b'{}')[0], 403)
        self.server.library.job['running'] = True
        self.assertEqual(self.request('POST', '/api/shutdown', b'{}', authorized)[0], 400)
        self.server.library.job['running'] = False
        self.assertEqual(self.request('POST', '/api/shutdown', b'{}', authorized)[0], 200)
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
