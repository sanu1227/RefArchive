"""Bounded container checks. Decoding and codec support belong to the browser."""
import io

from PIL import Image, ImageDraw


def mp4_boxes(data):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('잘린 MP4 헤더입니다.')
        size, kind = int.from_bytes(data[offset:offset + 4]), bytes(data[offset + 4:offset + 8])
        header = 8
        if size == 1:
            header = 16
            if len(data) - offset < header:
                raise ValueError('잘린 MP4 헤더입니다.')
            size = int.from_bytes(data[offset + 8:offset + 16])
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise ValueError('MP4 파일이 손상되었거나 전송이 잘렸습니다.')
        yield kind, data[offset + header:offset + size]
        offset += size


def ebml_elements(data):
    def number(offset, keep_marker=False):
        if offset >= len(data) or not data[offset]:
            raise ValueError('WebM 헤더가 올바르지 않습니다.')
        size = 9 - int(data[offset]).bit_length()
        if size > (4 if keep_marker else 8) or offset + size > len(data):
            raise ValueError('잘린 WebM 헤더입니다.')
        value = int.from_bytes(data[offset:offset + size])
        return (value if keep_marker else value & ((1 << (7 * size)) - 1)), offset + size, size
    offset = 0
    while offset < len(data):
        kind, offset, _ = number(offset, True)
        size, offset, width = number(offset)
        if size == (1 << (7 * width)) - 1:
            # Unknown size is legal for streamed Segment/Cluster containers.
            if kind not in (0x18538067, 0x1F43B675):
                raise ValueError('WebM 항목 크기가 올바르지 않습니다.')
            size = len(data) - offset
        if offset + size > len(data):
            raise ValueError('WebM 파일이 손상되었거나 전송이 잘렸습니다.')
        yield kind, data[offset:offset + size]
        offset += size


def validate(content, extension):
    data = memoryview(content)
    if extension == '.mp4':
        boxes = dict(mp4_boxes(data))
        if len(boxes.get(b'ftyp', b'')) < 8 or not boxes.get(b'mdat') or not boxes.get(b'moov'):
            raise ValueError('영상 데이터가 있는 MP4 파일을 선택하세요.')
        for kind, track in mp4_boxes(boxes[b'moov']):
            if kind == b'trak':
                for part, media in mp4_boxes(track):
                    if part == b'mdia' and any(key == b'hdlr' and bytes(value[8:12]) == b'vide' for key, value in mp4_boxes(media)):
                        return
    elif extension == '.webm':
        boxes = dict(ebml_elements(data))
        header = dict(ebml_elements(boxes.get(0x1A45DFA3, b'')))
        if bytes(header.get(0x4282, b'')) != b'webm':
            raise ValueError('WebM 형식의 파일을 선택하세요.')
        segment = dict(ebml_elements(boxes.get(0x18538067, b'')))
        if segment.get(0x1F43B675):
            for kind, track in ebml_elements(segment.get(0x1654AE6B, b'')):
                if kind == 0xAE and any(key == 0x83 and int.from_bytes(value) == 1 for key, value in ebml_elements(track)):
                    return
    raise ValueError('영상 트랙이 있는 MP4 또는 WebM 파일을 선택하세요.')


def thumbnail():
    image = Image.new('RGB', (480, 300), '#e2eae8')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((174, 90, 306, 210), radius=28, fill='#ffffff')
    draw.polygon(((228, 123), (228, 177), (269, 150)), fill='#287466')
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=85)
    return output.getvalue()
