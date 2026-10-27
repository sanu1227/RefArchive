"""Local backup scheduling, duplicate review and offline distribution support."""

from collections import deque
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import re
import secrets
import shutil
import threading
import time
import uuid

import numpy as np
from PIL import Image

from runtime_paths import VERSION


PILOT_CHECKS = {
    'launch': '압축을 풀고 EXE 실행',
    'upload': '이미지·영상 추가 및 재생',
    'bundle': '묶음 대표·순서·분리',
    'search': '키워드·카테고리 검색',
    'backup': '시험 자료함 백업·복원',
    'restart': '종료·재실행 후 자료 유지',
    'update': '업데이트 전후 기존 자료 유지',
}


class Maintenance:
    def __init__(self, library, error):
        self.library, self.Error = library, error
        self.lock = threading.RLock()
        self.settings_path = library.data / 'maintenance.json'
        self.settings = {'automatic': False, 'folder': str(library.data / 'backups'), 'lastBackup': '', 'lastFile': '', 'pilot': {}}
        defaults = dict(self.settings)
        self.worker = None
        self.stopping = False
        self.settings_error = ''
        try:
            if self.settings_path.exists():
                if self.settings_path.stat().st_size > 65536:
                    raise ValueError()
                saved = json.loads(self.settings_path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict) or type(saved.get('automatic')) is not bool or not isinstance(saved.get('folder'), str):
                    raise ValueError()
                self.settings.update({key: saved[key] for key in self.settings if key in saved})
                self.folder(self.settings['folder'])
                if any(not isinstance(self.settings[key], str) for key in ('lastBackup', 'lastFile')) or not isinstance(self.settings['pilot'], dict):
                    raise ValueError()
                if self.settings['lastBackup']:
                    if datetime.fromisoformat(self.settings['lastBackup']).tzinfo is None:
                        raise ValueError()
                pilot = self.settings['pilot']
                if pilot and (not isinstance(pilot.get('checks'), dict) or set(pilot['checks']) != set(PILOT_CHECKS)
                              or any(value not in ('pending', 'pass', 'fail') for value in pilot['checks'].values())
                              or not isinstance(pilot.get('version'), str) or not isinstance(pilot.get('saved'), str)):
                    raise ValueError()
        except (OSError, ValueError, TypeError, error):
            self.settings = defaults
            self.settings_error = '자동 백업 설정을 읽지 못했습니다. 저장 위치와 주기를 다시 저장하세요.'
        self.started = datetime.now(timezone.utc).isoformat()
        self.job = {'running': False, 'operation': '', 'done': 0, 'total': 0, 'error': self.settings_error}
        self.pairs = []
        self.skipped = 0
        self.limited = False
        self.latest = None
        self.tickets = {}
        self.next_check = 0
        self.request_errors = deque(maxlen=20)

    def folder(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise self.Error('백업 폴더의 전체 경로를 입력하세요.')
        folder = Path(value.strip())
        if not folder.is_absolute():
            raise self.Error('백업 폴더는 전체 경로로 입력하세요.')
        folder = folder.resolve()
        if folder == self.library.root or self.library.root in folder.parents:
            raise self.Error('자료함 내부 대신 별도 백업 폴더를 선택하세요.')
        return folder

    def persist(self):
        temporary = self.settings_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.settings_path)

    def configure(self, metadata):
        automatic = metadata.get('automatic')
        if type(automatic) is not bool:
            raise self.Error('자동 백업 설정을 확인하세요.')
        folder = self.folder(metadata.get('folder'))
        with self.lock:
            if self.job['running']:
                raise self.Error('진행 중인 보관 작업이 끝난 뒤 설정을 변경하세요.')
            folder.mkdir(parents=True, exist_ok=True)
            # Check write access without touching an existing user file.
            probe = folder / ('.refarchive-' + uuid.uuid4().hex)
            try:
                with probe.open('xb') as output:
                    output.write(b'')
            finally:
                probe.unlink(missing_ok=True)
            previous = dict(self.settings)
            self.settings.update(automatic=automatic, folder=str(folder))
            try:
                self.persist()
            except Exception:
                self.settings = previous
                raise
            self.next_check = 0
        return self.status()

    def status(self):
        with self.lock:
            return {**self.job, 'settings': dict(self.settings), 'version': VERSION, 'started': self.started,
                    'downloadReady': bool(self.latest and self.latest.is_file()),
                    'pairs': list(self.pairs), 'skipped': self.skipped, 'limited': self.limited,
                    'pilotChecks': PILOT_CHECKS}

    def progress(self, done, total):
        with self.lock:
            self.job.update(done=done, total=total)

    def start(self, metadata):
        operation = metadata.get('operation', 'backup')
        if operation not in ('backup', 'duplicates'):
            raise self.Error('지원하지 않는 보관 작업입니다.')
        # Never take the library lock while holding the status lock: backups report progress under it.
        if self.library.job['running']:
            raise self.Error('분석이 끝난 뒤 보관 작업을 실행하세요.')
        def work():
            try:
                if operation == 'backup':
                    self.backup()
                else:
                    self.find_duplicates()
            except Exception as exc:
                with self.lock:
                    self.job['error'] = str(exc) if isinstance(exc, self.Error) else '작업을 완료하지 못했습니다. 저장 공간과 파일 상태를 확인하세요.'
                    self.request_errors.append(type(exc).__name__)
            finally:
                with self.lock:
                    self.job['running'] = False
        with self.lock:
            if self.stopping:
                raise self.Error('프로그램이 종료 중입니다.')
            if self.job['running']:
                raise self.Error('보관 작업이 이미 진행 중입니다.')
            if operation == 'duplicates':
                self.pairs, self.skipped, self.limited = [], 0, False
            self.job = {'running': True, 'operation': operation, 'done': 0, 'total': 0, 'error': ''}
            self.worker = threading.Thread(target=work, daemon=False, name='archive-maintenance')
            self.worker.start()
        return self.status()

    def backup(self):
        folder = self.folder(self.settings['folder'])
        folder.mkdir(parents=True, exist_ok=True)
        name = f"RefArchive-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.zip"
        destination = folder / name
        partial = destination.with_suffix('.partial')
        try:
            with self.library.lock:
                size = sum(path.stat().st_size for path in self.library.root.rglob('*') if path.is_file())
                if size + 64 * 1024 * 1024 > shutil.disk_usage(folder).free:
                    raise self.Error('백업할 저장 공간이 부족합니다.')
                self.library.backup(partial, self.progress)
            partial.replace(destination)
            with self.lock:
                self.latest = destination
                self.settings.update(lastBackup=datetime.now(timezone.utc).isoformat(), lastFile=str(destination))
                self.persist()
        finally:
            partial.unlink(missing_ok=True)

    def tick(self):
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 60
        with self.lock:
            if self.stopping or not self.settings['automatic'] or self.job['running'] or self.library.job['running']:
                return
            last = self.settings['lastBackup']
            if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(days=1):
                return
        try:
            self.start({'operation': 'backup'})
        except self.Error:
            pass  # A manual operation won the race. Retry on the next tick.
        self.next_check = time.monotonic() + 3600  # Failed automatic backups retry at most hourly.

    def download_ticket(self, metadata):
        with self.lock:
            if not self.latest or not self.latest.is_file():
                raise self.Error('먼저 백업 ZIP을 만들어 주세요.')
            self.tickets = {key: value for key, value in self.tickets.items() if value[1] > time.monotonic()}
            key = secrets.token_urlsafe(32)
            self.tickets[key] = (self.latest, time.monotonic() + 120)
            return {'url': '/api/backup/download?ticket=' + key}

    def take_download(self, key, *, consume=True):
        with self.lock:
            value = self.tickets.pop(key, None) if consume else self.tickets.get(key)
        if not value or value[1] <= time.monotonic():
            raise self.Error('다운로드 시간이 지났습니다. ZIP 다운로드를 다시 누르세요.')
        return value[0]

    def find_duplicates(self):
        images = [item for item in self.library.state()['items'] if item['type'] == 'image' and not item['trashed']]
        if len(images) > 5000:
            raise self.Error('현재 중복 검사는 이미지 5,000개까지 지원합니다.')
        buckets, records = {}, []
        self.progress(0, len(images))
        for index, item in enumerate(images):
            try:
                with self.library.lock:
                    with Image.open(self.library.media_path(item['id'], True)) as source:
                        gray = np.asarray(source.convert('L').resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
                        pixels = np.asarray(source.convert('RGB').resize((16, 16), Image.Resampling.LANCZOS), dtype=np.int16)
                if float(gray.std()) < 10:
                    self.skipped += 1  # Flat images provide too little evidence for safe duplicate suggestions.
                    continue
                bits = (gray[:, 1:] > gray[:, :-1]).flatten()
                fingerprint = sum(int(bit) << position for position, bit in enumerate(bits))
                bands = [(band, (fingerprint >> (8 * band)) & 255) for band in range(8)]
                candidates = set().union(*(buckets.get(band, set()) for band in bands))
                ratio = item['width'] / max(1, item['height'])
                # ponytail: at most 5,000 images and 200 review pairs; use a persistent index above this ceiling.
                for candidate in sorted(candidates):
                    other, other_hash, other_pixels, other_ratio = records[candidate]
                    if (fingerprint ^ other_hash).bit_count() > 6 or abs(ratio / other_ratio - 1) > .02:
                        continue
                    delta = (pixels - other_pixels).astype(np.float32)
                    if float(np.abs(delta).mean()) <= 12 and float(np.sqrt((delta * delta).mean())) <= 18:
                        with self.lock:
                            self.pairs.append({'ids': [other, item['id']]})
                        if len(self.pairs) >= 200:
                            self.limited = True
                            return
                for band in bands:
                    buckets.setdefault(band, set()).add(len(records))
                records.append((item['id'], fingerprint, pixels, ratio))
            except (OSError, self.Error):
                self.skipped += 1
            finally:
                self.progress(index + 1, len(images))

    def resolve_duplicate(self, metadata):
        keep, remove = metadata.get('keep'), metadata.get('remove')
        if not isinstance(keep, str) or not isinstance(remove, str) or keep == remove:
            raise self.Error('비교할 파일 두 개를 선택하세요.')
        with self.library.lock, self.library.db() as connection:
            with self.lock:
                if self.job['running']:
                    raise self.Error('중복 검사가 끝난 뒤 정리하세요.')
                if not any(set(pair['ids']) == {keep, remove} for pair in self.pairs):
                    raise self.Error('중복 검사를 다시 실행하세요.')
            rows = list(connection.execute('SELECT id,trashed FROM items WHERE id IN (?,?)', (keep, remove)))
            if len(rows) != 2 or any(row['trashed'] for row in rows):
                raise self.Error('이미 이동했거나 삭제된 자료입니다. 다시 검사하세요.')
            bundle = next((value for value in self.library.bundles(connection).values() if remove in value['ids']), None)
            if bundle:
                self.library.detach_file(connection, bundle, remove)
            connection.execute('UPDATE items SET trashed=1 WHERE id=?', (remove,))
        with self.lock:
            self.pairs = [pair for pair in self.pairs if remove not in pair['ids']]
        return {'id': remove}

    def release(self, metadata):
        if metadata.get('format') != 'RefArchive-release' or metadata.get('platform') != 'windows-x64':
            raise self.Error('배포 ZIP에 포함된 업데이트 정보.json을 선택하세요.')
        version = metadata.get('version')
        if not isinstance(version, str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', version):
            raise self.Error('버전 정보가 올바르지 않습니다.')
        current, incoming = tuple(map(int, VERSION.split('.'))), tuple(map(int, version.split('.')))
        return {'current': VERSION, 'incoming': version, 'status': 'newer' if incoming > current else 'older' if incoming < current else 'same'}

    def pilot(self, metadata):
        checks = metadata.get('checks')
        if not isinstance(checks, dict) or set(checks) != set(PILOT_CHECKS) or any(value not in ('pending', 'pass', 'fail') for value in checks.values()):
            raise self.Error('다른 PC 점검 항목을 확인하세요.')
        with self.lock:
            previous = self.settings['pilot']
            self.settings['pilot'] = {'checks': checks, 'version': VERSION, 'saved': datetime.now(timezone.utc).isoformat()}
            try:
                self.persist()
            except Exception:
                self.settings['pilot'] = previous
                raise
        return self.report()

    def report(self):
        # Whitelist diagnostics. Never include library titles, notes, URLs, paths, tokens or raw logs.
        with self.library.lock, self.library.db() as connection:
            integrity = connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            counts = dict(connection.execute('SELECT type,COUNT(*) FROM items GROUP BY type'))
            missing = 0
            for row in connection.execute("SELECT id,path FROM items WHERE path IS NOT NULL"):
                missing += sum(not path.is_file() for path in (self.library.root / row['path'], self.library.root / 'thumbs' / (row['id'] + '.jpg')))
        with self.lock:
            return {'format': 'RefArchive-diagnostics', 'version': VERSION, 'created': datetime.now(timezone.utc).isoformat(),
                    'system': platform.system(), 'systemVersion': platform.release(), 'architecture': platform.machine(),
                    'running': True, 'databaseOK': integrity, 'counts': counts, 'missingFiles': missing,
                    'analysisErrorCount': len(self.library.job['errors']), 'errorTypes': list(self.request_errors),
                    'lastBackup': self.settings['lastBackup'], 'backupRunning': self.job['running'], 'pilot': self.settings['pilot']}
