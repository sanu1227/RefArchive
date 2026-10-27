from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import zipfile

from archive import ArchiveError, Library, SCHEMA_V2
import test_archive
from test_archive import multipart, picture


def mp4():
    def box(kind, data):
        return (len(data) + 8).to_bytes(4) + kind + data
    return box(b'ftyp', b'isom\0\0\0\0') + box(b'mdat', b'video-fixture') + box(b'moov', box(b'trak', box(b'mdia', box(b'hdlr', b'\0' * 8 + b'vide'))))


def webm():
    def element(kind, data):
        return kind + bytes([0x80 | len(data)]) + data
    header = element(b'\x1a\x45\xdf\xa3', element(b'\x42\x82', b'webm'))
    tracks = element(b'\x16\x54\xae\x6b', element(b'\xae', element(b'\x83', b'\x01')))
    return header + element(b'\x18\x53\x80\x67', tracks + element(b'\x1f\x43\xb6\x75', b'\xe7\x81\x00'))


class VideoGroupTests(unittest.TestCase):
    setUp, tearDown, request = test_archive.HTTPTests.setUp, test_archive.HTTPTests.tearDown, test_archive.HTTPTests.request

    def test_mixed_group_duplicates_restore_and_validation(self):
        library = self.server.library
        group = {'uploadGroup': 'a' * 32, 'uploadGroupName': '동작 참고'}
        first = library.add_file(picture(), {'filename': '이미지.png', **group})['id']
        second = library.add_file(mp4(), {'filename': '동작.mp4', 'tags': ['동작'], **group})['id']
        third = library.add_file(webm(), {'filename': '영상.webm', **group})['id']
        duplicate = library.add_file(mp4(), {'filename': '복사.mp4', 'tags': ['연습'], **group})
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(library.state()['collections'][0]['ids'], [first, second, third])
        self.assertEqual(library.media(second)[0], mp4())
        self.assertEqual(library.classification_scope({'mode': 'all', 'type': 'image'})['ids'], [first])
        self.assertEqual(set(library.classification_scope({'mode': 'all'})['ids']), {first, second, third})
        self.assertTrue(all(tag['examples'] == 0 for tag in library.state()['tags']))
        self.assertEqual(library.clean_filters({'type': 'video'})['type'], 'video')
        library.batch({'action': 'trash', 'ids': [second]})
        with self.assertRaisesRegex(ArchiveError, '휴지통'):
            library.add_file(mp4(), {'filename': '다시.mp4', **group})
        output = io.BytesIO(); library.backup(output)
        other = Library(Path(self.temporary.name) / 'restored')
        other.restore(output.getvalue())
        self.assertEqual(other.state()['items'], library.state()['items'])
        self.assertEqual(other.state()['collections'], library.state()['collections'])
        self.assertEqual(other.media(second)[0], mp4())
        before = other.state()['items']
        broken = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as source, zipfile.ZipFile(broken, 'w') as target:
            for name in source.namelist():
                target.writestr(name, b'corrupt video' if name.endswith('.mp4') else source.read(name))
        with self.assertRaises(ArchiveError): other.restore(broken.getvalue())
        self.assertEqual(other.state()['items'], before)
        for content, name in [(b'not a movie', 'bad.mp4'), (mp4()[:-3], 'short.mp4'), (picture(), 'fake.webm'), (webm()[:-1], 'short.webm'), (mp4(), 'wrong.webm')]:
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                library.add_file(content, {'filename': name})
        with patch('archive.VIDEO_LIMIT', 10), self.assertRaises(ArchiveError):
            library.add_file(mp4(), {'filename': 'large.mp4'})

    def test_group_failure_rolls_back_files_and_duplicate_labels(self):
        library = self.server.library
        image = library.add_file(picture(), {'filename': 'original.png', 'tags': ['원본']})['id']
        before = library.state()['items']
        paths = {str(path) for path in library.root.rglob('*')}
        missing = {'uploadGroup': 'a' * 32}
        for content, name in [(mp4(), '영상.mp4'), (picture('blue'), '새 이미지.png'), (picture(), '중복.png')]:
            with self.subTest(name=name), self.assertRaisesRegex(ArchiveError, '그룹이 삭제'):
                library.add_file(content, {'filename': name, 'tags': ['추가되면 안 됨'], **missing})
        self.assertEqual(library.state()['items'], before)
        self.assertEqual({str(path) for path in library.root.rglob('*')}, paths)
        self.assertEqual(library.media(image)[0], picture())

    def test_video_http_upload_seek_head_and_limits(self):
        body, headers = multipart(mp4(), {'filename': '테스트.mp4'})
        headers['X-RefArchive-Token'] = self.server.token
        status, _, content = self.request('POST', '/api/files', body, headers)
        self.assertEqual(status, 201)
        url = '/media/' + json.loads(content)['id']
        status, headers, content = self.request('HEAD', url)
        self.assertEqual((status, content), (200, b''))
        self.assertEqual(headers['Content-Type'], 'video/mp4')
        self.assertEqual(headers['Accept-Ranges'], 'bytes')
        self.assertEqual(int(headers['Content-Length']), len(mp4()))
        for value, expected in [('bytes=0-7', mp4()[:8]), ('bytes=8-', mp4()[8:]), ('bytes=-9', mp4()[-9:]), ('bytes=0-999', mp4())]:
            status, headers, content = self.request('GET', url, headers={'Range': value})
            self.assertEqual(status, 206)
            self.assertEqual(content, expected)
            self.assertTrue(headers['Content-Range'].startswith('bytes '))
        for value in ['bytes=9999-', 'bytes=9-8', 'bytes=-0', 'bytes=-', 'bytes=0-1,3-4']:
            self.assertEqual(self.request('GET', url, headers={'Range': value})[0], 416)

    def test_v2_upgrade_preserves_every_table_and_backup(self):
        library = self.server.library
        item = library.add_file(picture(), {'filename': '보존.png', 'note': '사용자 메모'})['id']
        library.saved({'kind': 'collection', 'name': '이전 모음', 'ids': [item]})
        library.saved({'kind': 'search', 'name': '이전 검색', 'filters': {'query': '보존'}})
        prior = library.state()
        old = library.root / 'v2.sqlite3'
        with closing(sqlite3.connect(old)) as target, library.db() as source:
            target.executescript(SCHEMA_V2)
            for table in ('items', 'tags', 'categories', 'item_tags', 'item_categories', 'extras'):
                columns = ','.join(row[1] for row in target.execute(f'PRAGMA table_info({table})'))
                rows = list(source.execute(f'SELECT {columns} FROM {table}'))
                if rows: target.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in rows[0])})", rows)
            target.commit()
        old.replace(library.root / 'archive.sqlite3')
        old_backup = io.BytesIO(); library.backup(old_backup)
        restored = Library(Path(self.temporary.name) / 'restored-v2')
        restored.restore(old_backup.getvalue())
        for key in ('items', 'collections', 'searches'):
            self.assertEqual(restored.state()[key], prior[key])
        reopened = Library(library.data)
        for key in ('items', 'collections', 'searches'):
            self.assertEqual(reopened.state()[key], prior[key])
        safety = next((library.data / 'backups').glob('migration-v2-*.sqlite3'))
        with closing(sqlite3.connect(safety)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
        reopened.add_file(mp4(), {'filename': '새 동영상.mp4'})


if __name__ == '__main__':
    unittest.main()
