import io
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pypdfium2 as pdfium
import analysis_tools
import launcher
import runtime_paths


class DistributionTests(unittest.TestCase):
    def test_frozen_data_and_model_never_use_bundle_or_cwd(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.frozen', True, create=True), patch.dict('os.environ', {'LOCALAPPDATA': directory}):
            self.assertEqual(runtime_paths.default_data(), Path(directory) / 'RefArchive' / 'data')
            self.assertEqual(runtime_paths.model_path(), Path(directory) / 'RefArchive' / 'models' / 'clip-vision-uint8.onnx')
        self.assertEqual(runtime_paths.default_data(), runtime_paths.ROOT / 'data')

    def test_existing_server_must_match_library_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory).resolve()
            (data / 'runtime.json').write_text('{"port":18766}', encoding='utf-8')
            def health(**updates):
                result = {'app': 'RefArchive', 'version': 2, 'appVersion': runtime_paths.VERSION, 'storage': str(data / 'library')}
                result.update(updates)
                return patch('launcher.urlopen', return_value=io.BytesIO(json.dumps(result).encode()))
            with health():
                self.assertEqual(launcher.existing_address(data), 'http://127.0.0.1:18766')
            with health(storage='other'), self.assertRaises(ValueError):
                launcher.existing_address(data)
            with health(appVersion='older'), self.assertRaises(RuntimeError):
                launcher.existing_address(data)
            (data / 'runtime.json').write_text('{"port":"https://other"}', encoding='utf-8')
            with self.assertRaises(ValueError):
                launcher.existing_address(data)

    def test_launcher_never_opens_data_already_used_by_development_server(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory).resolve()
            health = {'app': 'RefArchive', 'version': 2, 'storage': str(data / 'library')}
            args = argparse.Namespace(data=data, port=None, no_browser=True)
            with patch('archive.make_server', side_effect=OSError(10048, 'address in use')) as make_server, patch('launcher.urlopen', return_value=io.BytesIO(json.dumps(health).encode())):
                with self.assertRaisesRegex(RuntimeError, '같은 자료함'):
                    launcher.run(args)
            self.assertEqual(make_server.call_count, 1)

    def test_pdf_page_limit_and_scan_ocr(self):
        with pdfium.PdfDocument.new() as document:
            document.new_page(612, 792).close()
            output = io.BytesIO()
            document.save(output)
            content = output.getvalue()
            count, thumbnail = analysis_tools.pdf_info(content)
            self.assertEqual(count, 1)
            self.assertTrue(thumbnail.startswith(b'\xff\xd8'))
            with patch('analysis_tools.ocr', return_value='스캔 문서 테스트') as ocr:
                pages = analysis_tools.extract_pdf(content, True)
            self.assertEqual(pages, [{'number': 1, 'text': '스캔 문서 테스트', 'method': 'ocr'}])
            self.assertTrue(ocr.call_args.args[0].startswith(b'\x89PNG'))
            for _ in range(300):
                document.new_page(10, 10).close()
            output = io.BytesIO()
            document.save(output)
            with self.assertRaises(analysis_tools.AnalysisError):
                analysis_tools.pdf_info(output.getvalue())
