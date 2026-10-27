"""RefArchive: loopback-only personal archive with SQLite and original files."""

import argparse
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unicodedata
from urllib.parse import urlsplit, unquote, parse_qs
from urllib.request import urlopen
from urllib.error import URLError
import uuid
import warnings
import webbrowser
import zipfile

from PIL import Image, ImageOps, UnidentifiedImageError
import analysis_tools
import video_tools
import document_tools
import category_tools
from runtime_paths import ROOT, VERSION, POWERSHELL, default_data

IMAGE_LIMIT = 50 * 1024 * 1024
VIDEO_LIMIT = 200 * 1024 * 1024
BACKUP_LIMIT = 50 * 1024 * 1024 * 1024
FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
Image.MAX_IMAGE_PIXELS = 40_000_000
SCHEMA_V1 = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('image','link')),
 title TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', path TEXT UNIQUE,
 hash TEXT UNIQUE, source TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 favorite INTEGER NOT NULL DEFAULT 0, trashed INTEGER NOT NULL DEFAULT 0,
 width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
 size INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS item_tags (
 item_id TEXT REFERENCES items(id) ON DELETE CASCADE,
 name TEXT REFERENCES tags(name) ON UPDATE CASCADE ON DELETE CASCADE,
 PRIMARY KEY(item_id,name)
);
CREATE TABLE IF NOT EXISTS item_categories (
 item_id TEXT REFERENCES items(id) ON DELETE CASCADE,
 name TEXT REFERENCES categories(name) ON UPDATE CASCADE ON DELETE CASCADE,
 PRIMARY KEY(item_id,name)
);
PRAGMA user_version=1;
"""
EXTRA_KINDS = ('search', 'collection', 'mode', 'auto', 'prediction', 'analysis', 'preferences', 'bundle')
SCHEMA_V2 = SCHEMA_V1.replace("('image','link')", "('image','link','pdf')").replace('PRAGMA user_version=1;', """
CREATE TABLE IF NOT EXISTS extras (
 kind TEXT NOT NULL CHECK(kind IN ('search','collection','mode','auto','prediction','analysis','preferences')),
 key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(kind,key)
);
PRAGMA user_version=2;
""")
SCHEMA_V3 = SCHEMA_V2.replace("('image','link','pdf')", "('image','link','pdf','video')").replace('user_version=2', 'user_version=3')
SCHEMA_V4 = SCHEMA_V3.replace("'analysis','preferences'", "'analysis','preferences','bundle'").replace('user_version=3', 'user_version=4')
SCHEMA = (SCHEMA_V4.replace("'pdf','video'", "'pdf','video','document'")
          .replace('created TEXT NOT NULL', "created TEXT NOT NULL, material_date TEXT NOT NULL DEFAULT ''")
          .replace('user_version=4', 'user_version=5'))


def upgrade_database(root, safety=None):
    path = root / 'archive.sqlite3'
    with closing(sqlite3.connect(path)) as old:
        version = old.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5):
            raise ArchiveError('이 앱에서 지원하지 않는 라이브러리 버전입니다.')
        if version not in (1, 2, 3, 4):
            return
        with closing(sqlite3.connect(':memory:')) as reference:
            reference.executescript({1: SCHEMA_V1, 2: SCHEMA_V2, 3: SCHEMA_V3, 4: SCHEMA_V4}[version])
            schema = lambda db: list(db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))
            if schema(old) != schema(reference) or old.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ArchiveError('기존 자료함 구조를 확인하지 못했습니다.')
        temporary = root / 'upgrade.sqlite3'
        try:
            with closing(sqlite3.connect(temporary)) as new:
                new.executescript(SCHEMA)
                for table in ('items', 'tags', 'categories', 'item_tags', 'item_categories') + (('extras',) if version >= 2 else ()):
                    rows = list(old.execute(f'SELECT * FROM {table}'))
                    if rows:
                        columns = ','.join(row[1] for row in old.execute(f'PRAGMA table_info({table})'))
                        new.executemany(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in rows[0])})", rows)
                new.commit()
                if new.execute('PRAGMA foreign_key_check').fetchone():
                    raise ArchiveError('기존 자료 연결을 확인하지 못했습니다.')
            if safety:
                safety = safety.with_name(safety.name.replace('migration-v1-', f'migration-v{version}-'))
                safety.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(safety)) as snapshot:
                    old.backup(snapshot)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


class ArchiveError(Exception):
    pass


def clean_date(value):
    if value == '':
        return ''
    if isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise ArchiveError('자료 날짜는 올바른 YYYY-MM-DD 형식으로 입력하거나 비워 두세요.')


def clean_text(value, limit=200, required=False):
    if not isinstance(value, str):
        raise ArchiveError("입력 형식이 올바르지 않습니다.")
    value = value.strip()
    if len(value) > limit or (required and not value) or "\x00" in value:
        raise ArchiveError(f"입력은 {'1~' if required else '최대 '}{limit}자여야 합니다.")
    return value


def clean_names(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ArchiveError("키워드·카테고리는 최대 100개를 지정할 수 있습니다.")
    return list(dict.fromkeys(clean_text(name, 60, True) for name in value))


def clean_text_rule(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'fields', 'match', 'terms'}:
        raise ArchiveError('텍스트 분류 규칙을 확인하세요.')
    mode, fields, match, terms = (value.get(key) for key in ('mode', 'fields', 'match', 'terms'))
    if mode not in ('off', 'suggest', 'auto') or match not in ('any', 'all'):
        raise ArchiveError('텍스트 분류 모드와 포함 조건을 확인하세요.')
    if not isinstance(fields, list) or not 1 <= len(fields) <= 4 or any(field not in ('title', 'filename', 'note', 'content') for field in fields):
        raise ArchiveError('제목·파일명·메모·본문 중 검색할 항목을 선택하세요.')
    if not isinstance(terms, list) or len(terms) > 30 or mode != 'off' and not terms:
        raise ArchiveError('찾을 문구를 1~30개 입력하세요. 문구 하나는 최대 100자입니다.')
    return {'mode': mode, 'fields': list(dict.fromkeys(fields)), 'match': match,
            'terms': list(dict.fromkeys(clean_text(term, 100, True) for term in terms))}


def text_rule_matches(rule, item, bundles):
    normalize = lambda text: ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
    sources = [(field, 0, item[field]) for field in ('title', 'filename', 'note') if field in rule['fields']]
    for bundle in bundles:
        if item['id'] in bundle['ids']:
            sources += [(field, 0, bundle[field]) for field in ('title', 'note') if field in rule['fields']]
    if 'content' in rule['fields']:
        sources += [('content', page['number'], page['text']) for page in (item.get('analysis') or {}).get('pages', [])]
    sources = [(field, page, normalize(text)) for field, page, text in sources]
    matches = []
    for term in rule['terms']:
        found = next(((field, page) for field, page, text in sources if normalize(term) in text), None)
        if found:
            matches.append({'term': term, 'field': found[0], 'page': found[1]})
    return matches if matches and (rule['match'] == 'any' or len(matches) == len(rule['terms'])) else []


def clean_url(value, required=False):
    value = clean_text(value, 4000, required)
    if value:
        try:
            parts = urlsplit(value)
            host = parts.hostname or ""
            host.encode("idna")
            parts.port  # Validate malformed and out-of-range ports before the browser displays the URL.
        except (ValueError, UnicodeError) as exc:
            raise ArchiveError("주소 형식이 올바르지 않습니다.") from exc
        if (parts.scheme not in ("http", "https") or not host or parts.username or parts.password
                or any(character.isspace() or ord(character) < 32 for character in value)
                or any(character in host for character in "%\\<>{}")):
            raise ArchiveError("출처는 사용자 정보가 없는 http 또는 https 주소를 입력하세요.")
    return value


def image_info(content):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in FORMATS or getattr(image, "is_animated", False):
                    raise ArchiveError("현재는 정지 JPG, PNG, WebP만 지원합니다.")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                extension = FORMATS[image.format]
                width, height = image.size
                thumbnail = ImageOps.exif_transpose(image)
                thumbnail.thumbnail((480, 480))
                rgba = thumbnail.convert("RGBA")
                background = Image.new("RGB", rgba.size, "#f1f0eb")
                background.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                background.save(output, "JPEG", quality=85)
                return extension, width, height, output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ArchiveError("이미지를 읽을 수 없습니다. 손상 여부와 이미지 크기를 확인하세요.") from exc


class Library:
    def __init__(self, data):
        self.data = Path(data).resolve()
        self.root = self.data / "library"
        self.lock = threading.RLock()
        self.job = {'running': False, 'done': 0, 'total': 0, 'errors': [], 'message': ''}
        self.pending_classification = []
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "files").mkdir(exist_ok=True)
        (self.root / "thumbs").mkdir(exist_ok=True)
        upgrade_database(self.root, self.data / 'backups' / f'migration-v1-{uuid.uuid4().hex[:8]}.sqlite3')
        with self.db() as connection:
            connection.executescript(SCHEMA)
        from library_tools import Maintenance
        self.maintenance = Maintenance(self, ArchiveError)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.root / "archive.sqlite3")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def state(self):
        # ponytail: load all metadata for a personal library; add server pagination if large libraries slow down.
        with self.lock, self.db() as connection:
            items = [dict(row) for row in connection.execute("SELECT * FROM items ORDER BY created DESC, id")]
            by_id = {item["id"]: item for item in items}
            for item in items:
                item["tags"], item["categories"] = [], []
            for table, field in (("item_tags", "tags"), ("item_categories", "categories")):
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY name"):
                    by_id[row["item_id"]][field].append(row["name"])
            extra = {kind: {} for kind in EXTRA_KINDS}
            for row in connection.execute('SELECT * FROM extras'):
                extra[row['kind']][row['key']] = json.loads(row['value'])
            classified = {value['item'] for value in extra['prediction'].values()}
            category_predictions = [prediction for key, value in extra['preferences'].items() if key.startswith('category:') for prediction in value['decisions']]
            for item in items:
                item['autoTags'] = extra['auto'].get(item['id'], [])
                item['analysis'] = extra['analysis'].get(item['id'])
                item['classifiedAt'] = extra['preferences'].get('classified:' + item['id'], '')
                category_record = extra['preferences'].get('category:' + item['id'], {})
                item['autoCategories'] = [row['name'] for row in category_record.get('decisions', []) if row['status'] == 'applied']
                item['hasClassification'] = bool(item['classifiedAt']) or item['id'] in classified or bool(category_record.get('classifiedAt'))
            tags = []
            owners = {item_id: bundle['id'] for bundle in extra['bundle'].values() for item_id in bundle['ids']}
            for row in connection.execute("SELECT name FROM tags ORDER BY name"):
                name = row["name"]
                matching = [item for item in items if not item["trashed"] and name in item["tags"]]
                examples = sum(item["type"] == "image" and name not in item['autoTags'] for item in matching)
                tags.append({"name": name, "count": len({owners.get(item['id'], item['id']) for item in matching}), "examples": examples,
                             "eligible": examples >= 10, **extra['mode'].get(name, {'mode': 'off', 'threshold': .9})})
            categories = [row[0] for row in connection.execute("SELECT name FROM categories ORDER BY name")]
            rules = extra['preferences'].get('category-rules', {})
            category_rules = [{'name': name, **rules.get(name, category_tools.DEFAULT),
                'examples': len({owners.get(item['id'], item['id']) for item in items if not item['trashed'] and name in item['categories'] and name not in item['autoCategories']})} for name in categories]
            return {"items": items, "tags": tags, 'entryCount': len(items) - len(owners) + len(extra['bundle']),
                    "categories": categories, 'categoryRules': category_rules, 'categoryPredictions': category_predictions,
                    "storage": str(self.root), "automaticClassification": True,
                    'searches': list(extra['search'].values()), 'collections': list(extra['collection'].values()), 'bundles': list(extra['bundle'].values()),
                    'predictions': list(extra['prediction'].values()), 'preferences': {'downloads': False, 'summaries': False, 'ocr': False, 'language': 'ko-KR', **extra['preferences'].get('settings', {})},
                    'modelReady': analysis_tools.MODEL.is_file(), 'job': dict(self.job)}

    @staticmethod
    def put_extra(connection, kind, key, value):
        connection.execute('INSERT INTO extras VALUES (?,?,?) ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value',
                           (kind, key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def get_extra(connection, kind, key, default=None):
        row = connection.execute('SELECT value FROM extras WHERE kind=? AND key=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def bundles(connection):
        return {row['key']: json.loads(row['value']) for row in connection.execute("SELECT key,value FROM extras WHERE kind='bundle'")}

    @staticmethod
    def expand_ids(ids, bundles, whole=False):
        owners = {item_id: bundle['id'] for bundle in bundles.values() for item_id in bundle['ids']} if whole else {}
        return list(dict.fromkeys(member for key in ids for member in bundles.get(owners.get(key, key), {'ids': [key]})['ids']))

    def bundle(self, metadata):
        action = metadata.get('action', 'create')
        with self.lock, self.db() as connection:
            bundles = self.bundles(connection)
            if action in ('edit', 'detach'):
                key = clean_text(metadata.get('id'), 32, True)
                if key not in bundles:
                    raise ArchiveError('묶음 자료를 찾을 수 없습니다.')
                bundle = bundles[key]
                if action == 'detach':
                    member = metadata.get('file')
                    self.detach_file(connection, bundle, member)
                    return {'id': member}
                ids = metadata.get('ids', bundle['ids'])
                if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids) or len(ids) != len(bundle['ids']) or set(ids) != set(bundle['ids']):
                    raise ArchiveError('기존 파일을 빠짐없이 한 번씩 지정하세요.')
                cover = metadata.get('cover', bundle.get('cover', bundle['ids'][0]))
                if cover not in ids:
                    raise ArchiveError('대표 파일은 묶음 안에서 선택하세요.')
                bundle.update(ids=ids, cover=cover)
                self.put_extra(connection, 'bundle', key, bundle)
                return {'id': key}
            if action == 'split':
                key = clean_text(metadata.get('id'), 32, True)
                if key not in bundles:
                    raise ArchiveError('묶음 자료를 찾을 수 없습니다.')
                bundle = bundles[key]
                shared = '\n'.join(text for text in ('묶음: ' + bundle['title'], bundle['source'], bundle['note'], '묶음 자료 날짜: ' + bundle['material_date'] if bundle.get('material_date') else '') if text)
                for member in bundle['ids']:
                    original = connection.execute('SELECT note FROM items WHERE id=?', (member,)).fetchone()[0]
                    note = clean_text('\n\n'.join(text for text in (original, shared) if text), 20000)
                    connection.execute('UPDATE items SET note=? WHERE id=?', (note, member))
                # Keep shared metadata in each file's note when removing the wrapper.
                connection.execute("DELETE FROM extras WHERE kind='bundle' AND key=?", (key,))
                return {'ids': bundles[key]['ids']}
            if action != 'create':
                raise ArchiveError('지원하지 않는 묶음 작업입니다.')
            ids = metadata.get('ids')
            if not isinstance(ids, list) or not 2 <= len(ids) <= 5000 or any(not isinstance(key, str) for key in ids):
                raise ArchiveError('묶을 자료를 2~5,000개 선택하세요.')
            ids = list(dict.fromkeys(ids))
            if len(ids) < 2:
                raise ArchiveError('서로 다른 자료를 2개 이상 선택하세요.')
            key = uuid.uuid4().hex
            title = clean_text(metadata.get('title'), 200, True)
            for item_id in ids:
                self.attach_bundle(connection, item_id, {'uploadBundle': key, 'bundleTitle': title})
            return {'id': key}

    def detach_file(self, connection, bundle, member):
        if not isinstance(member, str) or member not in bundle['ids']:
            raise ArchiveError('분리할 파일을 묶음 안에서 선택하세요.')
        shared = '\n'.join(text for text in ('묶음: ' + bundle['title'], bundle['source'], bundle['note'], '묶음 자료 날짜: ' + bundle['material_date'] if bundle.get('material_date') else '') if text)
        original = connection.execute('SELECT note FROM items WHERE id=?', (member,)).fetchone()[0]
        note = clean_text('\n\n'.join(text for text in (original, shared) if text), 20000)
        connection.execute('UPDATE items SET note=? WHERE id=?', (note, member))
        bundle['ids'].remove(member)
        if bundle['ids']:
            if bundle.get('cover') == member:
                bundle['cover'] = bundle['ids'][0]
            self.put_extra(connection, 'bundle', bundle['id'], bundle)
        else:
            connection.execute("DELETE FROM extras WHERE kind='bundle' AND key=?", (bundle['id'],))

    def attach_bundle(self, connection, item_id, metadata, inherit=False):
        key = clean_text(metadata.get('uploadBundle', ''), 32)
        if not key:
            return
        if not re.fullmatch('[a-f0-9]{32}', key) or connection.execute('SELECT 1 FROM items WHERE id=?', (key,)).fetchone():
            raise ArchiveError('묶음 자료 식별자가 올바르지 않습니다.')
        bundles = self.bundles(connection)
        if any(item_id in bundle['ids'] and bundle['id'] != key for bundle in bundles.values()):
            raise ArchiveError('이미 다른 묶음에 있는 파일입니다. 기존 묶음을 풀고 다시 시도하세요.')
        item = connection.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not item or item['trashed']:
            raise ArchiveError('묶을 자료가 없거나 휴지통에 있습니다.')
        bundle = bundles.get(key)
        if bundle is None:
            bundle = {'id': key, 'title': clean_text(metadata.get('bundleTitle'), 200, True), 'ids': [],
                      'note': clean_text(metadata.get('note', ''), 20000), 'source': clean_url(metadata.get('source', '')),
                      'material_date': clean_date(metadata.get('material_date', ''))}
        if bundle['ids']:
            first = connection.execute('SELECT trashed FROM items WHERE id=?', (bundle['ids'][0],)).fetchone()
            if first['trashed']:
                raise ArchiveError('휴지통의 묶음에는 추가할 수 없습니다. 먼저 복원하세요.')
        if item_id not in bundle['ids']:
            if len(bundle['ids']) >= 5000:
                raise ArchiveError('묶음 하나에는 최대 5,000개 파일을 보관할 수 있습니다.')
            if inherit:
                tags, categories = set(), set()
                for member in bundle['ids']:
                    auto = self.get_extra(connection, 'auto', member, [])
                    tags.update(row[0] for row in connection.execute('SELECT name FROM item_tags WHERE item_id=?', (member,)) if row[0] not in auto)
                    categories.update(row[0] for row in connection.execute('SELECT name FROM item_categories WHERE item_id=?', (member,)))
                self.labels(connection, item_id, clean_names(sorted(tags)), clean_names(sorted(categories)))
            bundle['ids'].append(item_id)
        favorite = any(connection.execute('SELECT favorite FROM items WHERE id=?', (member,)).fetchone()[0] for member in bundle['ids'])
        connection.executemany('UPDATE items SET favorite=? WHERE id=?', [(favorite, member) for member in bundle['ids']])
        self.put_extra(connection, 'bundle', key, bundle)

    def manual_labels(self, connection, item_id, tags, replace=False, removed=False):
        auto = self.get_extra(connection, 'auto', item_id, [])
        if auto:
            self.put_extra(connection, 'auto', item_id, [tag for tag in auto if tag in tags] if replace else [tag for tag in auto if tag not in tags])
        for row in list(connection.execute("SELECT key,value FROM extras WHERE kind='prediction'")):
            prediction = json.loads(row['value'])
            if prediction['item'] == item_id and (replace or prediction['tag'] in tags):
                if replace and prediction['tag'] in auto and prediction['tag'] in tags:
                    continue
                prediction['status'] = 'accepted' if prediction['tag'] in tags and not removed else 'rejected'
                self.put_extra(connection, 'prediction', row['key'], prediction)

    def saved(self, metadata):
        kind, action = metadata.get('kind'), metadata.get('action', 'save')
        if kind not in ('search', 'collection') or action not in ('save', 'delete'):
            raise ArchiveError('지원하지 않는 저장 작업입니다.')
        key = metadata.get('id') or uuid.uuid4().hex
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key):
            raise ArchiveError('저장 항목을 찾을 수 없습니다.')
        with self.lock, self.db() as connection:
            if action == 'delete':
                connection.execute('DELETE FROM extras WHERE kind=? AND key=?', (kind, key))
                return {}
            name = clean_text(metadata.get('name'), 60, True)
            for row in connection.execute('SELECT key,value FROM extras WHERE kind=?', (kind,)):
                if row['key'] != key and json.loads(row['value'])['name'] == name:
                    raise ArchiveError('같은 이름이 있습니다. 다른 이름을 입력하세요.')
            value = {'id': key, 'name': name}
            if kind == 'search':
                value['filters'] = self.clean_filters(metadata.get('filters'))
            else:
                ids = metadata.get('ids', [])
                if not isinstance(ids, list) or len(ids) > 5000:
                    raise ArchiveError('자료 모음은 최대 5,000개 자료까지 지원합니다.')
                if any(not isinstance(key, str) for key in ids):
                    raise ArchiveError('모음에 넣을 자료를 확인하세요.')
                ids = self.expand_ids(ids, self.bundles(connection))
                if len(ids) > 5000:
                    raise ArchiveError('자료 모음은 최대 5,000개 파일까지 지원합니다.')
                known = {row[0] for row in connection.execute('SELECT id FROM items')}
                if any(not isinstance(item_id, str) or item_id not in known for item_id in ids):
                    raise ArchiveError('모음에 넣을 자료를 찾을 수 없습니다.')
                value['ids'] = list(dict.fromkeys(ids))
            self.put_extra(connection, kind, key, value)
        return {'id': key}

    @staticmethod
    def clean_filters(filters):
        if not isinstance(filters, dict):
            raise ArchiveError('검색 조건 형식이 올바르지 않습니다.')
        result = {}
        for key, options, default in (('view', ('all', 'unfiled', 'uncategorized', 'favorites', 'trash'), 'all'),
                                      ('type', ('all', 'image', 'link', 'pdf', 'video', 'document', 'bundle'), 'all'),
                                      ('match', ('all', 'any'), 'all'), ('sort', ('newest', 'oldest', 'title', 'date-desc', 'date-asc'), 'newest'),
                                      ('group', ('none', 'year', 'month', 'registered-day', 'registered-month', 'registered-year'), 'none')):
            value = filters.get(key, default)
            if value not in options:
                raise ArchiveError('지원하지 않는 검색 조건입니다.')
            result[key] = value
        result['dateFrom'] = clean_date(filters.get('dateFrom', ''))
        result['dateTo'] = clean_date(filters.get('dateTo', ''))
        if result['dateFrom'] and result['dateTo'] and result['dateFrom'] > result['dateTo']:
            raise ArchiveError('자료 날짜 시작일은 종료일보다 늦을 수 없습니다.')
        result['query'] = clean_text(filters.get('query', ''), 1000)
        result['tags'] = clean_names(filters.get('tags', []))
        result['exclude'] = clean_names(filters.get('exclude', []))
        result['category'] = clean_text(filters.get('category', ''), 60)
        result['collection'] = clean_text(filters.get('collection', ''), 32)
        if result['collection'] and not re.fullmatch('[a-f0-9]{32}', result['collection']):
            raise ArchiveError('자료 모음 검색 조건을 확인하세요.')
        return result

    def mode(self, metadata):
        name = clean_text(metadata.get('name'), 60, True)
        mode, threshold = metadata.get('mode'), metadata.get('threshold', .9)
        if mode not in ('off', 'suggest', 'auto') or isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not .5 <= threshold <= 1:
            raise ArchiveError('분류 모드와 0.5~1 사이 유사도 기준을 확인하세요.')
        with self.lock:
            if self.job['running']:
                raise ArchiveError('진행 중인 분석이 끝난 뒤 분류 설정을 변경하세요.')
            tag = next((tag for tag in self.state()['tags'] if tag['name'] == name), None)
            if not tag or mode != 'off' and not tag['eligible']:
                raise ArchiveError('서로 다른 확정 이미지 10개 이상인 키워드만 활성화할 수 있습니다.')
            if mode != 'off' and not analysis_tools.MODEL.is_file():
                raise ArchiveError('보관 · 백업에서 로컬 분석 모델을 먼저 준비하세요.')
            with self.db() as connection:
                previous = self.get_extra(connection, 'mode', name, {})
                self.put_extra(connection, 'mode', name, {**previous, 'mode': mode, 'threshold': float(threshold)})
        return {}

    def text_rule(self, metadata):
        name = clean_text(metadata.get('name'), 60, True)
        rule = clean_text_rule(metadata.get('rule'))
        with self.lock, self.db() as connection:
            if self.job['running']:
                raise ArchiveError('진행 중인 분석이 끝난 뒤 분류 설정을 변경하세요.')
            if not connection.execute('SELECT 1 FROM tags WHERE name=?', (name,)).fetchone():
                raise ArchiveError('규칙을 적용할 키워드를 먼저 추가하세요.')
            settings = self.get_extra(connection, 'mode', name, {'mode': 'off', 'threshold': .9})
            self.put_extra(connection, 'mode', name, {**settings, 'textRule': rule})
        return {}

    def preferences(self, metadata):
        if type(metadata.get('downloads')) is not bool:
            raise ArchiveError('링크 가져오기 설정을 확인하세요.')
        summaries, ocr = metadata.get('summaries', False), metadata.get('ocr', False)
        language = metadata.get('language', 'ko-KR')
        if type(summaries) is not bool or type(ocr) is not bool or language not in ('ko-KR', 'en-US'):
            raise ArchiveError('자동 요약·OCR 설정을 확인하세요.')
        with self.lock, self.db() as connection:
            self.put_extra(connection, 'preferences', 'settings', {'downloads': metadata['downloads'], 'summaries': summaries, 'ocr': ocr, 'language': language})
        return {}

    def inspect_link(self, metadata):
        if not self.state()['preferences']['downloads']:
            raise ArchiveError('보관 · 백업에서 링크 이미지 가져오기를 먼저 켜세요.')
        address = clean_url(metadata.get('source', ''), True)
        content, content_type, final = analysis_tools.public_get(address, 5 * 1024 * 1024)
        if content_type.startswith('image/'):
            return {'images': [final], 'source': final}
        if content_type != 'text/html':
            raise ArchiveError('공개 HTML 페이지와 직접 이미지 주소만 지원합니다.')
        parser = analysis_tools.ImageLinks(final)
        parser.feed(content.decode('utf-8', errors='replace'))
        return {'images': parser.images, 'source': final}

    def import_link(self, metadata):
        if not self.state()['preferences']['downloads']:
            raise ArchiveError('링크 이미지 가져오기가 꺼져 있습니다.')
        address = clean_url(metadata.get('image', ''), True)
        content, content_type, final = analysis_tools.public_get(address, IMAGE_LIMIT)
        if not content_type.startswith('image/'):
            raise ArchiveError('선택한 주소는 다운로드 가능한 이미지가 아닙니다.')
        metadata = {**metadata, 'filename': (Path(urlsplit(final).path).name or '링크 이미지')[:250],
                    'source': clean_url(metadata.get('source', final), True)}
        return self.add_image(content, metadata)

    def similar(self, metadata):
        item_id = metadata.get('id')
        items = [item for item in self.state()['items'] if item['type'] == 'image' and not item['trashed']]
        if item_id not in {item['id'] for item in items}:
            raise ArchiveError('비교할 이미지를 찾을 수 없습니다.')
        vector = analysis_tools.ENGINE.vector(self.media(item_id)[0])
        matches = [{'id': item['id'], 'score': round(float(vector @ analysis_tools.ENGINE.vector(self.media(item['id'])[0])), 4)}
                   for item in items if item['id'] != item_id]
        return {'items': sorted(matches, key=lambda match: match['score'], reverse=True)[:30]}

    def classify(self, item_id):
        self.classify_categories(item_id)
        self.classify_keywords(item_id)

    def category_rule(self, metadata):
        name = clean_text(metadata.get('name'), 60, True)
        try:
            rule = category_tools.clean_config(metadata.get('rule'))
        except ValueError as exc:
            raise ArchiveError(str(exc)) from exc
        with self.lock, self.db() as connection:
            if self.job['running']:
                raise ArchiveError('진행 중인 분석이 끝난 뒤 설정을 변경하세요.')
            if not connection.execute('SELECT 1 FROM categories WHERE name=?', (name,)).fetchone():
                raise ArchiveError('카테고리를 먼저 만드세요.')
            rules = self.get_extra(connection, 'preferences', 'category-rules', {})
            rules[name] = rule
            self.put_extra(connection, 'preferences', 'category-rules', rules)
        return {}

    def classify_categories(self, item_id):
        snapshot = self.state()
        item = next((value for value in snapshot['items'] if value['id'] == item_id), None)
        if not item or item['trashed'] or not snapshot['categories']:
            return
        if item['type'] == 'pdf' and not item['analysis'] and any(rule['mode'] != 'off' for rule in snapshot['categoryRules']):
            self.analyze_text(item_id, summary_enabled=False)
        # Keep content, rules and confirmed examples consistent through scoring and commit.
        with self.lock:
            snapshot = self.state()
            item = next(value for value in snapshot['items'] if value['id'] == item_id)
            if item['trashed']:
                return
            predictions = category_tools.predict(snapshot, item)
            manual = set(item['categories']) - set(item['autoCategories'])
            with self.db() as connection:
                key = 'category:' + item_id
                previous = self.get_extra(connection, 'preferences', key, {'decisions': []})
                confirmed = [row for row in previous['decisions'] if row['status'] in ('accepted', 'rejected')]
                protected = manual | {row['name'] for row in confirmed}
                predictions = [row for row in predictions if row['name'] not in protected]
                for name in item['autoCategories']:
                    connection.execute('DELETE FROM item_categories WHERE item_id=? AND name=?', (item_id, name))
                for row in predictions:
                    if row['status'] == 'applied':
                        connection.execute('INSERT OR IGNORE INTO item_categories VALUES (?,?)', (item_id, row['name']))
                self.put_extra(connection, 'preferences', key, {'decisions': [*confirmed, *predictions], 'classifiedAt': datetime.now(timezone.utc).isoformat()})

    def manual_categories(self, connection, item_id, names, replace=False, removed=False):
        key = 'category:' + item_id
        record = self.get_extra(connection, 'preferences', key)
        if not record and not removed:
            return
        record = record or {'decisions': [], 'classifiedAt': datetime.now(timezone.utc).isoformat()}
        if removed:
            known = {row[0] for row in connection.execute('SELECT name FROM categories')}
            recorded = {row['name'] for row in record['decisions']}
            for name in set(names) & known - recorded:
                record['decisions'].append({'item': item_id, 'name': name, 'score': 0, 'evidence': [],
                    'reasons': [{'term': '사용자가 분류 해제', 'field': 'manual', 'page': 0}],
                    'status': 'rejected', 'reason': '직접 제거한 분류 유지'})
        for row in record['decisions']:
            if replace and row['name'] in names and row['status'] == 'applied':
                continue
            if replace or row['name'] in names:
                row['status'] = 'accepted' if row['name'] in names and not removed else 'rejected'
        self.put_extra(connection, 'preferences', key, record)

    def category_review(self, metadata):
        item_id, name, action = metadata.get('id'), metadata.get('name'), metadata.get('action')
        if action not in ('accept', 'reject'):
            raise ArchiveError('카테고리 확인 작업을 선택하세요.')
        with self.lock, self.db() as connection:
            key = 'category:' + clean_text(item_id, 32, True)
            record = self.get_extra(connection, 'preferences', key, {'decisions': []})
            row = next((row for row in record['decisions'] if row['name'] == name), None)
            item = connection.execute('SELECT trashed FROM items WHERE id=?', (item_id,)).fetchone()
            if not row or row['status'] not in ('pending', 'applied') or not item or item['trashed']:
                raise ArchiveError('확인할 카테고리 제안을 찾을 수 없습니다.')
            if action == 'accept':
                connection.execute('INSERT OR IGNORE INTO item_categories VALUES (?,?)', (item_id, name))
            elif row['status'] == 'applied':
                connection.execute('DELETE FROM item_categories WHERE item_id=? AND name=?', (item_id, name))
            row['status'] = 'accepted' if action == 'accept' else 'rejected'
            self.put_extra(connection, 'preferences', key, record)
        return {}

    def classify_keywords(self, item_id):
        snapshot = self.state()
        target = next((item for item in snapshot['items'] if item['id'] == item_id), None)
        if not target or target['trashed']:
            return
        text_enabled = any(tag.get('textRule', {}).get('mode', 'off') != 'off' for tag in snapshot['tags'])
        if target['type'] == 'pdf' and not target['analysis'] and any(
                tag.get('textRule', {}).get('mode', 'off') != 'off' and 'content' in tag['textRule']['fields'] for tag in snapshot['tags']):
            self.analyze_text(item_id, summary_enabled=False)
            snapshot = self.state()
            target = next(item for item in snapshot['items'] if item['id'] == item_id)
        previous = {entry['tag']: entry for entry in snapshot['predictions'] if entry['item'] == item_id}
        decisions, reference_ids, vector = {}, {}, None
        for tag in snapshot['tags']:
            if tag['name'] in set(target['tags']) - set(target['autoTags']) or previous.get(tag['name'], {}).get('status') in ('accepted', 'rejected'):
                continue
            rule = tag.get('textRule', {})
            if rule.get('mode', 'off') != 'off':
                matches = text_rule_matches(rule, target, snapshot['bundles'])
                if matches:
                    decisions[tag['name']] = {'item': item_id, 'tag': tag['name'], 'score': 1, 'evidence': [],
                        'source': 'text', 'matches': matches, 'status': 'applied' if rule['mode'] == 'auto' else 'pending'}
                    continue
            if tag['mode'] == 'off' or target['type'] != 'image':
                if previous.get(tag['name'], {}).get('status') in ('pending', 'applied') or tag['name'] in target['autoTags']:
                    decisions[tag['name']] = None
                continue
            if not tag['eligible']:
                if previous.get(tag['name'], {}).get('source') == 'text':
                    decisions[tag['name']] = None
                continue
            examples = [item for item in snapshot['items'] if item['id'] != item_id and item['type'] == 'image'
                        and not item['trashed'] and tag['name'] in item['tags'] and tag['name'] not in item['autoTags']]
            if len(examples) < 10:
                continue
            if vector is None:
                vector = analysis_tools.ENGINE.vector(self.media(item_id)[0])
            matches = sorted([(float(vector @ analysis_tools.ENGINE.vector(self.media(item['id'])[0])), item['id']) for item in examples], reverse=True)
            score = sum(match[0] for match in matches[:3]) / 3
            reference_ids[tag['name']] = {item['id'] for item in examples}
            decisions[tag['name']] = {'item': item_id, 'tag': tag['name'], 'score': round(score, 4), 'evidence': [match[1] for match in matches[:3]],
                                    'status': 'applied' if tag['mode'] == 'auto' else 'pending'} if score >= tag['threshold'] else None
        # Compute first: a failed analysis must leave the previous automatic results intact.
        with self.lock:
            current = self.state()
            item = next((value for value in current['items'] if value['id'] == item_id), None)
            if not item or item['trashed']:
                return
            if any(item[field] != target[field] for field in ('title', 'filename', 'note', 'analysis')) or current['bundles'] != snapshot['bundles']:
                return
            auto = list(item['autoTags'])
            with self.db() as connection:
                for tag in snapshot['tags']:
                    name = tag['name']
                    if name not in decisions:
                        continue
                    current_tag = next((value for value in current['tags'] if value['name'] == name), None)
                    if not current_tag or any(current_tag.get(key) != tag.get(key) for key in ('mode', 'threshold', 'eligible', 'textRule')):
                        continue
                    current_examples = {value['id'] for value in current['items'] if value['id'] != item_id and value['type'] == 'image'
                                        and not value['trashed'] and name in value['tags'] and name not in value['autoTags']}
                    if name in reference_ids and current_examples != reference_ids[name]:
                        continue
                    key = item_id + ':' + name
                    old = self.get_extra(connection, 'prediction', key, {})
                    if old.get('status') in ('accepted', 'rejected') or name in set(item['tags']) - set(item['autoTags']):
                        continue
                    if name in auto:
                        connection.execute('DELETE FROM item_tags WHERE item_id=? AND name=?', (item_id, name))
                        auto.remove(name)
                    prediction = decisions[name]
                    if prediction is None:
                        connection.execute("DELETE FROM extras WHERE kind='prediction' AND key=?", (key,))
                    else:
                        self.put_extra(connection, 'prediction', key, prediction)
                        if prediction['status'] == 'applied':
                            self.labels(connection, item_id, [name], [])
                            auto.append(name)
                if auto != item['autoTags']:
                    self.put_extra(connection, 'auto', item_id, auto)
                if decisions or text_enabled or target['type'] == 'image' and any(tag['eligible'] and tag['mode'] != 'off' for tag in snapshot['tags']):
                    self.put_extra(connection, 'preferences', 'classified:' + item_id, datetime.now(timezone.utc).isoformat())

    def classification_ids(self, scope, snapshot=None):
        if not isinstance(scope, dict) or set(scope) - {'mode', 'tag', 'category', 'hours', 'ids', 'type'}:
            raise ArchiveError('자료 분류 범위를 확인하세요.')
        item_type = scope.get('type', 'all')
        if item_type not in ('all', 'image', 'pdf', 'document', 'video', 'link'):
            raise ArchiveError('분류할 자료 유형을 확인하세요.')
        mode = scope.get('mode', 'all')
        if mode not in ('all', 'unfiled', 'uncategorized', 'unorganized', 'new', 'ids'):
            raise ArchiveError('지원하지 않는 분류 범위입니다.')
        snapshot = snapshot or self.state()
        tag, category = clean_text(scope.get('tag', ''), 60), clean_text(scope.get('category', ''), 60)
        if tag and tag not in {value['name'] for value in snapshot['tags']} or category and category not in snapshot['categories']:
            raise ArchiveError('범위의 키워드 또는 카테고리를 찾을 수 없습니다.')
        hours = scope.get('hours')
        if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or not 0 < hours <= 8760):
            raise ArchiveError('최근 등록 시간은 0 초과 8760 이하로 입력하세요.')
        ids = scope.get('ids')
        known = {item['id'] for item in snapshot['items']}
        if mode == 'ids':
            bundles = {bundle['id']: bundle for bundle in snapshot.get('bundles', [])}
            if not isinstance(ids, list) or len(ids) > 10000 or any(not isinstance(key, str) or key not in known | bundles.keys() for key in ids):
                raise ArchiveError('분류할 선택 자료를 확인하세요.')
            ids = set(self.expand_ids(ids, bundles))
        elif ids is not None:
            raise ArchiveError('선택 자료 범위에서만 자료 목록을 지정하세요.')
        now = datetime.now(timezone.utc)
        result = []
        for item in snapshot['items']:
            if item['trashed'] or item_type != 'all' and item['type'] != item_type:
                continue
            if mode == 'ids' and item['id'] not in ids or mode == 'unorganized' and (item['tags'] or item['categories']) or mode == 'unfiled' and item['tags'] or mode == 'new' and item['hasClassification']:
                continue
            if mode == 'uncategorized' and item['categories']:
                continue
            if tag and tag not in item['tags'] or category and category not in item['categories']:
                continue
            if hours is not None:
                try:
                    created = datetime.fromisoformat(item['created'])
                    if created.tzinfo is None:
                        raise ValueError()
                except ValueError as exc:
                    raise ArchiveError('자료의 등록 시각을 확인하지 못했습니다.') from exc
                if not now - timedelta(hours=hours) <= created <= now:
                    continue
            result.append(item['id'])
        return result

    def classification_scope(self, metadata):
        ids = self.classification_ids(metadata)
        return {'ids': ids[:5000], 'count': len(ids), 'limit': 5000}

    def review(self, metadata):
        item_id, tag, action = metadata.get('id'), metadata.get('tag'), metadata.get('action')
        if action not in ('accept', 'reject'):
            raise ArchiveError('분류 확인 작업을 확인하세요.')
        with self.lock, self.db() as connection:
            key = str(item_id) + ':' + str(tag)
            prediction = self.get_extra(connection, 'prediction', key)
            item = connection.execute('SELECT trashed FROM items WHERE id=?', (item_id,)).fetchone()
            if not prediction or not item or item['trashed']:
                raise ArchiveError('확인할 자료를 찾을 수 없습니다.')
            if prediction['status'] not in ('pending', 'applied'):
                raise ArchiveError('이미 확인한 분류 결과입니다.')
            auto = self.get_extra(connection, 'auto', item_id, [])
            if action == 'accept':
                self.labels(connection, item_id, [tag], [])
            elif tag in auto:
                connection.execute('DELETE FROM item_tags WHERE item_id=? AND name=?', (item_id, tag))
            prediction['status'] = 'accepted' if action == 'accept' else 'rejected'
            self.put_extra(connection, 'prediction', key, prediction)
            self.put_extra(connection, 'auto', item_id, [name for name in auto if name != tag])
        return {}

    def analyze_text(self, item_id, use_ocr=False, language='ko-KR', summary_enabled=True):
        snapshot = self.state()
        item = next((item for item in snapshot['items'] if item['id'] == item_id), None)
        if not item or item['trashed']:
            raise ArchiveError('분석할 자료를 찾을 수 없습니다.')
        if item['type'] == 'pdf':
            pages = analysis_tools.extract_pdf(self.media(item_id)[0], use_ocr, language)
            method = 'PDF 텍스트·OCR' if use_ocr else 'PDF 텍스트'
        elif item['type'] == 'document':
            result = document_tools.inspect(self.media(item_id)[0], Path(item['path']).suffix)
            result['summary'], result['keywords'] = analysis_tools.summarize(result['pages'])
            result['summarized'] = summary_enabled
            if not summary_enabled:
                result['summary'] = []
            with self.lock, self.db() as connection:
                self.put_extra(connection, 'analysis', item_id, result)
            return result
        elif item['type'] == 'image' and use_ocr:
            pages = [{'number': 1, 'text': analysis_tools.ocr(self.media(item_id)[0], language), 'method': 'ocr'}]
            method = 'Windows OCR'
        else:
            bundle_note = next((bundle['note'] for bundle in snapshot['bundles'] if item_id in bundle['ids']), '')
            pages = [{'number': 1, 'text': '\n'.join(dict.fromkeys(note for note in (bundle_note, item['note']) if note)), 'method': 'note'}]
            method = '사용자 메모'
        summary, keywords = analysis_tools.summarize(pages)
        result = {'pages': pages, 'summary': summary if summary_enabled else [], 'keywords': keywords, 'method': method, 'summarized': summary_enabled}
        with self.lock, self.db() as connection:
            self.put_extra(connection, 'analysis', item_id, result)
        return result

    def analysis_edit(self, metadata):
        item_id = metadata.get('id')
        with self.lock, self.db() as connection:
            result = self.get_extra(connection, 'analysis', item_id)
            if not result:
                raise ArchiveError('분석 결과가 없습니다.')
            page = metadata.get('page')
            text = clean_text(metadata.get('text'), 100000)
            target = next((value for value in result['pages'] if value['number'] == page), None)
            if not target:
                raise ArchiveError('페이지를 찾을 수 없습니다.')
            target['text'], target['method'] = text, 'corrected'
            if sum(len(page['text']) for page in result['pages']) > 500000:
                raise ArchiveError('추출 텍스트는 최대 50만 자까지 지원합니다.')
            result['summary'], result['keywords'] = analysis_tools.summarize(result['pages'])
            if not result.get('summarized', True):
                result['summary'] = []
            self.put_extra(connection, 'analysis', item_id, result)
        return {}

    def start_job(self, metadata):
        operation = metadata.get('operation')
        if operation not in ('classify', 'text', 'model', 'import'):
            raise ArchiveError('지원하지 않는 분석 작업입니다.')
        use_ocr, language = metadata.get('ocr', False), metadata.get('language', 'ko-KR')
        if type(use_ocr) is not bool or language not in ('ko-KR', 'en-US'):
            raise ArchiveError('OCR 설정을 확인하세요.')
        with self.lock:
            if self.job['running']:
                raise ArchiveError('진행 중인 분석 작업이 끝난 뒤 실행하세요.')
            snapshot = self.state()
            known = {item['id'] for item in snapshot['items'] if not item['trashed']}
            if 'scope' in metadata:
                if operation != 'classify' or 'ids' in metadata:
                    raise ArchiveError('자료 분류 작업에서만 범위를 지정하세요.')
                ids = self.classification_ids(metadata['scope'], snapshot)
            else:
                ids = metadata.get('ids', sorted(known)) if operation != 'model' else ['model']
                if operation != 'model' and isinstance(ids, list) and all(isinstance(key, str) for key in ids):
                    ids = self.expand_ids(ids, {bundle['id']: bundle for bundle in snapshot['bundles']})
            if not isinstance(ids, list) or len(ids) > 5000 or operation != 'model' and any(not isinstance(key, str) or key not in known for key in ids):
                raise ArchiveError('분석할 자료를 확인하세요. 한 번에 최대 5,000개입니다.')
            ids = list(dict.fromkeys(ids))
            self.job = {'running': True, 'done': 0, 'total': len(ids), 'errors': [], 'message': operation}
        def work():
            try:
                for item_id in ids:
                    try:
                        if operation == 'model':
                            analysis_tools.install_model()
                        elif operation == 'classify':
                            self.classify(item_id)
                        elif operation == 'import':
                            snapshot = self.state()
                            item = next((item for item in snapshot['items'] if item['id'] == item_id), None)
                            preferences = snapshot['preferences']
                            if item and (preferences['summaries'] or preferences['ocr']) and (item['type'] in ('pdf', 'document') or item['note'] or preferences['ocr'] and item['type'] == 'image'):
                                try:
                                    self.analyze_text(item_id, preferences['ocr'] and item['type'] in ('image', 'pdf'), preferences['language'], preferences['summaries'])
                                except Exception as exc:
                                    with self.lock:
                                        self.job['errors'].append({'id': item_id, 'message': str(exc)[:300]})
                            self.classify(item_id)
                        else:
                            self.analyze_text(item_id, use_ocr, language)
                            self.classify(item_id)
                    except Exception as exc:
                        with self.lock:
                            self.job['errors'].append({'id': item_id, 'message': str(exc)[:300]})
                    finally:
                        with self.lock:
                            self.job['done'] += 1
            finally:
                with self.lock:
                    self.job['running'] = False
                    pending, self.pending_classification = self.pending_classification, []
                    if pending:
                        known = {item['id'] for item in self.state()['items'] if not item['trashed']}
                        pending = [item_id for item_id in pending if item_id in known]
                        self.pending_classification = pending[5000:]
                        if pending[:5000]:
                            self.start_job({'operation': 'import', 'ids': pending[:5000]})
        threading.Thread(target=work, daemon=True).start()
        return {'started': True}

    def queue_import(self, item_id):
        with self.lock:
            snapshot = self.state()
            if not any(rule['mode'] != 'off' for rule in snapshot['categoryRules']) and not any(tag['eligible'] and tag['mode'] != 'off' or tag.get('textRule', {}).get('mode', 'off') != 'off' for tag in snapshot['tags']) and not snapshot['preferences']['summaries'] and not snapshot['preferences']['ocr']:
                return
            if not any(item['id'] == item_id and not item['trashed'] for item in snapshot['items']):
                return
            if self.job['running']:
                self.pending_classification = list(dict.fromkeys([*self.pending_classification, item_id]))
            else:
                self.start_job({'operation': 'import', 'ids': [item_id]})

    def add_pdf(self, content, metadata):
        material_date = clean_date(metadata.get('material_date', ''))
        if not content or len(content) > IMAGE_LIMIT:
            raise ArchiveError('PDF는 파일당 최대 50 MB입니다.')
        filename = clean_text(metadata.get('filename'), 250, True)
        title = clean_text(metadata.get('title', ''), 200) or filename[:200]
        note = clean_text(metadata.get('note', ''), 20000)
        source = clean_url(metadata.get('source', ''))
        tags, categories = clean_names(metadata.get('tags', [])), clean_names(metadata.get('categories', []))
        pages, thumbnail = analysis_tools.pdf_info(content)
        digest = hashlib.sha256(content).hexdigest()
        with self.lock, self.db() as connection:
            existing = connection.execute('SELECT id,trashed FROM items WHERE hash=?', (digest,)).fetchone()
            if existing:
                if existing['trashed']:
                    raise ArchiveError('같은 PDF가 휴지통에 있습니다. 먼저 복원하세요.')
                self.labels(connection, existing['id'], tags, categories)
                self.manual_labels(connection, existing['id'], tags)
                self.upload_collection(connection, existing['id'], metadata)
                return {'id': existing['id'], 'duplicate': True}
            item_id = uuid.uuid4().hex
            relative = f'files/{item_id}.pdf'
            original, thumb = self.root / relative, self.root / 'thumbs' / f'{item_id}.jpg'
            try:
                original.write_bytes(content)
                thumb.write_bytes(thumbnail)
                connection.execute('INSERT INTO items(id,type,title,filename,path,hash,source,note,size,created) VALUES (?,\'pdf\',?,?,?,?,?,?,?,?)',
                                   (item_id, title, filename, relative, digest, source, note, len(content), datetime.now(timezone.utc).isoformat()))
                connection.execute('UPDATE items SET material_date=? WHERE id=?', (material_date, item_id))
                self.labels(connection, item_id, tags, categories)
                self.upload_collection(connection, item_id, metadata)
                connection.commit()
            except Exception:
                original.unlink(missing_ok=True)
                thumb.unlink(missing_ok=True)
                raise
        return {'id': item_id, 'duplicate': False, 'pages': pages}

    def add_file(self, content, metadata):
        filename = clean_text(metadata.get('filename', ''), 250, True)
        extension = Path(filename).suffix.lower()
        if extension in document_tools.EXTENSIONS:
            return self.add_document(content, metadata)
        if extension in ('.doc', '.xls', '.ppt', '.docm', '.xlsm', '.pptm', '.rtf', '.odt', '.ods', '.odp'):
            raise ArchiveError('이 문서 형식은 지원하지 않습니다. 매크로 없는 DOCX·XLSX·PPTX, PDF 또는 TXT로 변환해 주세요.')
        if Path(filename).suffix.lower() in ('.mp4', '.webm'):
            return self.add_video(content, metadata)
        return self.add_pdf(content, metadata) if filename.lower().endswith('.pdf') else self.add_image(content, metadata)

    def add_document(self, content, metadata):
        filename = clean_text(metadata.get('filename'), 250, True)
        extension = Path(filename).suffix.lower()
        try:
            analysis = document_tools.inspect(content, extension)
        except document_tools.DocumentError as exc:
            raise ArchiveError(str(exc)) from exc
        title = clean_text(metadata.get('title', ''), 200) or filename[:200]
        note, source = clean_text(metadata.get('note', ''), 20000), clean_url(metadata.get('source', ''))
        material_date = clean_date(metadata.get('material_date', ''))
        tags, categories = clean_names(metadata.get('tags', [])), clean_names(metadata.get('categories', []))
        digest = hashlib.sha256(content).hexdigest()
        with self.lock, self.db() as connection:
            existing = connection.execute('SELECT id,trashed FROM items WHERE hash=?', (digest,)).fetchone()
            if existing:
                if existing['trashed']:
                    raise ArchiveError('같은 문서가 휴지통에 있습니다. 먼저 복원하세요.')
                self.labels(connection, existing['id'], tags, categories)
                self.manual_labels(connection, existing['id'], tags)
                self.upload_collection(connection, existing['id'], metadata)
                return {'id': existing['id'], 'duplicate': True}
            item_id = uuid.uuid4().hex
            relative = f'files/{item_id}{extension}'
            original, thumb = self.root / relative, self.root / 'thumbs' / f'{item_id}.jpg'
            try:
                original.write_bytes(content)
                thumb.write_bytes(document_tools.thumbnail(extension))
                connection.execute("INSERT INTO items(id,type,title,filename,path,hash,source,note,size,created,material_date) VALUES (?,'document',?,?,?,?,?,?,?,?,?)",
                                   (item_id, title, filename, relative, digest, source, note, len(content), datetime.now(timezone.utc).isoformat(), material_date))
                self.put_extra(connection, 'analysis', item_id, analysis)
                self.labels(connection, item_id, tags, categories)
                self.upload_collection(connection, item_id, metadata)
                connection.commit()
            except Exception:
                original.unlink(missing_ok=True)
                thumb.unlink(missing_ok=True)
                raise
        return {'id': item_id, 'duplicate': False}

    def open_native(self, metadata):
        item_id = clean_text(metadata.get('id'), 32, True)
        with self.lock, self.db() as connection:
            item = connection.execute('SELECT * FROM items WHERE id=? AND trashed=0', (item_id,)).fetchone()
            if not item or item['type'] == 'link':
                raise ArchiveError('열 수 있는 원본 자료를 찾을 수 없습니다.')
            source = self.media_path(item_id)
            if item['type'] == 'document':
                try:
                    preview = document_tools.inspect(source.read_bytes(), source.suffix)
                except document_tools.DocumentError as exc:
                    raise ArchiveError(str(exc)) from exc
                if preview['openReason']:
                    raise ArchiveError('연결 앱 열기 제한: ' + preview['openReason'])
            if not hasattr(os, 'startfile'):
                raise ArchiveError('연결 앱 열기는 Windows에서 지원합니다. 원본 저장을 이용하세요.')
            # A separate copy prevents Office autosave from invalidating the archived original/hash.
            folder = self.data / 'opened' / uuid.uuid4().hex
            folder.mkdir(parents=True)
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(item['filename']).stem).strip(' .')[:120] or 'document'
            target = folder / ('RefArchive-' + name + source.suffix)
            shutil.copyfile(source, target)
            try:
                os.startfile(str(target), 'open')
            except OSError as exc:
                target.unlink(missing_ok=True)
                folder.rmdir()
                raise ArchiveError('연결된 앱을 열지 못했습니다. 해당 형식의 기본 앱을 설치·설정하거나 원본 저장을 이용하세요.') from exc
        return {'opened': True, 'copy': str(target)}

    def upload_collection(self, connection, item_id, metadata):
        self.attach_bundle(connection, item_id, metadata, inherit=True)
        key = clean_text(metadata.get('uploadGroup', ''), 32)
        name = clean_text(metadata.get('uploadGroupName', ''), 60)
        if not key:
            if name:
                raise ArchiveError('업로드 그룹을 선택하세요.')
            return
        if not re.fullmatch('[a-f0-9]{32}', key):
            raise ArchiveError('업로드 그룹을 확인하세요.')
        group = self.get_extra(connection, 'collection', key)
        if not group:
            if not name:
                raise ArchiveError('그룹이 삭제되었습니다. 다른 그룹을 선택하세요.')
            if any(json.loads(row[0])['name'] == name for row in connection.execute("SELECT value FROM extras WHERE kind='collection'")):
                raise ArchiveError('같은 이름의 그룹이 있습니다. 기존 그룹을 선택하세요.')
            group = {'id': key, 'name': name, 'ids': []}
        if item_id not in group['ids']:
            if len(group['ids']) >= 5000:
                raise ArchiveError('그룹은 최대 5,000개 자료까지 지원합니다.')
            group['ids'].append(item_id)
        self.put_extra(connection, 'collection', key, group)

    def add_video(self, content, metadata):
        material_date = clean_date(metadata.get('material_date', ''))
        if not content or len(content) > VIDEO_LIMIT:
            raise ArchiveError('동영상은 파일당 최대 200 MB입니다.')
        filename = clean_text(metadata.get('filename'), 250, True)
        extension = Path(filename).suffix.lower()
        try:
            video_tools.validate(content, extension)
        except ValueError as exc:
            raise ArchiveError(str(exc)) from exc
        title = clean_text(metadata.get('title', ''), 200) or filename[:200]
        note, source = clean_text(metadata.get('note', ''), 20000), clean_url(metadata.get('source', ''))
        tags, categories = clean_names(metadata.get('tags', [])), clean_names(metadata.get('categories', []))
        digest = hashlib.sha256(content).hexdigest()
        with self.lock, self.db() as connection:
            existing = connection.execute('SELECT id,trashed FROM items WHERE hash=?', (digest,)).fetchone()
            if existing:
                if existing['trashed']:
                    raise ArchiveError('같은 동영상이 휴지통에 있습니다. 먼저 복원하세요.')
                self.labels(connection, existing['id'], tags, categories)
                self.manual_labels(connection, existing['id'], tags)
                self.upload_collection(connection, existing['id'], metadata)
                return {'id': existing['id'], 'duplicate': True}
            item_id = uuid.uuid4().hex
            relative = f'files/{item_id}{extension}'
            original, thumb = self.root / relative, self.root / 'thumbs' / f'{item_id}.jpg'
            try:
                original.write_bytes(content)
                thumb.write_bytes(video_tools.thumbnail())
                connection.execute("INSERT INTO items(id,type,title,filename,path,hash,source,note,size,created) VALUES (?,'video',?,?,?,?,?,?,?,?)",
                                   (item_id, title, filename, relative, digest, source, note, len(content), datetime.now(timezone.utc).isoformat()))
                connection.execute('UPDATE items SET material_date=? WHERE id=?', (material_date, item_id))
                self.labels(connection, item_id, tags, categories)
                self.upload_collection(connection, item_id, metadata)
                connection.commit()
            except Exception:
                original.unlink(missing_ok=True)
                thumb.unlink(missing_ok=True)
                raise
        return {'id': item_id, 'duplicate': False}

    def labels(self, connection, item_id, tags, categories, replace=False):
        if replace:
            previous = {row[0] for row in connection.execute('SELECT name FROM item_categories WHERE item_id=?', (item_id,))}
            if previous - set(categories):
                self.manual_categories(connection, item_id, previous - set(categories), removed=True)
        for table, relation, names in (("tags", "item_tags", tags),
                                       ("categories", "item_categories", categories)):
            if replace:
                connection.execute(f"DELETE FROM {relation} WHERE item_id=?", (item_id,))
            for name in names:
                connection.execute(f"INSERT OR IGNORE INTO {table}(name) VALUES (?)", (name,))
                connection.execute(f"INSERT OR IGNORE INTO {relation} VALUES (?,?)", (item_id, name))
        self.manual_categories(connection, item_id, categories, replace=replace)

    def add_image(self, content, metadata):
        material_date = clean_date(metadata.get('material_date', ''))
        if not content or len(content) > IMAGE_LIMIT:
            raise ArchiveError("이미지는 파일당 최대 50 MB입니다.")
        tags = clean_names(metadata.get("tags", []))
        categories = clean_names(metadata.get("categories", []))
        filename = clean_text(metadata.get("filename", "이미지"), 250, True)
        title = clean_text(metadata.get("title", ""), 200) or filename[:200]
        source = clean_url(metadata.get("source", ""))
        note = clean_text(metadata.get("note", ""), 20000)
        digest = hashlib.sha256(content).hexdigest()
        with self.lock, self.db() as connection:
            existing = connection.execute("SELECT id,trashed FROM items WHERE hash=?", (digest,)).fetchone()
            if existing:
                if existing["trashed"]:
                    raise ArchiveError("같은 이미지가 휴지통에 있습니다. 먼저 복원하세요.")
                self.labels(connection, existing["id"], tags, categories)
                self.manual_labels(connection, existing['id'], tags)
                self.upload_collection(connection, existing['id'], metadata)
                return {"id": existing["id"], "duplicate": True}
        extension, width, height, thumbnail = image_info(content)
        item_id = uuid.uuid4().hex
        relative = f"files/{item_id}{extension}"
        original = self.root / relative
        thumb = self.root / "thumbs" / f"{item_id}.jpg"
        with self.lock, self.db() as connection:
            # Another request may have imported the same file while it was being decoded.
            existing = connection.execute("SELECT id,trashed FROM items WHERE hash=?", (digest,)).fetchone()
            if existing:
                if existing["trashed"]:
                    raise ArchiveError("같은 이미지가 휴지통에 있습니다. 먼저 복원하세요.")
                self.labels(connection, existing["id"], tags, categories)
                self.manual_labels(connection, existing['id'], tags)
                self.upload_collection(connection, existing['id'], metadata)
                return {"id": existing["id"], "duplicate": True}
            try:
                original.write_bytes(content)
                thumb.write_bytes(thumbnail)
                connection.execute("""INSERT INTO items
                    (id,type,title,filename,path,hash,source,note,width,height,size,created)
                    VALUES (?,'image',?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, title, filename, relative, digest, source, note, width, height,
                     len(content), datetime.now(timezone.utc).isoformat()))
                connection.execute('UPDATE items SET material_date=? WHERE id=?', (material_date, item_id))
                self.labels(connection, item_id, tags, categories)
                self.upload_collection(connection, item_id, metadata)
                connection.commit()
            except Exception:
                original.unlink(missing_ok=True)
                thumb.unlink(missing_ok=True)
                raise
        return {"id": item_id, "duplicate": False}

    def add_link(self, metadata):
        material_date = clean_date(metadata.get('material_date', ''))
        source = clean_url(metadata.get("source", ""), True)
        title = clean_text(metadata.get("title", ""), 200) or urlsplit(source).hostname
        note = clean_text(metadata.get("note", ""), 20000)
        tags = clean_names(metadata.get("tags", []))
        categories = clean_names(metadata.get("categories", []))
        item_id = uuid.uuid4().hex
        with self.lock, self.db() as connection:
            connection.execute("INSERT INTO items(id,type,title,source,note,created) VALUES (?,'link',?,?,?,?)",
                               (item_id, title, source, note, datetime.now(timezone.utc).isoformat()))
            connection.execute('UPDATE items SET material_date=? WHERE id=?', (material_date, item_id))
            self.labels(connection, item_id, tags, categories)
        return {"id": item_id}

    def update(self, metadata):
        item_id = clean_text(metadata.get("id"), 32, True)
        tags = clean_names(metadata.get("tags", []))
        categories = clean_names(metadata.get("categories", []))
        title = clean_text(metadata.get("title", ""), 200, True)
        source = clean_url(metadata.get("source", ""))
        note = clean_text(metadata.get("note", ""), 20000)
        with self.lock, self.db() as connection:
            bundle = self.get_extra(connection, 'bundle', item_id)
            if bundle:
                bundle.update(title=title, source=source, note=note)
                bundle['material_date'] = clean_date(metadata.get('material_date', bundle.get('material_date', '')))
                self.put_extra(connection, 'bundle', item_id, bundle)
                for member in bundle['ids']:
                    self.labels(connection, member, tags, categories, replace=True)
                    self.manual_labels(connection, member, tags, replace=True)
                return {'id': item_id}
            item = connection.execute("SELECT type,material_date FROM items WHERE id=?", (item_id,)).fetchone()
            if not item:
                raise ArchiveError("자료를 찾을 수 없습니다.")
            if item["type"] == "link" and not source:
                raise ArchiveError("링크 자료에는 주소가 필요합니다.")
            material_date = clean_date(metadata.get('material_date', item['material_date']))
            connection.execute("UPDATE items SET title=?,source=?,note=?,material_date=? WHERE id=?",
                               (title, source, note, material_date, item_id))
            self.labels(connection, item_id, tags, categories, replace=True)
            self.manual_labels(connection, item_id, tags, replace=True)
        return {"id": item_id}

    def batch(self, metadata):
        ids = metadata.get("ids", [])
        if not isinstance(ids, list) or not ids or len(ids) > 10000:
            raise ArchiveError("변경할 자료를 선택하세요.")
        ids = list(dict.fromkeys(clean_text(value, 32, True) for value in ids))
        action = metadata.get("action")
        tags = clean_names(metadata.get("tags", []))
        categories = clean_names(metadata.get("categories", []))
        if action not in ("add", "remove", "trash", "restore", "favorite", "unfavorite"):
            raise ArchiveError("지원하지 않는 작업입니다.")
        changed = len(ids)
        with self.lock, self.db() as connection:
            ids = self.expand_ids(ids, self.bundles(connection), whole=action in ('trash', 'restore', 'favorite', 'unfavorite'))
            for item_id in ids:
                if not connection.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                    raise ArchiveError("자료를 찾을 수 없습니다. 목록을 새로고침하세요.")
            for item_id in ids:
                if action == "add":
                    self.labels(connection, item_id, tags, categories)
                    self.manual_labels(connection, item_id, tags)
                elif action == "remove":
                    connection.executemany("DELETE FROM item_tags WHERE item_id=? AND name=?",
                                           [(item_id, name) for name in tags])
                    connection.executemany("DELETE FROM item_categories WHERE item_id=? AND name=?",
                                           [(item_id, name) for name in categories])
                    self.manual_labels(connection, item_id, tags, removed=True)
                    self.manual_categories(connection, item_id, categories, removed=True)
                elif action in ("trash", "restore"):
                    connection.execute("UPDATE items SET trashed=? WHERE id=?", (action == "trash", item_id))
                else:
                    connection.execute("UPDATE items SET favorite=? WHERE id=?", (action == "favorite", item_id))
        return {"changed": changed}

    def taxonomy(self, metadata):
        kind = metadata.get("kind")
        if kind not in ("tags", "categories"):
            raise ArchiveError("지원하지 않는 분류입니다.")
        name = clean_text(metadata.get("name"), 60, True)
        action = metadata.get("action", "add")
        with self.lock, self.db() as connection:
            if action == "add":
                connection.execute(f"INSERT OR IGNORE INTO {kind}(name) VALUES (?)", (name,))
            elif action in ("rename", "merge"):
                target = clean_text(metadata.get("target"), 60, True)
                if name == target:
                    return {}
                if not connection.execute(f"SELECT 1 FROM {kind} WHERE name=?", (name,)).fetchone():
                    raise ArchiveError("분류를 찾을 수 없습니다.")
                relation = "item_tags" if kind == "tags" else "item_categories"
                manual_target = set()
                if kind == 'tags':
                    manual_target = {row[0] for row in connection.execute('SELECT item_id FROM item_tags WHERE name=?', (target,))
                                     if target not in self.get_extra(connection, 'auto', row[0], [])}
                else:
                    manual_target = {row[0] for row in connection.execute('SELECT item_id,name FROM item_categories WHERE name IN (?,?)', (target, name))
                        if not any(p['name'] == row[1] and p['status'] == 'applied' for p in
                            self.get_extra(connection, 'preferences', 'category:' + row[0], {'decisions': []})['decisions'])}
                connection.execute(f"INSERT OR IGNORE INTO {kind}(name) VALUES (?)", (target,))
                connection.execute(f"INSERT OR IGNORE INTO {relation} SELECT item_id,? FROM {relation} WHERE name=?",
                                   (target, name))
                connection.execute(f"DELETE FROM {kind} WHERE name=?", (name,))
                self.rename_extras(connection, kind, name, target, manual_target)
            elif action == "delete":
                # Deleting a classification removes memberships only, never the original item.
                connection.execute(f"DELETE FROM {kind} WHERE name=?", (name,))
                self.rename_extras(connection, kind, name, None)
            else:
                raise ArchiveError("지원하지 않는 작업입니다.")
        return {}

    def rename_extras(self, connection, kind, name, target, manual_target=()):
        for row in list(connection.execute('SELECT * FROM extras')):
            value, key, extra_kind = json.loads(row['value']), row['key'], row['kind']
            if extra_kind == 'search' and target:
                filters = value['filters']
                if kind == 'tags':
                    for field in ('tags', 'exclude'):
                        filters[field] = list(dict.fromkeys(target if tag == name else tag for tag in filters[field]))
                elif filters['category'] == name:
                    filters['category'] = target
                self.put_extra(connection, extra_kind, key, value)
            if kind == 'categories' and extra_kind == 'preferences':
                if key == 'category-rules':
                    old = value.pop(name, None)
                    if target and old and target not in value:
                        value[target] = old
                    self.put_extra(connection, extra_kind, key, value)
                elif key.startswith('category:'):
                    merged = {}
                    priority = {'pending': 0, 'applied': 1, 'accepted': 2, 'rejected': 3}
                    for decision in value['decisions']:
                        if decision['name'] == name:
                            if not target:
                                continue
                            decision['name'] = target
                        if decision['name'] == target and key[9:] in manual_target:
                            decision['status'] = 'accepted'
                        previous = merged.get(decision['name'])
                        if not previous or priority[decision['status']] > priority[previous['status']]:
                            merged[decision['name']] = decision
                    value['decisions'] = list(merged.values())
                    if target in merged and merged[target]['status'] == 'rejected':
                        connection.execute('DELETE FROM item_categories WHERE item_id=? AND name=?', (key[9:], target))
                    self.put_extra(connection, extra_kind, key, value)
            if kind != 'tags':
                continue
            if extra_kind == 'auto':
                value = list(dict.fromkeys(target if tag == name else tag for tag in value if tag != name or target))
                if key in manual_target:
                    value = [tag for tag in value if tag != target]
                self.put_extra(connection, extra_kind, key, value)
            elif extra_kind == 'mode' and key == name:
                connection.execute('DELETE FROM extras WHERE kind=? AND key=?', (extra_kind, key))
                if target and not self.get_extra(connection, extra_kind, target):
                    self.put_extra(connection, extra_kind, target, value)
            elif extra_kind == 'prediction' and value['tag'] == name:
                connection.execute('DELETE FROM extras WHERE kind=? AND key=?', (extra_kind, key))
                if target:
                    value['tag'] = target
                    new_key = value['item'] + ':' + target
                    if not self.get_extra(connection, extra_kind, new_key):
                        self.put_extra(connection, extra_kind, new_key, value)

    def media(self, item_id, thumbnail=False):
        path = self.media_path(item_id, thumbnail)
        return path.read_bytes(), path.suffix

    def media_path(self, item_id, thumbnail=False):
        if not re.fullmatch(r"[a-f0-9]{32}", item_id):
            raise ArchiveError("자료를 찾을 수 없습니다.")
        with self.lock, self.db() as connection:
            row = connection.execute("SELECT path FROM items WHERE id=? AND type IN ('image','pdf','video','document')", (item_id,)).fetchone()
            if not row:
                raise ArchiveError("자료를 찾을 수 없습니다.")
            path = self.root / (f"thumbs/{item_id}.jpg" if thumbnail else row["path"])
            if not path.is_file() or self.root not in path.resolve().parents:
                raise ArchiveError("원본 파일을 찾을 수 없습니다.")
            return path

    def backup(self, destination, progress=None):
        with self.lock, tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "archive.sqlite3"
            with self.db() as connection, closing(sqlite3.connect(snapshot)) as target:
                connection.backup(target)
                target.commit()
            state = self.state()
            metadata = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
            manifest = json.dumps({"format": "RefArchive", "version": 1,
                                   "items": len(state["items"])}).encode("utf-8")
            files = []
            for item in state["items"]:
                if item["type"] in ('image', 'pdf', 'video', 'document'):
                    for relative in (item["path"], f"thumbs/{item['id']}.jpg"):
                        path = self.root / relative
                        if not path.is_file() or self.root not in path.resolve().parents:
                            raise ArchiveError("누락된 파일이 있어 백업을 완료할 수 없습니다.")
                        files.append((path, relative))
            total = snapshot.stat().st_size + len(metadata) + len(manifest) + sum(path.stat().st_size for path, _ in files)
            if total > BACKUP_LIMIT or len(files) + 3 > 50000:
                raise ArchiveError("백업·복원은 총 50 GiB, 50,000개 내부 파일까지 지원합니다.")
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                archive.writestr("manifest.json", manifest)
                archive.writestr("metadata.json", metadata)
                archive.write(snapshot, "archive.sqlite3")
                for index, (path, relative) in enumerate(files):
                    if progress:
                        progress(index, len(files))
                    archive.write(path, relative)
                if progress:
                    progress(len(files), len(files))
            zip_size = Path(destination).stat().st_size if isinstance(destination, (str, Path)) else destination.tell()
            if zip_size > BACKUP_LIMIT:
                raise ArchiveError("백업 파일이 복원 한도인 50 GiB를 초과했습니다.")

    def restore(self, content):
        if self.job['running'] or self.maintenance.status()['running']:
            raise ArchiveError('분석 작업이 끝난 뒤 백업을 복원하세요.')
        source = io.BytesIO(content) if isinstance(content, bytes) else content
        source.seek(0, 2)
        if not 0 < source.tell() <= BACKUP_LIMIT:
            raise ArchiveError("복원 파일은 최대 50 GiB입니다.")
        source.seek(0)
        staging = Path(tempfile.mkdtemp(prefix="restore-", dir=self.data))
        try:
            try:
                with zipfile.ZipFile(source) as archive:
                    infos = archive.infolist()
                    names = [info.filename for info in infos]
                    allowed = re.compile(r"(?:manifest\.json|metadata\.json|archive\.sqlite3|files/[a-f0-9]{32}\.(?:jpg|png|webp|pdf|mp4|webm|docx|xlsx|pptx|txt|md|csv|tsv|log)|thumbs/[a-f0-9]{32}\.jpg)")
                    if len(names) != len(set(names)) or len(names) > 50000 or sum(i.file_size for i in infos) > BACKUP_LIMIT:
                        raise ArchiveError("백업 크기 또는 중복 항목이 올바르지 않습니다.")
                    if not all(allowed.fullmatch(name) for name in names):
                        raise ArchiveError("백업에 허용되지 않는 파일 경로가 있습니다.")
                    required = sum(info.file_size for info in infos)
                    current_size = sum(path.stat().st_size for path in self.root.rglob('*') if path.is_file())
                    if required + current_size + 64 * 1024 * 1024 > shutil.disk_usage(self.data).free:
                        raise ArchiveError('복원과 현재 자료함 안전 백업을 위한 저장 공간이 부족합니다.')
                    if not {"manifest.json", "archive.sqlite3"}.issubset(names):
                        raise ArchiveError("RefArchive 백업 파일이 아닙니다.")
                    if archive.getinfo("manifest.json").file_size > 4096:
                        raise ArchiveError("백업 식별 정보가 올바르지 않습니다.")
                    manifest = json.loads(archive.read("manifest.json"))
                    if not isinstance(manifest, dict) or manifest.get("format") != "RefArchive" or manifest.get("version") != 1:
                        raise ArchiveError("지원하지 않는 백업 버전입니다.")
                    for info in infos:
                        target = staging / info.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                self.validate_backup(staging)
                upgrade_database(staging)
            except (zipfile.BadZipFile, KeyError, ValueError, TypeError, OSError, sqlite3.Error, RuntimeError) as exc:
                raise ArchiveError("백업이 손상되었거나 형식이 올바르지 않습니다.") from exc
            (staging / "files").mkdir(exist_ok=True)
            (staging / "thumbs").mkdir(exist_ok=True)
            with self.lock:
                if self.job['running'] or self.maintenance.status()['running']:
                    raise ArchiveError('분석 작업이 끝난 뒤 백업을 복원하세요.')
                backups = self.data / "backups"
                backups.mkdir(exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
                safety = backups / f"before-restore-{stamp}.zip"
                self.backup(safety)
                previous = self.data / f"previous-{stamp}"
                self.root.rename(previous)
                try:
                    staging.rename(self.root)
                except Exception:
                    previous.rename(self.root)
                    raise
                # Keep previous directory and ZIP so both a rollback and portable recovery are possible.
                with self.maintenance.lock:
                    self.maintenance.pairs = []
                return {"backup": str(safety), "items": self.state()['entryCount']}
        finally:
            if staging.exists() and staging.parent == self.data:
                shutil.rmtree(staging)

    @staticmethod
    def validate_backup(root):
        with closing(sqlite3.connect(f"{(root / 'archive.sqlite3').as_uri()}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or version not in (1, 2, 3, 4, 5):
                raise ArchiveError("백업 데이터베이스를 검증하지 못했습니다.")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {"items", "tags", "categories", "item_tags", "item_categories"} | ({'extras'} if version >= 2 else set()):
                raise ArchiveError("백업 데이터베이스 구조가 올바르지 않습니다.")
            with closing(sqlite3.connect(":memory:")) as reference:
                reference.executescript({1: SCHEMA_V1, 2: SCHEMA_V2, 3: SCHEMA_V3, 4: SCHEMA_V4, 5: SCHEMA}[version])
                schema = lambda db: {(row[0], row[1]): row[2] for row in db.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY name")}
                if schema(connection) != schema(reference):
                    raise ArchiveError("백업 데이터베이스 정의가 올바르지 않습니다.")
            expected = {"id", "type", "title", "filename", "path", "hash", "source", "note", "favorite", "trashed", "width", "height", "size", "created"}
            if version >= 5:
                expected.add('material_date')
            if {row[1] for row in connection.execute("PRAGMA table_info(items)")} != expected:
                raise ArchiveError("백업 자료 구조가 올바르지 않습니다.")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ArchiveError("백업의 자료 연결이 손상되었습니다.")
            for table in ("tags", "categories"):
                for row in connection.execute(f"SELECT name FROM {table}"):
                    clean_text(row[0], 60, True)
            for item in connection.execute("SELECT * FROM items"):
                if not re.fullmatch(r"[a-f0-9]{32}", item["id"] or "") or item["type"] not in ("image", "link", 'pdf', 'video', 'document'):
                    raise ArchiveError("백업 자료 식별자가 올바르지 않습니다.")
                if version >= 5:
                    clean_date(item['material_date'])
                clean_text(item["title"], 200, True)
                clean_text(item["note"], 20000)
                clean_url(item["source"], item["type"] == "link")
                if item["type"] in ('image', 'pdf', 'video', 'document'):
                    relative = item["path"] or ""
                    suffixes = {'pdf': 'pdf', 'video': '(?:mp4|webm)', 'image': '(?:jpg|png|webp)', 'document': '(?:docx|xlsx|pptx|txt|md|csv|tsv|log)'}[item['type']]
                    if not re.fullmatch(rf"files/{item['id']}\.{suffixes}", relative):
                        raise ArchiveError("백업 원본 경로가 올바르지 않습니다.")
                    original = root / relative
                    thumb = root / "thumbs" / f"{item['id']}.jpg"
                    if not original.is_file() or not thumb.is_file() or original.stat().st_size > (VIDEO_LIMIT if item['type'] == 'video' else IMAGE_LIMIT):
                        raise ArchiveError("백업에 원본 또는 미리보기 파일이 없습니다.")
                    if hashlib.sha256(original.read_bytes()).hexdigest() != item["hash"]:
                        raise ArchiveError("백업 원본의 체크섬이 일치하지 않습니다.")
                    if item['type'] == 'video':
                        video_tools.validate(original.read_bytes(), original.suffix)
                    elif item['type'] == 'document':
                        document_tools.inspect(original.read_bytes(), original.suffix)
                    elif item['type'] == 'pdf':
                        analysis_tools.pdf_info(original.read_bytes())
                    else:
                        extension, width, height, _ = image_info(original.read_bytes())
                        if original.suffix != extension or (width, height) != (item["width"], item["height"]):
                            raise ArchiveError("백업 이미지 정보가 일치하지 않습니다.")
                    with Image.open(thumb) as image:
                        if image.format != "JPEG" or max(image.size) > 480:
                            raise ArchiveError("백업 미리보기 형식이 올바르지 않습니다.")
                        image.verify()
            if version >= 2:
                Library.validate_extras(connection)

    @staticmethod
    def validate_category_record(connection, item_id, value, items, categories):
        if item_id not in items or not isinstance(value, dict) or set(value) != {'decisions', 'classifiedAt'}:
            raise ArchiveError('백업 카테고리 이력을 확인하세요.')
        try:
            stamp = datetime.fromisoformat(value['classifiedAt'])
        except (ValueError, TypeError):
            stamp = None
        decisions = value['decisions']
        if stamp is None or stamp.tzinfo is None or not isinstance(decisions, list) or len(decisions) > len(categories):
            raise ArchiveError('백업 카테고리 이력을 확인하세요.')
        attached = {row[0] for row in connection.execute('SELECT name FROM item_categories WHERE item_id=?', (item_id,))}
        seen = set()
        for row in decisions:
            if not isinstance(row, dict) or row.get('item') != item_id or not isinstance(row.get('name'), str) or row['name'] not in categories or row['name'] in seen:
                raise ArchiveError('백업 카테고리 연결을 확인하세요.')
            seen.add(row['name'])
            score, reasons, evidence = row.get('score'), row.get('reasons'), row.get('evidence')
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1 or row.get('status') not in ('pending', 'applied', 'accepted', 'rejected'):
                raise ArchiveError('백업 카테고리 결과를 확인하세요.')
            if row['status'] == 'applied' and row['name'] not in attached:
                raise ArchiveError('백업 자동 카테고리 연결이 손상되었습니다.')
            clean_text(row.get('reason'), 100, True)
            if not isinstance(evidence, list) or len(evidence) > 3 or any(not isinstance(key, str) or key not in items for key in evidence):
                raise ArchiveError('백업 카테고리 사례를 확인하세요.')
            if not isinstance(reasons, list) or not 1 <= len(reasons) <= 31:
                raise ArchiveError('백업 카테고리 근거를 확인하세요.')
            for match in reasons:
                if not isinstance(match, dict) or match.get('field') not in ('title', 'filename', 'note', 'content', 'description', 'example', 'manual') or type(match.get('page')) is not int or not 0 <= match['page'] <= 300:
                    raise ArchiveError('백업 카테고리 근거를 확인하세요.')
                clean_text(match.get('term'), 100, True)

    @staticmethod
    def validate_extras(connection):
        items = {row['id']: dict(row) for row in connection.execute('SELECT * FROM items')}
        tags = {row[0] for row in connection.execute('SELECT name FROM tags')}
        categories = {row[0] for row in connection.execute('SELECT name FROM categories')}
        bundled = set()
        for row in connection.execute('SELECT * FROM extras'):
            kind, key = row['kind'], row['key']
            value = json.loads(row['value'])
            if kind == 'bundle':
                if not isinstance(value, dict) or value.get('id') != key or not re.fullmatch('[a-f0-9]{32}', key) or key in items:
                    raise ArchiveError('백업 묶음 식별자가 올바르지 않습니다.')
                clean_date(value.get('material_date', ''))
                clean_text(value.get('title'), 200, True)
                clean_text(value.get('note'), 20000)
                clean_url(value.get('source'))
                ids = value.get('ids')
                if not isinstance(ids, list) or not 1 <= len(ids) <= 5000 or any(not isinstance(member, str) or member not in items for member in ids):
                    raise ArchiveError('백업 묶음 연결이 올바르지 않습니다.')
                if len(set(ids)) != len(ids) or bundled.intersection(ids) or len({(items[member]['trashed'], items[member]['favorite']) for member in ids}) != 1:
                    raise ArchiveError('백업 묶음에 중복 파일 또는 불일치한 상태가 있습니다.')
                if 'cover' in value and value['cover'] not in ids:
                    raise ArchiveError('백업 묶음의 대표 파일을 찾을 수 없습니다.')
                bundled.update(ids)
            elif kind in ('search', 'collection'):
                if not isinstance(value, dict) or value.get('id') != key or not re.fullmatch('[a-f0-9]{32}', key):
                    raise ArchiveError('백업 저장 항목을 확인하지 못했습니다.')
                clean_text(value.get('name'), 60, True)
                if kind == 'search':
                    Library.clean_filters(value.get('filters'))
                elif not isinstance(value.get('ids'), list) or len(value['ids']) > 5000 or any(not isinstance(key, str) or key not in items for key in value['ids']):
                    raise ArchiveError('백업 자료 모음 연결이 올바르지 않습니다.')
            elif kind == 'mode':
                if key not in tags or not isinstance(value, dict) or value.get('mode') not in ('off', 'suggest', 'auto'):
                    raise ArchiveError('백업 분류 설정이 올바르지 않습니다.')
                threshold = value.get('threshold')
                if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not .5 <= threshold <= 1:
                    raise ArchiveError('백업 분류 기준이 올바르지 않습니다.')
                if 'textRule' in value:
                    clean_text_rule(value['textRule'])
            elif kind == 'preferences':
                if key == 'category-rules':
                    if not isinstance(value, dict) or not set(value).issubset(categories):
                        raise ArchiveError('백업 카테고리 설정을 확인하세요.')
                    for rule in value.values():
                        try:
                            category_tools.clean_config(rule)
                        except ValueError as exc:
                            raise ArchiveError(str(exc)) from exc
                    continue
                if key.startswith('category:'):
                    Library.validate_category_record(connection, key[9:], value, items, categories)
                    continue
                if key.startswith('classified:'):
                    try:
                        stamp = datetime.fromisoformat(value) if isinstance(value, str) else None
                    except ValueError:
                        stamp = None
                    if key[11:] not in items or stamp is None or stamp.tzinfo is None:
                        raise ArchiveError('백업 자료 분류 이력이 올바르지 않습니다.')
                    continue
                if key != 'settings' or not isinstance(value, dict) or type(value.get('downloads')) is not bool:
                    raise ArchiveError('백업 링크 설정이 올바르지 않습니다.')
                if type(value.get('summaries', False)) is not bool or type(value.get('ocr', False)) is not bool or value.get('language', 'ko-KR') not in ('ko-KR', 'en-US'):
                    raise ArchiveError('백업 자동 내용 분석 설정이 올바르지 않습니다.')
            elif kind == 'auto':
                names = clean_names(value)
                if key not in items or not set(names).issubset(tags):
                    raise ArchiveError('백업 자동 분류 연결이 올바르지 않습니다.')
                attached = {row[0] for row in connection.execute('SELECT name FROM item_tags WHERE item_id=?', (key,))}
                if not set(names).issubset(attached):
                    raise ArchiveError('백업 자동 분류 연결이 손상되었습니다.')
            elif kind == 'prediction':
                if not isinstance(value, dict) or value.get('item') not in items or value.get('tag') not in tags or key != value['item'] + ':' + value['tag']:
                    raise ArchiveError('백업 분류 제안을 확인하지 못했습니다.')
                score = value.get('score')
                evidence = value.get('evidence')
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -1 <= score <= 1 or value.get('status') not in ('pending', 'applied', 'accepted', 'rejected') or not isinstance(evidence, list) or len(evidence) > 3 or any(not isinstance(key, str) or key not in items or items[key]['type'] != 'image' for key in evidence):
                    raise ArchiveError('백업 분류 제안 내용이 올바르지 않습니다.')
                if value.get('source', 'image') not in ('image', 'text'):
                    raise ArchiveError('백업 분류 출처를 확인하세요.')
                if value.get('source') == 'text':
                    matches = value.get('matches')
                    if not isinstance(matches, list) or not 1 <= len(matches) <= 30 or evidence:
                        raise ArchiveError('백업 텍스트 분류 근거를 확인하세요.')
                    for match in matches:
                        if not isinstance(match, dict) or match.get('field') not in ('title', 'filename', 'note', 'content') or type(match.get('page')) is not int or not 0 <= match['page'] <= 300:
                            raise ArchiveError('백업 텍스트 분류 근거를 확인하세요.')
                        clean_text(match.get('term'), 100, True)
                elif items[value['item']]['type'] != 'image':
                    raise ArchiveError('이미지 분류 제안의 자료 유형을 확인하세요.')
            elif kind == 'analysis':
                if key not in items or not isinstance(value, dict) or not isinstance(value.get('pages'), list) or len(value['pages']) > 300:
                    raise ArchiveError('백업 내용 분석 결과가 올바르지 않습니다.')
                clean_text(value.get('method'), 60, True)
                if type(value.get('summarized', True)) is not bool:
                    raise ArchiveError('백업 요약 설정이 올바르지 않습니다.')
                numbers, total = set(), 0
                for page in value['pages']:
                    if not isinstance(page, dict) or type(page.get('number')) is not int or not 1 <= page['number'] <= 300 or page['number'] in numbers:
                        raise ArchiveError('백업 분석 페이지가 올바르지 않습니다.')
                    numbers.add(page['number'])
                    total += len(clean_text(page.get('text'), 500000))
                    if page.get('method') not in ('embedded', 'ocr', 'note', 'corrected'):
                        raise ArchiveError('백업 분석 출처가 올바르지 않습니다.')
                if total > 500000 or not isinstance(value.get('summary'), list) or len(value['summary']) > 5:
                    raise ArchiveError('백업 요약 크기가 올바르지 않습니다.')
                for sentence in value['summary']:
                    if not isinstance(sentence, dict) or sentence.get('page') not in numbers:
                        raise ArchiveError('백업 요약 근거가 올바르지 않습니다.')
                    clean_text(sentence.get('text'), 1500)
                clean_names(value.get('keywords'))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not persist titles, source URLs, file names, or session tokens in HTTP logs.
        pass

    def headers_common(self, content_type):
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def response(self, content, content_type, status=200):
        self.send_response(status)
        self.headers_common(content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(content)

    def serve_media(self, item_id, thumbnail=False):
        library = self.server.library
        # Open under the library lock so a concurrent restore cannot swap this file.
        with library.lock:
            path = library.media_path(item_id, thumbnail)
            source = path.open('rb')
        with source:
            size = source.seek(0, 2)
            start, end, status = 0, size - 1, 200
            requested = self.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', requested)
                valid = bool(match and any(match.groups()))
                if valid:
                    left, right = match.groups()
                    if left:
                        start, end = int(left), min(int(right), size - 1) if right else size - 1
                    else:
                        start = max(0, size - int(right))
                    valid = 0 <= start <= end < size
                if not valid:
                    self.send_response(416)
                    self.headers_common('application/octet-stream')
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.headers_common({'.jpg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.pdf': 'application/pdf', '.mp4': 'video/mp4', '.webm': 'video/webm'}.get(path.suffix, 'application/octet-stream'))
            if path.suffix in document_tools.EXTENSIONS:
                self.send_header('Content-Disposition', f'attachment; filename="document{path.suffix}"')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command == 'HEAD':
                return
            source.seek(start)
            remaining = end - start + 1
            try:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Closing a player or seeking cancels its previous request.

    def json_response(self, payload, status=200):
        self.response(json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def valid_host(self):
        host = self.headers.get("Host", "")
        return host == f"127.0.0.1:{self.server.server_port}"

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.valid_host():
            self.json_response({"error": "로컬 주소로 접속하세요."}, 403)
            return
        try:
            path = urlsplit(self.path).path
            if path == "/api/state":
                self.json_response(self.server.library.state())
            elif path == "/api/health":
                self.json_response({"app": "RefArchive", "version": 2, 'appVersion': VERSION, 'storage': str(self.server.library.root)})
            elif path == '/api/job':
                with self.server.library.lock:
                    self.json_response(dict(self.server.library.job))
            elif path == '/api/maintenance':
                self.json_response(self.server.library.maintenance.status())
            elif path == '/api/backup/download':
                ticket = parse_qs(urlsplit(self.path).query).get('ticket', [''])[0]
                target = self.server.library.maintenance.take_download(ticket, consume=self.command != 'HEAD')
                with target.open('rb') as source:
                    self.send_response(200)
                    self.headers_common('application/zip')
                    self.send_header('Content-Length', str(target.stat().st_size))
                    self.send_header('Content-Disposition', f'attachment; filename="{target.name}"')
                    self.end_headers()
                    if self.command != 'HEAD':
                        shutil.copyfileobj(source, self.wfile, 1024 * 1024)
            elif path in ("/", "/app.js", "/style.css", '/workspace.css'):
                filename = "index.html" if path == "/" else path[1:]
                content = (ROOT / "web" / filename).read_bytes()
                if filename == "index.html":
                    content = content.replace(b"__SESSION_TOKEN__", self.server.token.encode())
                content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                                "style.css": "text/css; charset=utf-8", 'workspace.css': 'text/css; charset=utf-8'}[filename]
                self.response(content, content_type)
            elif path.startswith("/media/") or path.startswith("/thumb/"):
                self.serve_media(path.split('/')[-1], path.startswith('/thumb/'))
            elif unquote(path) in ('/guide', '/user-guide.html', '/사용설명서.html', '/distribution', '/distribution-guide.html', '/배포안내.html', '/guide.css'):
                name = {'/guide': 'user-guide.html', '/user-guide.html': 'user-guide.html', '/사용설명서.html': 'user-guide.html', '/distribution': 'distribution-guide.html', '/distribution-guide.html': 'distribution-guide.html', '/배포안내.html': 'distribution-guide.html', '/guide.css': 'guide.css'}[unquote(path)]
                self.response((ROOT / 'docs' / name).read_bytes(), 'text/css; charset=utf-8' if name == 'guide.css' else 'text/html; charset=utf-8')
            else:
                self.json_response({"error": "페이지를 찾을 수 없습니다."}, 404)
        except ArchiveError as exc:
            self.json_response({"error": str(exc)}, 400)
        except (OSError, sqlite3.Error):
            self.json_response({"error": "자료를 읽지 못했습니다. 저장 공간과 파일 상태를 확인하세요."}, 500)

    def do_POST(self):
        if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-RefArchive-Token", ""), self.server.token):
            self.json_response({"error": "요청을 확인하지 못했습니다. 화면을 새로고침하세요."}, 403)
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
            self.json_response({"error": "다른 사이트의 요청은 허용하지 않습니다."}, 403)
            return
        try:
            path = urlsplit(self.path).path
            limit = BACKUP_LIMIT if path == "/api/restore" else VIDEO_LIMIT + 1024 * 1024 if path in ("/api/images", '/api/files') else 1024 * 1024
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > limit:
                raise ArchiveError("요청 크기가 허용 범위를 초과했습니다.")
            library = self.server.library
            if path == '/api/restore':
                if length + 64 * 1024 * 1024 > shutil.disk_usage(library.data).free:
                    raise ArchiveError('복원 파일을 임시 보관할 저장 공간이 부족합니다.')
                with tempfile.TemporaryFile(dir=library.data) as temporary:
                    remaining = length
                    self.connection.settimeout(120)
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ArchiveError('전송이 완료되지 않았습니다.')
                        temporary.write(chunk)
                        remaining -= len(chunk)
                    self.json_response(library.restore(temporary))
                return
            content = self.rfile.read(length)
            if len(content) != length:
                raise ArchiveError("전송이 완료되지 않았습니다.")
            library = self.server.library
            if path in ("/api/images", '/api/files'):
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data"):
                    raise ArchiveError("이미지 전송 형식이 올바르지 않습니다.")
                message = BytesParser(policy=email_policy).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + content)
                parts = list(message.iter_parts())
                fields = {part.get_param("name", header="content-disposition"): part for part in parts}
                if len(parts) != 2 or set(fields) != {"metadata", "file"}:
                    raise ArchiveError("이미지 전송 항목이 올바르지 않습니다.")
                metadata = json.loads(fields["metadata"].get_payload(decode=True))
                if not isinstance(metadata, dict):
                    raise ArchiveError("입력 형식이 올바르지 않습니다.")
                result = library.add_file(fields["file"].get_payload(decode=True), metadata)
                self.json_response(result, 201)
                if not result.get('duplicate'):
                    library.queue_import(result['id'])
            elif path == "/api/backup":
                with tempfile.TemporaryFile() as temporary:
                    library.backup(temporary)
                    length = temporary.tell()
                    temporary.seek(0)
                    self.send_response(200)
                    self.headers_common("application/zip")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Disposition", 'attachment; filename="RefArchive-backup.zip"')
                    self.end_headers()
                    shutil.copyfileobj(temporary, self.wfile)
            elif path == '/api/shutdown':
                with library.lock:
                    if library.job['running'] or library.maintenance.status()['running']:
                        raise ArchiveError('분석·보관 작업이 끝난 뒤 종료하세요.')
                    self.json_response({'stopped': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == '/api/shortcut':
                import subprocess
                command = [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'install-shortcut.ps1')]
                if getattr(sys, 'frozen', False):
                    command += ['-Target', sys.executable]
                result = subprocess.run(command, capture_output=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    raise ArchiveError('바로가기를 만들지 못했습니다. 바탕화면의 같은 이름 파일을 확인하세요.')
                self.json_response({'created': True})
            else:
                metadata = json.loads(content or b"{}")
                if not isinstance(metadata, dict):
                    raise ArchiveError("입력 형식이 올바르지 않습니다.")
                methods = {"/api/open": library.open_native, "/api/links": library.add_link, "/api/update": library.update,
                           "/api/batch": library.batch, "/api/taxonomy": library.taxonomy,
                           '/api/saved': library.saved, '/api/bundle': library.bundle, '/api/mode': library.mode, '/api/text-rule': library.text_rule, '/api/review': library.review,
                           '/api/category-rule': library.category_rule, '/api/category-review': library.category_review,
                           '/api/job': library.start_job, '/api/classification/scope': library.classification_scope, '/api/analysis/edit': library.analysis_edit,
                           '/api/preferences': library.preferences, '/api/link/inspect': library.inspect_link,
                           '/api/link/import': library.import_link, '/api/similar': library.similar,
                           '/api/maintenance': library.maintenance.start, '/api/backup/settings': library.maintenance.configure,
                           '/api/backup/download': library.maintenance.download_ticket,
                           '/api/duplicates/resolve': library.maintenance.resolve_duplicate,
                           '/api/release/check': library.maintenance.release, '/api/pilot': library.maintenance.pilot,
                           '/api/diagnostics': lambda _: library.maintenance.report()}
                if path not in methods:
                    self.json_response({"error": "지원하지 않는 요청입니다."}, 404)
                    return
                result = methods[path](metadata)
                self.json_response(result)
                if path in ('/api/link/import', '/api/links') and not result.get('duplicate'):
                    library.queue_import(result['id'])
        except (ArchiveError, json.JSONDecodeError, ValueError, TypeError) as exc:
            message = str(exc) if isinstance(exc, (ArchiveError, analysis_tools.AnalysisError)) else "입력 형식이 올바르지 않습니다."
            self.json_response({"error": message}, 400)
        except (OSError, sqlite3.Error) as exc:
            self.server.library.maintenance.request_errors.append(type(exc).__name__)
            self.json_response({"error": "저장하지 못했습니다. 저장 공간과 파일 사용 상태를 확인하세요."}, 500)


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False

    def service_actions(self):
        self.library.maintenance.tick()

    def shutdown(self):
        self.library.maintenance.stopping = True
        super().shutdown()

    def server_close(self):
        if hasattr(self, 'library'):
            maintenance = self.library.maintenance
            with maintenance.lock:
                maintenance.stopping = True
                worker = maintenance.worker
            if worker:
                worker.join()
        super().server_close()

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(data, port=18765):
    server = LocalServer(("127.0.0.1", port), Handler)
    try:
        server.library = Library(data)
    except Exception:
        server.server_close()
        raise
    server.token = secrets.token_urlsafe(32)
    return server


def main():
    parser = argparse.ArgumentParser(description="RefArchive 로컬 자료함")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--data", type=Path, default=default_data())
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    try:
        server = make_server(args.data, args.port)
    except OSError:
        address = f"http://127.0.0.1:{args.port}"
        try:
            with urlopen(address + "/api/health", timeout=1) as response:
                existing = json.load(response)
                if isinstance(existing, dict) and existing.get("app") == "RefArchive" and existing.get('storage') == str(args.data.resolve() / 'library'):
                    print(f"RefArchive가 이미 실행 중입니다: {address}")
                    if args.open:
                        webbrowser.open(address)
                    return 0
        except (URLError, TimeoutError, ValueError):
            pass
        print("실행 포트가 사용 중입니다. 이미 실행한 창을 확인하거나 --port 옵션으로 다른 포트를 지정하세요.")
        return 1
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"RefArchive: {address}\n자료 위치: {server.library.root}\n종료: Ctrl+C")
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
