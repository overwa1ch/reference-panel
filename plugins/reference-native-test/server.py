#!/usr/bin/env python3
"""Local MCP reference panel. System preview is launched only on a user click."""
import datetime
import atexit
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import queue
import stat
import subprocess
import sys
import threading
import time
import uuid

VERSION = "0.7.2"
URI = "ui://reference-test/panel-0.7.2.html"
MIME = "text/html;profile=mcp-app"
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from preview import PreviewService, THUMB_BATCH, Thumbnails, ToolError
from project_scope import ProjectScopes

PREVIEWS = PreviewService(ROOT)
atexit.register(PREVIEWS.close)
INSTANCE = str(uuid.uuid4())
# Everything this plugin keeps lives in one per-user folder, outside any
# project and the plugin cache, so moving or reinstalling keeps it.
STATE_DIR = Path(os.environ.get("REFERENCE_STATE_DIR") or Path.home() / "Library/Application Support/codex-reference-panel")
LOG = Path(os.environ.get("REFERENCE_PROBE_LOG") or STATE_DIR / "diagnostics/native-panel.jsonl")
PICKER_LOG = LOG.with_name("file-picker.jsonl")
ACTION_LOG = LOG.with_name("file-actions.jsonl")
LIST_DIR = STATE_DIR / "lists"
THUMB_DIR = STATE_DIR / "thumbnails"
THUMBNAILS = Thumbnails(ROOT, lambda: THUMB_DIR)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp"}
SECRET_KEY = re.compile(r"token|secret|password|authorization|cookie|credential|api.?key", re.I)
REFERENCE_KEYS = {"name": str, "path": str, "source": str, "addedAt": str, "size": int, "lastModified": int,
                  "exists": (bool, type(None)), "isFile": (bool, type(None)), "isPackage": bool}
MAX_REFERENCES = 2000
MAX_BATCH = 150
# Hosts that send no conversation id (only Codex does today) share one list.
SHARED_OWNER = "shared:no-conversation-id"
PROJECTS = ProjectScopes()
DRAG_CAPTURES = {}
DRAG_TTL_SECONDS = 45
LOG_LOCK = threading.Lock()
DRAG_LOCK = threading.Lock()


class MethodNotFound(Exception):
    pass


class NativePicker:
    """Prepare AppKit at panel opening and reuse one private native child."""

    def __init__(self):
        self.process = None
        self.responses = queue.Queue()
        self.ready = threading.Event()
        self.error = None
        self.lifecycle_lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.stopping = False
        self.selecting = False
        self.cancel_pending = False

    def warm(self):
        with self.lifecycle_lock:
            return self._warm()

    def _warm(self):
        if self.stopping:
            self.error = "参考服务正在关闭"
            return False
        if self.process is not None and self.process.poll() is None:
            return True
        self.responses = queue.Queue()
        self.ready = threading.Event()
        self.error = None
        try:
            self.process = subprocess.Popen(
                [str(ROOT / "bin/reference-picker")], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,
            )
        except (FileNotFoundError, PermissionError):
            self.error = "本机选择器未安装或插件已更新，请重新打开 Codex"
            return False
        except OSError:
            self.error = "本机选择器无法启动"
            return False
        process, responses, ready = self.process, self.responses, self.ready

        def read():
            try:
                for line in process.stdout:
                    item = json.loads(line)
                    if item.get("status") == "ready":
                        ready.set()
                    else:
                        responses.put(item)
            except (OSError, ValueError) as error:
                responses.put({"status": "error", "message": str(error)})
            finally:
                responses.put({"status": "error", "message": "文件选择器已退出，请重试"})
                ready.clear()
                process.stdout.close()

        threading.Thread(target=read, daemon=True).start()
        return True

    def _wait_ready(self, process, ready):
        # A helper that dies before 'ready' fails at once instead of after 5 s.
        deadline = time.monotonic() + 5
        while not ready.wait(0.05):
            if process.poll() is not None:
                self.close()
                raise OSError(f"本机助手启动失败（退出码 {process.returncode}）")
            if time.monotonic() > deadline:
                self.close()
                raise TimeoutError("本机助手未能启动，请重新打开面板")

    def request(self, command, timeout):
        if not self.request_lock.acquire(blocking=False):
            raise BlockingIOError("文件选择器正忙，请先完成或取消选择")
        selecting = command == "select"
        try:
            with self.lifecycle_lock:
                if selecting:
                    self.selecting, self.cancel_pending = True, False
                if not self._warm():
                    raise OSError(self.error or "本机选择器未安装")
                process, ready, responses = self.process, self.ready, self.responses
            # A fresh process must start its AppKit event loop before commands.
            self._wait_ready(process, ready)
            with self.lifecycle_lock:
                if selecting and self.cancel_pending:
                    return {"status": "cancelled", "source": "NSOpenPanel", "paths": []}
                process.stdin.write(command + "\n")
                process.stdin.flush()
            try:
                return responses.get(timeout=timeout)
            except queue.Empty:
                self.close()
                raise TimeoutError("本机助手响应超时，请重试")
        finally:
            if selecting:
                with self.lifecycle_lock:
                    self.selecting = self.cancel_pending = False
            self.request_lock.release()

    def select(self):
        return self.request("select", 120)

    def drag_paths(self):
        return self.request("drag-paths", 4)

    def cancel(self):
        with self.lifecycle_lock:
            # A cancel during cold start is kept and checked before 'select'.
            if self.selecting:
                self.cancel_pending = True
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.stdin.write("cancel\n")
                    self.process.stdin.flush()
                except OSError:
                    pass

    def close(self):
        with self.lifecycle_lock:
            self._close()

    def shutdown(self):
        with self.lifecycle_lock:
            self.stopping = True
            self._close()

    def _close(self):
        process, self.process = self.process, None
        self.ready.clear()
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


PICKER = NativePicker()
atexit.register(PICKER.close)


def scrub(value, depth=0):
    if depth > 12:
        return "[depth limit]"
    if isinstance(value, dict):
        return {str(k): "[redacted]" if SECRET_KEY.search(str(k)) else scrub(v, depth + 1)
                for k, v in list(value.items())[:150]}
    if isinstance(value, list):
        return [scrub(v, depth + 1) for v in value[:150]]
    if isinstance(value, str):
        return value[:16000]
    return value


def context(meta):
    # Reinstalling a local plugin can remove the inode of a running process's
    # working directory. Diagnostics must not break every tool in that case.
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    return {
        "observedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "serverInstance": INSTANCE,
        "serverVersion": VERSION,
        "serverPid": os.getpid(),
        "serverCwd": cwd,
        "serverCwdMeaning": "MCP process working directory; not proof of active project",
        "requestMeta": scrub(meta or {}),
        "logPath": str(LOG),
        "pickerLogPath": str(PICKER_LOG),
    }


def append_record(path, row):
    encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode()
    if len(encoded) > 512000:
        raise ToolError("诊断记录超过 512 KB")
    # Create folders inside the state folder; elsewhere only the log's own
    # folder, so a log path inside a moved project never rebuilds the project.
    try:
        if STATE_DIR in path.parents:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            path.parent.mkdir(exist_ok=True)
    except FileNotFoundError:
        raise ToolError("诊断记录目录不存在", "log") from None
    with LOG_LOCK:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "ab") as output:
            output.write(encoded)


def log_quietly(path, row):
    """Picker and Finder records are evidence only; never fail the action."""
    try:
        append_record(path, row)
        return True
    except (OSError, ValueError):
        return False


def legacy_lists(log):
    """Each owner's last list in a 0.4.x diagnostic log, oldest owner first."""
    lists = {}
    if not log.exists():
        return lists
    with LOG_LOCK, log.open("rb") as source:
        lines = source.read().splitlines()
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        panel = row.get("panel", {}) if isinstance(row, dict) else {}
        owner = drag_owner(row.get("toolContext", {}).get("requestMeta")) if isinstance(row, dict) else None
        if owner and isinstance(panel, dict) and isinstance(panel.get("references"), list):
            lists[owner] = clean_references(panel["references"])
    return lists


def legacy_references(owner):
    """0.4.x kept lists inside the shared diagnostic log; read it once as a fallback."""
    if not owner or not LOG.exists():
        return []
    with LOG_LOCK, LOG.open("rb") as source:
        end = source.seek(0, 2)
        source.seek(max(0, end - 16 * 1024 * 1024))
        lines = source.read().splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        panel = row.get("panel", {}) if isinstance(row, dict) else {}
        if drag_owner(row.get("toolContext", {}).get("requestMeta")) == owner and isinstance(panel.get("references"), list):
            return clean_references(panel["references"])
    return []


def clean_references(references):
    """Keep known fields of absolute-path entries, first occurrence per path."""
    cleaned, seen = [], set()
    for item in references:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path or len(path) > 4096 or path in seen:
            continue
        entry = {key: item[key] for key, kind in REFERENCE_KEYS.items()
                 if key in item and isinstance(item[key], kind) and not (isinstance(item[key], str) and len(item[key]) > 4096)}
        entry.setdefault("name", Path(path).name)
        seen.add(path)
        cleaned.append(entry)
    return cleaned


def with_status(references):
    """Re-check each original so moved or deleted files show at once."""
    output = []
    for item in references:
        entry = dict(item)
        try:
            info = os.stat(entry["path"])
        except (FileNotFoundError, NotADirectoryError):
            entry.update(exists=False, isFile=False)
        except OSError:
            entry.update(exists=None, isFile=None)
        else:
            regular = stat.S_ISREG(info.st_mode)
            entry.update(exists=True, isFile=regular, isPackage=stat.S_ISDIR(info.st_mode),
                         lastModified=info.st_mtime_ns // 1000000)
            if regular:
                entry["size"] = info.st_size
        output.append(entry)
    return output


class ReferenceStore:
    """One revisioned JSON list per project, with conversation fallback."""

    def __init__(self):
        self.lock = threading.Lock()

    @staticmethod
    def path(owner):
        return LIST_DIR / (hashlib.sha256(owner.encode()).hexdigest() + ".json")

    def document(self, owner):
        try:
            data = json.loads(self.path(owner).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"revision": 0, "references": legacy_references(owner)}
        except (OSError, ValueError):
            raise ToolError("参考列表记录无法读取", "store") from None
        if not isinstance(data, dict) or not isinstance(data.get("revision"), int) or not isinstance(data.get("references"), list):
            raise ToolError("参考列表记录格式无效", "store")
        migrated = data.get("migratedThreads", [])
        if not isinstance(migrated, list) or not all(isinstance(item, str) for item in migrated):
            raise ToolError("参考列表迁移记录无效", "store")
        removed = data.get("removedPathHashes", [])
        if not isinstance(removed, list) or not all(isinstance(item, str) for item in removed):
            raise ToolError("参考列表移除记录无效", "store")
        revisions = data.get("legacyRevisions", {})
        if not isinstance(revisions, dict) or not all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in revisions.items()):
            raise ToolError("参考列表迁移修订无效", "store")
        return data

    def load(self, owner):
        if not owner:
            return 0, []
        data = self.document(owner)
        return data["revision"], clean_references(data["references"])

    def write(self, owner, references, document):
        payload = {"schema": 2, "revision": document["revision"] + 1,
                   "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "references": references,
                   "migratedThreads": document.get("migratedThreads", []),
                   "removedPathHashes": document.get("removedPathHashes", []),
                   "legacyRevisions": document.get("legacyRevisions", {})}
        target = self.path(owner)
        temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False)
        os.replace(temporary, target)
        return payload["revision"]

    def migrate(self, owner, thread):
        """Merge changed legacy lists, retaining originals and removal protections."""
        if not thread or thread == owner:
            return self.load(owner)
        marker = self.path(thread).stem
        claim = LIST_DIR / ("migration-" + marker + ".json")
        scope_id = self.path(owner).stem
        current = self.document(owner)
        def claimed_elsewhere():
            try:
                destination = json.loads(claim.read_text(encoding="utf-8")).get("scopeId")
            except FileNotFoundError:
                return False
            except (OSError, ValueError, AttributeError):
                raise ToolError("参考列表迁移归属无法读取", "store") from None
            if not isinstance(destination, str) or len(destination) != 64:
                raise ToolError("参考列表迁移归属无效", "store")
            return destination != scope_id
        if claimed_elsewhere():
            return current["revision"], clean_references(current["references"])
        old_revision, old = self.load(thread)
        if marker in current.get("migratedThreads", []) and current.get("legacyRevisions", {}).get(marker, -1) >= old_revision:
            return current["revision"], clean_references(current["references"])
        if not old and not self.path(thread).exists():
            return current["revision"], clean_references(current["references"])
        LIST_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock, open(LIST_DIR / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.document(owner)
            if claimed_elsewhere():
                return current["revision"], clean_references(current["references"])
            if marker not in current.get("migratedThreads", []) or current.get("legacyRevisions", {}).get(marker, -1) < old_revision:
                removed = set(current.get("removedPathHashes", []))
                candidates = [item for item in old if hashlib.sha256(item["path"].encode()).hexdigest() not in removed]
                references = clean_references(current["references"] + candidates)
                if len(references) > MAX_REFERENCES:
                    raise ToolError(f"合并后超过 {MAX_REFERENCES} 个参考，原列表已保留", "too-many")
                # Claim first: after a crash this project can retry, while a
                # moved chat cannot import its old shelf into another project.
                temporary = claim.with_name(claim.name + f".{os.getpid()}.tmp")
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
                    json.dump({"scopeId": scope_id}, output)
                os.replace(temporary, claim)
                if marker not in current.get("migratedThreads", []):
                    current["migratedThreads"] = current.get("migratedThreads", []) + [marker]
                current["legacyRevisions"] = {**current.get("legacyRevisions", {}), marker: old_revision}
                revision = self.write(owner, references, current)
                return revision, references
            return current["revision"], clean_references(current["references"])

    def save(self, owner, references, base_revision):
        if not owner:
            raise ToolError("当前对话没有提供身份，无法保存参考列表", "no-owner")
        references = clean_references(references)
        if len(references) > MAX_REFERENCES:
            raise ToolError(f"参考列表最多保存 {MAX_REFERENCES} 项", "too-many")
        LIST_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock, open(LIST_DIR / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            document = self.document(owner)
            revision, current = document["revision"], clean_references(document["references"])
            # A write based on an unread or outdated list must not replace it.
            if base_revision != revision:
                return {"status": "conflict", "revision": revision, "references": current}
            if owner.startswith("project:"):
                before = {hashlib.sha256(item["path"].encode()).hexdigest() for item in current}
                after = {hashlib.sha256(item["path"].encode()).hexdigest() for item in references}
                document["removedPathHashes"] = sorted((set(document.get("removedPathHashes", [])) | (before - after)) - after)
            revision = self.write(owner, references, document)
            return {"status": "saved", "revision": revision, "references": references}


STORE = ReferenceStore()


def saved_references(meta):
    """The current shelf, including deliberate empty removals."""
    return STORE.load(list_owner(meta))[1]


class FileActions:
    def __init__(self):
        self.preview = None
        self.lock = threading.Lock()

    def perform(self, action, path):
        if action not in ("preview", "reveal", "open"):
            raise ToolError("不支持的文件操作")
        if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
            raise ToolError("文件路径无效")
        # Finder and the default app also accept file packages (.key, .rtfd).
        present = Path(path).is_file() if action == "preview" else os.path.exists(path)
        if not present:
            raise ToolError("原文件已移动或删除", "missing")
        video = action == "preview" and Path(path).suffix.lower() in VIDEO_EXTENSIONS
        if action == "preview" and not video:
            with self.lock:
                self.close()
                self.preview = subprocess.Popen(["/usr/bin/qlmanage", "-p", path],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    code = self.preview.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    code = None
                if code not in (None, 0):
                    raise OSError("系统预览启动失败，请使用「用默认应用打开」")
        else:
            command = ["/usr/bin/open", "-R", path] if action == "reveal" else ["/usr/bin/open", path]
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True, timeout=5, check=False)
            if result.returncode:
                raise OSError("系统没有接受此操作，请稍后重试")
        return {"status": "requested", "action": action,
                "handler": "system-player" if video else "quick-look" if action == "preview" else "system-open"}

    def close(self):
        process, self.preview = self.preview, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


FILE_ACTIONS = FileActions()
atexit.register(FILE_ACTIONS.close)


def inspect_selected_paths(paths, source="NSOpenPanel.URLs.path"):
    if not isinstance(paths, list) or not all(isinstance(p, str) and os.path.isabs(p) and "\x00" not in p for p in paths):
        raise ValueError("本机助手返回的路径无效")
    if len(paths) > MAX_BATCH:
        raise ToolError(f"一次最多添加 {MAX_BATCH} 个文件，本次选择了 {len(paths)} 个", "too-many")
    files = []
    for raw in dict.fromkeys(paths):
        entry = {"name": Path(raw).name, "path": raw, "source": source}
        try:
            info = os.stat(raw)
            entry.update(exists=True, isFile=stat.S_ISREG(info.st_mode), isPackage=stat.S_ISDIR(info.st_mode),
                         size=info.st_size, lastModified=info.st_mtime_ns // 1000000)
        except FileNotFoundError:
            entry.update(exists=False, isFile=False)
        except OSError as error:
            entry.update(exists=None, isFile=None, statError=str(error))
        files.append(entry)
    return files


def drag_owner(meta):
    meta = meta or {}
    return meta.get("threadId") or meta.get("thread_id")


def list_owner(meta):
    scope = PROJECTS.resolve(meta)
    if scope["listScope"] == "project":
        STORE.migrate(scope["owner"], scope["thread"])
    return scope["owner"]


def request_owner(meta):
    return drag_owner(meta) or list_owner(meta)


def capture_drag(meta):
    with DRAG_LOCK:
        for key, value in list(DRAG_CAPTURES.items()):
            if time.monotonic() - value["time"] > DRAG_TTL_SECONDS:
                del DRAG_CAPTURES[key]
    try:
        capture = PICKER.drag_paths()
    except (OSError, ValueError, TimeoutError):
        return {"id": None, "available": False}
    if capture.get("status") != "captured" or not capture.get("paths"):
        return {"id": None, "available": False}
    identifier = str(uuid.uuid4())
    with DRAG_LOCK:
        if len(DRAG_CAPTURES) >= 20:
            del DRAG_CAPTURES[next(iter(DRAG_CAPTURES))]
        DRAG_CAPTURES[identifier] = {"time": time.monotonic(), "owner": request_owner(meta), "capture": capture}
    return {"id": identifier, "available": True}


def read_drag_pasteboard():
    try:
        return PICKER.drag_paths()
    except (OSError, ValueError, TimeoutError):
        return {}


def resolve_drop(args, meta):
    """Resolve this explicit drop; no filename search, content reads or copies."""
    descriptors = args.get("files", [])
    paths = args.get("paths", [])
    if not isinstance(descriptors, list) or not all(
        isinstance(f, dict) and isinstance(f.get("name"), str)
        and isinstance(f.get("size"), int) and f["size"] >= 0
        for f in descriptors
    ):
        raise ValueError("拖放数据格式无效")
    if len(descriptors) > MAX_BATCH or (isinstance(paths, list) and len(paths) > MAX_BATCH):
        count = max(len(descriptors), len(paths) if isinstance(paths, list) else 0)
        return {"status": "unavailable", "files": [], "message": f"一次最多添加 {MAX_BATCH} 个文件，本次拖入了 {count} 个"}
    # Browser paths, if present, come from File.path or file: URI data only.
    source = "browser-drop-path"
    if not paths:
        source = "NSPasteboardNameDrag.fileURL"
        identifier = args.get("captureId")
        with DRAG_LOCK:
            cached = DRAG_CAPTURES.pop(identifier, None) if isinstance(identifier, str) else None
        if cached is not None and cached["owner"] != request_owner(meta):
            return {"status": "unavailable", "files": [], "message": "拖放来自其他对话，请重新拖入文件"}
        if cached is not None and time.monotonic() - cached["time"] <= DRAG_TTL_SECONDS:
            capture = cached["capture"]
        else:
            # The drag pasteboard still holds the drag just released; the
            # name/size check below rejects anything older or different.
            capture = read_drag_pasteboard()
        paths = capture.get("paths", []) if capture.get("status") == "captured" else []
    elif isinstance(args.get("captureId"), str):
        with DRAG_LOCK:
            DRAG_CAPTURES.pop(args["captureId"], None)
    if not paths:
        return {"status": "unavailable", "files": [], "message": "未能取得原文件路径，请点「添加」选择这些文件"}
    try:
        files = inspect_selected_paths(paths, source)
    except ToolError as error:
        return {"status": "unavailable", "files": [], "message": str(error)}
    if any(f.get("isPackage") for f in files):
        return {"status": "unavailable", "files": [], "message": "暂不支持拖入文件夹或文件包，请点「添加」选择"}
    if not files or any(f.get("exists") is not True or f.get("isFile") is not True for f in files):
        return {"status": "unavailable", "files": [], "message": "部分文件无法访问；请从 Finder 拖入本机文件，或点「添加」"}
    # Never add a stale/different drag or a subset of a multi-file drag.
    if descriptors:
        expected = Counter((f["name"], f["size"]) for f in descriptors)
        observed = Counter((f["name"], f["size"]) for f in files)
        if expected != observed:
            return {"status": "unavailable", "files": [], "message": "未能匹配这次拖入的文件，请重新拖入或点「添加」"}
    elif source != "browser-drop-path":
        return {"status": "unavailable", "files": [], "message": "拖入数据不包含文件，请从 Finder 选择文件拖入"}
    return {"status": "selected", "source": source, "files": files, "allPathsExist": True}


def choose_files():
    try:
        picked = PICKER.select()
    except TimeoutError:
        return {"status": "error", "message": "文件选择等待超时，请重新点击选择文件", "files": []}
    except (OSError, ValueError) as error:
        return {"status": "error", "message": str(error)[:2000] or "文件选择器启动失败", "files": []}
    if picked.get("status") == "error":
        return {"status": "error", "message": picked.get("message", "文件选择器启动失败"), "files": []}
    if picked.get("status") == "cancelled":
        return {"status": "cancelled", "source": "NSOpenPanel", "files": []}
    if picked.get("status") != "selected" or picked.get("source") != "NSOpenPanel":
        return {"status": "error", "message": "本机选择器返回了无效结果", "files": []}
    try:
        files = inspect_selected_paths(picked.get("paths"))
    except ValueError as error:
        return {"status": "error", "message": str(error), "files": []}
    return {"status": "selected", "source": "NSOpenPanel", "files": files,
            "allPathsExist": bool(files) and all(f["exists"] is True and (f["isFile"] is True or f.get("isPackage")) for f in files),
            "requiresFinderComparison": True}


def result(data):
    return {"content": [{"type": "text", "text": "Reference Test 本机诊断数据"}],
            "structuredContent": data}


def tool_error(error):
    message = str(error)
    return {"content": [{"type": "text", "text": message}], "isError": True,
            "structuredContent": {"error": {"code": getattr(error, "code", "error"), "message": message}}}


def tool(name, title, description, schema=None, readonly=True, meta=None):
    item = {
        "name": name, "title": title, "description": description,
        "inputSchema": schema or {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": readonly, "destructiveHint": False, "openWorldHint": False},
    }
    item["_meta"] = {"ui": {"visibility": ["model", "app"]}, "openai/widgetAccessible": True, **(meta or {})}
    item["_meta"]["ui"].setdefault("visibility", ["model", "app"])
    return item


def app_only():
    return {"ui": {"visibility": ["app"]}}


TOOLS = [
    tool("open_reference_probe", "参考", "Open the native Reference panel with optional diagnostics.", meta={
        "ui": {"resourceUri": URI},
        "openai/outputTemplate": URI,
        "openai/widgetAccessible": True,
        "openai/ui": {"entrypoints": [{"type": "thread"}]},
    }),
    tool("read_probe_context", "Read host tool context", "Return host metadata and the project-shared reference list. Falls back to the conversation when no project is resolved.",
         {"type": "object", "properties": {"knownRevision": {"type": ["integer", "null"]},
          "scopeId": {"type": ["string", "null"]}}, "additionalProperties": False}),
    tool("choose_reference_files", "选择文件（可多选）", "Show the local macOS file picker and return selected absolute paths. Does not read or upload file contents.",
         readonly=False, meta={"ui": {"visibility": ["app"]}, "openai/widgetAccessible": True}),
    tool("cancel_reference_selection", "取消文件选择", "Cancel this panel's open file picker without blocking other tools.",
         readonly=False, meta=app_only()),
    tool("reference_file_action", "操作参考文件", "Preview, open, or reveal a local reference file after a direct user click. Never deletes or uploads the file.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["preview", "reveal", "open"]},
          "path": {"type": "string"}}, "required": ["action", "path"], "additionalProperties": False},
         readonly=False, meta=app_only()),
    tool("prepare_reference_preview", "在面板预览参考", "Use when the user clicks a saved reference to view it inside the panel. Reads the selected local file only; never opens an external app or uploads it.",
         {"type": "object", "properties": {"path": {"type": "string"}, "page": {"type": "integer", "minimum": 1}},
          "required": ["path"], "additionalProperties": False}, readonly=True, meta=app_only()),
    tool("release_reference_preview", "关闭面板预览", "Release a temporary local preview when the user switches, closes, or removes it. Does not change the source file.",
         {"type": "object", "properties": {"leaseId": {"type": "string"}},
          "required": ["leaseId"], "additionalProperties": False}, readonly=False, meta=app_only()),
    tool("read_reference_preview_chunk", "读取面板预览片段", "Use when the panel loads a prepared preview. Returns one bounded local byte chunk to the widget only; never uploads or modifies the file.",
         {"type": "object", "properties": {"leaseId": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
          "required": ["leaseId", "offset"], "additionalProperties": False}, readonly=True, meta=app_only()),
    tool("capture_reference_drag", "Capture current file drag", "Capture local file URLs when a file drag enters the reference panel. Reads only the drag pasteboard, not the copy clipboard.",
         readonly=True, meta=app_only()),
    tool("resolve_reference_drop", "Resolve dropped reference files", "Resolve this explicit file drop and validate its paths against the dropped file names and sizes. Does not read or copy file contents.",
         {"type": "object", "properties": {
             "files": {"type": "array", "maxItems": 1000, "items": {"type": "object", "properties": {
                 "name": {"type": "string"}, "size": {"type": "integer", "minimum": 0}},
                 "required": ["name", "size"], "additionalProperties": False}},
             "paths": {"type": "array", "maxItems": 1000, "items": {"type": "string"}},
             "captureId": {"type": ["string", "null"]}}, "required": ["files", "paths"], "additionalProperties": False},
         readonly=True, meta=app_only()),
    tool("read_reference_thumbnails", "读取参考缩略图", "Return small cached Quick Look thumbnails and durations for files already in this conversation's list. Widget-only bytes; never uploads or modifies files.",
         {"type": "object", "properties": {"paths": {"type": "array", "maxItems": THUMB_BATCH, "items": {"type": "string"}}},
          "required": ["paths"], "additionalProperties": False}, readonly=True, meta=app_only()),
    tool("save_reference_list", "保存参考列表", "Save the current project's reference list from the panel. Rejects outdated revisions and changed project scopes. Does not touch the files.",
         {"type": "object", "properties": {"references": {"type": "array", "maxItems": MAX_REFERENCES, "items": {"type": "object"}},
          "baseRevision": {"type": ["integer", "null"]}, "scopeId": {"type": ["string", "null"]}}, "required": ["references", "baseRevision"], "additionalProperties": False},
         readonly=False, meta=app_only()),
    tool("save_probe_snapshot", "Save local diagnostic snapshot", "Append panel diagnostics to the local diagnostic log. Does not read or upload files.",
         {"type": "object", "properties": {"snapshot": {"type": "object"}}, "required": ["snapshot"], "additionalProperties": False},
         readonly=False, meta=app_only()),
]


def call_tool(name, args, meta):
    data = context(meta)
    scoped = name in {"open_reference_probe", "read_probe_context", "save_reference_list", "prepare_reference_preview",
                      "read_reference_thumbnails", "read_reference_preview_chunk", "release_reference_preview"}
    scope = PROJECTS.resolve(meta) if scoped else None
    owner = scope["owner"] if scope else drag_owner(meta) or SHARED_OWNER
    if scope and scope["listScope"] == "project" and name not in {"read_reference_preview_chunk", "release_reference_preview"}:
        STORE.migrate(owner, scope["thread"])
    capability_owner = drag_owner(meta) or owner
    if scope and scope["listScope"] == "project":
        capability_owner = scope["scopeId"] + ":" + capability_owner
    if name in ("open_reference_probe", "read_probe_context"):
        PICKER.warm()
        revision, references = STORE.load(owner)
        info = {"probeContext": data, "listRevision": revision, "listScope": scope["listScope"],
                "scopeId": scope["scopeId"], "project": scope["project"], "projectLookup": scope["projectLookup"]}
        if args.get("knownRevision") == revision and args.get("scopeId") == scope["scopeId"]:
            return result({**info, "unchanged": True})
        return result({**info, "references": with_status(references)})
    if name == "cancel_reference_selection":
        PICKER.cancel()
        return result({"cancelRequested": True})
    if name == "save_reference_list":
        if (scope["listScope"] == "project" or args.get("scopeId") is not None) and args.get("scopeId") != scope["scopeId"]:
            raise ToolError("项目已变更或面板版本过旧，请重新打开参考面板后操作", "scope-changed")
        references, base = args.get("references"), args.get("baseRevision")
        if not isinstance(references, list) or not (base is None or (isinstance(base, int) and not isinstance(base, bool))):
            raise ValueError("参考列表格式无效")
        outcome = STORE.save(owner, references, base)
        if outcome["status"] == "saved":
            PREVIEWS.retain({f["path"] for f in outcome["references"]}, capability_owner)
        else:
            outcome["references"] = with_status(outcome["references"])
        return result({"list": outcome})
    if name == "prepare_reference_preview":
        path = args.get("path")
        if path not in {f["path"] for f in STORE.load(owner)[1]}:
            raise ToolError("请先将文件添加到当前参考列表", "not-listed")
        preview = PREVIEWS.prepare(path, capability_owner, args.get("page", 1))
        return {"content": [], "structuredContent": {"previewReady": True}, "_meta": {"referencePreview": preview}}
    if name == "read_reference_thumbnails":
        paths = args.get("paths")
        if not isinstance(paths, list) or len(paths) > THUMB_BATCH or not all(isinstance(p, str) for p in paths):
            raise ValueError("缩略图请求格式无效")
        listed = {f["path"] for f in STORE.load(owner)[1]}
        items = THUMBNAILS.get([p for p in paths if p in listed])
        items.update({p: {"status": "not-listed"} for p in paths if p not in listed})
        return {"content": [], "structuredContent": {"thumbnailsReady": True}, "_meta": {"referenceThumbnails": items}}
    if name == "release_reference_preview":
        PREVIEWS.release(args.get("leaseId"), capability_owner)
        return result({"released": True})
    if name == "read_reference_preview_chunk":
        chunk = PREVIEWS.read_chunk(args.get("leaseId"), capability_owner, args.get("offset"))
        return {"content": [], "structuredContent": {"chunkReady": True}, "_meta": {"referenceChunk": chunk}}
    if name == "reference_file_action":
        record = {"toolContext": data, "action": args.get("action"), "path": args.get("path")}
        try:
            outcome = FILE_ACTIONS.perform(args.get("action"), args.get("path"))
        except Exception as error:
            log_quietly(ACTION_LOG, {**record, "status": "error", "message": str(error)})
            raise
        log_quietly(ACTION_LOG, {**record, **outcome})
        return result({"fileAction": outcome})
    if name == "capture_reference_drag":
        return result({"dragCapture": capture_drag(meta)})
    if name == "resolve_reference_drop":
        return result({"selection": resolve_drop(args, meta)})
    if name == "choose_reference_files":
        selection = choose_files()
        event_id = str(uuid.uuid4())
        row = {"schema": 1, "eventId": event_id, "toolContext": data, "selection": selection}
        saved = log_quietly(PICKER_LOG, row)
        return result({"probeContext": data, "selection": selection, "pickerRecordSaved": saved,
                       "eventId": event_id})
    if name == "save_probe_snapshot":
        if not isinstance(args.get("snapshot"), dict):
            raise ValueError("snapshot 必须是对象")
        snapshot = scrub(args["snapshot"])
        # 0.4.x panels still carried the list in the snapshot; keep them working.
        if isinstance(snapshot.get("references"), list) and snapshot.get("schema", 0) < 3:
            revision = STORE.load(owner)[0]
            STORE.save(owner, args["snapshot"]["references"], revision)
        row = {"schema": 1, "eventId": str(uuid.uuid4()), "toolContext": data, "panel": snapshot}
        append_record(LOG, row)
        return result({"probeContext": data, "saved": True, "eventId": row["eventId"]})
    raise MethodNotFound(f"未知工具：{name}")


def dispatch(method, params):
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "reference-native-test", "version": VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "resources/list":
        return {"resources": [{"uri": URI, "name": "参考", "mimeType": MIME}]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        if params.get("uri") != URI:
            raise ValueError("Unknown resource URI")
        return {"contents": [{"uri": URI, "mimeType": MIME, "text": (ROOT / "panel.html").read_text(),
                              "_meta": {"ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": ["blob:", "data:"], "frameDomains": ["blob:"]}},
                                        "openai/widgetPrefersBorder": True}}]}
    if method == "tools/call":
        try:
            return call_tool(params.get("name"), params.get("arguments") or {}, params.get("_meta"))
        except ToolError as error:
            # Expected failures are tool results with a code, not protocol errors.
            return tool_error(error)
    raise MethodNotFound(method)


SLOW_TOOLS = {"open_reference_probe", "read_probe_context", "capture_reference_drag", "resolve_reference_drop", "read_reference_thumbnails",
              "reference_file_action", "prepare_reference_preview", "read_reference_preview_chunk"}


def serve(input_stream=None, output_stream=None):
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    output_lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="reference-action")
    # The picker can wait for minutes; keep it off the shared pool.
    picker_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reference-picker")
    choosing = set()

    def send(response):
        with output_lock:
            print(json.dumps(response, ensure_ascii=False), file=output_stream, flush=True)

    def respond(request):
        key = request["id"] if isinstance(request["id"], (str, int)) else None
        try:
            value = dispatch(request.get("method"), request.get("params") or {})
            response = {"jsonrpc": "2.0", "id": request["id"], "result": value}
        except MethodNotFound as error:
            response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": str(error)}}
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32602, "message": str(error)}}
        finally:
            choosing.discard(key)
        send(response)

    try:
        for line in input_stream:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                send({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                      "error": {"code": -32600, "message": "Expected a JSON-RPC 2.0 object"}})
                continue
            if "id" not in request:
                params = request.get("params") or {}
                # A host-cancelled picker call must close the dialog it opened.
                if request.get("method") == "notifications/cancelled" and isinstance(params, dict) \
                        and isinstance(params.get("requestId"), (str, int)) and params["requestId"] in choosing:
                    PICKER.cancel()
                continue
            name = (request.get("params") or {}).get("name") if request.get("method") == "tools/call" else None
            # Long native operations must not hold up ping, context or cancellation.
            if name == "choose_reference_files":
                if isinstance(request["id"], (str, int)):
                    choosing.add(request["id"])
                picker_executor.submit(respond, request)
            elif name in SLOW_TOOLS:
                executor.submit(respond, request)
            else:
                respond(request)
    finally:
        PICKER.shutdown()
        picker_executor.shutdown(wait=True)
        executor.shutdown(wait=True)


def import_legacy_log(log):
    """Copy 0.4.x lists into the store for owners that have no list yet."""
    imported, kept = [], []
    for owner, references in legacy_lists(Path(log)).items():
        if STORE.path(owner).exists():
            kept.append(owner)
        else:
            STORE.save(owner, references, 0)
            imported.append({"owner": owner, "items": len(references)})
    return {"imported": imported, "alreadyStored": kept, "store": str(LIST_DIR)}


if __name__ == "__main__":
    if sys.argv[1:2] == ["--import-legacy-log"] and len(sys.argv) == 3:
        print(json.dumps(import_legacy_log(sys.argv[2]), ensure_ascii=False, indent=2))
    else:
        serve()
