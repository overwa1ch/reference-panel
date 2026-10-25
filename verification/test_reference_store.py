"""Per-conversation list store, error mapping and 0.5.0 backend fixes. No GUI is launched."""
import base64
import codecs
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "plugins/reference-native-test"
spec = importlib.util.spec_from_file_location("reference_store_server", ROOT / "server.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
preview = probe.sys.modules["preview"]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name, value in [("LOG", self.folder / "runs/log.jsonl"), ("LIST_DIR", self.folder / "state/lists")]:
            patcher = patch.object(probe, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.file = self.folder / "参考 视频.mp4"
        self.file.write_bytes(b"fixture")

    def call(self, name, arguments=None, thread="A"):
        return probe.dispatch("tools/call", {"name": name, "arguments": arguments or {}, "_meta": {"threadId": thread}})

    def save(self, references, base, thread="A"):
        return self.call("save_reference_list", {"references": references, "baseRevision": base}, thread)["structuredContent"]["list"]

    def read(self, thread="A"):
        content = self.call("read_probe_context", thread=thread)["structuredContent"]
        return content["listRevision"], content["references"]

    def test_lists_survive_any_amount_of_other_activity(self):
        self.assertEqual(self.save([{"path": str(self.file)}], 0)["status"], "saved")
        row = {"toolContext": {"requestMeta": {"threadId": "B"}}, "panel": {"events": ["x" * 7000]}}
        with patch.object(probe.PICKER, "warm"):
            for _ in range(400):  # ~2.8 MiB of diagnostics from another chat
                probe.append_record(probe.LOG, row)
            revision, references = self.read()
        self.assertEqual(revision, 1)
        self.assertEqual([f["path"] for f in references], [str(self.file)])

    def test_stale_or_unread_writes_are_rejected_with_the_current_list(self):
        first = {"path": str(self.file)}
        other = {"path": str(self.folder / "b.pdf")}
        self.save([first, other], 0)
        conflict = self.save([], None)  # a panel whose first read failed
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["revision"], 1)
        self.assertEqual(len(conflict["references"]), 2)
        self.assertEqual(self.save([first], 0)["status"], "conflict")
        self.assertEqual(self.save([first], 1)["status"], "saved")
        with patch.object(probe.PICKER, "warm"):
            revision, references = self.read()
        self.assertEqual((revision, len(references)), (2, 1))

    def test_owner_isolation_file_mode_and_empty_list(self):
        self.save([{"path": str(self.file)}], 0, "A")
        self.save([], 0, "B")
        with patch.object(probe.PICKER, "warm"):
            self.assertEqual(len(self.read("A")[1]), 1)
            self.assertEqual(self.read("B"), (1, []))
        files = sorted(p for p in probe.LIST_DIR.iterdir() if p.suffix == ".json")
        self.assertEqual(len(files), 2)
        self.assertTrue(all("A" not in p.name for p in files), "thread ids are hashed, not used as names")
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        # A host without a conversation id gets its own shared list.
        self.assertEqual(self.call("save_reference_list", {"references": [], "baseRevision": 0}, None)["structuredContent"]["list"]["status"], "saved")

    def test_more_than_150_references_are_kept_and_previewable(self):
        names = []
        for index in range(220):
            path = self.folder / f"clip-{index:03}.txt"
            path.write_text(str(index))
            names.append({"path": str(path)})
        self.assertEqual(self.save(names, 0)["status"], "saved")
        last = self.call("prepare_reference_preview", {"path": names[-1]["path"]})
        self.assertEqual(last["_meta"]["referencePreview"]["text"], "219")

    def test_restore_rechecks_moved_files(self):
        self.save([{"path": str(self.file), "exists": True, "isFile": True}], 0)
        moved = self.folder / "moved.mp4"
        self.file.rename(moved)
        with patch.object(probe.PICKER, "warm"):
            entry = self.read()[1][0]
        self.assertIs(entry["exists"], False)
        failed = self.call("prepare_reference_preview", {"path": str(self.file)})
        self.assertTrue(failed["isError"])
        self.assertEqual(failed["structuredContent"]["error"]["code"], "missing")
        self.assertIn("已移动或删除", failed["content"][0]["text"])

    def test_diagnostic_snapshots_do_not_touch_the_list(self):
        self.save([{"path": str(self.file)}], 0)
        self.call("save_probe_snapshot", {"snapshot": {"schema": 3, "events": [], "referenceCount": 1}})
        with patch.object(probe.PICKER, "warm"):
            self.assertEqual(self.read()[0], 1)
        # A 0.4.x panel still carries its list in the snapshot.
        self.call("save_probe_snapshot", {"snapshot": {"references": []}})
        with patch.object(probe.PICKER, "warm"):
            self.assertEqual(self.read(), (2, []))

    def test_missing_log_parent_is_not_recreated(self):
        gone = self.folder / "moved-project/repos/x/verification/runs/log.jsonl"
        with self.assertRaises(probe.ToolError):
            probe.append_record(gone, {"a": 1})
        self.assertFalse((self.folder / "moved-project").exists())


class PortabilityTests(unittest.TestCase):
    """Other hosts (no conversation id), other machines (no fixed paths)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name, value in [("LOG", self.folder / "state/diagnostics/log.jsonl"), ("LIST_DIR", self.folder / "state/lists"),
                            ("PICKER_LOG", self.folder / "state/diagnostics/file-picker.jsonl"),
                            ("ACTION_LOG", self.folder / "state/diagnostics/file-actions.jsonl"), ("STATE_DIR", self.folder / "state")]:
            patcher = patch.object(probe, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.file = self.folder / "镜头.txt"
        self.file.write_text("镜头一")

    def call(self, name, arguments=None, meta=None):
        return probe.dispatch("tools/call", {"name": name, "arguments": arguments or {}, **({"_meta": meta} if meta is not None else {})})

    def test_hosts_without_conversation_id_share_one_working_list(self):
        with patch.object(probe.PICKER, "warm"):
            first = self.call("read_probe_context")["structuredContent"]
            self.assertEqual((first["listScope"], first["listRevision"]), ("shared", 0))
            saved = self.call("save_reference_list", {"references": [{"path": str(self.file)}], "baseRevision": 0})
            self.assertEqual(saved["structuredContent"]["list"]["status"], "saved")
            again = self.call("read_probe_context", meta={})["structuredContent"]
            self.assertEqual([f["path"] for f in again["references"]], [str(self.file)])
            codex = self.call("read_probe_context", meta={"threadId": "T"})["structuredContent"]
            self.assertEqual((codex["listScope"], codex["references"]), ("conversation", []), "Codex chats keep their own lists")
        shown = self.call("prepare_reference_preview", {"path": str(self.file)})["_meta"]["referencePreview"]
        self.assertEqual(shown["text"], "镜头一")
        with patch.object(probe.PICKER, "drag_paths", return_value={"status": "captured", "paths": [str(self.file)]}):
            capture = probe.capture_drag({})
        drop = probe.resolve_drop({"files": [{"name": self.file.name, "size": self.file.stat().st_size}], "paths": [], "captureId": capture["id"]}, {})
        self.assertEqual(drop["status"], "selected")

    def test_unwritable_records_never_cost_the_users_selection_or_action(self):
        gone = self.folder / "moved-project/verification/runs"
        with patch.object(probe, "PICKER_LOG", gone / "file-picker.jsonl"), patch.object(probe, "ACTION_LOG", gone / "file-actions.jsonl"), \
             patch.object(probe.PICKER, "select", return_value={"status": "selected", "source": "NSOpenPanel", "paths": [str(self.file)]}):
            picked = self.call("choose_reference_files", meta={"threadId": "T"})
            self.assertNotIn("isError", picked)
            self.assertEqual(picked["structuredContent"]["selection"]["files"][0]["path"], str(self.file))
            self.assertFalse(picked["structuredContent"]["pickerRecordSaved"])
            with patch.object(probe.subprocess, "run", return_value=probe.subprocess.CompletedProcess([], 0, "", "")):
                action = self.call("reference_file_action", {"action": "reveal", "path": str(self.file)}, {"threadId": "T"})
            self.assertEqual(action["structuredContent"]["fileAction"]["status"], "requested")
        self.assertFalse((self.folder / "moved-project").exists(), "a log path outside the state folder is never rebuilt")

    def test_default_records_live_in_the_state_folder(self):
        state = self.folder / "fresh-state"
        env = {k: v for k, v in os.environ.items() if k != "REFERENCE_PROBE_LOG"}
        env["REFERENCE_STATE_DIR"] = str(state)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "save_probe_snapshot", "arguments": {"snapshot": {"schema": 3}}}}
        run = subprocess.run(["/usr/bin/python3", str(ROOT / "server.py")], input=json.dumps(request) + "\n", text=True,
                             capture_output=True, timeout=10, env=env, check=True)
        self.assertTrue(json.loads(run.stdout)["result"]["structuredContent"]["saved"])
        log = state / "diagnostics/native-panel.jsonl"
        self.assertTrue(log.is_file())
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)

    def test_shipped_plugin_has_no_machine_specific_paths(self):
        config = json.loads((ROOT / "mcp.json").read_text())
        server = config["mcpServers"]["reference-probe"]
        self.assertNotIn("env", server)
        for name in ("mcp.json", "plugin.json", "server.py", "preview.py", "panel.html"):
            self.assertNotIn("/Users/", (ROOT / name).read_text(), name)
        for binary in ("bin/reference-picker", "bin/reference-render"):
            if (ROOT / binary).exists():
                archs = subprocess.run(["/usr/bin/lipo", "-archs", str(ROOT / binary)], capture_output=True, text=True, check=True).stdout.split()
                self.assertEqual(sorted(archs), ["arm64", "x86_64"], binary)

    def test_legacy_lists_are_imported_once(self):
        log = self.folder / "legacy.jsonl"
        rows = [("A", [{"path": "/old/a.mov"}]), ("B", []), ("A", [{"path": "/old/a.mov"}, {"path": "/old/b.pdf"}]), ("C", [{"path": "/old/c.png"}])]
        log.write_text("".join(json.dumps({"toolContext": {"requestMeta": {"threadId": o}}, "panel": {"references": r}}) + "\n" for o, r in rows) + "not json\n")
        probe.STORE.save("C", [{"path": "/kept.png"}], 0)
        report = probe.import_legacy_log(log)
        self.assertEqual(sorted((i["owner"], i["items"]) for i in report["imported"]), [("A", 2), ("B", 0)])
        self.assertEqual(report["alreadyStored"], ["C"])
        self.assertEqual([f["path"] for f in probe.STORE.load("A")[1]], ["/old/a.mov", "/old/b.pdf"])
        self.assertEqual(probe.STORE.load("C")[1][0]["path"], "/kept.png")
        self.assertEqual(probe.import_legacy_log(log)["imported"], [], "a second import changes nothing")


class ProjectSharingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / 'Project A'
        self.root.mkdir()
        self.file = self.folder / 'reference.txt'
        self.file.write_text('shared reference')
        for name, value in [('LIST_DIR', self.folder / 'state/lists'), ('LOG', self.folder / 'log.jsonl'),
                            ('PROJECTS', probe.ProjectScopes()), ('PREVIEWS', probe.PreviewService(ROOT))]:
            patcher = patch.object(probe, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(probe.PREVIEWS.close)
        picker = patch.object(probe.PICKER, 'warm')
        picker.start()
        self.addCleanup(picker.stop)

    def call(self, name, args=None, thread='A', root=None):
        return probe.dispatch('tools/call', {'name': name, 'arguments': args or {},
                              '_meta': {'threadId': thread, 'projectRoot': str(root or self.root)}})

    def read(self, thread='A', root=None):
        return self.call('read_probe_context', thread=thread, root=root)['structuredContent']

    def save(self, references, context, thread='A', root=None):
        return self.call('save_reference_list', {'references': references, 'baseRevision': context['listRevision'],
                         'scopeId': context['scopeId']}, thread, root)

    def test_same_project_shares_add_and_remove_other_projects_stay_isolated(self):
        initial = self.read()
        self.assertEqual(initial['listScope'], 'project')
        self.assertEqual(initial['project']['root'], str(self.root.resolve()))
        self.assertEqual(self.save([{'path': str(self.file)}], initial)['structuredContent']['list']['status'], 'saved')
        second = self.read('B')
        self.assertEqual(second['scopeId'], initial['scopeId'])
        self.assertEqual(second['references'][0]['path'], str(self.file))
        self.assertEqual(self.read('C', self.folder / 'Project B')['references'], [])
        shown = self.call('prepare_reference_preview', {'path': str(self.file)}, 'B')
        self.assertEqual(shown['_meta']['referencePreview']['text'], 'shared reference')
        self.save([], second, 'B')
        self.assertEqual(self.read()['references'], [])
        self.assertTrue(self.file.exists())

    def test_stale_project_scope_cannot_save_into_a_new_project(self):
        before = self.read()
        failed = self.save([{'path': str(self.file)}], before, root=self.folder / 'Different project')
        self.assertTrue(failed['isError'])
        self.assertEqual(failed['structuredContent']['error']['code'], 'scope-changed')
        self.assertEqual(self.read(root=self.folder / 'Different project')['references'], [])

    def test_unchanged_shelf_poll_does_not_stat_every_reference(self):
        first = self.read()
        self.save([{'path': str(self.file)}], first)
        current = self.read()
        with patch.object(probe, 'with_status', side_effect=AssertionError('must not re-stat unchanged files')):
            result = self.call('read_probe_context', {'knownRevision': current['listRevision'], 'scopeId': current['scopeId']})['structuredContent']
        self.assertTrue(result['unchanged'])
        self.assertNotIn('references', result)

    def test_old_lists_merge_once_and_do_not_resurrect_removed_references(self):
        other = self.folder / 'other.txt'
        other.write_text('other')
        probe.STORE.save('A', [{'path': str(self.file)}], 0)
        probe.STORE.save('B', [{'path': str(self.file)}, {'path': str(other)}], 0)
        first = self.read()
        second = self.read('B')
        self.assertEqual(len(second['references']), 2)
        self.assertGreater(second['listRevision'], first['listRevision'])
        stale = self.save([], first)['structuredContent']['list']
        self.assertEqual(stale['status'], 'conflict')
        self.save([], second, 'B')
        self.assertEqual(self.read()['references'], [])
        self.assertEqual(self.read('B')['references'], [])
        self.assertEqual(len(probe.STORE.load('A')[1]), 1, 'legacy backup is retained')
        # A fresh backend reads durable migration markers rather than re-importing.
        with patch.object(probe, 'STORE', probe.ReferenceStore()):
            self.assertEqual(self.read()['references'], [])

    def test_project_paths_normalize_symlinks_without_using_process_cwd(self):
        alias = self.folder / 'Alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.read()['scopeId'], self.read('B', alias)['scopeId'])
        resolver = probe.ProjectScopes()
        self.assertEqual(resolver.resolve({'threadId': 'X', 'workspaces': {str(self.root): {}}})['listScope'], 'conversation')
        self.assertEqual(resolver.resolve({'threadId': 'X'})['listScope'], 'conversation')
        self.assertEqual(resolver.resolve({'projectId': 'explicit-project'})['listScope'], 'project')
        with patch.dict(os.environ, {'REFERENCE_PROJECT_ROOT': str(self.root)}):
            self.assertEqual(resolver.resolve({})['scopeId'], self.read()['scopeId'])

    def test_moving_a_chat_does_not_import_its_old_list_into_another_project(self):
        probe.STORE.save('A', [{'path': str(self.file)}], 0)
        self.assertEqual(len(self.read()['references']), 1)
        moved = self.read(root=self.folder / 'Project B')
        self.assertEqual(moved['references'], [])
        self.assertEqual(len(self.read('B')['references']), 1)

    def test_later_legacy_chats_do_not_bring_back_removed_paths(self):
        other = self.folder / 'other.txt'
        other.write_text('other')
        probe.STORE.save('A', [{'path': str(self.file)}], 0)
        probe.STORE.save('B', [{'path': str(self.file)}, {'path': str(other)}], 0)
        probe.STORE.save('C', [{'path': str(self.file)}], 0)
        first = self.read()
        self.save([], first)
        second = self.read('B')
        self.assertEqual([item['path'] for item in second['references']], [str(other)])
        self.assertEqual([item['path'] for item in self.read('C')['references']], [str(other)])
        # An explicit add/undo remains available after migration filtering.
        second = self.read('B')
        outcome = self.save(second['references'] + [{'path': str(self.file)}], second, 'B')
        self.assertEqual(outcome['structuredContent']['list']['status'], 'saved')
        self.assertEqual(len(self.read()['references']), 2)

    def test_codex_lookup_is_cached_and_keeps_scope_on_transient_failure(self):
        module = probe.sys.modules['project_scope']
        resolver = probe.ProjectScopes()
        meta = {'threadId': 'T', 'plugin_id': 'reference-native-test@test'}
        found = {'root': str(self.root), 'source': 'codex-thread'}
        with patch.object(module, 'codex_thread_project', return_value=found) as lookup:
            a, b = resolver.resolve(meta), resolver.resolve(meta)
            self.assertEqual(a['scopeId'], b['scopeId'])
            self.assertEqual(lookup.call_count, 1)
        resolver.cache['T'] = (0, found)
        with patch.object(module, 'codex_thread_project', side_effect=OSError('unavailable')):
            self.assertEqual(resolver.resolve(meta)['scopeId'], a['scopeId'])
        with patch.object(module, 'codex_thread_project', return_value=None):
            self.assertEqual(resolver.resolve({'threadId': 'Missing', 'plugin_id': 'x'})['listScope'], 'conversation')

    def test_media_leases_are_still_private_to_each_conversation(self):
        video = self.folder / 'clip.mp4'
        video.write_bytes(b'fixture')
        self.save([{'path': str(video)}], self.read())
        prepared = self.call('prepare_reference_preview', {'path': str(video)})['_meta']['referencePreview']
        forbidden = self.call('read_reference_preview_chunk', {'leaseId': prepared['leaseId'], 'offset': 0}, 'B')
        self.assertTrue(forbidden['isError'])
        moved = self.call('read_reference_preview_chunk', {'leaseId': prepared['leaseId'], 'offset': 0}, root=self.folder / 'Project B')
        self.assertTrue(moved['isError'])
        allowed = self.call('read_reference_preview_chunk', {'leaseId': prepared['leaseId'], 'offset': 0})
        self.assertEqual(allowed['_meta']['referenceChunk']['byteLength'], 7)

    def test_two_backends_migrate_without_losing_either_list(self):
        other = self.folder / 'another.txt'
        other.write_text('other')
        probe.STORE.save('A', [{'path': str(self.file)}], 0)
        probe.STORE.save('B', [{'path': str(other)}], 0)
        owner = probe.PROJECTS.resolve({'projectRoot': str(self.root)})['owner']
        barrier = threading.Barrier(2)
        def migrate(thread):
            store = probe.ReferenceStore()
            barrier.wait(timeout=3)
            return store.migrate(owner, thread)
        with probe.ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(migrate, thread) for thread in ('A', 'B')]
            for job in jobs:
                job.result(timeout=5)
        revision, references = probe.STORE.load(owner)
        self.assertEqual(revision, 2)
        self.assertEqual({item['path'] for item in references}, {str(self.file), str(other)})

    def test_codex_adapter_requests_only_stored_thread_routing(self):
        executable = self.folder / 'fake-codex'
        requests = self.folder / 'requests.jsonl'
        executable.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                              'for line in sys.stdin:\n'
                              ' m=json.loads(line)\n'
                              ' with open(' + repr(str(requests)) + ',"a") as f:f.write(json.dumps(m)+"\\n")\n'
                              ' if "id" not in m:continue\n'
                              ' result={"thread":{"cwd":' + json.dumps(str(self.root)) + '}} if m["method"]=="thread/read" else {}\n'
                              ' print(json.dumps({"id":m["id"],"result":result}),flush=True)\n')
        executable.chmod(0o755)
        with patch.dict(os.environ, {'CODEX_BIN': str(executable)}):
            found = probe.sys.modules['project_scope'].codex_thread_project('fixture')
        self.assertEqual(found['root'], str(self.root.resolve()))
        calls = [json.loads(line) for line in requests.read_text().splitlines()]
        self.assertEqual([item['method'] for item in calls], ['initialize', 'initialized', 'thread/read'])
        self.assertEqual(calls[-1]['params'], {'threadId': 'fixture', 'includeTurns': False})


class BackendFixTests(unittest.TestCase):
    def test_too_many_files_has_a_chinese_message(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            for index in range(151):
                path = Path(temp) / f"{index}.txt"
                path.write_text("x")
                paths.append(str(path))
            with patch.object(probe.PICKER, "select", return_value={"status": "selected", "source": "NSOpenPanel", "paths": paths}):
                result = probe.choose_files()
            self.assertEqual(result["status"], "error")
            self.assertIn("一次最多添加 150 个文件", result["message"])
            drop = probe.resolve_drop({"files": [{"name": "x", "size": 1}] * 151, "paths": []}, {"threadId": "A"})
            self.assertIn("150", drop["message"])

    def test_packages_can_be_picked_revealed_and_explain_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "Deck.key"
            package.mkdir()
            with patch.object(probe.PICKER, "select", return_value={"status": "selected", "source": "NSOpenPanel", "paths": [str(package)]}):
                result = probe.choose_files()
            self.assertTrue(result["allPathsExist"])
            self.assertTrue(result["files"][0]["isPackage"])
            with patch.object(probe.subprocess, "run", return_value=probe.subprocess.CompletedProcess([], 0, "", "")) as run:
                probe.FileActions().perform("reveal", str(package))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-R", str(package)])
            shown = preview.PreviewService(ROOT).prepare(str(package), "A")
            self.assertEqual((shown["kind"], shown["reason"]), ("unsupported", "package"))

    def test_text_encodings_and_cut_characters(self):
        cases = [("中文笔记".encode("gb18030"), "gb18030"), (codecs.BOM_UTF16_LE + "中文".encode("utf-16-le"), "utf-16-le"),
                 (codecs.BOM_UTF32_LE + "中文".encode("utf-32-le"), "utf-32-le"), ("中文".encode("utf-8"), "utf-8")]
        for data, encoding in cases:
            text, detected = preview.decode_text(data, False)
            self.assertEqual(detected, encoding)
            self.assertIn("中文", text)
        cut = ("参" * 3).encode("utf-8")[:-1]
        self.assertEqual(preview.decode_text(cut, True), ("参参", "utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            subtitles = Path(temp) / "片段.srt"
            subtitles.write_bytes("1\n00:00:01,000 --> 00:00:02,000\n镜头一\n".encode("gb18030"))
            shown = preview.PreviewService(ROOT).prepare(str(subtitles), "A")
            self.assertEqual(shown["kind"], "text")
            self.assertIn("镜头一", shown["text"])

    def test_documents_are_converted_locally_to_text(self):
        with tempfile.TemporaryDirectory() as temp:
            source, document = Path(temp) / "brief.txt", Path(temp) / "需求.docx"
            source.write_text("分镜需求：三个镜头")
            subprocess.run(["/usr/bin/textutil", "-convert", "docx", str(source), "-output", str(document)], check=True)
            shown = preview.PreviewService(ROOT).prepare(str(document), "A")
            self.assertEqual(shown["kind"], "text")
            self.assertTrue(shown["document"])
            self.assertIn("分镜需求", shown["text"])

    def test_unplayable_video_and_icloud_placeholder_skip_transfer(self):
        with tempfile.TemporaryDirectory() as temp:
            clip = Path(temp) / "camera.MTS"
            clip.write_bytes(b"\x47" * 188)
            shown = preview.PreviewService(ROOT).prepare(str(clip), "A")
            self.assertEqual((shown["kind"], shown["reason"]), ("unsupported", "codec"))
            self.assertNotIn("leaseId", shown)
            video = Path(temp) / "evicted.mp4"
            video.write_bytes(b"x")
            real = os.stat(video)
            evicted = type("Info", (), {name: getattr(real, name) for name in dir(real) if name.startswith("st_")})
            evicted.st_flags = preview.SF_DATALESS
            with patch.object(Path, "stat", return_value=evicted):
                shown = preview.PreviewService(ROOT).prepare(str(video), "A")
            self.assertEqual(shown["reason"], "icloud")

    def test_unreadable_renderer_and_page_errors_are_chinese(self):
        with tempfile.TemporaryDirectory() as temp:
            pdf = Path(temp) / "x.pdf"
            pdf.write_bytes(b"not a pdf")
            service = preview.PreviewService(ROOT)
            with self.assertRaisesRegex(preview.ToolError, "无法读取此 PDF"):
                service.prepare(str(pdf), "A")
            missing = preview.PreviewService(Path(temp))
            with self.assertRaisesRegex(preview.ToolError, "渲染器未安装"):
                missing.prepare(str(pdf), "A")


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.cache = self.folder / "thumbs"
        self.service = preview.Thumbnails(ROOT, lambda: self.cache)
        self.note = self.folder / "镜头笔记.txt"
        self.note.write_text("镜头一：推近\n镜头二：横移")

    def test_quick_look_thumbnail_is_cached_and_reused(self):
        first = self.service.get([str(self.note)])[str(self.note)]
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["mime"], "image/jpeg")
        self.assertTrue(base64.b64decode(first["data"]).startswith(b"\xff\xd8"))
        self.assertEqual(stat.S_IMODE(next(self.cache.glob("*.jpg")).stat().st_mode), 0o600)
        with patch.object(preview.subprocess, "run", side_effect=AssertionError("cache must be reused")):
            again = self.service.get([str(self.note)])[str(self.note)]
        self.assertEqual(again["data"], first["data"])
        os.utime(self.note, ns=(1, 1))  # a changed file gets a new thumbnail
        with patch.object(preview.subprocess, "run", side_effect=OSError("renderer gone")):
            self.assertEqual(self.service.get([str(self.note)])[str(self.note)]["status"], "none")

    def test_missing_renderer_caches_nothing_and_skips_icloud(self):
        broken = preview.Thumbnails(self.folder / "no-plugin", lambda: self.cache)
        self.assertEqual(broken.get([str(self.note)])[str(self.note)], {"status": "none"})
        self.assertFalse(any(self.cache.glob("*.json")))
        self.assertEqual(self.service.get(["/does/not/exist.mov"])["/does/not/exist.mov"]["status"], "missing")
        real = os.stat(self.note)
        evicted = type("Info", (), {name: getattr(real, name) for name in dir(real) if name.startswith("st_")})
        evicted.st_flags = preview.SF_DATALESS
        with patch.object(preview.os, "stat", return_value=evicted), \
             patch.object(preview.subprocess, "run", side_effect=AssertionError("must not download")):
            self.assertEqual(self.service.get([str(self.note)])[str(self.note)]["status"], "icloud")

    def test_cache_is_bounded(self):
        notes = []
        for index in range(4):
            path = self.folder / f"{index}.txt"
            path.write_text(str(index))
            notes.append(str(path))
        with patch.object(preview, "THUMB_CACHE_LIMIT", 2):
            self.service.get(notes)
        self.assertEqual(len(list(self.cache.glob("*.json"))), 2)

    def test_tool_only_serves_files_in_this_chats_list(self):
        with patch.object(probe, "LIST_DIR", self.folder / "lists"), patch.object(probe, "THUMBNAILS", self.service):
            call = lambda arguments, thread="A": probe.dispatch("tools/call", {"name": "read_reference_thumbnails", "arguments": arguments, "_meta": {"threadId": thread}})
            probe.dispatch("tools/call", {"name": "save_reference_list", "arguments": {"references": [{"path": str(self.note)}], "baseRevision": 0}, "_meta": {"threadId": "A"}})
            secret = str(self.folder / "other.txt")
            items = call({"paths": [str(self.note), secret]})["_meta"]["referenceThumbnails"]
            self.assertEqual(items[str(self.note)]["status"], "ok")
            self.assertEqual(items[secret], {"status": "not-listed"})
            self.assertEqual(call({"paths": [str(self.note)]}, "B")["_meta"]["referenceThumbnails"][str(self.note)]["status"], "not-listed")
            with self.assertRaises(ValueError):
                call({"paths": [str(self.note)] * 13})


class PickerLifecycleTests(unittest.TestCase):
    def test_cancel_during_cold_start_prevents_the_dialog(self):
        helper = probe.NativePicker()
        writes = []

        class Process:
            returncode = None
            stdin = type("In", (), {"write": lambda self, text: writes.append(text), "flush": lambda self: None})()

            def poll(self):
                return None

        def warm():
            helper.process = Process()
            return True

        def wait_ready(process, ready):
            helper.cancel()  # the user clicks 取消选择 while AppKit starts

        with patch.object(helper, "_warm", side_effect=warm), patch.object(helper, "_wait_ready", side_effect=wait_ready):
            result = helper.request("select", 1)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("select\n", writes)

    def test_helper_that_exits_before_ready_fails_at_once(self):
        helper = probe.NativePicker()
        process = subprocess.Popen(["/usr/bin/false"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        process.wait()
        helper.process = process
        with self.assertRaisesRegex(OSError, "退出码 1"):
            helper._wait_ready(process, threading.Event())

    def test_missing_binary_message_is_chinese(self):
        helper = probe.NativePicker()
        with patch.object(probe, "ROOT", Path("/does-not-exist")):
            self.assertFalse(helper.warm())
        self.assertIn("本机选择器未安装", helper.error)


class ProtocolTests(unittest.TestCase):
    def serve(self, lines):
        output = io.StringIO()
        with patch.object(probe.PICKER, "shutdown"):
            probe.serve(iter(lines), output)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_blank_lines_parse_errors_and_unknown_methods(self):
        rows = self.serve(["\n", "{bad json\n", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "nope"}) + "\n",
                           json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}}) + "\n"])
        self.assertEqual([row["error"]["code"] for row in rows], [-32700, -32601, -32601])

    def test_host_cancellation_closes_an_open_picker(self):
        started = threading.Event()
        release = threading.Event()

        def choose():
            started.set()
            release.wait(2)
            return {"status": "cancelled", "source": "NSOpenPanel", "files": []}

        def lines():
            yield json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "choose_reference_files"}}) + "\n"
            self.assertTrue(started.wait(1))
            yield json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}) + "\n"

        with tempfile.TemporaryDirectory() as temp, patch.object(probe, "PICKER_LOG", Path(temp) / "picker.jsonl"), \
             patch.object(probe, "choose_files", side_effect=choose), patch.object(probe.PICKER, "cancel", side_effect=release.set) as cancel:
            self.serve(lines())
        cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
