"""Transport/persistence fixtures. These are not native UI acceptance tests."""
import json
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1] / "plugins/reference-native-test/server.py"
spec = importlib.util.spec_from_file_location("reference_probe_server_test", SERVER)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NativeProbeTests(unittest.TestCase):
    def invoke(self, requests, log):
        run = subprocess.run(
            ["/usr/bin/python3", str(SERVER)], input="".join(json.dumps(r) + "\n" for r in requests),
            text=True, capture_output=True, timeout=10, check=True,
            env={**os.environ, "REFERENCE_PROBE_LOG": str(log), "REFERENCE_STATE_DIR": str(log.parent / "state")},
        )
        self.assertEqual(run.stderr, "")
        # Slow tools answer from a worker pool, so order replies by request id.
        return sorted((json.loads(line) for line in run.stdout.splitlines()), key=lambda row: row.get("id") or 0)

    @staticmethod
    def request(id_, method, params=None):
        return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}

    def test_stdio_resource_and_metadata_do_not_infer_project(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "probe.jsonl"
            requests = [self.request(1, "initialize", {"protocolVersion": "2025-06-18"}),
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        self.request(2, "tools/list"),
                        self.request(3, "resources/read", {"uri": probe.URI}),
                        self.request(4, "tools/call", {"name": "read_probe_context", "_meta": {"threadId": "fixture-A"}}),
                        self.request(5, "tools/call", {"name": "read_probe_context", "_meta": {"threadId": "fixture-B"}})]
            rows = self.invoke(requests, log)
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[0]["result"]["protocolVersion"], "2025-06-18")
            tools = rows[1]["result"]["tools"]
            self.assertEqual(tools[0]["_meta"]["openai/ui"]["entrypoints"], [{"type": "thread"}])
            for tool in tools:
                self.assertIn("app", tool["_meta"]["ui"]["visibility"])
            # The model can open and read the panel but never rewrite the list.
            model_tools = {t["name"] for t in tools if "model" in t["_meta"]["ui"]["visibility"]}
            self.assertEqual(model_tools, {"open_reference_probe", "read_probe_context"})
            resource = rows[2]["result"]["contents"][0]
            self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
            self.assertIn("ui/initialize", resource["text"])
            a, b = [row["result"]["structuredContent"]["probeContext"] for row in rows[3:]]
            self.assertEqual(a["requestMeta"], {"threadId": "fixture-A"})
            self.assertEqual(b["requestMeta"], {"threadId": "fixture-B"})
            self.assertEqual(a["serverCwd"], b["serverCwd"])
            self.assertNotIn("projectId", a)
            self.assertFalse(log.exists())
            self.assertFalse((log.parent / "state").exists(), "reading must never create the list store")

    def test_snapshot_appends_and_redacts_without_opening_file(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "nested/probe.jsonl"
            snapshot = {"label": "fixture 项目 A", "drops": [{"files": [{"path": "/does-not-exist/素材.mp4"}]}],
                        "authorization": "fixture-secret", "nested": {"api_key": "fixture-secret"}}
            rows = self.invoke([self.request(i, "tools/call", {"name": "save_probe_snapshot", "arguments": {"snapshot": snapshot},
                                                             "_meta": {"thread_id": str(i), "access_token": "fixture-secret"}})
                                for i in [1, 2]], log)
            self.assertTrue(all(r["result"]["structuredContent"]["saved"] for r in rows))
            lines = [json.loads(s) for s in log.read_text().splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertNotEqual(lines[0]["eventId"], lines[1]["eventId"])
            self.assertNotIn("fixture-secret", log.read_text())
            self.assertEqual(lines[0]["panel"]["drops"][0]["files"][0]["path"], "/does-not-exist/素材.mp4")
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_invalid_resource_and_snapshot_do_not_write(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "probe.jsonl"
            rows = self.invoke([self.request(1, "resources/read", {"uri": "file:///etc/passwd"}),
                                self.request(2, "tools/call", {"name": "save_probe_snapshot", "arguments": {"snapshot": "bad"}}),
                                self.request(3, "ping")], log)
            self.assertIn("error", rows[0])
            self.assertIn("error", rows[1])
            self.assertEqual(rows[2]["result"], {})
            self.assertFalse(log.exists())


class NativePickerTests(unittest.TestCase):
    @staticmethod
    def picker_output(value, returncode=0, stderr=""):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(value), stderr=stderr)

    def test_multi_select_real_paths_and_separate_log(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            first = folder / "中文 视频.mp4"
            second = folder / "文档\n'引号\".txt"
            first.write_bytes(b"fixture only")
            second.write_text("fixture only")
            paths = [str(first), str(second), str(first)]
            returned = {"status": "selected", "source": "NSOpenPanel", "paths": paths}
            output_log = folder / "file-picker.jsonl"
            with patch.object(probe.PICKER, "select", return_value=returned) as select, patch.object(probe, "PICKER_LOG", output_log):
                r = probe.dispatch("tools/call", {"name": "choose_reference_files", "_meta": {"threadId": "fixture-A"}})
            selection = r["structuredContent"]["selection"]
            self.assertEqual(selection["status"], "selected")
            self.assertEqual([f["path"] for f in selection["files"]], paths[:2])
            self.assertTrue(selection["allPathsExist"])
            self.assertTrue(selection["requiresFinderComparison"])
            self.assertTrue(r["structuredContent"]["pickerRecordSaved"])
            row = json.loads(output_log.read_text())
            self.assertEqual(row["toolContext"]["requestMeta"], {"threadId": "fixture-A"})
            self.assertEqual(len(row["selection"]["files"]), 2)
            self.assertEqual(stat.S_IMODE(output_log.stat().st_mode), 0o600)
            select.assert_called_once_with()
            self.assertEqual(first.read_bytes(), b"fixture only")

    def test_cancel_has_no_selected_files(self):
        returned = {"status": "cancelled", "source": "NSOpenPanel", "paths": []}
        with patch.object(probe.PICKER, "select", return_value=returned):
            r = probe.choose_files()
        self.assertEqual(r, {"status": "cancelled", "source": "NSOpenPanel", "files": []})

    def test_native_error_and_timeout_are_explicit(self):
        with patch.object(probe.PICKER, "select", side_effect=OSError("fixture native error")):
            self.assertEqual(probe.choose_files()["status"], "error")
        with patch.object(probe.PICKER, "select", side_effect=TimeoutError("fixture picker timeout")):
            self.assertEqual(probe.choose_files()["status"], "error")

    def test_relative_path_cannot_pass_as_native_absolute_path(self):
        with self.assertRaises(ValueError):
            probe.inspect_selected_paths(["/filename.png", "relative/path.png"])
        missing = probe.inspect_selected_paths(["/does-not-exist/fixture.txt"])
        self.assertFalse(missing[0]["exists"])


if __name__ == "__main__":
    unittest.main()
