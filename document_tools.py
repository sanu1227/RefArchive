"""Bounded, local text previews. Never render active content or extract attachments."""

import io
import posixpath
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw

TEXT_EXTENSIONS = ('.txt', '.md', '.csv', '.tsv', '.log')
OFFICE_EXTENSIONS = ('.docx', '.xlsx', '.pptx')
EXTENSIONS = TEXT_EXTENSIONS + OFFICE_EXTENSIONS
MAX_TEXT = 500000
MAX_XML = 10 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


class DocumentError(ValueError):
    pass


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


def inspect(content, extension):
    if extension not in EXTENSIONS:
        raise DocumentError('DOCX·XLSX·PPTX 또는 TXT·MD·CSV·TSV·LOG 파일을 선택하세요. 구형·매크로 문서는 지원하지 않습니다.')
    if not content or len(content) > 50 * 1024 * 1024:
        raise DocumentError('문서는 파일당 최대 50 MB입니다.')
    reasons, sections = [], []
    if extension in TEXT_EXTENSIONS:
        try:
            encoding = 'utf-16' if content.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
            text = content.decode(encoding)
        except UnicodeError:
            try:
                text = content.decode('cp949')
            except UnicodeError as exc:
                raise DocumentError('텍스트 인코딩을 읽지 못했습니다. UTF-8·UTF-16(BOM)·CP949 파일을 사용하세요.') from exc
        if any(ord(char) < 32 and char not in '\t\n\r' for char in text):
            raise DocumentError('일반 텍스트 파일이 아닙니다.')
        if extension in ('.csv', '.tsv'):
            # Spreadsheet applications can execute formulas on opening a CSV.
            reasons.append('CSV·TSV는 수식 실행 가능성이 있어 연결 앱 열기를 제공하지 않습니다.')
        sections = [text]
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as package:
                infos = package.infolist()
                names = [info.filename for info in infos]
                if (len(names) > 2000 or len(names) != len(set(names)) or
                        sum(info.file_size for info in infos) > MAX_UNPACKED or
                        any(info.flag_bits & 1 or '\\' in info.filename or info.filename.startswith('/') or
                            '..' in info.filename.split('/') for info in infos)):
                    raise DocumentError('문서 내부 크기·경로·암호화 상태를 확인하세요.')
                if any('vba' in name.lower() for name in names):
                    raise DocumentError('매크로 또는 실행 구성요소가 있는 Office 문서는 지원하지 않습니다.')

                def xml(name):
                    if name not in names or package.getinfo(name).file_size > MAX_XML:
                        raise DocumentError('필수 문서 정보가 없거나 XML 크기가 10 MB를 초과했습니다.')
                    data = package.read(name)
                    # XML declarations may use UTF-16; remove zero bytes for this pre-parse check.
                    lowered = data.replace(b'\x00', b'').lower()
                    if b'<!doctype' in lowered or b'<!entity' in lowered:
                        raise DocumentError('DTD·엔티티가 포함된 문서는 지원하지 않습니다.')
                    return ET.fromstring(data)

                types = xml('[Content_Types].xml')
                if any('macro' in node.get('ContentType', '').lower() or 'vba' in node.get('ContentType', '').lower() for node in types):
                    raise DocumentError('매크로 문서는 지원하지 않습니다.')
                if any(any(kind in node.get('ContentType', '').lower() for kind in ('oleobject', 'activex', 'embedded', 'external')) for node in types):
                    reasons.append('첨부 또는 외부 콘텐츠가 포함되어 있습니다.')
                main = {'.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml', '.pptx': 'ppt/presentation.xml'}[extension]
                expected = {'.docx': 'wordprocessingml.document.main+xml', '.xlsx': 'spreadsheetml.sheet.main+xml', '.pptx': 'presentationml.presentation.main+xml'}[extension]
                if not any(node.get('PartName') == '/' + main and node.get('ContentType', '').endswith(expected) for node in types):
                    raise DocumentError('확장자와 Office 문서 형식이 일치하지 않습니다.')
                if any('/embeddings/' in info.filename.lower() or '/activex/' in info.filename.lower() or info.filename.lower().endswith(('connections.xml', 'vbadata.xml')) for info in infos if not info.is_dir()):
                    reasons.append('첨부·외부 데이터·실행 구성요소가 포함되어 있습니다.')
                for name in names:
                    if name.endswith('.rels'):
                        for relationship in xml(name):
                            if relationship.get('TargetMode', '').lower() == 'external':
                                reasons.append('외부 연결이 포함되어 있습니다.')
                            if relationship.get('Type', '').rsplit('/', 1)[-1].lower() in ('oleobject', 'package', 'activex', 'control'):
                                reasons.append('첨부 또는 실행 구성요소가 포함되어 있습니다.')
                    elif name.endswith('.xml'):
                        # Include headers, notes and hidden sheets, not only the visible body.
                        if any(local_name(node.tag) in ('instrText', 'fldSimple', 'altChunk', 'object', 'f', 'externalReference', 'ddeLink', 'oleLink') for node in xml(name).iter()):
                            reasons.append('필드·수식·첨부 콘텐츠가 포함되어 있습니다.')
                    elif name.lower().endswith('.bin'):
                        reasons.append('바이너리 구성요소가 포함되어 있습니다.')
                root = xml(main)

                def related(part):
                    folder, filename = posixpath.split(part)
                    relationships = xml(f'{folder}/_rels/{filename}.rels')
                    result = {}
                    for node in relationships:
                        if node.get('TargetMode') == 'External':
                            continue
                        target = node.get('Target', '')
                        path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(folder, target))
                        if path.startswith('../') or '\\' in path:
                            raise DocumentError('문서 연결 경로가 올바르지 않습니다.')
                        result[node.get('Id')] = path
                    return result

                def paragraphs(tree):
                    return '\n'.join(''.join(node.text or '' for node in paragraph.iter() if local_name(node.tag) == 't')
                                     for paragraph in tree.iter() if local_name(paragraph.tag) == 'p')

                if extension == '.docx':
                    sections = [paragraphs(root)]
                    if any(local_name(node.tag) in ('instrText', 'fldSimple', 'altChunk', 'object') for node in root.iter()):
                        reasons.append('필드·첨부 콘텐츠가 포함되어 있습니다.')
                elif extension == '.pptx':
                    relationships = related(main)
                    for slide in root.iter():
                        if local_name(slide.tag) == 'sldId':
                            sections.append(paragraphs(xml(relationships[slide.get('{' + REL + '}id')])))
                else:
                    relationships = related(main)
                    remaining = MAX_TEXT + 1
                    shared = []
                    if 'xl/sharedStrings.xml' in names:
                        shared = [''.join(node.text or '' for node in entry.iter() if local_name(node.tag) == 't')
                                  for entry in xml('xl/sharedStrings.xml')]
                    for sheet in root.iter():
                        if local_name(sheet.tag) != 'sheet':
                            continue
                        tree = xml(relationships[sheet.get('{' + REL + '}id')])
                        rows = [sheet.get('name', '시트')]
                        for row in tree.iter():
                            if local_name(row.tag) != 'row':
                                continue
                            cells = []
                            for cell in row:
                                value = next((node.text or '' for node in cell if local_name(node.tag) == 'v'), '')
                                if any(local_name(node.tag) == 'f' for node in cell):
                                    reasons.append('수식이 포함되어 있습니다. 미리보기는 저장된 계산값만 표시합니다.')
                                if cell.get('t') == 's' and value:
                                    index = int(value)
                                    if not 0 <= index < len(shared):
                                        raise DocumentError('공유 문자열 연결이 올바르지 않습니다.')
                                    value = shared[index]
                                elif cell.get('t') == 'inlineStr':
                                    value = ''.join(node.text or '' for node in cell.iter() if local_name(node.tag) == 't')
                                label = f"{cell.get('r', '')}: "
                                cells.append((label + value[:remaining])[:remaining])
                                remaining -= len(cells[-1])
                                if remaining <= 0:
                                    break
                            rows.append('\t'.join(cells))
                            if remaining <= 0:
                                break
                        sections.append('\n'.join(rows))
                        if remaining <= 0:
                            break
                    if any(local_name(node.tag) in ('definedName', 'externalReference') for node in root.iter()):
                        reasons.append('정의된 이름 또는 외부 참조가 포함되어 있습니다.')
        except DocumentError:
            raise
        except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, IndexError, RuntimeError, NotImplementedError) as exc:
            raise DocumentError('암호화되지 않은 정상 Office 문서를 선택하세요.') from exc
    # One bounded preview fits existing searchable analysis metadata and backup validation.
    text = '\n\n'.join(sections)
    truncated = len(text) > MAX_TEXT
    text = text[:MAX_TEXT].replace('\r\n', '\n').replace('\r', '\n')
    return {'pages': [{'number': 1, 'text': text, 'method': 'embedded'}], 'summary': [], 'keywords': [],
            'method': '문서 텍스트', 'summarized': False, 'truncated': truncated,
            'openReason': ' '.join(dict.fromkeys(reasons))}


def thumbnail(extension):
    image = Image.new('RGB', (480, 300), '#e9edf4')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((178, 50, 302, 250), radius=10, fill='white')
    draw.text((206, 135), extension[1:].upper(), fill='#34547a', font_size=24)
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=85)
    return output.getvalue()
