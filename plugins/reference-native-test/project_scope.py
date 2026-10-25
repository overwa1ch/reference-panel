"""Resolve a reference shelf from host metadata, never from the plugin's cwd."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time


def project_root(value):
    if isinstance(value, str) and os.path.isabs(value) and '\x00' not in value and len(value) <= 4096:
        return os.path.realpath(value)
    return None


def codex_thread_project(thread_id):
    """Read only a stored thread's routing summary through the public API."""
    executable = os.environ.get('CODEX_BIN') or shutil.which('codex')
    if not executable:
        for candidate in ('/Applications/Codex.app/Contents/Resources/codex',
                          '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                executable = candidate
                break
    if not executable:
        return None
    inbox = queue.Queue()
    process = subprocess.Popen([executable, 'app-server', '--listen', 'stdio://'], cwd=Path.home(),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, bufsize=1)

    def reader():
        try:
            for line in process.stdout:
                try:
                    inbox.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            inbox.put(None)

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    deadline = time.monotonic() + 3

    def request(identifier, method, params):
        process.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Project lookup timed out')
            response = inbox.get(timeout=remaining)
            if response is None:
                raise OSError('Project lookup ended')
            if response.get('id') == identifier:
                if 'error' in response:
                    return None
                return response.get('result')

    try:
        if request(1, 'initialize', {'clientInfo': {'name': 'reference-panel-project', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}}) is None:
            return None
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        result = request(2, 'thread/read', {'threadId': thread_id, 'includeTurns': False}) or {}
        thread = result.get('thread') if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            return None
        root = project_root(thread.get('cwd'))
        return {'root': root, 'source': 'codex-thread'} if root else None
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=.3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        worker.join(timeout=.3)
        process.stdout.close()


class ProjectScopes:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def resolve(self, meta):
        meta = meta if isinstance(meta, dict) else {}
        thread = meta.get('threadId') or meta.get('thread_id')
        thread = thread if isinstance(thread, str) and thread and len(thread) <= 256 else None
        root = project_root(os.environ.get('REFERENCE_PROJECT_ROOT'))
        source = 'configuration' if root else None
        if not root:
            project = meta.get('project') if isinstance(meta.get('project'), dict) else {}
            # These are explicit request fields; generic workspaces/OS cwd are
            # deliberately excluded because they can name a parent checkout.
            for value in (meta.get('projectRoot'), meta.get('project_root'), project.get('root'), project.get('path'),
                          meta.get('workspaceRoot'), meta.get('cwd')):
                root = project_root(value)
                if root:
                    source = 'host-metadata'
                    break
        # Widget-initiated calls commonly carry only threadId/thread_id. The
        # agent-only plugin/turn metadata must not gate project resolution.
        codex = (isinstance(meta.get('x-codex-turn-metadata'), dict) or isinstance(meta.get('plugin_id'), str)
                 or bool(thread and re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', thread)))
        project = meta.get('project') if isinstance(meta.get('project'), dict) else {}
        project_id = meta.get('projectId') or meta.get('project_id') or project.get('id')
        project_id = project_id if isinstance(project_id, str) and 0 < len(project_id) <= 256 and not any(ord(c) < 32 for c in project_id) else None
        if not root and not project_id and thread and codex:
            with self.lock:
                cached = self.cache.get(thread)
                if cached and cached[0] > time.monotonic():
                    found = cached[1]
                else:
                    try:
                        found = codex_thread_project(thread)
                    except (OSError, ValueError, TimeoutError, queue.Empty):
                        found = None
                    # Preserve an already-resolved scope across a transient API
                    # outage. A successful lookup can still move a chat later.
                    if not found and cached:
                        found = cached[1]
                    self.cache[thread] = (time.monotonic() + (60 if found else 10), found)
                    if len(self.cache) > 1024:
                        self.cache.pop(next(iter(self.cache)))
                if found:
                    root, source = found['root'], found['source']
        owner = 'project:path:' + root if root else 'project:id:' + project_id if project_id else thread or 'shared:no-conversation-id'
        return {'owner': owner, 'scopeId': hashlib.sha256(owner.encode()).hexdigest(),
                'listScope': 'project' if root or project_id else 'conversation' if thread else 'shared',
                'projectLookup': 'resolved' if root or project_id else 'failed' if thread and codex else 'not-provided',
                'project': {'root': root, 'name': (Path(root).name or root) if root else project_id,
                            'source': source or 'host-metadata'} if root or project_id else None,
                'thread': thread}
