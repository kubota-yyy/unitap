import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unitap_pkg import commands, qa_repeat

MENU = "NEK/QA/AppMemo 1017 Home/Phase after"


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class QaRepeatGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "Assets").mkdir()
        (self.root / "Assets/Screen.cs").write_text("class Screen {}\n")
        git(self.root, "init", "-q")
        git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
        git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_qa_menus_are_guarded(self):
        self.assertTrue(qa_repeat.is_qa_menu(MENU))
        self.assertTrue(qa_repeat.is_qa_menu("QA/Run"))
        self.assertFalse(qa_repeat.is_qa_menu("NEK/Build/iOS"))
        self.assertFalse(qa_repeat.is_qa_menu("Tools/QAReport"))

    def test_unchanged_project_blocks_the_same_menu(self):
        self.assertIsNone(qa_repeat.check_repeat(self.root, MENU))
        qa_repeat.record_run(self.root, MENU, "s1")
        previous = qa_repeat.check_repeat(self.root, MENU)
        self.assertIsNotNone(previous)
        self.assertEqual("s1", previous["session"])
        # Another QA section is a different menu and stays allowed.
        self.assertIsNone(qa_repeat.check_repeat(self.root, "NEK/QA/AppMemo 1017 Home/Phase before"))

    def test_any_edit_allows_the_rerun(self):
        qa_repeat.record_run(self.root, MENU)
        (self.root / "Assets/Screen.cs").write_text("class Screen { int fixedIt; }\n")
        self.assertIsNone(qa_repeat.check_repeat(self.root, MENU))
        qa_repeat.record_run(self.root, MENU)
        (self.root / "Assets/NewQa.cs").write_text("class NewQa {}\n")  # untracked QA edit
        self.assertIsNone(qa_repeat.check_repeat(self.root, MENU))

    def test_files_outside_the_project_inputs_do_not_count(self):
        qa_repeat.record_run(self.root, MENU)
        (self.root / "Library/cache.bin").write_text("x")  # Library/ holds the guard's own state
        (self.root / "notes.txt").write_text("x")
        self.assertIsNotNone(qa_repeat.check_repeat(self.root, MENU))

    def test_guard_can_be_disabled(self):
        qa_repeat.record_run(self.root, MENU)
        with patch.dict(os.environ, {"UNITAP_QA_REPEAT_GUARD": "0"}):
            self.assertIsNone(qa_repeat.check_repeat(self.root, MENU))

    def test_without_git_the_guard_stays_out_of_the_way(self):
        with tempfile.TemporaryDirectory() as plain:
            qa_repeat.record_run(Path(plain), MENU)
            self.assertIsNone(qa_repeat.check_repeat(Path(plain), MENU))

    def test_execute_menu_refuses_and_allow_repeat_overrides(self):
        qa_repeat.record_run(self.root, MENU)
        args = SimpleNamespace(project=str(self.root), json=True, allow_repeat=False)
        out = io.StringIO()
        with patch.object(commands, "find_project_root", return_value=self.root), redirect_stdout(out):
            with self.assertRaises(SystemExit) as stop:
                commands._check_qa_repeat(args, MENU)
        self.assertEqual(2, stop.exception.code)
        self.assertEqual("qa_repeat_unchanged", json.loads(out.getvalue())["error"]["code"])
        args.allow_repeat = True
        with patch.object(commands, "find_project_root", return_value=self.root):
            commands._check_qa_repeat(args, MENU)  # no exit


if __name__ == "__main__":
    unittest.main()
