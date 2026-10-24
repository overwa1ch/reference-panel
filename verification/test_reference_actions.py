"""Stall, file-action and restoration fixtures. No real GUI is launched."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "reference_action_server", Path(__file__).resolve().parents[1] / "plugins/reference-native-test/server.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ActionsTests(unittest.TestCase):
    def test_slow_picker_does_not_block_ping_or_cancel(self):
        release = threading.Event()
        started = threading.Event()

        class Output(io.StringIO):
            def write(self, text):
                result = super().write(text)
                if '"id": 2' in text:
                    release.set()
                return result

        def choose():
            started.set()
            if not release.wait(2):
                raise AssertionError("ping blocked behind the picker")
            return {"status": "cancelled", "source": "NSOpenPanel", "files": []}

        def requests():
            yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "choose_reference_files"}}) + "\n"
            self.assertTrue(started.wait(1))
            yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
            yield json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cancel_reference_selection"}}) + "\n"

        with tempfile.TemporaryDirectory() as temp, patch.object(probe, "PICKER_LOG", Path(temp)/"picker.jsonl"), \
             patch.object(probe, "choose_files", side_effect=choose), patch.object(probe.PICKER, "shutdown"), \
             patch.object(probe.PICKER, "cancel") as cancel:
            output = Output()
            probe.serve(requests(), output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]["id"], 2)
        self.assertTrue(all("result" in row for row in rows))
        cancel.assert_called_once()

    def test_busy_helper_fails_fast_instead_of_waiting_behind_a_picker(self):
        helper = probe.NativePicker()
        helper.request_lock.acquire()
        try:
            with self.assertRaises(BlockingIOError):
                helper.drag_paths()
        finally:
            helper.request_lock.release()

    def test_file_actions_use_argument_vectors_and_never_delete_files(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp)/"中文 文档 'name'.txt"
            file.write_bytes(b"fixture document")
            actions = probe.FileActions()
            process = MagicMock()
            process.poll.return_value = None
            process.wait.side_effect = [probe.subprocess.TimeoutExpired("qlmanage", .8), 0]
            with patch.object(probe.subprocess, "Popen", return_value=process) as launch:
                result = actions.perform("preview", str(file))
            self.assertEqual(result["status"], "requested")
            self.assertEqual(launch.call_args.args[0], ["/usr/bin/qlmanage", "-p", str(file)])
            with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")) as run:
                actions.perform("reveal", str(file))
                self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-R", str(file)])
                actions.perform("open", str(file))
                self.assertEqual(run.call_args.args[0], ["/usr/bin/open", str(file)])
            actions.close()
            self.assertEqual(file.read_bytes(), b"fixture document")

    def test_video_preview_uses_system_player_including_uppercase_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp)/"中文 视频.MOV"
            file.write_bytes(b"fixture")
            with patch.object(probe.subprocess, "Popen") as quicklook, \
                 patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")) as run:
                result = probe.FileActions().perform("preview", str(file))
            quicklook.assert_not_called()
            self.assertEqual(run.call_args.args[0], ["/usr/bin/open", str(file)])
            self.assertEqual(result["handler"], "system-player")

    def test_crashed_preview_is_reported_as_error(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp)/"fixture.pdf"
            file.write_bytes(b"fixture")
            process = MagicMock()
            process.wait.return_value = -6
            with patch.object(probe.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(OSError, "系统预览启动失败"):
                    probe.FileActions().perform("preview", str(file))

    def test_replaced_working_directory_does_not_break_tool_context(self):
        with patch.object(probe.os, "getcwd", side_effect=FileNotFoundError("removed working directory")):
            result = probe.context({"threadId": "fixture"})
        self.assertIsNone(result["serverCwd"])
        self.assertEqual(result["requestMeta"]["threadId"], "fixture")

    def test_missing_file_does_not_launch_system_actions(self):
        with patch.object(probe.subprocess, "Popen") as launch:
            with self.assertRaises(ValueError):
                probe.FILE_ACTIONS.perform("preview", "/does-not-exist/reference-fixture.mov")
        launch.assert_not_called()

    def test_restore_latest_chat_list_and_respect_removal(self):
        # 0.4.x lists inside the diagnostic log remain readable until first save.
        with tempfile.TemporaryDirectory() as temp, patch.object(probe, "LOG", Path(temp)/"records.jsonl"), \
             patch.object(probe, "LIST_DIR", Path(temp)/"lists"):
            def record(owner, references):
                probe.append_record(probe.LOG, {"toolContext": {"requestMeta": {"threadId": owner}}, "panel": {"references": references}})
            file = {"name": "fixture.mov", "path": "/fixture/reference.mov"}
            record("A", [file])
            record("B", [{"name": "other.mov", "path": "/fixture/other.mov"}])
            self.assertEqual(probe.saved_references({"threadId": "A"}), [file])
            record("A", [])
            self.assertEqual(probe.saved_references({"threadId": "A"}), [])
            self.assertEqual(len(probe.saved_references({"threadId": "B"})), 1)


if __name__ == "__main__":
    unittest.main()
