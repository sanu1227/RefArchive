from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import parse_headers
import io
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

import numpy as np
from PIL import Image

from archive import ArchiveError, Library
from library_tools import PILOT_CHECKS
from runtime_paths import VERSION
import test_archive
from test_archive import picture


def textured(size=256, format='PNG', different=False):
    y, x = np.indices((256, 256))
    pixels = np.stack([(x * 3 + y) % 256, (x + y * 2) % 256, (x // 40 % 2) * 180 + 25], axis=-1).astype('uint8')
    if different:
        pixels = 255 - pixels
    output = io.BytesIO()
    Image.fromarray(pixels).resize((size, size)).save(output, format, quality=92)
    return output.getvalue()


class MaintenanceTests(unittest.TestCase):
    setUp, tearDown = test_archive.HTTPTests.setUp, test_archive.HTTPTests.tearDown

    def request(self, method, path, content=None, headers=None):
        return test_archive.HTTPTests.request(self, method, path, content, {'X-RefArchive-Token': self.server.token, **(headers or {})})

    def head(self, path, host=None):
        # HTTPConnection hides HEAD bodies; read the wire to detect accidental ZIP writes.
        port = self.server.server_port
        host = host or f'127.0.0.1:{port}'
        with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
            connection.sendall(f'HEAD {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode('ascii'))
            with connection.makefile('rb') as response:
                status = int(response.readline().split()[1])
                headers = dict(parse_headers(response))
                return status, headers, response.read()

    def download_url(self):
        maintenance = self.server.library.maintenance
        maintenance.latest = Path(self.temporary.name) / 'backup.zip'
        self.server.library.backup(maintenance.latest)
        status, _, content = self.request('POST', '/api/backup/download', b'{}')
        self.assertEqual(status, 200)
        return json.loads(content)['url']

    def wait(self):
        deadline = time.monotonic() + 10
        while self.server.library.maintenance.status()['running']:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        return self.server.library.maintenance.status()

    def add(self, content=None, **metadata):
        return self.server.library.add_file(content or picture(), {'filename': '개인제목.png', **metadata})['id']

    def test_bundle_cover_order_detach_rollback_roundtrip(self):
        library = self.server.library
        a, b, c = self.add(), self.add(picture('blue')), self.add(picture('green'))
        key = library.bundle({'ids': [a, b, c], 'title': '묶음 제목'})['id']
        library.update({'id': key, 'title': '묶음 제목', 'note': '공통 메모', 'source': 'https://example.com', 'tags': ['분류']})
        library.bundle({'action': 'edit', 'id': key, 'ids': [c, a, b], 'cover': b})
        with self.assertRaises(ArchiveError): library.bundle({'action': 'edit', 'id': key, 'ids': [a, b, b]})
        with self.assertRaises(ArchiveError): library.bundle({'action': 'edit', 'id': key, 'cover': 'x' * 32})
        output = io.BytesIO(); library.backup(output)
        library.restore(output)
        bundle = library.state()['bundles'][0]
        self.assertEqual(bundle['ids'], [c, a, b]); self.assertEqual(bundle['cover'], b)
        library.bundle({'action': 'detach', 'id': key, 'file': b})
        self.assertEqual(library.state()['bundles'][0]['cover'], c)
        detached = next(item for item in library.state()['items'] if item['id'] == b)
        self.assertIn('공통 메모', detached['note']); self.assertIn('https://example.com', detached['note']); self.assertEqual(detached['tags'], ['분류'])
        self.assertEqual(library.media(b)[0], picture('blue'))
        with library.db() as connection:
            connection.execute('UPDATE items SET note=? WHERE id=?', ('a' * 20000, a))
        before = library.state()
        with self.assertRaises(ArchiveError): library.bundle({'action': 'detach', 'id': key, 'file': a})
        self.assertEqual(library.state()['bundles'], before['bundles'])
        library.bundle({'action': 'detach', 'id': key, 'file': c})
        self.assertEqual(library.state()['bundles'][0]['ids'], [a])

    def test_backup_job_download_one_use_restore_and_failure(self):
        library = self.server.library
        self.add(textured(), note='비공개 메모')
        maintenance = library.maintenance
        folder = Path(self.temporary.name) / '사용자 백업'
        maintenance.configure({'folder': str(folder), 'automatic': False})
        status, _, _ = self.request('POST', '/api/maintenance', json.dumps({'operation': 'backup'}).encode())
        self.assertEqual(status, 200)
        result = self.wait(); self.assertEqual(result['error'], '')
        self.assertEqual(result['done'], result['total']); self.assertTrue(result['downloadReady'])
        status, _, content = self.request('POST', '/api/backup/download', b'{}')
        self.assertEqual(status, 200)
        url = json.loads(content)['url']
        status, headers, data = self.request('GET', url)
        self.assertEqual(status, 200); self.assertEqual(headers['Content-Type'], 'application/zip')
        self.assertEqual(self.request('GET', url)[0], 400)
        self.assertEqual(self.request('POST', '/api/restore', data)[0], 200)
        self.assertEqual(len(library.state()['items']), 1)
        self.assertFalse(list(folder.glob('*.partial')))
        previous = maintenance.settings['lastBackup']
        def fail_backup(path, progress=None):
            Path(path).write_bytes(b'partial zip')
            raise OSError('disk full')
        with patch.object(library, 'backup', side_effect=fail_backup):
            maintenance.start({'operation': 'backup'}); result = self.wait()
        self.assertTrue(result['error']); self.assertEqual(maintenance.settings['lastBackup'], previous)
        self.assertFalse(list(folder.glob('*.partial')))
        self.assertEqual(len(list(folder.glob('*.zip'))), 1)
        with patch('archive.shutil.disk_usage', return_value=type('Disk', (), {'free': 1})()):
            with self.assertRaisesRegex(ArchiveError, '공간'): library.restore(data)
        self.assertEqual(len(library.state()['items']), 1)
        self.assertFalse(list(library.data.glob('restore-*')))
        before = library.state()['items']
        original_rename = Path.rename
        def fail_swap(path, destination):
            if path.name.startswith('restore-'):
                raise OSError('simulated replacement failure')
            return original_rename(path, destination)
        with patch('archive.Path.rename', fail_swap):
            with self.assertRaises(OSError): library.restore(data)
        self.assertEqual(library.state()['items'], before)
        self.assertEqual(library.media(before[0]['id'])[0], textured())
        self.assertFalse(list(library.data.glob('restore-*')))

    def test_download_repeated_head_then_one_get(self):
        url = self.download_url()
        expected = self.server.library.maintenance.latest.read_bytes()
        for _ in range(3):
            status, headers, content = self.head(url)
            self.assertEqual((status, content), (200, b''))
            self.assertEqual(int(headers['Content-Length']), len(expected))
            self.assertEqual(headers['Content-Type'], 'application/zip')
            self.assertEqual(headers['Content-Disposition'], 'attachment; filename="backup.zip"')
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        status, get_headers, content = self.request('GET', url)
        self.assertEqual((status, content), (200, expected))
        for key in ('Content-Length', 'Content-Type', 'Content-Disposition', 'Cache-Control', 'X-Content-Type-Options'):
            self.assertEqual(headers[key], get_headers[key])
        status, _, content = self.head(url)
        self.assertEqual((status, content), (400, b''))
        self.assertEqual(self.request('GET', url)[0], 400)

    def test_download_head_access_and_expiry_guards(self):
        url = self.download_url()
        maintenance = self.server.library.maintenance
        key = parse_qs(urlsplit(url).query)['ticket'][0]
        for headers in ({}, {'X-RefArchive-Token': 'invalid'},
                        {'X-RefArchive-Token': self.server.token, 'Origin': 'https://evil.example'}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(test_archive.HTTPTests.request(self, 'POST', '/api/backup/download', b'{}', headers)[0], 403)
        status, _, content = self.head(url, host='evil.example')
        self.assertEqual((status, content), (403, b''))
        self.assertEqual(self.head(url)[0], 200)
        for invalid in ('/api/backup/download', '/api/backup/download?ticket=invalid'):
            with self.subTest(url=invalid):
                status, _, content = self.head(invalid)
                self.assertEqual((status, content), (400, b''))
                self.assertEqual(self.request('GET', invalid)[0], 400)
        expires = maintenance.tickets[key][1]
        for now in (expires, expires + 1):
            with self.subTest(now=now), patch('library_tools.time.monotonic', return_value=now):
                status, _, content = self.head(url)
                self.assertEqual((status, content), (400, b''))
        with patch('library_tools.time.monotonic', return_value=expires + 1):
            self.assertEqual(self.request('GET', url)[0], 400)

    def test_download_concurrent_get_is_still_one_use(self):
        url = self.download_url()
        self.assertEqual(self.head(url)[0], 200)
        expected = self.server.library.maintenance.latest.read_bytes()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.request('GET', url), range(2)))
        self.assertEqual(sorted(result[0] for result in results), [200, 400])
        self.assertEqual(next(result[2] for result in results if result[0] == 200), expected)

    def test_daily_schedule_settings_reopen_and_boundaries(self):
        library = self.server.library
        maintenance = library.maintenance
        with self.assertRaises(ArchiveError): maintenance.configure({'automatic': True, 'folder': 'relative'})
        with self.assertRaises(ArchiveError): maintenance.configure({'automatic': True, 'folder': str(library.root / 'backups')})
        maintenance.configure({'automatic': True, 'folder': str(library.data / 'automatic')})
        with patch.object(maintenance, 'start') as start:
            maintenance.next_check = 0; maintenance.tick(); start.assert_called_once()
            start.reset_mock()
            maintenance.settings['lastBackup'] = datetime.now(timezone.utc).isoformat()
            maintenance.next_check = 0; maintenance.tick(); start.assert_not_called()
            maintenance.settings['lastBackup'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            maintenance.next_check = 0; maintenance.tick(); start.assert_called_once()
        maintenance.persist()
        reopened = Library(library.data)
        self.assertTrue(reopened.maintenance.settings['automatic'])
        # Machine-local settings stay outside portable library backups.
        output = io.BytesIO(); library.backup(output)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn('maintenance.json', archive.namelist())
        maintenance.configure({'automatic': False, 'folder': str(library.data / 'automatic')})

    def test_duplicate_review_only_trashes_selected_bundle_member(self):
        library = self.server.library
        a = self.add(textured()); b = self.add(textured(128, 'JPEG'))
        c = self.add(textured(different=True)); self.add(picture('red'))
        key = library.bundle({'ids': [a, b], 'title': '보존할 묶음'})['id']
        library.update({'id': key, 'title': '보존할 묶음', 'note': '공통 설명', 'tags': ['확정']})
        maintenance = library.maintenance
        maintenance.start({'operation': 'duplicates'}); result = self.wait()
        self.assertEqual(result['error'], '')
        self.assertEqual([set(pair['ids']) for pair in result['pairs']], [{a, b}]); self.assertEqual(result['skipped'], 1)
        with self.assertRaises(ArchiveError): maintenance.resolve_duplicate({'keep': a, 'remove': c})
        maintenance.resolve_duplicate({'keep': a, 'remove': b})
        items = {item['id']: item for item in library.state()['items']}
        self.assertFalse(items[a]['trashed']); self.assertTrue(items[b]['trashed']); self.assertFalse(items[c]['trashed'])
        self.assertIn('공통 설명', items[b]['note']); self.assertEqual(library.state()['bundles'][0]['ids'], [a])
        library.batch({'ids': [b], 'action': 'restore'})
        self.assertEqual(library.media(b)[0], textured(128, 'JPEG'))
        self.assertEqual(maintenance.status()['pairs'], [])

    def test_offline_release_pilot_and_private_diagnostics(self):
        library = self.server.library
        self.add(note='비공개 메모', source='https://private.example.com')
        maintenance = library.maintenance
        for version, expected in [(VERSION, 'same'), ('9999.0.0', 'newer'), ('0.1.0', 'older')]:
            status, _, content = self.request('POST', '/api/release/check', json.dumps({'format': 'RefArchive-release', 'version': version, 'platform': 'windows-x64'}).encode())
            self.assertEqual(status, 200); self.assertEqual(json.loads(content)['status'], expected)
        with self.assertRaises(ArchiveError): maintenance.release({'format': 'other'})
        checks = dict.fromkeys(PILOT_CHECKS, 'pending'); checks['launch'] = 'pass'
        report = maintenance.pilot({'checks': checks})
        self.assertTrue(report['databaseOK']); self.assertEqual(report['missingFiles'], 0)
        serialized = json.dumps(report, ensure_ascii=False)
        for private in ('비공개 메모', '개인제목', 'private.example.com', str(library.root), self.server.token):
            self.assertNotIn(private, serialized)
        self.assertEqual(Library(library.data).maintenance.settings['pilot']['checks']['launch'], 'pass')

    def test_invalid_local_settings_disable_schedule_and_browser_fallback(self):
        library = self.server.library
        settings = library.data / 'maintenance.json'
        settings.write_text(json.dumps({'automatic': True, 'folder': str(library.data / 'backups'), 'lastBackup': 'invalid'}), encoding='utf-8')
        reopened = Library(library.data)
        self.assertFalse(reopened.maintenance.settings['automatic'])
        self.assertEqual(reopened.maintenance.settings['lastBackup'], '')
        self.assertTrue(reopened.maintenance.status()['error'])
        reopened.maintenance.tick()
        import launcher
        with patch('launcher.webbrowser.open', side_effect=OSError('no browser')), patch('launcher.ctypes.windll.user32.MessageBoxW') as message:
            self.assertFalse(launcher.open_window('http://127.0.0.1:18768'))
            deadline = time.monotonic() + 2
            while not message.called and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(message.called)


if __name__ == '__main__':
    unittest.main()
