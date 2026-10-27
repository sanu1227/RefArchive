"""Install/update/uninstall only in a new test directory; preserve existing installs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import winreg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_paths import VERSION


def hashes(folder):
    return {file.relative_to(folder).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in folder.rglob('*') if file.is_file()}


def main():
    # The installer writes one per-user uninstall entry. Do not replace a real installation.
    key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{83E093B9-256E-449C-8434-8520E08AFA9C}_is1'
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | view):
                raise SystemExit('기존 설치가 있습니다. 설치·제거 자동 시험을 중단합니다.')
        except FileNotFoundError:
            pass
    root = ROOT / 'test-results' / f'installer-smoke-{time.time_ns()}'
    installed = (root / '한글 설치 경로').resolve()
    if not installed.is_relative_to((ROOT / 'test-results').resolve()):
        raise SystemExit('시험 설치 경로가 작업 폴더 밖입니다.')
    root.mkdir(parents=True)
    installer = ROOT / 'release' / f'RefArchive-{VERSION}-Setup-x64.exe'
    command = [str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', '/TASKS=',
               '/LANG=korean', f'/DIR={installed}', f'/LOG={root / "install.log"}']
    subprocess.run(command, check=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    result = subprocess.run([sys.executable, str(ROOT / 'packaging' / 'smoke_release.py'), '--bundle', str(installed)], check=True, capture_output=True, timeout=120, encoding='utf-8')
    # Python's console encoding can vary; force UTF-8 in this test's child process.
    verification = json.loads(result.stdout)
    profile = Path(verification['profile'])
    before = hashes(profile / 'RefArchive')
    env = {**os.environ, 'LOCALAPPDATA': str(profile)}
    subprocess.run(command, check=True, timeout=90, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    assert hashes(profile / 'RefArchive') == before, '업데이트가 개인 자료를 변경했습니다.'
    subprocess.run([str(installed / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/LOG={root / "uninstall.log"}'], check=True, timeout=90, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 20
    while (installed / 'RefArchive.exe').exists() and time.monotonic() < deadline:
        time.sleep(.1)
    assert not (installed / 'RefArchive.exe').exists(), '프로그램 제거가 끝나지 않았습니다.'
    assert hashes(profile / 'RefArchive') == before, '제거가 개인 자료를 변경했습니다.'
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | view):
                raise AssertionError('시험 설치의 제거 등록이 남았습니다.')
        except FileNotFoundError:
            pass
    verification['checks'] += ['한국어 설치파일 실제 설치·업데이트·제거', '업데이트·제거 전후 개인 자료·모델 SHA-256 일치', '시험 설치 제거 등록 정리']
    (root / 'verification.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(verification, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    # Keep child output deterministic on Korean Windows.
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    main()
