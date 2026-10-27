"""One-off >1 GiB backup and HTTP streaming restore test, isolated from user data."""
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from archive import make_server
from test_video_groups import mp4


def main():
    target = ROOT / 'test-results' / f'large-backup-{time.time_ns()}'
    target.mkdir(parents=True)
    if shutil.disk_usage(target).free < 8 * 1024**3:
        raise RuntimeError('시험에 필요한 저장 공간 8 GiB가 없습니다.')
    server = make_server(target / 'data', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    library = server.library
    try:
        # Valid container fixtures; not playable videos. Padding is intentionally incompressible.
        block = os.urandom(1024 * 1024)
        for index in range(6):
            seed = mp4() + (9).to_bytes(4) + b'free' + bytes([index])
            key = library.add_file(seed, {'filename': f'large-{index}.mp4'})['id']
            path = library.media_path(key)
            with path.open('ab') as output:
                output.write((180 * 1024 * 1024 + 8).to_bytes(4) + b'free')
                for _ in range(180):
                    output.write(block)
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            with library.db() as connection:
                connection.execute('UPDATE items SET size=?,hash=? WHERE id=?', (path.stat().st_size, digest, key))
        before = library.state()['items']
        print('Large fixture ready', flush=True)
        library.maintenance.backup()
        backup = library.maintenance.latest
        assert backup.stat().st_size > 1024**3
        print(f'Backup ready: {backup.stat().st_size} bytes', flush=True)
        library.batch({'ids': [before[0]['id']], 'action': 'trash'})
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=300)
        try:
            with backup.open('rb') as source:
                connection.request('POST', '/api/restore', source, {'Content-Length': str(backup.stat().st_size), 'X-RefArchive-Token': server.token})
            response = connection.getresponse()
            result = json.load(response)
            assert response.status == 200, result
        finally:
            connection.close()
        assert library.state()['items'] == before
        for item in before:
            with library.media_path(item['id']).open('rb') as source:
                assert hashlib.file_digest(source, 'sha256').hexdigest() == item['hash']
        record = {'zipBytes': backup.stat().st_size, 'originalBytes': sum(item['size'] for item in before),
                  'files': len(before), 'metadataPreserved': True, 'originalHashesPreserved': True, 'transport': 'HTTP file stream'}
        (target / 'verification.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        print(json.dumps(record), flush=True)
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
    # Remove only this test's generated multi-GiB fixture after preserving its verification record.
    fixture = (target / 'data').resolve()
    assert fixture.parent == target.resolve() and target.resolve().is_relative_to((ROOT / 'test-results').resolve())
    shutil.rmtree(fixture)


if __name__ == '__main__':
    main()
