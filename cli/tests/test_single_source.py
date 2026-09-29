import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unitap_pkg import clone, identity

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _make_project(root: Path, manifest_ref: str | None) -> Path:
    (root / "Assets").mkdir(parents=True)
    (root / "ProjectSettings").mkdir()
    (root / "Packages").mkdir()
    deps = {identity.PACKAGE_NAME: manifest_ref} if manifest_ref else {}
    (root / "Packages" / "manifest.json").write_text(json.dumps({"dependencies": deps}))
    return root


class IdentityTests(unittest.TestCase):
    def test_matches_only_when_manifest_resolves_to_this_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            project = _make_project(tmp / "game", "file:../../unitap")
            with patch.object(identity, "cli_package_root", return_value=tmp / "unitap"):
                self.assertTrue(identity.check_cli_identity(project)["matches"])
            with patch.object(identity, "cli_package_root", return_value=tmp / "other" / "unitap"):
                self.assertFalse(identity.check_cli_identity(project)["matches"])

    def test_projects_without_unitap_are_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp) / "game", None)
            self.assertIsNone(identity.check_cli_identity(project)["matches"])


class SubmoduleCloneTests(unittest.TestCase):
    def _setup(self, tmp: Path) -> tuple[Path, Path]:
        pkg = tmp / "pkgsrc"
        pkg.mkdir()
        (pkg / "package.json").write_text('{"name": "com.nilone.unitap"}')
        _git(pkg, "init", "-q")
        _git(pkg, "add", ".")
        _git(pkg, "commit", "-qm", "pkg")
        repo = tmp / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        project = _make_project(repo / "game", "file:../../unitap")
        (repo / ".gitignore").write_text("Library/\n")
        _git(repo, "submodule", "add", "-q", str(pkg), "unitap")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        return repo, project

    def test_clone_checks_out_submodule_package_and_protects_its_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            repo, project = self._setup(tmp)
            with patch.object(clone, "list_unity_processes", return_value=[]):
                result = clone.create_clone(project, label="s2", copy_library=False)
                cloned = Path(result["projectPath"])
                self.assertEqual([], result["unresolvedFilePackages"])
                self.assertTrue((tmp / "repo--s2" / "unitap" / "package.json").exists())
                self.assertEqual("", _git(tmp / "repo--s2", "status", "--porcelain").strip())

                sub = tmp / "repo--s2" / "unitap"
                _git(sub, "switch", "-q", "-c", "work")
                (sub / "x.txt").write_text("x")
                _git(sub, "add", ".")
                _git(sub, "commit", "-qm", "work in clone")
                with self.assertRaises(RuntimeError) as raised:
                    clone.remove_clone(project, cloned)
                self.assertIn("unitap", str(raised.exception))
                clone.remove_clone(project, cloned, force=True)
            self.assertFalse((tmp / "repo--s2").exists())


if __name__ == "__main__":
    unittest.main()
