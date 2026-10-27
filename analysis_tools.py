"""Local image features, PDF extraction, OCR and bounded public image downloads."""

from collections import Counter
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import http.client
import io
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

import numpy as np
from PIL import Image, ImageOps
import pypdfium2 as pdfium
from runtime_paths import ROOT, POWERSHELL, model_path

MODEL = model_path()
MODEL_SHA = 'b95448754a6ae56964ec80c570c80bb9863787ee85f51b664abed8c0e5f22a7a'
MODEL_URL = 'https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/d15189d/onnx/vision_model_uint8.onnx'
MODEL_INSTALL_LOCK = threading.Lock()


class AnalysisError(ValueError):
    pass


def _install_model():
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    if MODEL.is_file() and hashlib.sha256(MODEL.read_bytes()).hexdigest() == MODEL_SHA:
        return
    temporary = MODEL.with_suffix('.part')
    try:
        with urlopen(MODEL_URL, timeout=60) as response, temporary.open('wb') as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 100 * 1024 * 1024:
                    raise AnalysisError('모델 다운로드 크기를 확인하지 못했습니다.')
                output.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != MODEL_SHA:
            raise AnalysisError('모델 체크섬이 일치하지 않습니다.')
        temporary.replace(MODEL)
    finally:
        temporary.unlink(missing_ok=True)


def install_model():
    with MODEL_INSTALL_LOCK:
        _install_model()


class ClipEngine:
    def __init__(self):
        self.session = None
        self.lock = threading.Lock()
        self.cache = {}

    def vector(self, content):
        digest = hashlib.sha256(content).hexdigest()
        with self.lock:
            if digest in self.cache:
                return self.cache[digest]
            if self.session is None:
                if not MODEL.is_file():
                    raise AnalysisError('보관 · 백업에서 로컬 분석 모델을 먼저 준비하세요.')
                if hashlib.sha256(MODEL.read_bytes()).hexdigest() != MODEL_SHA:
                    raise AnalysisError('분석 모델이 손상됐습니다. 모델을 다시 준비하세요.')
                import onnxruntime as ort
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.log_severity_level = 3
                self.session = ort.InferenceSession(str(MODEL), options, providers=['CPUExecutionProvider'])
            with Image.open(io.BytesIO(content)) as original:
                image = ImageOps.exif_transpose(original).convert('RGB')
                width, height = image.size
                scale = 224 / min(width, height)
                image = image.resize((max(224, int(width * scale)), max(224, int(height * scale))), Image.Resampling.BICUBIC)
                left, top = (image.width - 224) // 2, (image.height - 224) // 2
                pixels = np.asarray(image.crop((left, top, left + 224, top + 224)), dtype=np.float32) / 255
            pixels = (pixels - [0.48145466, 0.4578275, 0.40821073]) / [0.26862954, 0.26130258, 0.27577711]
            values = np.transpose(pixels.astype(np.float32), (2, 0, 1))[None]
            output = self.session.run(['image_embeds'], {'pixel_values': values})[0][0]
            vector = output / max(float(np.linalg.norm(output)), 1e-12)
            # ponytail: bounded process cache; persist embeddings if repeated large-library scans become slow.
            if len(self.cache) >= 5000:
                self.cache.clear()
            self.cache[digest] = vector
            return vector


ENGINE = ClipEngine()


# PDFium is not thread-safe, including operations on different documents.
PDF_LOCK = threading.RLock()


def open_pdf(content):
    try:
        document = pdfium.PdfDocument(content)
    except pdfium.PdfiumError as exc:
        raise AnalysisError('PDF를 읽을 수 없습니다. 암호 없는 PDF인지 확인하세요.') from exc
    if pdfium.raw.FPDF_GetSecurityHandlerRevision(document) >= 0 or not 1 <= len(document) <= 300:
        document.close()
        raise AnalysisError('암호 없는 1~300페이지 PDF만 지원합니다.')
    return document


def render_pdf(page, scale):
    with closing(page.render(scale=scale)) as bitmap:
        return bitmap.to_pil().convert('RGB')


def pdf_info(content):
    try:
        with PDF_LOCK, open_pdf(content) as document, closing(document[0]) as page:
            width, height = page.get_size()
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise AnalysisError('PDF 페이지 크기가 올바르지 않습니다.')
            image = render_pdf(page, 480 / max(width, height))
            thumbnail = io.BytesIO()
            image.save(thumbnail, 'JPEG', quality=85)
            return len(document), thumbnail.getvalue()
    except pdfium.PdfiumError as exc:
        raise AnalysisError('PDF를 읽을 수 없습니다.') from exc


def ocr(content, language='ko-KR'):
    if language not in ('ko-KR', 'en-US'):
        raise AnalysisError('OCR 언어는 한국어 또는 영어를 선택하세요.')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'ocr.png'
        with Image.open(io.BytesIO(content)) as original:
            image = ImageOps.exif_transpose(original).convert('RGB')
            image.thumbnail((2400, 2400))
            image.save(path)
        result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'ocr.ps1'), '-ImagePath', str(path), '-Language', language], capture_output=True, timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise AnalysisError('Windows OCR를 실행하지 못했습니다. 선택한 언어의 OCR 언어팩을 확인하세요.')
        return json.loads(result.stdout.decode('utf-8-sig'))['text']


def extract_pdf(content, use_ocr=False, language='ko-KR'):
    pages, total = [], 0
    try:
        with PDF_LOCK, open_pdf(content) as document:
            for index in range(len(document)):
                with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                    text = textpage.get_text_bounded().replace('\r\n', '\n').strip()
                    method = 'embedded'
                    if use_ocr and not text:
                        if index >= 30:
                            raise AnalysisError('스캔 PDF OCR는 한 번에 최대 30페이지까지 지원합니다.')
                        width, height = page.get_size()
                        if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                            raise AnalysisError('PDF 페이지 크기가 올바르지 않습니다.')
                        image = render_pdf(page, min(2, 2400 / max(width, height)))
                        output = io.BytesIO()
                        image.save(output, 'PNG')
                        text = ocr(output.getvalue(), language).strip()
                        method = 'ocr'
                    total += len(text)
                    if total > 500000:
                        raise AnalysisError('추출 텍스트는 최대 50만 자까지 지원합니다.')
                    pages.append({'number': index + 1, 'text': text, 'method': method})
    except pdfium.PdfiumError as exc:
        raise AnalysisError('PDF를 읽을 수 없습니다.') from exc
    return pages


def summarize(pages):
    stop = {'그리고', '또한', '대한', '위한', '있는', '하는', '입니다', 'the', 'and', 'this', 'that', 'with', 'from', 'have', 'for', 'are', 'was', 'in', 'of', 'on', 'to', 'as', 'an', 'is', 'it', 'be', 'by', 'or', 'at', 'not'}
    tokens = lambda text: [word for word in re.findall(r'[가-힣A-Za-z][가-힣A-Za-z0-9]{1,29}', text.lower()) if word not in stop]
    frequency = Counter(tokens('\n'.join(page['text'] for page in pages)))
    candidates = []
    order = 0
    for page in pages:
        for sentence in re.split(r'(?<=[.!?。])\s+|\n+', page['text']):
            sentence = sentence.strip()
            if len(sentence) < 12:
                continue
            words = tokens(sentence)
            score = sum(frequency[word] for word in set(words)) / max(len(words) ** .5, 1)
            candidates.append((score, order, {'text': sentence[:1500], 'page': page['number']}))
            order += 1
    selected, seen = [], set()
    for score, order, sentence in sorted(candidates, key=lambda row: row[0], reverse=True):
        if sentence['text'] not in seen:
            selected.append((order, sentence))
            seen.add(sentence['text'])
        if len(selected) == 5:
            break
    return [sentence for _, sentence in sorted(selected)], [word for word, _ in frequency.most_common(10)]


class ImageLinks(HTMLParser):
    def __init__(self, address):
        super().__init__()
        self.address, self.images = address, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get('src') if tag == 'img' else attrs.get('content') if tag == 'meta' and attrs.get('property') in ('og:image', 'twitter:image') else None
        if value and len(self.images) < 100:
            address = urljoin(self.address, value)
            if address not in self.images and urlsplit(address).scheme in ('http', 'https'):
                self.images.append(address)


def public_get(address, limit=50 * 1024 * 1024):
    try:
        return _public_get(address, limit)
    except (OSError, http.client.HTTPException) as exc:
        raise AnalysisError('사이트에 연결하지 못했습니다. 네트워크와 사이트의 다운로드 허용 상태를 확인하세요.') from exc


def _public_get(address, limit):
    """Resolve once per redirect and pin the socket to a public address; never use ambient proxies/cookies."""
    for _ in range(5):
        parts = urlsplit(address)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise AnalysisError('공개 http 또는 https 주소만 지원합니다.')
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        if port not in (80, 443):
            raise AnalysisError('링크 가져오기는 기본 웹 포트만 지원합니다.')
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global or ipaddress.ip_address(entry[4][0]).is_multicast or ipaddress.ip_address(entry[4][0]).is_reserved for entry in addresses):
            raise AnalysisError('로컬·내부망 주소의 다운로드는 허용하지 않습니다.')
        ip = next((entry[4][0] for entry in addresses if entry[0] == socket.AF_INET), addresses[0][4][0])
        sock = socket.create_connection((ip, port), timeout=15)
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=15)
        def interrupt():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(30, interrupt)
        timer.daemon = True
        timer.start()
        try:
            if parts.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
            connection.sock = sock
            connection.request('GET', (parts.path or '/') + ('?' + parts.query if parts.query else ''), headers={'User-Agent': 'RefArchive/2 (user requested archive)', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                address = urljoin(address, response.getheader('Location', ''))
                continue
            if response.status != 200:
                raise AnalysisError(f'사이트가 다운로드를 허용하지 않았습니다. HTTP {response.status}')
            if int(response.getheader('Content-Length', '0')) > limit:
                raise AnalysisError('가져올 자료가 크기 한도를 초과했습니다.')
            content = response.read(limit + 1)
            if len(content) > limit:
                raise AnalysisError('가져올 자료가 크기 한도를 초과했습니다.')
            return content, response.getheader('Content-Type', '').split(';')[0].lower(), address
        finally:
            timer.cancel()
            connection.close()
            sock.close()
    raise AnalysisError('리디렉션이 너무 많습니다.')


if __name__ == '__main__':
    install_model()
    print('로컬 CLIP 모델 준비 완료')
