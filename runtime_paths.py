"""Read-only bundle resources and writable per-user runtime paths."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
VERSION = '0.7.0'
POWERSHELL = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')


def default_data():
    if getattr(sys, 'frozen', False):
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'RefArchive' / 'data'
    return ROOT / 'data'


def model_path():
    folder = default_data().parent / 'models' if getattr(sys, 'frozen', False) else ROOT / '.models'
    return folder / 'clip-vision-uint8.onnx'
