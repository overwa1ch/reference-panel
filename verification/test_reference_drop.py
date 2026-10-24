"""Local path-resolution fixtures; never drive Finder or read real drag data."""
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "reference_drop_server", Path(__file__).resolve().parents[1] / "plugins/reference-native-test/server.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class DropTests(unittest.TestCase):
    def setUp(self):
        probe.DRAG_CAPTURES.clear()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.first = Path(self.folder.name) / "中文 视频.mp4"
        self.second = Path(self.folder.name) / "another.mov"
        self.first.write_bytes(b"video fixture A")
        self.second.write_bytes(b"video fixture B")
        self.paths = [str(self.first), str(self.second)]
        self.files = [{"name": p.name, "size": p.stat().st_size} for p in [self.first, self.second]]
        self.meta = {"threadId": "fixture-thread"}

    def native(self):
        return {"status": "captured", "source": "NSPasteboardNameDrag", "paths": self.paths}

    def test_native_drag_without_browser_paths_adds_both_actual_files(self):
        with patch.object(probe.PICKER, "drag_paths", return_value=self.native()) as read:
            capture = probe.capture_drag(self.meta)
        read.assert_called_once()
        with patch.object(probe.PICKER, "drag_paths", side_effect=AssertionError("must reuse captured drag")):
            result = probe.resolve_drop({"files": self.files, "paths": [], "captureId": capture["id"]}, self.meta)
        self.assertEqual(result["status"], "selected")
        self.assertEqual([f["path"] for f in result["files"]], self.paths)
        self.assertEqual(result["source"], "NSPasteboardNameDrag.fileURL")
        self.assertNotIn(capture["id"], probe.DRAG_CAPTURES)
        self.assertEqual(self.first.read_bytes(), b"video fixture A")

    def test_browser_file_paths_are_validated_without_native_read(self):
        with patch.object(probe.PICKER, "drag_paths", side_effect=AssertionError("unnecessary read")):
            result = probe.resolve_drop({"files": self.files, "paths": self.paths}, self.meta)
        self.assertEqual(result["status"], "selected")

    def test_other_thread_capture_is_rejected(self):
        with patch.object(probe.PICKER, "drag_paths", return_value=self.native()):
            capture = probe.capture_drag(self.meta)
        with patch.object(probe.PICKER, "drag_paths", side_effect=AssertionError("must not re-read for another chat")):
            result = probe.resolve_drop({"files": self.files, "paths": [], "captureId": capture["id"]}, {"threadId": "other-thread"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["files"], [])

    def test_expired_capture_rereads_and_still_checks_names_and_sizes(self):
        with patch.object(probe.PICKER, "drag_paths", return_value=self.native()):
            capture = probe.capture_drag(self.meta)
        probe.DRAG_CAPTURES[capture["id"]]["time"] = time.monotonic() - 46
        with patch.object(probe.PICKER, "drag_paths", return_value=self.native()) as reread:
            result = probe.resolve_drop({"files": self.files, "paths": [], "captureId": capture["id"]}, self.meta)
        reread.assert_called_once()
        self.assertEqual(result["status"], "selected")
        stale = {"status": "captured", "paths": [str(self.second)]}
        with patch.object(probe.PICKER, "drag_paths", return_value=stale):
            result = probe.resolve_drop({"files": self.files, "paths": []}, self.meta)
        self.assertEqual(result["status"], "unavailable")

    def test_mismatched_or_partial_drag_adds_nothing(self):
        cases = [self.paths[:1], [self.paths[0], self.paths[0]]]
        for paths in cases:
            result = probe.resolve_drop({"files": self.files, "paths": paths}, self.meta)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["files"], [])
        bad_size = [dict(self.files[0], size=999), self.files[1]]
        result = probe.resolve_drop({"files": bad_size, "paths": self.paths}, self.meta)
        self.assertEqual(result["status"], "unavailable")

    def test_missing_drag_paths_has_actionable_feedback(self):
        with patch.object(probe.PICKER, "drag_paths", return_value={"status": "captured", "paths": []}):
            result = probe.resolve_drop({"files": self.files, "paths": []}, self.meta)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("添加", result["message"])

    def test_directory_virtual_path_and_relative_path_are_not_added(self):
        result = probe.resolve_drop({"files": [], "paths": [self.folder.name]}, self.meta)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("文件夹", result["message"])
        result = probe.resolve_drop({"files": self.files, "paths": ["/视频.mp4"]}, self.meta)
        self.assertEqual(result["status"], "unavailable")
        with self.assertRaises(ValueError):
            probe.resolve_drop({"files": self.files, "paths": ["relative.mp4"]}, self.meta)


if __name__ == "__main__":
    unittest.main()
