"""Local development host for the exact widget. This is not the Codex MCP host.

Usage: python3 verification/preview_browser_fixture.py /absolute/video.mov [host-context.json]
Only the named video and generated PDF/text/image fixtures are available. No picker,
Finder, external-app actions, upload, general file route, or external network.
The optional host context (for example one saved from a real snapshot) supplies
theme variables; the page's theme menu switches between it and the fallbacks.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from test_inline_preview import probe, make_pdf, ROOT


def main():
    import tempfile
    with tempfile.TemporaryDirectory(prefix='reference-browser-fixture-') as folder:
        folder = Path(folder)
        probe.LOG = folder / 'snapshots.jsonl'
        probe.LIST_DIR = folder / 'lists'
        probe.THUMB_DIR = folder / 'thumbnails'
        probe.PICKER.warm = lambda: None
        video = Path(sys.argv[1]).resolve(strict=True)
        host_context = json.loads(Path(sys.argv[2]).read_text()).get('hostContext', {}) if len(sys.argv) > 2 else {}
        pdf, note, gbk, subtitles = folder / 'Reference.pdf', folder / 'Notes.txt', folder / '中文笔记 GBK.txt', folder / 'Subtitles.srt'
        poster, moved = folder / 'Poster.png', folder / 'Moved clip.mov'
        make_pdf(pdf)
        note.write_text('Reference panel\n\nClick a file to preview it here.\n<script>This stays plain text.</script>')
        gbk.write_bytes('参考面板\n这是一份 GB18030 编码的中文笔记。\n'.encode('gb18030'))
        subtitles.write_text('1\n00:00:01,000 --> 00:00:03,000\n镜头一：推近\n')
        subprocess.run([str(ROOT / 'bin/reference-render'), 'pdf', str(pdf), str(poster), '1'], check=True, capture_output=True)
        # Optional extra real files, one absolute path per line.
        extra = [Path(line).resolve(strict=True) for line in os.environ.get('REFERENCE_FIXTURE_EXTRA', '').splitlines() if line.strip()]
        present = [video, *extra, pdf, poster, note, gbk, subtitles]
        paths = {str(path) for path in present + [moved]}
        references = [{'path': str(p), 'name': p.name, 'size': p.stat().st_size, 'exists': True} for p in present]
        references.append({'path': str(moved), 'name': moved.name, 'size': 1024, 'exists': True})
        meta = {'threadId': 'inline-browser-fixture'}
        probe.dispatch('tools/call', {'name': 'save_reference_list', 'arguments': {'references': references, 'baseRevision': 0}, '_meta': meta})
        token = '/' + secrets.token_urlsafe(32)
        page = '''<!doctype html><meta charset="utf-8"><title>Reference · 本地预览验证</title>
<style>body{margin:0;background:#e9e8e3;font:12px -apple-system,sans-serif;color:#555850}header{display:flex;gap:12px;align-items:center;padding:8px 16px}
iframe{display:block;border:0;width:min(100%,WIDTHpx);height:calc(100vh - 40px);margin:0 16px;background:#fff;box-shadow:0 0 0 1px #0002}</style>
<header>本地开发验证 · 与原生面板使用相同页面和后台<label>主题 <select id="theme"><option value="host">宿主变量</option><option value="light">浅色回退</option><option value="dark">深色回退</option></select></label></header>
<iframe title="参考" src="TOKEN/panel"></iframe>
<script>const frame=document.querySelector('iframe');const host=HOST;
function context(mode){return mode==='host'&&host.styles?{...host,theme:host.theme||'light'}:{theme:mode==='dark'?'dark':'light',styles:{variables:{}}};}
document.getElementById('theme').value=host.styles?'host':'light';
document.getElementById('theme').addEventListener('change',e=>frame.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/host-context-changed',params:context(e.target.value)},location.origin));
window.addEventListener('message',async e=>{
if(e.source!==frame.contentWindow||e.data?.jsonrpc!=='2.0'||e.data.id==null||e.data.method==null)return;
const m=e.data;try{let result;
if(m.method==='ui/initialize')result={hostInfo:{name:'local-fixture',version:'1'},hostContext:context(document.getElementById('theme').value),hostCapabilities:{},protocolVersion:'2026-01-26'};
else if(m.method==='tools/call'){const response=await fetch('TOKEN/rpc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(m.params)});result=await response.json();if(result.error)throw Error(result.error);}
else result={};frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result},location.origin);
}catch(error){frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,error:{message:error.message}},location.origin);}});</script>'''
        page = page.replace('TOKEN', token).replace('HOST', json.dumps(host_context)).replace('WIDTH', '440')

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def allowed(self):
                return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

            def send(self, body, mime, csp=None):
                encoded = body.encode()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Cache-Control', 'no-store')
                if csp:
                    self.send_header('Content-Security-Policy', csp)
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                if not self.allowed():
                    self.send_error(403)
                elif self.path == token:
                    self.send(page, 'text/html; charset=utf-8')
                elif self.path == token + '/panel':
                    # Re-read on each load so page edits show up on refresh.
                    text = probe.dispatch('resources/read', {'uri': probe.URI})['contents'][0]['text']
                    self.send(text, 'text/html; charset=utf-8',
                              "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src blob: data:; media-src blob: data:; frame-src blob:; connect-src 'none'; base-uri 'none'; form-action 'none'")
                else:
                    self.send_error(404)

            def do_POST(self):
                if not self.allowed() or self.path != token + '/rpc' or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}':
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 512000:
                        raise ValueError('Invalid size')
                    params = json.loads(self.rfile.read(length))
                    name = params.get('name')
                    if name not in {'read_probe_context', 'save_probe_snapshot', 'save_reference_list', 'prepare_reference_preview',
                                    'release_reference_preview', 'read_reference_preview_chunk', 'read_reference_thumbnails'}:
                        raise ValueError('Unavailable in fixture')
                    args = params.get('arguments') or {}
                    if name == 'read_reference_thumbnails' and any(p not in paths for p in args.get('paths', [])):
                        raise ValueError('Not a fixture')
                    if name == 'prepare_reference_preview' and args.get('path') not in paths:
                        raise ValueError('Not a fixture')
                    if name == 'save_reference_list' and any(f.get('path') not in paths for f in args.get('references', [])):
                        raise ValueError('Not a fixture')
                    if name == 'save_probe_snapshot' and 'references' in args.get('snapshot', {}):
                        raise ValueError('Not a fixture')
                    output = probe.dispatch('tools/call', {**params, '_meta': meta})
                except Exception as error:
                    output = {'error': str(error)}
                self.send(json.dumps(output), 'application/json')

        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        print(f'http://127.0.0.1:{http.server_port}{token}', flush=True)
        try:
            http.serve_forever()
        finally:
            http.server_close()
            probe.PREVIEWS.close()


if __name__ == '__main__':
    main()
