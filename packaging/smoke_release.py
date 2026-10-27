"""Exercise the actual EXE with an isolated profile and no Python on its PATH."""
import argparse
import hashlib
from http.client import HTTPConnection, parse_headers
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_archive import multipart, picture
from test_features import pdf
from test_video_groups import mp4, webm
from test_documents_dates import office
from archive import SCHEMA_V4
from runtime_paths import VERSION
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, default=ROOT / 'release' / 'RefArchive')
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    checks = []
    manifest = json.loads((bundle / 'manifest-sha256.json').read_text())
    for name, digest in manifest.items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest, name
    checks.append(f'패키지 {len(manifest)}개 파일 체크섬 일치')
    target = ROOT / 'test-results' / f'release-smoke-{time.time_ns()}'
    profile = target / '격리된 사용자'
    work = target / '임의 작업 폴더'
    work.mkdir(parents=True)
    env = {**os.environ, 'LOCALAPPDATA': str(profile), 'PATH': str(Path(os.environ['SystemRoot']) / 'System32'), 'PYTHONPATH': '', 'PYTHONHOME': ''}
    data = profile / 'RefArchive' / 'data'
    executable = bundle / 'RefArchive.exe'
    process = None
    port = None
    token = None
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        test_port = probe.getsockname()[1]

    def request(method, path, body=None, headers=None, expected=(200, 201, 206)):
        connection = HTTPConnection('127.0.0.1', port, timeout=60)
        try:
            connection.request(method, path, body, {'X-RefArchive-Token': token or '', **(headers or {})})
            response = connection.getresponse()
            content = response.read()
            assert response.status in expected, (path, response.status, content[:300])
            return content
        finally:
            connection.close()

    def head(path):
        # Read raw HTTP so an accidental HEAD body cannot be hidden by HTTPConnection.
        with socket.create_connection(('127.0.0.1', port), timeout=10) as connection:
            connection.sendall(f'HEAD {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n'.encode('ascii'))
            with connection.makefile('rb') as response:
                status = int(response.readline().split()[1])
                headers = dict(parse_headers(response))
                return status, headers, response.read()

    def state():
        return json.loads(request('GET', '/api/state'))

    def post(path, value):
        return json.loads(request('POST', path, json.dumps(value).encode(), {'Content-Type': 'application/json'}))

    def start():
        nonlocal process, port, token
        process = subprocess.Popen([str(executable), '--no-browser', '--port', str(test_port)], cwd=work, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f'실행 실패: {process.returncode}; 기록: {data / "logs"}')
            try:
                port = json.loads((data / 'runtime.json').read_text())['port']
                page = request('GET', '/').decode()
                token = re.search(r'name="refarchive-token" content="([^"]+)"', page).group(1)
                return
            except (OSError, ValueError, AttributeError):
                time.sleep(.1)
        raise AssertionError('시작 시간 초과')

    def upload(content, **metadata):
        body, headers = multipart(content, metadata)
        item_id = json.loads(request('POST', '/api/files', body, headers))['id']
        wait_job()
        return item_id

    def job(**value):
        wait_job()
        post('/api/job', value)
        return wait_job()

    def wait_job():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = json.loads(request('GET', '/api/job'))
            if not status['running']:
                assert not status['errors'], status
                return status
            time.sleep(.1)
        raise AssertionError('분석 시간 초과')

    try:
        start()
        assert state()['items'] == []
        assert not state()['modelReady']
        health = json.loads(request('GET', '/api/health'))
        assert health['appVersion'] == VERSION
        assert health['storage'] == str(data / 'library')
        assert '<article>' in request('GET', '/guide').decode()
        assert '<article>' in request('GET', '/distribution').decode()
        assert len(request('GET', '/guide.css')) > 1000
        assert (bundle / 'user-guide.html').is_file() and (bundle / 'distribution-guide.html').is_file()
        assert not (bundle / '_internal' / 'docs' / 'user-guide.md').exists()
        assert len(request('GET', '/app.js')) > 1000
        assert len(request('GET', '/workspace.css')) > 1000
        checks.append(f'Python PATH 제외·한글 경로·임의 작업 폴더·빈 자료함 실행 (포트 {port})')
        second = subprocess.Popen([str(executable), '--no-browser', '--port', str(test_port)], cwd=work, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        assert second.wait(timeout=20) == 0
        assert process.poll() is None
        checks.append('두 번째 실행은 기존 자료함 사용, 원래 프로세스 유지')
        legacy = target / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as connection:
            connection.executescript(SCHEMA_V4)
            connection.execute("INSERT INTO items(id,type,title,source,created) VALUES (?,'link','이전 자료','https://example.invalid','2020-01-02T00:00:00+00:00')", ('a' * 32,))
        old_backup = io.BytesIO()
        with zipfile.ZipFile(old_backup, 'w') as output:
            output.writestr('manifest.json', '{"format":"RefArchive","version":1}')
            output.write(legacy, 'archive.sqlite3')
        request('POST', '/api/restore', old_backup.getvalue())
        old_item = state()['items'][0]
        assert old_item['material_date'] == '' and old_item['created'] == '2020-01-02T00:00:00+00:00'
        checks.append('실행파일에서 v4 백업을 v5로 복원·기존 등록일 보존·자료 날짜 미지정 유지')
        document = upload(pdf(), filename='테스트 문서.pdf', tags=['문서'])
        job(operation='text', ids=[document])
        item = next(item for item in state()['items'] if item['id'] == document)
        assert len(item['analysis']['pages']) == 2
        assert 'lighting' in item['analysis']['pages'][1]['text']
        assert item['analysis']['summary']
        assert request('GET', '/media/' + document) == pdf()
        checks.append('PDF 원본 보존·미리보기·텍스트·요약')
        group = {'uploadGroup': 'd' * 32, 'uploadGroupName': '혼합 업로드'}
        video_ids = []
        for content, name in [(mp4(), '동영상.mp4'), (webm(), '동영상.webm')]:
            video = upload(content, filename=name, **group)
            video_ids.append(video)
            assert request('GET', '/media/' + video) == content
            assert request('GET', '/media/' + video, headers={'Range': 'bytes=0-7'}) == content[:8]
        grouped_image = upload(picture('purple'), filename='그룹.png', **group)
        assert upload(mp4(), filename='중복.mp4', **group) == video_ids[0]
        assert state()['collections'][0]['ids'] == [*video_ids, grouped_image]
        checks.append('MP4·WebM 컨테이너 업로드·원본·구간 응답·이미지 혼합 그룹·중복 연결')
        bundle_meta = {'uploadBundle': 'e' * 32, 'bundleTitle': '한 카드에 두 파일', 'tags': ['묶음 시험']}
        bundle_image = upload(picture('orange'), filename='묶음-첫째.png', **bundle_meta)
        bundle_second = upload(picture('pink'), filename='묶음-둘째.png', **bundle_meta)
        assert state()['bundles'][0]['ids'] == [bundle_image, bundle_second]
        assert state()['entryCount'] == len(state()['items']) - 1
        post('/api/batch', {'ids': ['e' * 32], 'action': 'trash'})
        assert all(item['trashed'] for item in state()['items'] if item['id'] in (bundle_image, bundle_second))
        post('/api/batch', {'ids': ['e' * 32], 'action': 'restore'})
        assert post('/api/classification/scope', {'mode': 'ids', 'ids': ['e' * 32]})['count'] == 2
        checks.append('묶음 업로드·한 자료 집계·전체 휴지통과 복원·묶음 내부 분류 범위')
        post('/api/bundle', {'action': 'edit', 'id': 'e' * 32, 'ids': [bundle_second, bundle_image], 'cover': bundle_image})
        edited = next(value for value in state()['bundles'] if value['id'] == 'e' * 32)
        assert edited['ids'] == [bundle_second, bundle_image] and edited['cover'] == bundle_image
        post('/api/bundle', {'action': 'detach', 'id': 'e' * 32, 'file': bundle_second})
        assert next(value for value in state()['bundles'] if value['id'] == 'e' * 32)['ids'] == [bundle_image]
        documents = {}
        for extension in ('.docx', '.xlsx', '.pptx', '.txt'):
            content = office(extension) if extension != '.txt' else '자료 날짜와 본문 검색 검증'.encode()
            item_id = upload(content, filename='문서' + extension, tags=['텍스트 자료'], categories=['문서'], material_date='2024-02-29')
            documents[item_id] = content
            item = next(item for item in state()['items'] if item['id'] == item_id)
            assert item['type'] == 'document' and item['material_date'] == '2024-02-29'
            assert item['analysis']['pages'][0]['text'] and not item['analysis']['openReason']
            assert request('GET', '/media/' + item_id) == content
            assert request('GET', '/thumb/' + item_id).startswith(b'\xff\xd8')
        check_id = next(iter(documents))
        item = next(item for item in state()['items'] if item['id'] == check_id)
        created = item['created']
        post('/api/update', {**item, 'material_date': '2000-02-29'})
        edited = next(item for item in state()['items'] if item['id'] == check_id)
        assert edited['material_date'] == '2000-02-29' and edited['created'] == created
        request('POST', '/api/update', json.dumps({**edited, 'material_date': '2026-02-30'}).encode(), {'Content-Type': 'application/json'}, expected=(400,))
        assert next(item for item in state()['items'] if item['id'] == check_id)['material_date'] == '2000-02-29'
        post('/api/update', {**edited, 'material_date': ''})
        assert next(item for item in state()['items'] if item['id'] == check_id)['material_date'] == ''
        filters = {'type': 'document', 'dateFrom': '2024-01-01', 'dateTo': '2024-12-31', 'sort': 'date-asc', 'group': 'month'}
        post('/api/saved', {'kind': 'search', 'name': '문서 기간', 'filters': filters})
        assert state()['searches'][0]['filters']['dateFrom'] == filters['dateFrom']
        assert 'date-group' in request('GET', '/').decode()
        assert '자료 날짜로 정리하기' in request('GET', '/guide').decode()
        checks.append('DOCX·XLSX·PPTX·TXT 원본·미리보기·본문, 날짜 수정·비우기·오류 거절, 기간·정렬·그룹 검색 저장')
        post('/api/taxonomy', {'kind': 'tags', 'name': '텍스트 규칙 시험'})
        rule = {'mode': 'auto', 'fields': ['title', 'content'], 'match': 'any', 'terms': ['한글']}
        post('/api/text-rule', {'name': '텍스트 규칙 시험', 'rule': rule})
        docx = next(item['id'] for item in state()['items'] if item['filename'] == '문서.docx')
        job(operation='classify', ids=[docx])
        assert '텍스트 규칙 시험' in next(item for item in state()['items'] if item['id'] == docx)['autoTags']
        prediction = next(p for p in state()['predictions'] if p['item'] == docx and p['tag'] == '텍스트 규칙 시험')
        assert prediction['source'] == 'text' and prediction['matches'][0]['field'] == 'content'
        post('/api/text-rule', {'name': '텍스트 규칙 시험', 'rule': {**rule, 'mode': 'suggest'}})
        job(operation='classify', ids=[docx])
        assert '텍스트 규칙 시험' not in next(item for item in state()['items'] if item['id'] == docx)['tags']
        post('/api/review', {'id': docx, 'tag': '텍스트 규칙 시험', 'action': 'accept'})
        post('/api/text-rule', {'name': '텍스트 규칙 시험', 'rule': {**rule, 'mode': 'off'}})
        checks.append('문서 본문 규칙 자동 적용·일치 근거·추천만 재분류·승인·모델 및 사례 없는 분류')
        protected = upload(office('.docx', {'word/embeddings/oleObject1.bin': b'not executable'}), filename='첨부 포함.docx')
        request('POST', '/api/open', json.dumps({'id': protected}).encode(), {'Content-Type': 'application/json'}, expected=(400,))
        request('POST', '/api/open', json.dumps({'id': check_id}).encode(), {'Content-Type': 'application/json', 'X-RefArchive-Token': ''}, expected=(403,))
        macro, headers = multipart(office('.docx', {'word/vbaProject.bin': b'not executable'}), {'filename': 'macro.docx'})
        request('POST', '/api/files', macro, headers, expected=(400,))
        assert not (data / 'opened').exists()
        checks.append('매크로 가져오기·첨부 문서 연결 앱 열기·무인증 열기 거부; 실제 연결 앱 미실행')
        post('/api/maintenance', {'operation': 'backup'})
        deadline = time.monotonic() + 30
        while True:
            maintenance = json.loads(request('GET', '/api/maintenance'))
            if not maintenance['running']:
                break
            assert time.monotonic() < deadline
            time.sleep(.1)
        assert not maintenance['error'] and maintenance['downloadReady'], maintenance
        ticket = post('/api/backup/download', {})['url']
        for _ in range(3):
            status, headers, body = head(ticket)
            assert (status, body) == (200, b'')
            assert headers['Content-Type'] == 'application/zip'
            assert headers['Content-Disposition'].startswith('attachment; filename="')
            assert headers['Cache-Control'] == 'no-store'
        downloaded = request('GET', ticket)
        assert len(downloaded) == int(headers['Content-Length'])
        with zipfile.ZipFile(io.BytesIO(downloaded)) as saved:
            assert saved.testzip() is None
        status, _, body = head(ticket)
        assert (status, body) == (400, b'')
        request('GET', ticket, expected=(400,))
        checks.append('반복 HEAD: 헤더만 반환·권한 보존, 이후 GET ZIP 정상, 사용 후 HEAD·GET 거부')
        release = json.loads((bundle / '업데이트 정보.json').read_text(encoding='utf-8'))
        assert post('/api/release/check', release)['status'] == 'same'
        diagnostic = post('/api/diagnostics', {})
        assert diagnostic['databaseOK'] and diagnostic['missingFiles'] == 0
        assert str(profile) not in json.dumps(diagnostic)
        post('/api/maintenance', {'operation': 'duplicates'})
        deadline = time.monotonic() + 20
        while json.loads(request('GET', '/api/maintenance'))['running']:
            assert time.monotonic() < deadline
            time.sleep(.1)
        assert not json.loads(request('GET', '/api/maintenance'))['error']
        checks.append('대표·순서·개별 분리, 비동기 백업·다운로드, 중복 검사, ZIP 버전 확인과 진단 보고서')
        text_image = Image.new('RGB', (1100, 220), 'white')
        ImageDraw.Draw(text_image).text((30, 50), 'ARCHIVE SEARCH TEST', font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 60), fill='black')
        output = io.BytesIO()
        text_image.save(output, 'PNG')
        text_id = upload(output.getvalue(), filename='ocr.png')
        job(operation='text', ids=[text_id], ocr=True, language='ko-KR')
        assert 'ARCHIVE' in next(item for item in state()['items'] if item['id'] == text_id)['analysis']['pages'][0]['text']
        checks.append('실행파일에서 Windows OCR 실제 인식')

        source_model = ROOT / '.models' / 'clip-vision-uint8.onnx'
        if source_model.is_file():
            models = profile / 'RefArchive' / 'models'
            models.mkdir(parents=True)
            shutil.copy2(source_model, models / source_model.name)
            for index in range(10):
                upload(picture((20 + index, 70, 30)), filename=f'사례-{index}.png', tags=['초록'], categories=['시험'])
            probe = upload(picture((25, 70, 31)), filename='새 이미지.png')
            post('/api/mode', {'name': '초록', 'mode': 'suggest', 'threshold': .8})
            job(operation='classify', ids=[probe])
            assert any(value['item'] == probe and value['tag'] == '초록' for value in state()['predictions'])
            checks.append('동봉하지 않은 모델을 시험 프로필에만 복사, ONNX 실제 분류')
        else:
            checks.append('모델 없음: ONNX 실제 분류는 미검증')

        post('/api/taxonomy', {'kind': 'categories', 'name': '프로젝트 회의'})
        category_document = upload('회의 안건과 결정 사항을 기록했습니다.'.encode(), filename='프로젝트 회의록.txt')
        assert next(item for item in state()['items'] if item['id'] == category_document)['autoCategories'] == ['프로젝트 회의']
        rule = {'mode': 'suggest', 'description': '회의의 결정 사항과 담당자 기록', 'include': ['회의록'], 'exclude': ['회의실 예약']}
        post('/api/category-rule', {'name': '프로젝트 회의', 'rule': rule})
        job(operation='classify', ids=[category_document])
        assert next(item for item in state()['items'] if item['id'] == category_document)['categories'] == []
        post('/api/category-review', {'id': category_document, 'name': '프로젝트 회의', 'action': 'accept'})
        assert next(item for item in state()['items'] if item['id'] == category_document)['autoCategories'] == []
        category_rules, category_predictions = state()['categoryRules'], state()['categoryPredictions']
        checks.append('카테고리 생성·업로드 자동 배정·설정 변경 재분류·승인 실제 HTTP 흐름')
        before = state()['items']
        before_groups = state()['collections']
        before_bundles = state()['bundles']
        before_searches = state()['searches']
        backup = request('POST', '/api/backup', b'{}')
        with zipfile.ZipFile(io.BytesIO(backup)) as archive:
            assert archive.testzip() is None
        post('/api/batch', {'ids': [document], 'action': 'trash'})
        request('POST', '/api/restore', backup)
        assert state()['items'] == before
        assert state()['collections'] == before_groups
        assert state()['bundles'] == before_bundles
        assert state()['searches'] == before_searches
        assert state()['categoryRules'] == category_rules
        assert state()['categoryPredictions'] == category_predictions
        for item_id, content in documents.items():
            assert request('GET', '/media/' + item_id) == content
        checks.append('ZIP 백업·휴지통·복원 후 메타데이터 일치')
        post('/api/shutdown', {})
        assert process.wait(timeout=20) == 0
        start()
        assert state()['items'] == before
        assert state()['bundles'] == before_bundles
        assert state()['searches'] == before_searches
        post('/api/shutdown', {})
        assert process.wait(timeout=20) == 0
        checks.append('정상 종료·재실행 후 개인 자료 유지')
        assert not (bundle / 'data').exists()
        result = {'version': VERSION, 'bundle': str(bundle), 'profile': str(profile), 'checks': checks}
        (target / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if process and process.poll() is None:
            try:
                post('/api/shutdown', {})
                process.wait(timeout=20)
            except Exception:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
