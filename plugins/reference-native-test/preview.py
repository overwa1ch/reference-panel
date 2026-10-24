"""Bounded local preview bytes delivered only through the MCP widget channel."""
import base64
import codecs
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time


VIDEO = {'.mp4', '.mov', '.m4v', '.webm', '.mkv'}
# Containers Chromium's <video> cannot demux; say so before transferring bytes.
UNPLAYABLE_VIDEO = {'.avi', '.mpg', '.mpeg', '.mts', '.m2ts', '.3gp', '.wmv', '.flv'}
AUDIO = {'.mp3', '.m4a', '.wav', '.ogg', '.aac', '.flac'}
IMAGE = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.avif', '.bmp'}
RENDERED_IMAGE = {'.heic', '.heif', '.tif', '.tiff'}
TEXT = {'.txt', '.md', '.markdown', '.json', '.csv', '.tsv', '.log', '.yaml', '.yml', '.toml', '.ini', '.conf',
        '.xml', '.html', '.htm', '.srt', '.vtt', '.ass', '.sh', '.py', '.js', '.ts', '.jsx', '.tsx', '.css', '.swift'}
# textutil converts these locally without loading linked resources.
DOCUMENT = {'.docx', '.doc', '.rtf', '.rtfd', '.odt'}
THUMB_EDGE = 480
THUMB_ALPHA = {'.png', '.gif', '.webp', '.svg'}
THUMB_BATCH = 12
THUMB_CACHE_LIMIT = 800
TEXT_BYTES = 256 * 1024
CHUNK_BYTES = 512 * 1024
SF_DATALESS = 0x40000000
# Blocked reads (slow volumes) must not occupy every server worker.
CHUNK_SLOTS = threading.BoundedSemaphore(4)


class ToolError(ValueError):
    """A user-facing failure; `code` lets the panel choose the right state."""

    def __init__(self, message, code='error'):
        super().__init__(message)
        self.code = code


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def decode_text(data, truncated):
    """Decode a bounded prefix: BOM first, then strict UTF-8, then GB18030."""
    boms = [(codecs.BOM_UTF32_LE, 'utf-32-le'), (codecs.BOM_UTF32_BE, 'utf-32-be'), (codecs.BOM_UTF8, 'utf-8'),
            (codecs.BOM_UTF16_LE, 'utf-16-le'), (codecs.BOM_UTF16_BE, 'utf-16-be')]
    for bom, encoding in boms:
        if data.startswith(bom):
            body = data[len(bom):]
            return codecs.getincrementaldecoder(encoding)(errors='replace').decode(body, final=not truncated), encoding
    for encoding in ('utf-8', 'gb18030'):
        try:
            # final=False drops a multi-byte character cut by the size limit.
            return codecs.getincrementaldecoder(encoding)(errors='strict').decode(data, final=not truncated), encoding
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace'), 'utf-8'


class PreviewService:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.leases = {}

    def prepare(self, path, owner, page=1):
        if not owner or not isinstance(path, str) or not os.path.isabs(path) or '\x00' in path:
            raise ToolError('参考文件路径无效')
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ToolError('页码无效')
        source = Path(path)
        try:
            info = source.stat()
        except (FileNotFoundError, NotADirectoryError):
            raise ToolError('原文件已移动或删除', 'missing') from None
        except PermissionError:
            raise ToolError('没有读取此文件的权限', 'permission') from None
        except OSError:
            raise ToolError('无法读取原文件', 'unreadable') from None
        ext = source.suffix.lower()
        common = {'name': source.name, 'size': info.st_size}
        if getattr(info, 'st_flags', 0) & SF_DATALESS:
            return {**common, 'kind': 'unsupported', 'reason': 'icloud',
                    'message': '文件仍在 iCloud 中，尚未下载到本机。可用默认应用打开，下载后再预览。'}
        if ext in DOCUMENT:
            return {**common, **self._document_text(source)}
        if stat.S_ISDIR(info.st_mode):
            return {**common, 'kind': 'unsupported', 'reason': 'package', 'message': '文件包无法在面板内预览，可用默认应用打开。'}
        if not stat.S_ISREG(info.st_mode):
            raise ToolError('只能预览普通文件', 'unsupported')
        if ext in TEXT:
            try:
                with source.open('rb') as stream:
                    data = stream.read(TEXT_BYTES + 1)
            except OSError:
                raise ToolError('无法读取原文件', 'unreadable') from None
            truncated = len(data) > TEXT_BYTES
            text, encoding = decode_text(data[:TEXT_BYTES], truncated)
            return {**common, 'kind': 'text', 'text': text, 'truncated': truncated, 'encoding': encoding}
        if ext in UNPLAYABLE_VIDEO:
            return {**common, 'kind': 'unsupported', 'reason': 'codec',
                    'message': f'面板无法播放 {ext[1:].upper()} 视频，可用默认应用打开。'}
        directory = None
        target = source
        mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if ext in VIDEO:
            kind = 'video'
            # Chromium decodes the container from its bytes; MOV/H.264 shares
            # the MP4 demuxer even when video/quicktime isn't advertised.
            if ext in {'.mov', '.m4v'}:
                mime = 'video/mp4'
        elif ext in AUDIO:
            kind = 'audio'
        elif ext in IMAGE:
            kind = 'image'
        elif ext == '.pdf' or ext in RENDERED_IMAGE:
            kind = 'pdf' if ext == '.pdf' else 'image'
            directory = tempfile.mkdtemp(prefix='codex-reference-preview-')
            target = Path(directory) / 'page.png'
            try:
                common.update(self._render(kind, source, target, page))
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            mime = 'image/png'
        else:
            return {**common, 'kind': 'unsupported', 'reason': 'format', 'message': '此格式暂不支持面板预览，可用默认应用打开。'}
        try:
            byte_size = target.stat().st_size
            lease_id = secrets.token_urlsafe(32)
            with self.lock:
                self._expire()
                while len(self.leases) >= 64:
                    self._remove(next(iter(self.leases)))
                self.leases[lease_id] = {'owner': owner, 'source': str(source), 'signature': signature(info),
                                         'target': str(target), 'mime': mime, 'size': byte_size, 'directory': directory, 'expires': time.monotonic() + 3600}
            return {**common, 'kind': kind, 'transport': 'mcp-chunks', 'mime': mime,
                    'byteSize': byte_size, 'chunkSize': CHUNK_BYTES, 'leaseId': lease_id}
        except Exception:
            if directory:
                shutil.rmtree(directory, ignore_errors=True)
            raise

    def _render(self, kind, source, target, page):
        try:
            rendered = subprocess.run([str(self.root / 'bin/reference-render'), kind, str(source), str(target), str(page)],
                                      capture_output=True, text=True, timeout=20, check=False)
        except subprocess.TimeoutExpired:
            raise ToolError('渲染超时，文件可能过大或仍在下载', 'timeout') from None
        except OSError:
            raise ToolError('本机渲染器未安装，请重新构建插件', 'renderer') from None
        if rendered.returncode == 7:
            raise ToolError('此 PDF 已加密，无法在面板内预览', 'locked')
        if rendered.returncode:
            raise ToolError('无法读取此 PDF，文件可能已损坏' if kind == 'pdf' else '无法读取此图片，文件可能已损坏', 'damaged')
        try:
            return json.loads(rendered.stdout)
        except ValueError:
            raise ToolError('本机渲染器返回了无效结果', 'renderer') from None

    def _document_text(self, source):
        try:
            converted = subprocess.run(['/usr/bin/textutil', '-convert', 'txt', '-stdout', '-encoding', 'UTF-8', str(source)],
                                       capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            raise ToolError('文档转换超时，可用默认应用打开', 'timeout') from None
        except OSError:
            raise ToolError('本机无法转换此文档', 'unsupported') from None
        if converted.returncode:
            return {'kind': 'unsupported', 'reason': 'format', 'message': '无法读取此文档的文字，可用默认应用打开。'}
        data = converted.stdout
        truncated = len(data) > TEXT_BYTES
        text, _ = decode_text(data[:TEXT_BYTES], truncated)
        return {'kind': 'text', 'document': True, 'text': text, 'truncated': truncated, 'encoding': 'utf-8'}

    def _remove(self, lease_id):
        lease = self.leases.pop(lease_id, None)
        if lease and lease['directory']:
            shutil.rmtree(lease['directory'], ignore_errors=True)

    def _expire(self):
        now = time.monotonic()
        for lease_id, lease in list(self.leases.items()):
            if lease['expires'] < now:
                self._remove(lease_id)

    def release(self, lease_id, owner):
        with self.lock:
            if self.leases.get(lease_id, {}).get('owner') == owner:
                self._remove(lease_id)

    def retain(self, paths, owner):
        with self.lock:
            for lease_id, lease in list(self.leases.items()):
                if lease['owner'] == owner and lease['source'] not in paths:
                    self._remove(lease_id)

    def read_chunk(self, lease_id, owner, offset):
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolError('预览读取位置无效')
        if not CHUNK_SLOTS.acquire(blocking=False):
            raise ToolError('预览仍在读取，请稍后重试', 'busy')
        try:
            return self._read_chunk(lease_id, owner, offset)
        finally:
            CHUNK_SLOTS.release()

    def _read_chunk(self, lease_id, owner, offset):
        with self.lock:
            self._expire()
            lease = self.leases.get(lease_id)
            if not owner or not lease or lease['owner'] != owner:
                raise ToolError('预览已关闭或过期，请重试', 'expired')
            lease['expires'] = time.monotonic() + 3600
            if offset > lease['size']:
                raise ToolError('预览读取位置超出文件')
        try:
            if signature(os.stat(lease['source'])) != lease['signature']:
                self.release(lease_id, owner)
                raise ToolError('原文件已经改变，请重新预览', 'changed')
            stream = open(lease['target'], 'rb')
        except OSError as error:
            self.release(lease_id, owner)
            raise ToolError('原文件已移动或无法读取', 'missing') from error
        with stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != lease['size'] or (not lease['directory'] and signature(info) != lease['signature']):
                raise ToolError('原文件已经改变，请重新预览', 'changed')
            stream.seek(offset)
            data = stream.read(CHUNK_BYTES)
        return {'offset': offset, 'byteLength': len(data), 'totalBytes': lease['size'],
                'data': base64.b64encode(data).decode('ascii')}

    def close(self):
        with self.lock:
            for lease_id in list(self.leases):
                self._remove(lease_id)


def thumbnail_kind(path):
    ext = Path(path).suffix.lower()
    if ext in VIDEO or ext in UNPLAYABLE_VIDEO:
        return 'video'
    if ext in AUDIO:
        return 'audio'
    return 'alpha' if ext in THUMB_ALPHA else 'other'


class Thumbnails:
    """Quick Look thumbnails and durations, cached by path, size and mtime."""

    def __init__(self, root, cache_dir):
        self.root = Path(root)
        self.cache_dir = cache_dir
        self.lock = threading.Lock()

    @staticmethod
    def key(path, info):
        return hashlib.sha256(f'1\0{path}\0{info.st_size}\0{info.st_mtime_ns}'.encode()).hexdigest()

    def _cached(self, key):
        try:
            meta = json.loads((self.cache_dir() / (key + '.json')).read_text())
        except (OSError, ValueError):
            return None
        if meta.get('status') == 'ok' and not (self.cache_dir() / meta.get('file', '')).is_file():
            return None
        return meta

    def get(self, paths):
        results, todo = {}, []
        for path in dict.fromkeys(paths):
            try:
                info = os.stat(path)
            except OSError:
                results[path] = {'status': 'missing'}
                continue
            # Reading an evicted iCloud file would start a download.
            if getattr(info, 'st_flags', 0) & SF_DATALESS:
                results[path] = {'status': 'icloud'}
                continue
            key = self.key(path, info)
            cached = self._cached(key)
            if cached is None:
                todo.append((path, key))
            else:
                results[path] = cached
        if todo:
            with self.lock:
                results.update(self._generate(todo))
        output = {}
        for path, meta in results.items():
            item = {k: meta[k] for k in ('status', 'mime', 'width', 'height', 'duration') if k in meta}
            if meta.get('status') == 'ok':
                try:
                    data = (self.cache_dir() / meta['file']).read_bytes()
                except OSError:
                    data = b''
                if data and len(data) <= 400 * 1024:
                    item['data'] = base64.b64encode(data).decode('ascii')
                else:
                    item['status'] = 'none'
            output[path] = item
        return output

    def _generate(self, todo):
        cache = self.cache_dir()
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        results = {}
        with tempfile.TemporaryDirectory(prefix='codex-reference-thumbs-') as folder:
            entries = [f'{thumbnail_kind(path)}\t{path}' for path, _ in todo]
            try:
                rendered = subprocess.run([str(self.root / 'bin/reference-render'), 'thumbs', str(THUMB_EDGE), folder, *entries],
                                          capture_output=True, text=True, timeout=min(60, 10 + 8 * len(todo)), check=False)
                items = json.loads(rendered.stdout) if rendered.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired, ValueError):
                items = None
            if not isinstance(items, list) or len(items) != len(todo):
                # Renderer missing or failed: answer without caching anything.
                return {path: {'status': 'none'} for path, _ in todo}
            for (path, key), item in zip(todo, items):
                meta = {k: item[k] for k in ('status', 'mime', 'width', 'height', 'duration') if k in item}
                if item.get('status') == 'ok':
                    source = Path(item.get('file', ''))
                    if source.parent != Path(folder) or not source.is_file():
                        meta = {'status': 'none'}
                    else:
                        name = key + source.suffix
                        shutil.move(str(source), cache / name)
                        os.chmod(cache / name, 0o600)
                        meta['file'] = name
                if meta.get('status') in ('ok', 'none'):
                    (cache / (key + '.json')).write_text(json.dumps(meta))
                results[path] = meta
        self._prune(cache)
        return results

    @staticmethod
    def _prune(cache):
        metas = sorted(cache.glob('*.json'), key=lambda p: p.stat().st_mtime)
        for meta in metas[:max(0, len(metas) - THUMB_CACHE_LIMIT)]:
            for path in cache.glob(meta.stem + '.*'):
                path.unlink(missing_ok=True)
