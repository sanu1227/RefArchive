from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import unittest
import zipfile

from archive import ArchiveError, Library, SCHEMA_V3
import test_archive
from test_archive import picture, multipart
from test_video_groups import mp4


class BundleTests(unittest.TestCase):
    setUp, tearDown, request = test_archive.HTTPTests.setUp, test_archive.HTTPTests.tearDown, test_archive.HTTPTests.request

    def test_upload_one_entry_edit_trash_and_roundtrip(self):
        library = self.server.library
        metadata = {'uploadBundle': 'b' * 32, 'bundleTitle': '캐릭터 설정', 'tags': ['설정'], 'note': '공통 설명'}
        image = library.add_file(picture(), {'filename': '정면.png', **metadata})['id']
        video = library.add_file(mp4(), {'filename': '동작.mp4', **metadata})['id']
        duplicate = library.add_file(picture(), {'filename': '복사.png', **metadata})
        snapshot = library.state()
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(snapshot['entryCount'], 1)
        self.assertEqual(snapshot['bundles'][0]['ids'], [image, video])
        self.assertEqual(snapshot['tags'][0]['count'], 1)
        self.assertEqual(snapshot['tags'][0]['examples'], 1)
        self.assertEqual(library.classification_scope({'mode': 'ids', 'ids': ['b' * 32], 'type': 'image'})['ids'], [image])
        self.assertEqual(set(library.classification_scope({'mode': 'ids', 'ids': ['b' * 32]})['ids']), {image, video})
        library.update({'id': 'b' * 32, 'title': '설정 한 묶음', 'note': '새 공통 메모', 'tags': ['인체'], 'categories': ['캐릭터']})
        self.assertTrue(all(item['tags'] == ['인체'] and item['categories'] == ['캐릭터'] for item in library.state()['items']))
        self.assertTrue(all(item['note'] == '공통 설명' for item in library.state()['items']))
        self.assertEqual(library.batch({'ids': ['b' * 32], 'action': 'trash'})['changed'], 1)
        self.assertTrue(all(item['trashed'] for item in library.state()['items']))
        with self.assertRaisesRegex(ArchiveError, '휴지통'):
            library.add_file(picture('blue'), {'filename': '추가.png', **metadata})
        library.batch({'ids': [image], 'action': 'restore'})
        library.batch({'ids': ['b' * 32], 'action': 'favorite'})
        self.assertTrue(all(item['favorite'] and not item['trashed'] for item in library.state()['items']))
        output = io.BytesIO(); library.backup(output)
        restored = Library(Path(self.temporary.name) / 'restored')
        restored.restore(output.getvalue())
        for key in ('items', 'bundles', 'entryCount'):
            self.assertEqual(restored.state()[key], library.state()[key])
        self.assertEqual(restored.media(image)[0], picture())
        self.assertEqual(restored.media(video)[0], mp4())
        restored.bundle({'action': 'split', 'id': 'b' * 32})
        self.assertEqual(restored.state()['entryCount'], 2)
        self.assertTrue(all('새 공통 메모' in item['note'] and '공통 설명' in item['note'] for item in restored.state()['items']))

    def test_existing_files_bundle_and_failed_attach_are_atomic(self):
        library = self.server.library
        first = library.add_file(picture(), {'filename': '원본.png', 'note': '원래 메모', 'tags': ['빨강']})['id']
        second = library.add_file(picture('blue'), {'filename': '다음.png'})['id']
        other = library.add_file(picture('green'), {'filename': '외부.png'})['id']
        key = library.bundle({'ids': [first, second], 'title': '기존 자료'})['id']
        before = library.state()
        with self.assertRaises(ArchiveError):
            library.bundle({'ids': [other, first], 'title': '실패해야 함'})
        with self.assertRaisesRegex(ArchiveError, '다른 묶음'):
            library.add_file(picture(), {'filename': '중복.png', 'tags': ['추가금지'], 'uploadBundle': 'c' * 32, 'bundleTitle': '다른 묶음'})
        with self.assertRaises(ArchiveError):
            library.add_file(b'broken', {'filename': '손상.png', 'uploadBundle': 'd' * 32, 'bundleTitle': '빈 묶음 금지'})
        for field in ('items', 'bundles', 'tags'):
            self.assertEqual(before[field], library.state()[field])
        third = library.add_file(picture('yellow'), {'filename': '묶음에 추가.png', 'uploadBundle': key})['id']
        added = next(item for item in library.state()['items'] if item['id'] == third)
        self.assertEqual(added['tags'], ['빨강'])
        collection = library.saved({'kind': 'collection', 'name': '모음', 'ids': [key]})
        self.assertEqual(library.state()['collections'][0]['ids'], [first, second, third])
        library.bundle({'action': 'split', 'id': key})
        self.assertEqual(library.state()['collections'][0]['id'], collection['id'])
        self.assertEqual(library.media(first)[0], picture())

    def test_backup_rejects_broken_membership_without_replacing_data(self):
        library = self.server.library
        a = library.add_file(picture(), {'filename': 'a.png'})['id']
        b = library.add_file(picture('blue'), {'filename': 'b.png'})['id']
        bundle = library.bundle({'ids': [a, b], 'title': '보존'})['id']
        output = io.BytesIO(); library.backup(output)
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
            db_bytes = archive.read('archive.sqlite3')
        database = Path(self.temporary.name) / 'broken.sqlite3'; database.write_bytes(db_bytes)
        with closing(sqlite3.connect(database)) as connection:
            value = {'id': bundle, 'title': '보존', 'ids': [a, a], 'note': '', 'source': ''}
            connection.execute("UPDATE extras SET value=? WHERE kind='bundle'", (json.dumps(value),)); connection.commit()
        broken = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as source, zipfile.ZipFile(broken, 'w') as target:
            for name in source.namelist(): target.writestr(name, database.read_bytes() if name == 'archive.sqlite3' else source.read(name))
        before = library.state()
        with self.assertRaises(ArchiveError): library.restore(broken.getvalue())
        self.assertEqual(library.state()['bundles'], before['bundles'])
        self.assertEqual(library.media(a)[0], picture())

    def test_v3_upgrade_preserves_files_and_collection(self):
        library = self.server.library
        first = library.add_file(picture(), {'filename': '이전.png', 'note': '이전 메모'})['id']
        library.saved({'kind': 'collection', 'name': '기존 모음', 'ids': [first]})
        before = library.state()
        old = library.root / 'old.sqlite3'
        with closing(sqlite3.connect(old)) as target, library.db() as source:
            target.executescript(SCHEMA_V3)
            for table in ('items', 'tags', 'categories', 'item_tags', 'item_categories', 'extras'):
                columns = ','.join(row[1] for row in target.execute(f'PRAGMA table_info({table})'))
                rows = list(source.execute(f'SELECT {columns} FROM {table}'))
                if rows: target.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in rows[0])})", rows)
            target.commit()
        old.replace(library.root / 'archive.sqlite3')
        old_backup = io.BytesIO(); library.backup(old_backup)
        restored = Library(Path(self.temporary.name) / 'old-restored'); restored.restore(old_backup.getvalue())
        reopened = Library(library.data)
        for current in (reopened, restored):
            self.assertEqual(current.state()['items'], before['items'])
            self.assertEqual(current.state()['collections'], before['collections'])
            self.assertEqual(current.media(first)[0], picture())
        self.assertTrue(list((library.data / 'backups').glob('migration-v3-*.sqlite3')))

    def test_http_bundle_upload_and_mutation(self):
        metadata = {'filename': '파일.png', 'uploadBundle': 'a' * 32, 'bundleTitle': 'HTTP 묶음'}
        body, headers = multipart(picture(), metadata); headers['X-RefArchive-Token'] = self.server.token
        self.assertEqual(self.request('POST', '/api/files', body, headers)[0], 201)
        body, headers = multipart(picture('blue'), metadata); headers['X-RefArchive-Token'] = self.server.token
        self.assertEqual(self.request('POST', '/api/files', body, headers)[0], 201)
        snapshot = json.loads(self.request('GET', '/api/state')[2])
        self.assertEqual(snapshot['entryCount'], 1)
        self.assertEqual(len(snapshot['bundles'][0]['ids']), 2)
        body = json.dumps({'action': 'split', 'id': 'a' * 32}).encode()
        self.assertEqual(self.request('POST', '/api/bundle', body, {'X-RefArchive-Token': self.server.token, 'Content-Type': 'application/json'})[0], 200)


if __name__ == '__main__': unittest.main()
