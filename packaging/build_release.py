"""Build an allowlisted Windows bundle, installer, portable ZIP and checksums."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_paths import VERSION
from packaging.requirements import Requirement
from render_guides import render_guides


def runtime_dependencies():
    pending = ['Pillow', 'numpy', 'onnxruntime', 'pypdfium2']
    found = {}
    while pending:
        distribution = metadata.distribution(pending.pop())
        name = distribution.metadata['Name']
        if name in found:
            continue
        found[name] = distribution
        for requirement in distribution.requires or []:
            dependency = Requirement(requirement)
            if dependency.marker is None or dependency.marker.evaluate({'extra': ''}):
                pending.append(dependency.name)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compiler', type=Path, required=True, help='Inno Setup ISCC.exe')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'release', help='Local package output directory')
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Windows x64 Python에서 빌드하세요.')
    if not args.compiler.is_file():
        raise SystemExit('Inno Setup의 ISCC.exe 경로를 지정하세요.')
    for line in (ROOT / 'requirements-release.txt').read_text().splitlines():
        requirement = Requirement(line)
        if metadata.version(requirement.name) not in requirement.specifier:
            raise SystemExit(f'requirements-build.txt를 설치하세요: {line}')

    assets = ROOT / 'build' / 'release-assets'
    render_guides()
    assets.mkdir(parents=True, exist_ok=True)
    dependencies = runtime_dependencies()
    notices = ['RefArchive third-party notices', 'Bundled versions and license texts follow.',
               'The CLIP model is downloaded separately at the user\'s request; it is not in this package.',
               'CLIP: https://github.com/openai/CLIP (MIT); ONNX model: https://huggingface.co/Xenova/clip-vit-base-patch32',
               '\nPython ' + platform.python_version() + '\n' + (Path(sys.base_prefix) / 'LICENSE.txt').read_text(encoding='utf-8')]
    for name, distribution in sorted(dependencies.items()):
        notices.append(f'\n{name} {distribution.version}\n{distribution.metadata.get("Home-page", "")}')
        license_files = [file for file in distribution.files or [] if any(word in str(file).lower() for word in ('license', 'copying', 'notice', 'authors')) and Path(file).suffix.lower() not in ('.py', '.pyc', '.dll', '.pyd')]
        if not license_files:
            fallback = ROOT / 'packaging' / 'licenses' / f'{name}-{distribution.version}-LICENSE.txt'
            if not fallback.is_file():
                raise SystemExit(f'라이선스 파일을 확인하세요: {name}')
            notices += [str(fallback.relative_to(ROOT)), fallback.read_text(encoding='utf-8')]
        for file in license_files:
            notices += [str(file), Path(distribution.locate_file(file)).read_text(encoding='utf-8', errors='replace')]
    (assets / 'THIRD_PARTY_NOTICES.txt').write_text('\n\n'.join(notices), encoding='utf-8-sig')
    versions = {name: distribution.version for name, distribution in sorted(dependencies.items())}
    versions['Python'] = platform.python_version()
    (assets / 'versions.json').write_text(json.dumps({'appVersion': VERSION, 'dependencies': versions}, indent=2), encoding='utf-8')
    # Only listed resources enter the bundle. Never collect the repository, data or sample folders.
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed', '--noupx',
               '--name', 'RefArchive', '--distpath', str(output_dir), '--workpath', str(ROOT / 'build' / 'pyinstaller'),
               '--specpath', str(ROOT / 'build'), '--exclude-module', 'pymupdf', '--exclude-module', 'fitz']
    for source, destination in [('web', 'web'), ('docs/사용설명서.html', 'docs'), ('docs/배포안내.html', 'docs'), ('docs/guide.css', 'docs'), ('ocr.ps1', '.'), ('install-shortcut.ps1', '.'), (str(assets / 'THIRD_PARTY_NOTICES.txt'), '.')]:
        command += ['--add-data', f'{ROOT / source}:{destination}']
    command.append(str(ROOT / 'launcher.py'))
    subprocess.run(command, cwd=ROOT, check=True)
    bundle = output_dir / 'RefArchive'
    shutil.copy2(ROOT / 'packaging' / 'start-guide.txt', bundle / '처음 읽어주세요.txt')
    for name in ('사용설명서.html', '배포안내.html', 'guide.css'):
        shutil.copy2(ROOT / 'docs' / name, bundle / name)
    shutil.copy2(assets / 'THIRD_PARTY_NOTICES.txt', bundle)
    shutil.copy2(assets / 'versions.json', bundle)
    (bundle / '업데이트 정보.json').write_text(json.dumps({'format': 'RefArchive-release', 'version': VERSION,
        'platform': 'windows-x64', 'distribution': 'installer-and-portable'}, ensure_ascii=False, indent=2), encoding='utf-8')
    forbidden = {'.venv', '.models', 'data', 'sample', '.git', 'tests', 'test-results', 'pymupdf', 'fitz'}
    paths = [path for path in bundle.rglob('*') if path.is_file()]
    if any(forbidden.intersection(path.relative_to(bundle).parts) or path.suffix in ('.sqlite3', '.onnx') for path in paths):
        raise SystemExit('배포본에 개인 자료 또는 제외 대상이 있습니다.')
    manifest = {path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    (bundle / 'manifest-sha256.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    subprocess.run([str(args.compiler.resolve()), f'/DAppVersion={VERSION}', f'/DReleaseDir={output_dir}', str(ROOT / 'packaging' / 'installer.iss')], cwd=ROOT, check=True)
    portable = output_dir / f'RefArchive-{VERSION}-Portable-x64.zip'
    with zipfile.ZipFile(portable, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in bundle.rglob('*'):
            if path.is_file():
                output.write(path, path.relative_to(bundle.parent).as_posix())
    installer = output_dir / f'RefArchive-{VERSION}-Setup-x64.exe'
    checksums = '\n'.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}' for path in (installer, portable)) + '\n'
    (output_dir / 'SHA256SUMS.txt').write_text(checksums, encoding='utf-8')
    # Keep release asset names stable so recipients can reuse the latest/download URL.
    online = output_dir / '온라인 배포'
    online.mkdir(exist_ok=True)
    shutil.copy2(installer, online / 'RefArchive-Setup-x64.exe')
    shutil.copy2(portable, online / 'RefArchive-Portable-x64.zip')
    shutil.copy2(bundle / '업데이트 정보.json', online / 'update-info.json')
    (online / 'SHA256SUMS.txt').write_text('\n'.join(
        f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}'
        for path, name in ((installer, 'RefArchive-Setup-x64.exe'), (portable, 'RefArchive-Portable-x64.zip'))) + '\n', encoding='utf-8')
    print(f'완료: {installer}\n{portable}')


if __name__ == '__main__':
    main()
