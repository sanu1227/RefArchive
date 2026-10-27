from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from archive import ArchiveError, Library, SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, clean_date
import document_tools
import test_archive
from test_archive import multipart, picture
from test_features import pdf
from test_video_groups import mp4


def office(extension, extras=None):
    ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    if extension == '.docx':
        main, kind = 'word/document.xml', 'wordprocessingml.document'
        files = {main: '<w:document xmlns:w="urn:word"><w:body><w:p><w:r><w:t>한글 &lt;script&gt; 참고 본문</w:t></w:r></w:p></w:body></w:document>'}
    elif extension == '.xlsx':
        main, kind = 'xl/workbook.xml', 'spreadsheetml.sheet'
        files = {main: f'<workbook xmlns:r="{ns}"><sheets><sheet name="예산" r:id="s1"/></sheets></workbook>',
                 'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="s1" Target="worksheets/sheet1.xml"/></Relationships>',
                 'xl/worksheets/sheet1.xml': '<worksheet><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>123</v></c></row></sheetData></worksheet>',
                 'xl/sharedStrings.xml': '<sst><si><t>한글 예산</t></si></sst>'}
    else:
        main, kind = 'ppt/presentation.xml', 'presentationml.presentation'
        files = {main: f'<presentation xmlns:r="{ns}"><sldIdLst><sldId r:id="s2"/><sldId r:id="s1"/></sldIdLst></presentation>',
                 'ppt/_rels/presentation.xml.rels': '<Relationships><Relationship Id="s1" Target="slides/slide1.xml"/><Relationship Id="s2" Target="slides/slide2.xml"/></Relationships>',
                 'ppt/slides/slide1.xml': '<slide><p><t>두 번째 슬라이드</t></p></slide>',
                 'ppt/slides/slide2.xml': '<slide><p><t>첫 번째 슬라이드</t></p></slide>'}
    files['[Content_Types].xml'] = f'<Types><Override PartName="/{main}" ContentType="application/vnd.openxmlformats-officedocument.{kind}.main+xml"/></Types>'
    files.update(extras or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content)
    return output.getvalue()


class DocumentsDatesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temporary.name) / 'data')

    def tearDown(self):
        self.temporary.cleanup()

    def test_office_text_original_tags_collection_bundle_backup(self):
        contents = {}
        for extension in document_tools.OFFICE_EXTENSIONS:
            content = office(extension)
            result = self.library.add_file(content, {'filename': '참고' + extension, 'tags': ['학습'], 'categories': ['문서'],
                'material_date': '2000-02-29', 'uploadBundle': 'b' * 32, 'bundleTitle': '강의', 'uploadGroup': 'c' * 32, 'uploadGroupName': '공부'})
            contents[result['id']] = content
        state = self.library.state()
        self.assertEqual(state['entryCount'], 1)
        self.assertEqual(state['bundles'][0]['material_date'], '2000-02-29')
        self.assertEqual(len(state['collections'][0]['ids']), 3)
        for item in state['items']:
            self.assertEqual(item['type'], 'document')
            self.assertEqual(item['tags'], ['학습'])
            self.assertEqual(self.library.media(item['id'])[0], contents[item['id']])
            self.assertFalse(item['analysis']['openReason'])
        texts = {Path(item['filename']).suffix: item['analysis']['pages'][0]['text'] for item in state['items']}
        self.assertIn('<script>', texts['.docx'])
        self.assertIn('A1: 한글 예산\tB1: 123', texts['.xlsx'])
        self.assertLess(texts['.pptx'].index('첫 번째'), texts['.pptx'].index('두 번째'))
        backup = io.BytesIO(); self.library.backup(backup)
        other = Library(Path(self.temporary.name) / 'restored')
        other.restore(backup.getvalue())
        self.assertEqual(other.state()['items'], state['items'])
        self.assertEqual(other.state()['bundles'], state['bundles'])
        for key, content in contents.items():
            self.assertEqual(other.media(key)[0], content)

    def test_plain_text_encodings_preview_limits_and_binary_rejection(self):
        for encoding in ('utf-8-sig', 'utf-16', 'cp949'):
            result = document_tools.inspect('한글 <script>\r\n본문'.encode(encoding), '.txt')
            self.assertEqual(result['pages'][0]['text'], '한글 <script>\n본문')
        result = document_tools.inspect(b'x' * (document_tools.MAX_TEXT + 10), '.md')
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['pages'][0]['text']), document_tools.MAX_TEXT)
        for content in (b'\0binary', b'\x81', b''):
            with self.assertRaises(document_tools.DocumentError):
                document_tools.inspect(content, '.txt')
        self.assertTrue(document_tools.inspect(b'name,=1+1', '.csv')['openReason'])

    def test_normal_package_metadata_and_empty_attachment_folders_allow_open(self):
        content = office('.pptx', {
            '_rels/.rels': '<Relationships><Relationship Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>',
            'docProps/core.xml': '<coreProperties/>', 'ppt/embeddings/': b''})
        self.assertEqual(document_tools.inspect(content, '.pptx')['openReason'], '')

    def test_unsafe_invalid_and_oversized_packages_are_rejected_without_files(self):
        invalid = [b'not a zip', office('.xlsx'),
                   office('.docx', {'word/vbaProject.bin': b'macro'}),
                   office('.docx', {'../outside.txt': b'bad'}),
                   office('.docx', {'word/document.xml': '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>'}),
                   office('.docx', {'word/document.xml': b'<?xml version="1.0"?><invalid'})]
        with patch.object(document_tools, 'MAX_UNPACKED', 5):
            with self.assertRaises(document_tools.DocumentError):
                document_tools.inspect(office('.docx'), '.docx')
        with patch.object(document_tools, 'MAX_XML', 5):
            with self.assertRaises(document_tools.DocumentError):
                document_tools.inspect(office('.docx'), '.docx')
        for content in invalid:
            with self.assertRaises(ArchiveError):
                self.library.add_file(content, {'filename': 'bad.docx'})
        for extension in ('.doc', '.xls', '.ppt', '.docm', '.xlsm', '.pptm'):
            with self.assertRaisesRegex(ArchiveError, '지원하지'):
                self.library.add_file(office('.docx'), {'filename': 'bad' + extension})
        self.assertEqual(self.library.state()['items'], [])
        self.assertEqual(list((self.library.root / 'files').iterdir()), [])

    def test_active_content_is_passive_and_native_open_is_blocked(self):
        cases = [('.docx', {'word/_rels/document.xml.rels': '<Relationships><Relationship TargetMode="External" Target="https://example.invalid"/></Relationships>'}),
                 ('.docx', {'word/embeddings/oleObject1.bin': b'not executed'}),
                 ('.docx', {'word/header1.xml': '<header><instrText>DDEAUTO command</instrText></header>'}),
                 ('.xlsx', {'xl/worksheets/sheet2.xml': '<worksheet><c><f>WEBSERVICE("x")</f><v>cached</v></c></worksheet>'})]
        with patch('archive.os.startfile', create=True) as launch:
            for extension, extras in cases:
                content = office(extension, extras)
                item = self.library.add_file(content, {'filename': 'passive' + extension})['id']
                self.assertEqual(self.library.media(item)[0], content)
                with self.assertRaisesRegex(ArchiveError, '연결 앱 열기 제한'):
                    self.library.open_native({'id': item})
            launch.assert_not_called()

    def test_shared_string_expansion_is_bounded(self):
        content = office('.xlsx', {'xl/sharedStrings.xml': '<sst><si><t>' + 'x' * 400000 + '</t></si></sst>',
            'xl/worksheets/sheet1.xml': '<worksheet><row>' + '<c t="s"><v>0</v></c>' * 10000 + '</row></worksheet>'})
        preview = document_tools.inspect(content, '.xlsx')
        self.assertTrue(preview['truncated'])
        self.assertEqual(len(preview['pages'][0]['text']), document_tools.MAX_TEXT)

    def test_optional_document_summary_keeps_preview_and_open_restriction(self):
        content = b'Reference documents help organize useful research material. Searchable text makes reference material easy to find.'
        item = self.library.add_file(content, {'filename': 'summary.csv'})['id']
        self.library.preferences({'downloads': False, 'summaries': True, 'ocr': False})
        self.library.queue_import(item)
        deadline = time.monotonic() + 5
        while self.library.job['running'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.library.job['running'])
        analysis = self.library.state()['items'][0]['analysis']
        self.assertTrue(analysis['summary'])
        self.assertTrue(analysis['openReason'])
        self.assertEqual(analysis['pages'][0]['text'], content.decode())

    def test_native_open_copy_failure_and_revalidation(self):
        content = office('.docx')
        item = self.library.add_file(content, {'filename': '../../CON.docx'})['id']
        with patch('archive.os.startfile', create=True) as launch:
            result = self.library.open_native({'id': item})
            target = Path(result['copy'])
            self.assertIn(self.library.data / 'opened', target.parents)
            launch.assert_called_once_with(str(target), 'open')
            self.assertEqual(target.read_bytes(), content)
            target.write_bytes(b'edited copy')
            self.assertEqual(self.library.media(item)[0], content)
        with patch('archive.os.startfile', side_effect=OSError('no app'), create=True):
            with self.assertRaisesRegex(ArchiveError, '기본 앱'):
                self.library.open_native({'id': item})
        self.assertEqual(len(list((self.library.data / 'opened').iterdir())), 1)
        self.library.media_path(item).write_bytes(office('.docx', {'word/vbaProject.bin': b'macro'}))
        with patch('archive.os.startfile', create=True) as launch:
            with self.assertRaises(ArchiveError):
                self.library.open_native({'id': item})
            launch.assert_not_called()

    def test_date_validation_duplicate_preservation_and_all_types(self):
        for value in ('2023-02-29', '2026-1-01', '0000-01-01', '10000-01-01', '2026-10-18T00:00:00', None, 3):
            with self.assertRaises(ArchiveError): clean_date(value)
        self.assertEqual(clean_date('0001-01-01'), '0001-01-01')
        self.assertEqual(clean_date('9999-12-31'), '9999-12-31')
        files = [(picture(), 'image.png'), (pdf(), 'doc.pdf'), (mp4(), 'video.mp4'), (b'plain', 'text.txt')]
        for content, filename in files:
            item = self.library.add_file(content, {'filename': filename, 'material_date': '2020-02-29'})['id']
            self.library.add_file(content, {'filename': filename, 'material_date': '2026-10-18'})
            value = next(row for row in self.library.state()['items'] if row['id'] == item)
            self.assertEqual(value['material_date'], '2020-02-29')
            self.library.update({'id': item, 'title': filename})
            self.assertEqual(next(row for row in self.library.state()['items'] if row['id'] == item)['material_date'], '2020-02-29')
            self.library.update({'id': item, 'title': filename, 'material_date': ''})
            self.assertEqual(next(row for row in self.library.state()['items'] if row['id'] == item)['created'], value['created'])
        link = self.library.add_link({'source': 'https://example.invalid', 'material_date': '2001-01-01'})['id']
        self.assertEqual(next(row for row in self.library.state()['items'] if row['id'] == link)['material_date'], '2001-01-01')
        before = self.library.state()['items']
        with self.assertRaises(ArchiveError): self.library.update({'id': link, 'title': 'must not save', 'source': 'https://example.invalid', 'material_date': '2026-02-30'})
        self.assertEqual(self.library.state()['items'], before)
        with self.assertRaises(ArchiveError): self.library.add_file(picture('green'), {'filename': 'new.png', 'material_date': 'bad'})
        self.assertEqual(self.library.state()['items'], before)

    def test_bundle_date_is_explicit_and_saved_filters_roundtrip(self):
        first = self.library.add_file(b'one', {'filename': 'one.txt', 'material_date': '1999-01-01'})['id']
        second = self.library.add_file(b'two', {'filename': 'two.txt'})['id']
        bundle = self.library.bundle({'ids': [first, second], 'title': '묶음'})['id']
        self.assertEqual(self.library.state()['bundles'][0]['material_date'], '')
        self.library.update({'id': bundle, 'title': '묶음', 'material_date': '2024-02-29'})
        third = self.library.add_file(b'three', {'filename': 'three.txt', 'uploadBundle': bundle, 'material_date': '2000-01-01'})['id']
        self.assertEqual(self.library.state()['bundles'][0]['material_date'], '2024-02-29')
        filters = {'type': 'document', 'sort': 'date-asc', 'group': 'month', 'dateFrom': '2000-01-01', 'dateTo': '2026-10-18'}
        self.library.saved({'kind': 'search', 'name': '문서 기간', 'filters': filters})
        with self.assertRaises(ArchiveError): self.library.clean_filters({**filters, 'dateFrom': '9999-12-31'})
        output = io.BytesIO(); self.library.backup(output)
        other = Library(Path(self.temporary.name) / 'restored'); other.restore(output.getvalue())
        self.assertEqual(other.state()['searches'], self.library.state()['searches'])
        other.bundle({'action': 'detach', 'id': bundle, 'file': third})
        item = next(item for item in other.state()['items'] if item['id'] == third)
        self.assertEqual(item['material_date'], '2000-01-01')
        self.assertIn('묶음 자료 날짜: 2024-02-29', item['note'])

    def test_v1_to_v4_migrations_and_old_backups_keep_dates_unknown(self):
        for version, schema in enumerate((SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4), 1):
            root = Path(self.temporary.name) / f'old{version}' / 'library'
            root.mkdir(parents=True)
            with closing(sqlite3.connect(root / 'archive.sqlite3')) as connection:
                connection.executescript(schema)
                connection.execute("INSERT INTO items(id,type,title,source,created) VALUES (?,'link','과거 자료','https://example.invalid','2010-01-01T00:00:00+00:00')", ('a' * 32,))
                connection.commit()
            backup = io.BytesIO()
            with zipfile.ZipFile(backup, 'w') as output:
                output.writestr('manifest.json', '{"format":"RefArchive","version":1}')
                output.write(root / 'archive.sqlite3', 'archive.sqlite3')
            migrated = Library(root.parent)
            item = migrated.state()['items'][0]
            self.assertEqual(item['material_date'], '')
            self.assertEqual(item['created'], '2010-01-01T00:00:00+00:00')
            snapshots = list((root.parent / 'backups').glob(f'migration-v{version}-*.sqlite3'))
            self.assertEqual(len(snapshots), 1)
            with closing(sqlite3.connect(snapshots[0])) as old:
                self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0], version)
            other = Library(Path(self.temporary.name) / f'restored{version}')
            other.restore(backup.getvalue())
            self.assertEqual(other.state()['items'], migrated.state()['items'])

    def test_corrupt_date_restore_is_atomic(self):
        self.library.add_file(b'original', {'filename': 'keep.txt'})
        before = self.library.state()['items']
        bad = Library(Path(self.temporary.name) / 'bad')
        bad.add_file(b'other', {'filename': 'other.txt'})
        with bad.db() as connection:
            connection.execute("UPDATE items SET material_date='2026-02-30'")
        output = io.BytesIO(); bad.backup(output)
        with self.assertRaises(ArchiveError): self.library.restore(output.getvalue())
        self.assertEqual(self.library.state()['items'], before)
        self.assertEqual(self.library.media(before[0]['id'])[0], b'original')


class DocumentHTTPTests(unittest.TestCase):
    setUp, tearDown, request = test_archive.HTTPTests.setUp, test_archive.HTTPTests.tearDown, test_archive.HTTPTests.request

    def test_upload_download_auth_and_native_open_without_window(self):
        authorization = {'X-RefArchive-Token': self.server.token}
        content = office('.docx')
        body, headers = multipart(content, {'filename': '수업.docx', 'material_date': '2024-02-29'})
        status, _, response = self.request('POST', '/api/files', body, {**authorization, **headers})
        self.assertEqual(status, 201, response)
        item = json.loads(response)['id']
        status, headers, saved = self.request('GET', '/media/' + item)
        self.assertEqual(status, 200)
        self.assertEqual(saved, content)
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertIn('attachment', headers['Content-Disposition'])
        data = json.dumps({'id': item}).encode()
        with patch('archive.os.startfile', create=True) as launch:
            self.assertEqual(self.request('POST', '/api/open', data)[0], 403)
            self.assertEqual(self.request('POST', '/api/open', data, {**authorization, 'Origin': 'https://evil.invalid'})[0], 403)
            self.assertEqual(self.request('GET', '/api/open')[0], 404)
            launch.assert_not_called()
            self.assertEqual(self.request('POST', '/api/open', data, authorization)[0], 200)
            launch.assert_called_once()
