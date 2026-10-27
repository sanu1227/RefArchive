"""Check an existing 0.3.0 portable profile with the new release on this PC."""
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from runtime_paths import VERSION
from test_archive import picture, multipart


def main():
    target = ROOT / 'test-results' / f'portable-update-{time.time_ns()}'
    old = target / '이전 프로그램'
    profile = target / '사용자 프로필'
    old.mkdir(parents=True)
    # Both archives are locally built releases. Still enforce containment on extraction.
    with zipfile.ZipFile(ROOT / 'release/RefArchive-0.3.0-Portable-x64.zip') as package:
        for name in package.namelist():
            if not (old / name).resolve().is_relative_to(old.resolve()):
                raise ValueError('Invalid portable ZIP path')
        package.extractall(old)
    profile.mkdir()
    env = {**os.environ, 'LOCALAPPDATA': str(profile), 'PATH': os.environ['SystemRoot'] + '/System32'}
    process = None
    port, token = 0, ''
    def request(method, path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', port, timeout=30)
        try:
            connection.request(method, path, body, {'X-RefArchive-Token': token, **(headers or {})})
            response = connection.getresponse(); content = response.read()
            assert response.status in (200, 201), (path, response.status, content[:200])
            return content
        finally:
            connection.close()
    def post(path, body):
        return json.loads(request('POST', path, json.dumps(body).encode()))
    def start(executable, expected):
        nonlocal process, port, token
        record = profile / 'RefArchive/data/runtime.json'
        process = subprocess.Popen([str(executable), '--no-browser'], env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            assert process.poll() is None
            if record.exists():
                port = json.loads(record.read_text())['port']
                try:
                    health = json.loads(request('GET', '/api/health'))
                    if health['appVersion'] == expected:
                        break
                except OSError:
                    pass
            time.sleep(.1)
        else:
            raise RuntimeError('EXE start timed out')
        token = re.search('name="refarchive-token" content="([^"]+)"', request('GET', '/').decode('utf-8-sig')).group(1)
    def hashes():
        library = profile / 'RefArchive/data/library'
        return {str(path.relative_to(library)): hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in ('files', 'thumbs') for path in (library / folder).iterdir()}
    try:
        start(old / 'RefArchive/RefArchive.exe', '0.3.0')
        for index, color in enumerate(('red', 'blue')):
            body, headers = multipart(picture(color), {'filename': f'이전 자료{index}.png', 'uploadBundle': 'd' * 32,
                                                       'bundleTitle': '업데이트 보존 묶음', 'tags': ['기존 키워드'], 'note': '원래 메모'})
            request('POST', '/api/files', body, headers)
        before = json.loads(request('GET', '/api/state')); original_hashes = hashes()
        post('/api/shutdown', {}); assert process.wait(20) == 0
        start(ROOT / 'release/RefArchive/RefArchive.exe', VERSION)
        after = json.loads(request('GET', '/api/state'))
        for key in before:
            if key != 'job': assert before[key] == after[key], key
        assert hashes() == original_hashes
        post('/api/shutdown', {}); assert process.wait(20) == 0
        result = {'from': '0.3.0', 'to': VERSION, 'profile': str(profile), 'sameDefaultData': True,
                  'items': len(after['items']), 'bundles': len(after['bundles']), 'originalHashesPreserved': True, 'metadataPreserved': True}
        (target / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if process and process.poll() is None:
            try:
                post('/api/shutdown', {}); process.wait(20)
            except Exception:
                process.terminate(); process.wait(10)


if __name__ == '__main__':
    main()
