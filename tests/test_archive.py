import hashlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile

from PIL import Image

from archive import ArchiveError, Library, make_server


def picture(color="red", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format)
    return output.getvalue()


def multipart(content, metadata):
    boundary = "refarchive-test-boundary"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()
            + json.dumps(metadata, ensure_ascii=False).encode()
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="test.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
            + content + f"\r\n--{boundary}--\r\n".encode())
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temporary.name) / "data")

    def tearDown(self):
        self.temporary.cleanup()

    def add(self, color="red", **metadata):
        return self.library.add_image(picture(color), {"filename": "참고.png", **metadata})["id"]

    def test_original_duplicate_and_multi_category(self):
        item_id = self.add(tags=["갑옷"], categories=["의상", "인체"])
        duplicate = self.library.add_image(picture(), {"filename": "다른 이름.jpg", "tags": ["전신"], "categories": ["연습"]})
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["id"], item_id)
        item = self.library.state()["items"][0]
        self.assertEqual(item["tags"], ["갑옷", "전신"])
        self.assertEqual(set(item["categories"]), {"의상", "인체", "연습"})
        self.assertEqual(len(self.library.state()["items"]), 1)
        self.assertEqual(self.library.media(item_id)[0], picture())
        self.assertEqual(item["hash"], hashlib.sha256(picture()).hexdigest())

    def test_ten_examples_threshold_is_not_storage_limit(self):
        ids = [self.add((index, 20, 30), tags=["갑옷"]) for index in range(9)]
        self.assertFalse(self.library.state()["tags"][0]["eligible"])
        ids.append(self.add((9, 20, 30), tags=["갑옷"]))
        self.assertTrue(self.library.state()["tags"][0]["eligible"])
        self.add((10, 20, 30), tags=["갑옷"])
        self.assertEqual(self.library.state()["tags"][0]["examples"], 11)
        self.library.add_link({"source": "https://example.com", "tags": ["갑옷"]})
        self.assertEqual(self.library.state()["tags"][0]["examples"], 11)
        self.library.batch({"ids": ids[:2], "action": "trash"})
        self.assertFalse(self.library.state()["tags"][0]["eligible"])
        self.library.batch({"ids": ids[:2], "action": "restore"})
        self.assertTrue(self.library.state()["tags"][0]["eligible"])

    def test_taxonomy_merge_and_delete_preserves_files(self):
        first = self.add(tags=["손", "손자료"], categories=["인체"])
        second = self.add("blue", tags=["손자료"])
        self.library.taxonomy({"kind": "tags", "name": "손자료", "target": "손", "action": "merge"})
        tag = self.library.state()["tags"][0]
        self.assertEqual((tag["name"], tag["examples"]), ("손", 2))
        self.library.taxonomy({"kind": "categories", "name": "인체", "action": "delete"})
        self.assertEqual(self.library.state()["items"][0]["categories"], [])
        self.assertEqual(self.library.media(first)[0], picture())
        self.assertEqual(self.library.media(second)[0], picture("blue"))

    def test_edit_replace_batch_and_transaction_rollback(self):
        item_id = self.add(tags=["이전"], categories=["인체"])
        self.library.update({"id": item_id, "title": "새 제목", "tags": ["손"], "categories": [], "note": "구도 설명"})
        self.library.batch({"ids": [item_id], "action": "add", "tags": ["역광"], "categories": ["연습"]})
        self.library.batch({"ids": [item_id], "action": "remove", "tags": ["손"]})
        item = self.library.state()["items"][0]
        self.assertEqual((item["title"], item["note"], item["tags"], item["categories"]), ("새 제목", "구도 설명", ["역광"], ["연습"]))
        with self.assertRaises(ArchiveError):
            self.library.batch({"ids": [item_id, "f" * 32], "action": "trash"})
        self.assertFalse(self.library.state()["items"][0]["trashed"])

    def test_invalid_files_urls_and_path_traversal(self):
        with self.assertRaises(ArchiveError):
            self.library.add_image(b"<html>not an image</html>", {"filename": "image.png"})
        with self.assertRaises(ArchiveError):
            self.library.add_image(picture(format="GIF"), {"filename": "image.png"})
        for url in ("javascript:alert(1)", "file:///C:/Windows", "https://name:password@example.com",
                    "https://example.com:bad", "https://example.com:99999", "https://bad host", "http://[bad]", "https://%zz"):
            with self.assertRaises(ArchiveError):
                self.library.add_link({"source": url})
        with self.assertRaises(ArchiveError):
            self.library.media("../../archive.sqlite3")
        self.assertEqual(self.library.state()["items"], [])

    def test_deleted_duplicate_requires_restore(self):
        item_id = self.add()
        self.library.batch({"ids": [item_id], "action": "trash"})
        with self.assertRaisesRegex(ArchiveError, "휴지통"):
            self.add()
        self.assertEqual(self.library.media(item_id)[0], picture())
        self.library.batch({"ids": [item_id], "action": "restore"})
        self.assertEqual(self.add(), item_id)

    def test_backup_restore_into_other_directory_and_safety_backup(self):
        item_id = self.add(tags=["갑옷", "전신"], categories=["의상"], source="https://example.com/artist", note="원본 메모")
        self.library.batch({"ids": [item_id], "action": "favorite"})
        link = self.library.add_link({"title": "문서", "source": "https://example.com/docs", "tags": ["강의"]})["id"]
        self.library.batch({"ids": [link], "action": "trash"})
        backup = io.BytesIO()
        self.library.backup(backup)
        other = Library(Path(self.temporary.name) / "other")
        old_id = other.add_image(picture("blue"), {"filename": "이전.png"})["id"]
        result = other.restore(backup.getvalue())
        self.assertEqual(result["items"], 2)
        self.assertEqual(other.state()["items"], self.library.state()["items"])
        self.assertEqual(other.media(item_id)[0], picture())
        safety = Path(result["backup"])
        self.assertTrue(safety.is_file())
        recovered = Library(Path(self.temporary.name) / "recovered")
        recovered.restore(safety.read_bytes())
        self.assertEqual(recovered.state()["items"][0]["id"], old_id)
        self.assertEqual(recovered.media(old_id)[0], picture("blue"))
        reopened = Library(other.data)
        self.assertEqual(reopened.state()["items"], other.state()["items"])

    def test_corrupted_restore_leaves_current_archive_unchanged(self):
        item_id = self.add()
        backup = io.BytesIO()
        self.library.backup(backup)
        original = backup.getvalue()
        for corruption in ("traversal", "checksum", "missing", "trigger", "database", "manifest"):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as destination:
                for name in source.namelist():
                    data = source.read(name)
                    if corruption == "missing" and name.startswith("thumbs/"):
                        continue
                    if corruption == "checksum" and name.startswith("files/"):
                        data = picture("blue")
                    if corruption == "database" and name == "archive.sqlite3":
                        data = b"broken database"
                    if corruption == "manifest" and name == "manifest.json":
                        data = b"null"
                    if corruption == "trigger" and name == "archive.sqlite3":
                        database = Path(self.temporary.name) / "tampered.sqlite3"
                        database.write_bytes(data)
                        connection = sqlite3.connect(database)
                        connection.execute("CREATE TRIGGER corrupt AFTER INSERT ON tags BEGIN DELETE FROM items; END;")
                        connection.commit()
                        connection.close()
                        data = database.read_bytes()
                    destination.writestr(name, data)
                if corruption == "traversal":
                    destination.writestr("../outside.txt", "bad")
            with self.subTest(corruption=corruption), self.assertRaises(ArchiveError):
                self.library.restore(output.getvalue())
            self.assertEqual(self.library.state()["items"][0]["id"], item_id)
            self.assertEqual(self.library.media(item_id)[0], picture())
        self.assertFalse((self.library.data / "outside.txt").exists())

    def test_backup_missing_original_is_an_error(self):
        self.add()
        item = self.library.state()["items"][0]
        (self.library.root / item["path"]).unlink()
        with self.assertRaisesRegex(ArchiveError, "누락"):
            self.library.backup(io.BytesIO())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temporary.name) / "data", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, method, path, content=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=content, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def test_http_upload_edit_backup_restore_and_guards(self):
        status, headers, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), page)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("POST", "/api/links", b"{}")[0], 403)
        authorized = {"X-RefArchive-Token": self.server.token}
        self.assertEqual(self.request("POST", "/api/links", b"{}", {**authorized, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        upload, upload_headers = multipart(picture(), {"filename": "테스트.png", "tags": ["손"], "note": "가" * 20000})
        status, _, result = self.request("POST", "/api/images", upload, {**authorized, **upload_headers})
        self.assertEqual(status, 201)
        item_id = json.loads(result)["id"]
        self.assertEqual(self.request("GET", "/media/" + item_id)[2], picture())
        body = json.dumps({"id": item_id, "title": "검색할 제목", "tags": ["갑옷"], "categories": ["의상"], "note": "메모"}).encode()
        self.assertEqual(self.request("POST", "/api/update", body, authorized)[0], 200)
        status, _, backup = self.request("POST", "/api/backup", b"{}", authorized)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("POST", "/api/restore", backup, authorized)[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/state")[2])["items"][0]["title"], "검색할 제목")
        self.assertEqual(self.request("POST", "/api/update", b"null", authorized)[0], 400)
        upload, upload_headers = multipart(picture(), None)
        self.assertEqual(self.request("POST", "/api/images", upload, {**authorized, **upload_headers})[0], 400)
        self.assertEqual(self.request("GET", "/../../archive.py")[0], 404)

    def test_second_server_cannot_bind_same_port(self):
        with self.assertRaises(OSError):
            make_server(Path(self.temporary.name) / "second", self.server.server_port)
        self.assertFalse((Path(self.temporary.name) / "second").exists())

    def test_category_rule_review_api_validation_and_authentication(self):
        authorized = {'X-RefArchive-Token': self.server.token}
        for path in ('/api/category-rule', '/api/category-review'):
            self.assertEqual(self.request('POST', path, b'{}')[0], 403)
            self.assertEqual(self.request('POST', path, b'{}', authorized)[0], 400)
        self.server.library.taxonomy({'kind': 'categories', 'name': '회의'})
        rule = {'mode': 'suggest', 'description': '결정 사항', 'include': ['회의록'], 'exclude': ['초안']}
        body = json.dumps({'name': '회의', 'rule': rule}).encode()
        self.assertEqual(self.request('POST', '/api/category-rule', body, authorized)[0], 200)
        key = self.server.library.add_file('회의 결과'.encode(), {'filename': '회의록.txt'})['id']
        self.server.library.classify(key)
        body = json.dumps({'id': key, 'name': '회의', 'action': 'accept'}).encode()
        self.assertEqual(self.request('POST', '/api/category-review', body, authorized)[0], 200)
        item = self.server.library.state()['items'][0]
        self.assertEqual(item['categories'], ['회의'])
        self.assertEqual(item['autoCategories'], [])

    def test_offline_html_guides_and_encoded_document_links(self):
        from urllib.parse import quote
        for path in ('/guide', '/' + quote('사용설명서.html'), '/distribution', '/' + quote('배포안내.html')):
            status, headers, content = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'text/html; charset=utf-8')
            self.assertIn(b'<article>', content)
            self.assertIn(b'guide.css', content)
            self.assertNotIn(b'<script', content)
        status, headers, content = self.request('GET', '/guide.css')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'text/css; charset=utf-8')
        self.assertIn(b'@media', content)
        self.assertEqual(self.request('GET', '/docs/user-guide.md')[0], 404)


if __name__ == "__main__":
    unittest.main()
