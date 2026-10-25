"""Bounded MCP byte transfer and native PDF rendering, without opening UI."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'plugins/reference-native-test'
spec = importlib.util.spec_from_file_location('inline_probe', ROOT / 'server.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def make_pdf(path):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>']
    for number in (5, 6):
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 7 0 R >> >> /Contents {number} 0 R >>'.encode())
    for number in (1, 2):
        content = f'BT /F1 24 Tf 30 100 Td (Reference page {number}) Tj ET'.encode()
        objects.append(b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{index} 0 obj\n'.encode() + value + b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(data)


class InlinePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.service = probe.PreviewService(ROOT)
        self.file = self.folder / '中文 视频.mov'
        self.data = bytes(range(256)) * 1024
        self.file.write_bytes(self.data)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_chunks_reconstruct_exact_bytes_and_bound_requests(self):
        self.file.write_bytes(self.data * 5)
        preview = self.service.prepare(str(self.file), 'A')
        self.assertEqual(preview['transport'], 'mcp-chunks')
        self.assertNotIn('url', preview)
        chunks = []
        for offset in range(0, preview['byteSize'], preview['chunkSize']):
            chunk = self.service.read_chunk(preview['leaseId'], 'A', offset)
            content = base64.b64decode(chunk['data'])
            self.assertEqual(chunk['offset'], offset)
            self.assertEqual(chunk['byteLength'], len(content))
            self.assertLessEqual(len(content), 512 * 1024)
            chunks.append(content)
        self.assertEqual(b''.join(chunks), self.data * 5)
        for bad in (-1, 1.5, True, preview['byteSize'] + 1):
            with self.assertRaises(ValueError):
                self.service.read_chunk(preview['leaseId'], 'A', bad)

    def test_capability_owner_and_removal(self):
        preview = self.service.prepare(str(self.file), 'A')
        lease = preview['leaseId']
        for wrong_lease, owner in [(lease + 'wrong', 'A'), (lease, 'B'), (lease, None)]:
            with self.assertRaises(ValueError):
                self.service.read_chunk(wrong_lease, owner, 0)
        self.service.release(lease, 'B')
        self.service.retain([], 'B')
        self.assertTrue(self.service.read_chunk(lease, 'A', 0)['data'])
        self.service.retain([], 'A')
        with self.assertRaises(ValueError):
            self.service.read_chunk(lease, 'A', 0)
        lease = self.service.prepare(str(self.file), 'A')['leaseId']
        self.file.write_bytes(b'replaced')
        with self.assertRaisesRegex(ValueError, '改变'):
            self.service.read_chunk(lease, 'A', 0)

    def test_empty_text_and_expiry(self):
        self.file.write_bytes(b'')
        preview = self.service.prepare(str(self.file), 'A')
        self.assertEqual(self.service.read_chunk(preview['leaseId'], 'A', 0)['byteLength'], 0)
        self.service.leases[preview['leaseId']]['expires'] = 0
        with self.assertRaises(ValueError):
            self.service.read_chunk(preview['leaseId'], 'A', 0)
        text = self.folder / 'note.txt'
        text.write_text('<script>not markup</script>' + 'a' * 300000)
        output = self.service.prepare(str(text), 'A')
        self.assertEqual(output['kind'], 'text')
        self.assertTrue(output['truncated'])
        self.assertEqual(len(output['text']), 256 * 1024)
        self.assertNotIn('data', output)

    def test_large_files_have_no_total_size_gate_and_reads_stay_bounded(self):
        for size in (64 * 1024 * 1024 + 1, 3 * 1024 * 1024 * 1024 + 7):
            with self.subTest(size=size):
                with self.file.open('wb') as stream:
                    stream.seek(size - 4)
                    stream.write(b'TAIL')
                preview = self.service.prepare(str(self.file), 'A')
                self.assertEqual(preview['byteSize'], size)
                first = self.service.read_chunk(preview['leaseId'], 'A', 0)
                self.assertEqual(first['byteLength'], 512 * 1024)
                tail = self.service.read_chunk(preview['leaseId'], 'A', size - 4)
                self.assertEqual(base64.b64decode(tail['data']), b'TAIL')
                self.service.release(preview['leaseId'], 'A')

    def test_html_keeps_full_page_and_embedded_images_without_scripts_or_navigation(self):
        page = self.folder / 'article.html'
        tail = '<p id="end">文章结尾 &amp; 完整内容</p>'
        page.write_text('<!doctype html><html><head><style>body{color:#123}</style>'
                        '<meta http-equiv="refresh" content="0;url=https://example.invalid">'
                        '</head><body onload="alert(1)"><script>parent.postMessage("bad", "*")</script>'
                        '<iframe srcdoc="bad"></iframe><a href="https://example.invalid">source</a>'
                        '<a href="#end">结尾</a><img src="data:image/png;base64,AAAA">'
                        '<img src="https://example.invalid/tracker"><p>' + '正文' * 200000 + '</p>' + tail + '</body></html>', encoding='utf-8')
        original = page.read_bytes()
        preview = self.service.prepare(str(page), 'A')
        self.assertEqual(preview['kind'], 'html')
        self.assertEqual(preview['transport'], 'mcp-chunks')
        self.assertNotIn('text', preview)
        body = b''.join(base64.b64decode(self.service.read_chunk(preview['leaseId'], 'A', offset)['data'])
                        for offset in range(0, preview['byteSize'], preview['chunkSize'])).decode()
        self.assertIn(tail, body)
        self.assertIn('body{color:#123}', body)
        self.assertIn('src="data:image/png;base64,AAAA"', body)
        self.assertIn('href="about:srcdoc#end"', body)
        for unwanted in ('<script', '<iframe', 'onload=', 'https://example.invalid', 'http-equiv="refresh"'):
            self.assertNotIn(unwanted, body)
        self.assertIn('Content-Security-Policy', body)
        self.assertIn("script-src 'none'", body)
        temporary = Path(self.service.leases[preview['leaseId']]['target'])
        self.service.release(preview['leaseId'], 'A')
        self.assertFalse(temporary.parent.exists())
        self.assertEqual(page.read_bytes(), original)

    def test_html_declared_encoding_and_htm_extension(self):
        page = self.folder / '中文.htm'
        page.write_bytes('<meta charset="gb18030"><h1>完整中文页面</h1>'.encode('gb18030'))
        preview = self.service.prepare(str(page), 'A')
        body = base64.b64decode(self.service.read_chunk(preview['leaseId'], 'A', 0)['data']).decode('utf-8')
        self.assertEqual(preview['kind'], 'html')
        self.assertIn('<h1>完整中文页面</h1>', body)

    def test_real_pdf_pages_and_cache_cleanup(self):
        pdf = self.folder / 'reference.pdf'
        make_pdf(pdf)
        before = pdf.read_bytes()
        previews = [self.service.prepare(str(pdf), 'A', page) for page in (1, 2)]
        for number, preview in enumerate(previews, 1):
            self.assertEqual((preview['page'], preview['pageCount']), (number, 2))
            body = base64.b64decode(self.service.read_chunk(preview['leaseId'], 'A', 0)['data'])
            self.assertEqual(preview['mime'], 'image/png')
            self.assertTrue(body.startswith(b'\x89PNG\r\n\x1a\n'))
            target = Path(self.service.leases[preview['leaseId']]['target'])
            self.service.release(preview['leaseId'], 'A')
            self.assertFalse(target.parent.exists())
        # A page past the end (the PDF shrank) shows the last page instead of failing.
        clamped = self.service.prepare(str(pdf), 'A', 3)
        self.assertEqual((clamped['page'], clamped['pageCount']), (2, 2))
        self.assertEqual(pdf.read_bytes(), before)

    def test_dispatch_scopes_paths_and_returns_bytes_only_to_widget(self):
        meta = {'threadId': 'fixture-inline-A'}
        with patch.object(probe, 'LOG', self.folder / 'log.jsonl'), patch.object(probe, 'LIST_DIR', self.folder / 'lists'), \
             patch.object(probe, 'PREVIEWS', self.service):
            def call(name, arguments, owner=meta):
                return probe.dispatch('tools/call', {'name': name, 'arguments': arguments, '_meta': owner})
            refused = call('prepare_reference_preview', {'path': str(self.file)})
            self.assertTrue(refused['isError'])
            self.assertEqual(refused['structuredContent']['error']['code'], 'not-listed')
            saved = call('save_reference_list', {'references': [{'path': str(self.file)}], 'baseRevision': 0})
            self.assertEqual(saved['structuredContent']['list']['status'], 'saved')
            self.assertTrue(call('prepare_reference_preview', {'path': str(self.file)}, {'threadId': 'B'})['isError'])
            output = call('prepare_reference_preview', {'path': str(self.file)})
            preview = output.pop('_meta')['referencePreview']
            self.assertNotIn(preview['leaseId'], json.dumps(output))
            output = call('read_reference_preview_chunk', {'leaseId': preview['leaseId'], 'offset': 0})
            chunk = output.pop('_meta')['referenceChunk']
            self.assertNotIn(chunk['data'], json.dumps(output))
            csp = probe.dispatch('resources/read', {'uri': probe.URI})['contents'][0]['_meta']['ui']['csp']
            self.assertEqual(csp['resourceDomains'], ['blob:', 'data:'])
            self.assertEqual(csp['frameDomains'], ['blob:'])
            self.assertEqual(csp['connectDomains'], [])
            call('save_reference_list', {'references': [], 'baseRevision': 1})
            self.assertTrue(call('read_reference_preview_chunk', {'leaseId': preview['leaseId'], 'offset': 0})['isError'])


if __name__ == '__main__':
    unittest.main()
