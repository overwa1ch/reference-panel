"""Explicit agent import and category updates; isolated fixtures only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/reference-native-test'))
import server as probe


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.root = self.folder / 'project'
        self.root.mkdir()
        self.files = [self.root / f'{i}.txt' for i in range(4)]
        for path in self.files:
            path.write_text('original fixture')
        for key, val in [('LIST_DIR', self.folder / 'state/lists'), ('PROJECTS', probe.ProjectScopes())]:
            p = patch.object(probe, key, val); p.start(); self.addCleanup(p.stop)
        self.scope = probe.PROJECTS.resolve({'projectRoot': str(self.root)})

    def edit(self, files=None, category=None, classify=False):
        return probe.edit_reference_files(self.scope, [str(p) for p in (files or self.files)], self.scope['scopeId'], category, classify)

    def test_import_is_additive_idempotent_and_does_not_read_contents(self):
        # Only state JSON can be read. Media must never be opened by import.
        original = Path.open
        def guard(path, *args, **kwargs):
            self.assertNotIn(path, self.files)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guard):
            first = self.edit(category='GoldSrc')
            second = self.edit(category='Different')
        self.assertEqual(len(first['added']), 4)
        self.assertEqual(second['revision'], first['revision'])
        self.assertEqual(second['added'], [])
        self.assertTrue(all(f['category'] == 'GoldSrc' for f in second['references']))
        self.assertEqual(self.files[0].read_text(), 'original fixture')

    def test_category_update_clear_and_missing_file(self):
        self.edit(category='GoldSrc')
        self.files[0].unlink()
        changed = self.edit([self.files[0]], 'Source', True)
        self.assertEqual(changed['references'][0]['category'], 'Source')
        cleared = self.edit([self.files[0]], '', True)
        self.assertEqual(cleared['references'][0]['category'], '')
        self.assertEqual(self.edit([self.files[0]], '', True)['revision'], cleared['revision'])

    def test_invalid_batch_is_atomic_and_scopes_are_checked(self):
        with self.assertRaises(probe.ToolError):
            self.edit([self.files[0], self.root / 'missing.txt'])
        self.assertEqual(probe.STORE.load(self.scope['owner']), (0, []))
        for scope_id in ['', 'other-project']:
            with self.assertRaises(probe.ToolError):
                probe.edit_reference_files(self.scope, [str(self.files[0])], scope_id)
        self.assertEqual(probe.STORE.load(self.scope['owner']), (0, []))

    def test_outside_directory_symlink_and_special_files_are_rejected(self):
        outside = self.folder / 'outside.txt'; outside.write_text('outside')
        alias = self.root / 'escape.txt'; alias.symlink_to(outside)
        fifo = self.root / 'pipe'; os.mkfifo(fifo)
        for path in [outside, alias, self.root, fifo, Path('relative.txt')]:
            with self.subTest(path=path), self.assertRaises(probe.ToolError):
                self.edit([path])
        self.assertEqual(probe.STORE.load(self.scope['owner']), (0, []))

    def test_category_requires_listed_paths_and_valid_label(self):
        self.edit([self.files[0]])
        for label in ['x\ny', 'x'*81, 123, None]:
            with self.subTest(label=label), self.assertRaises(probe.ToolError):
                self.edit([self.files[0]], label, True)
        with self.assertRaises(probe.ToolError):
            self.edit(self.files[:2], 'Source', True)
        self.assertNotIn('category', probe.STORE.load(self.scope['owner'])[1][0])

    def test_concurrent_imports_preserve_all_additions_and_old_panels_conflict(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda p: self.edit([p], 'Group'), self.files))
        revision, refs = probe.STORE.load(self.scope['owner'])
        self.assertEqual(len(refs), 4)
        self.assertEqual(probe.STORE.save(self.scope['owner'], [], 0)['status'], 'conflict')
        old = [{k:v for k,v in f.items() if k != 'category'} for f in refs]
        saved = probe.STORE.save(self.scope['owner'], old, revision)
        self.assertTrue(all(f['category'] == 'Group' for f in saved['references']))

    def test_mcp_surface_and_other_project_isolation(self):
        result = probe.dispatch('tools/call', {'name':'import_reference_files',
            'arguments':{'paths':[str(self.files[0])], 'scopeId':self.scope['scopeId'], 'category':'Test'},
            '_meta':{'projectRoot':str(self.root)}})
        self.assertEqual(len(result['structuredContent']['list']['added']), 1)
        other = probe.PROJECTS.resolve({'projectRoot':str(self.folder / 'different')})
        self.assertEqual(probe.STORE.load(other['owner']), (0, []))

    def test_limits_reject_without_mutation(self):
        for paths in [[], [str(self.files[0])]*151]:
            with self.assertRaises(probe.ToolError):
                probe.edit_reference_files(self.scope, paths, self.scope['scopeId'])
        with patch.object(probe, 'MAX_REFERENCES', 2), self.assertRaises(probe.ToolError):
            self.edit()
        self.assertEqual(probe.STORE.load(self.scope['owner']), (0, []))

    def test_cli_uses_same_scoped_operations(self):
        paths = self.folder / 'paths.json'; paths.write_text(json.dumps([str(self.files[0])]))
        script = Path(__file__).resolve().parents[1] / 'scripts/reference-files.py'
        env = {**os.environ, 'REFERENCE_STATE_DIR': str(self.folder / 'state')}
        run = subprocess.run([sys.executable, str(script), 'import', '--project', str(self.root),
            '--scope-id', self.scope['scopeId'], '--paths-json', str(paths), '--category', 'CLI'],
            env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout)['total'], 1)
        self.assertEqual(probe.STORE.load(self.scope['owner'])[1][0]['category'], 'CLI')


if __name__ == '__main__':
    unittest.main()
