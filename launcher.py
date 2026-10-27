"""Windowless Windows distribution entry point; never needs a system Python."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from urllib.request import urlopen
import webbrowser

from runtime_paths import VERSION, default_data


def open_window(address):
    try:
        opened = webbrowser.open(address)
    except Exception:
        opened = False
    if not opened:
        # A browser association failure must not shut down an otherwise healthy library.
        print('브라우저 열기 실패. 접속 주소: ' + address, flush=True)
        threading.Thread(target=ctypes.windll.user32.MessageBoxW,
                         args=(None, 'RefArchive는 실행 중입니다.\n브라우저 주소창에 아래 주소를 입력하세요.\n\n' + address,
                               'RefArchive 실행 중', 0x40), daemon=False).start()
    return opened


def existing_address(data):
    record = json.loads((data / 'runtime.json').read_text(encoding='utf-8'))
    port = record['port']
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('실행 주소가 올바르지 않습니다.')
    address = f'http://127.0.0.1:{port}'
    with urlopen(address + '/api/health', timeout=1) as response:
        health = json.load(response)
    if health.get('app') != 'RefArchive' or health.get('version') != 2 or health.get('storage') != str(data / 'library'):
        raise ValueError('실행 중인 자료함을 확인하지 못했습니다.')
    if health.get('appVersion') != VERSION:
        raise RuntimeError('이전 버전이 실행 중입니다. 보관 · 백업에서 프로그램을 종료한 뒤 다시 실행하세요.')
    return address


def run(args):
    import msvcrt
    data = args.data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    # A held byte lock prevents two launchers opening the same SQLite library.
    with (data / 'runtime.lock').open('a+b') as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            for _ in range(100):
                try:
                    address = existing_address(data)
                    if not args.no_browser:
                        open_window(address)
                    return 0
                except RuntimeError:
                    raise
                except (OSError, ValueError, KeyError):
                    time.sleep(.1)
            raise RuntimeError('자료함이 시작 중이거나 종료 중입니다. 잠시 후 다시 실행하세요.')

        import archive
        server = None
        ports = [args.port] if args.port is not None else range(18765, 18785)
        for port in ports:
            try:
                server = archive.make_server(data, port)
                break
            except OSError as exc:
                if getattr(exc, 'winerror', None) != 10048 and exc.errno not in (98, 10048):
                    raise
                try:
                    with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
                        health = json.load(response)
                except (OSError, ValueError):
                    continue
                if health.get('app') == 'RefArchive' and health.get('storage') == str(data / 'library'):
                    raise RuntimeError('같은 자료함이 다른 실행 방식으로 열려 있습니다. 기존 앱에서 프로그램을 종료한 뒤 다시 실행하세요.')
        if server is None:
            raise RuntimeError('실행 포트가 사용 중입니다. 실행 중인 프로그램을 확인하세요.')
        record = data / 'runtime.json'
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel.CreateMutexW(None, False, 'RefArchive.Application')
        if not mutex:
            server.server_close()
            raise ctypes.WinError()
        try:
            record.write_text(json.dumps({'port': server.server_port}), encoding='utf-8')
            address = f'http://127.0.0.1:{server.server_port}'
            print(f'RefArchive {VERSION}: {address}\n자료 위치: {server.library.root}', flush=True)
            if not args.no_browser:
                open_window(address)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            kernel.CloseHandle(mutex)
            record.unlink(missing_ok=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description='RefArchive Windows 실행기')
    parser.add_argument('--data', type=Path, default=default_data())
    parser.add_argument('--port', type=int, choices=range(1024, 65536), metavar='PORT')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args()
    log = args.data.resolve() / 'logs' / f'runtime-{os.getpid()}.log'
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        if getattr(sys, 'frozen', False):
            sys.stdout = sys.stderr = log.open('a', encoding='utf-8', buffering=1)
        return run(args)
    except Exception as exc:
        message = f'RefArchive를 실행하지 못했습니다.\n{exc}\n\n실행 기록: {log}'
        if sys.stderr:
            traceback.print_exc()
        if not args.no_browser:
            ctypes.windll.user32.MessageBoxW(None, message, 'RefArchive 실행 오류', 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
