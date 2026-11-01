import argparse
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from unitap_pkg import gui_launch, remote, remote_helper


def probe(free_slots=3, free=80.0, versions=("6000.3.24f1",), idle=None, mirrors=None, ok=True):
    if not ok:
        return {"ok": False, "error": {"message": "unreachable"}}
    return {"ok": True, "freeSlots": free_slots, "editorCount": 5 - free_slots, "maxEditors": 5,
            "memory": {"freePercent": free, "physicalBytes": 128 * 1024 ** 3}, "unityVersions": list(versions),
            "idleSeconds": idle, "mirrors": mirrors or []}


def at(hour, minute=0):
    return time.struct_time((2026, 11, 1, hour, minute, 0, 6, 305, 0))


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def make_project(self, project, deps):
        (project / "Packages").mkdir(parents=True)
        (project / "ProjectSettings").mkdir()
        (project / "Packages" / "manifest.json").write_text(json.dumps({"dependencies": deps}))
        return project

    def test_nek_layout_keeps_packages_inside_project(self):
        project = self.make_project(self.tmp / "nek-unity--duo-walk", {"com.nilone.unitap": "file:../unitap"})
        (project / "unitap" / "cli").mkdir(parents=True)
        layout = remote.project_layout(project)
        self.assertEqual("nek-unity", layout["key"])
        self.assertEqual(project.resolve(), layout["anchor"])
        self.assertEqual(".", layout["projectRel"])
        self.assertEqual(1, len(layout["items"]))

    def test_rwd_layout_anchors_at_repo_and_adds_outside_package_and_config(self):
        repo = self.tmp / "rwd--daily-release"
        project = self.make_project(repo / "rwd-unity", {"com.nilone.unitap": "file:../../unitap",
                                                           "x": "file:../Packages/G/x.tgz"})
        (repo / "unitap" / "cli").mkdir(parents=True)
        (project / "Packages" / "G").mkdir()
        (project / "Packages" / "G" / "x.tgz").write_text("x")
        (repo / "Artifacts" / "ReferenceImages").mkdir(parents=True)
        (project / "unitap-remote.json").write_text(json.dumps({"sync": ["../Artifacts/ReferenceImages"],
                                                                 "exclude": ["/Rom/"]}))
        layout = remote.project_layout(project)
        self.assertEqual("rwd", layout["key"])
        self.assertEqual(repo.resolve(), layout["anchor"])
        self.assertEqual("rwd-unity", layout["projectRel"])
        self.assertEqual(["rwd-unity", "unitap", "Artifacts/ReferenceImages"], [i["rel"] for i in layout["items"]])
        self.assertIn("/Rom/", layout["excludes"])

    def shared_project(self, shared):
        repo = self.tmp / "rwd--x"
        project = self.make_project(repo / "rwd-unity", {})
        (project / "Assets").mkdir()
        (project / "Assets" / "Defines.asset").write_text("  catalogVersion: 8.1.6\n")
        (repo / "rwd-master").mkdir()
        (project / "unitap-remote.json").write_text(json.dumps({"sync": ["../rwd-master"], "shared": shared}))
        assets = self.tmp / "rwd-remote-asset"
        for name in ("rwd-asset-8-1-5", "rwd-asset-8-1-6", "rwd-asset-8-1-6-ios"):
            (assets / name).mkdir(parents=True)
            (assets / name / "master.bytes").write_text(name)
        return project

    def test_shared_folder_beside_anchor_with_current_version_only(self):
        project = self.shared_project([{
            "path": "../../rwd-remote-asset", "only": ["rwd-asset-{version}", "rwd-asset-{version}-ios"],
            "versionFrom": {"file": "Assets/Defines.asset", "pattern": r"catalogVersion: (\S+)", "slug": True}}])
        layout = remote.project_layout(project)
        self.assertEqual([{"path": (self.tmp / "rwd-remote-asset").resolve(), "rel": "../rwd-remote-asset",
                           "only": ["rwd-asset-8-1-6", "rwd-asset-8-1-6-ios"]}], layout["shared"])
        calls = []
        state = self.tmp / "shared.json"
        with patch.object(remote, "_shared_state_file", return_value=state), \
                patch.object(remote.subprocess, "run"), \
                patch.object(remote, "_rsync", side_effect=lambda args, timeout=3600: calls.append(args[-1])):
            self.assertEqual(["../rwd-remote-asset"], remote.sync_shared(layout, "e@h", "/r/rwd/m1"))
            self.assertEqual(["e@h:/r/rwd/rwd-remote-asset/rwd-asset-8-1-6/",
                              "e@h:/r/rwd/rwd-remote-asset/rwd-asset-8-1-6-ios/"], calls)
            self.assertEqual([], remote.sync_shared(layout, "e@h", "/r/rwd/m1"))  # unchanged: skipped
            (self.tmp / "rwd-remote-asset" / "rwd-asset-8-1-6" / "master.bytes").write_text("new master")
            self.assertEqual(["../rwd-remote-asset"], remote.sync_shared(layout, "e@h", "/r/rwd/m1"))

    def test_shared_folder_must_sit_beside_anchor(self):
        project = self.shared_project(["../../../elsewhere"])
        with self.assertRaises(remote.RemoteError):
            remote.project_layout(project)


class ArgsAndPathsTests(unittest.TestCase):
    def test_rewrite_args_drops_project_and_host_and_maps_paths(self):
        anchor = Path("/w/nek-unity")
        argv = ["--project", "/w/nek-unity", "--host=auto", "--json", "wait_result", "--done",
                "/w/nek-unity/.tmp/qa-runs/1/done.txt", "--fail", "/w/nek-unity-other/x"]
        out = remote.rewrite_args(argv, anchor, "/r/m1")
        self.assertEqual(["--json", "wait_result", "--done", "/r/m1/.tmp/qa-runs/1/done.txt",
                          "--fail", "/w/nek-unity-other/x"], out)

    def test_tar_copy_anchors_leading_slash_patterns_only(self):
        with patch.object(remote.subprocess, "Popen") as popen, patch.object(remote.subprocess, "run") as run:
            popen.return_value.communicate.return_value = (b"", b"")
            popen.return_value.returncode = 0
            run.return_value.returncode = 0
            remote._tar_copy(Path("/src"), "u@h", "/r/m1/Library", ["/Bee/", ".git", "/IOS_Distribution*/"], 10)
        pack = popen.call_args[0][0]
        self.assertEqual(["--exclude=./Bee", "--exclude=.git", "--exclude=./IOS_Distribution*"], pack[3:6])
        self.assertIn("tar -xf - -C /r/m1/Library", run.call_args[0][0][-1])

    def test_sync_never_deletes_the_mirror_editors_user_settings(self):
        project = Path("/w/rwd--x/rwd-unity")
        layout = {"project": project, "excludes": list(remote.DEFAULT_EXCLUDES),
                  "items": [{"path": project, "rel": "rwd-unity", "dir": True}]}
        with patch.object(remote, "_rsync") as rsync:
            remote.sync_tree(layout, "u@h", "/r/m2")
        args = rsync.call_args[0][0]
        self.assertIn("--delete", args)
        self.assertIn("--exclude=/UserSettings/", args)

    def test_mirror_paths_found_in_json_output(self):
        text = json.dumps({"runDir": "/r/m1/.tmp/qa-runs/7", "images": ["/r/m1/Artifacts/a.png"], "x": "/r/m10/y"})
        self.assertEqual(["/.tmp/qa-runs/7", "/Artifacts/a.png"], remote._mirror_paths_in(text, "/r/m1"))

    def test_pull_never_takes_a_mirrored_root_or_a_parent(self):
        layout = {"anchor": Path("/w/rwd"), "items": [{"rel": "rwd-unity"}, {"rel": "unitap"}]}
        with patch.object(remote, "_rsync") as rsync, patch.object(remote, "_tracked", return_value=False):
            pulled, skipped = remote.pull_paths(layout, "h", "/r/m1", ["/rwd-unity", "", "/../etc"])
        rsync.assert_not_called()
        self.assertEqual([], pulled)
        self.assertEqual(3, len(skipped))

    def test_pull_skips_editor_state_and_takes_outputs(self):
        layout = {"anchor": Path("/w/rwd"), "projectRel": "rwd-unity",
                  "items": [{"rel": "rwd-unity"}, {"rel": "unitap"}]}
        with patch.object(remote, "_rsync") as rsync, patch.object(remote, "_tracked", return_value=False), \
             patch.object(Path, "mkdir"):
            pulled, skipped = remote.pull_paths(layout, "h", "/r/m1", [
                "/rwd-unity/Assets/../Library/Unitap/file-transport", "/rwd-unity/Temp/x",
                "/Artifacts/Reports/20261101", "/rwd-unity/.tmp/qa-runs/5"])
        self.assertEqual(["/w/rwd/Artifacts/Reports/20261101", "/w/rwd/rwd-unity/.tmp/qa-runs/5"], pulled)
        self.assertEqual(2, rsync.call_count)
        self.assertEqual(["rwd-unity/Library/Unitap/file-transport", "rwd-unity/Temp/x"], skipped)


class EvaluateTests(unittest.TestCase):
    def test_reserved_window_crosses_midnight(self):
        config = {"reserved": [{"from": "23:00", "to": "07:30", "why": "nightly"}]}
        self.assertIsNotNone(remote.reserved_now(config, at(2)))
        self.assertIsNotNone(remote.reserved_now(config, at(23, 30)))
        self.assertIsNone(remote.reserved_now(config, at(12)))

    def test_ineligible_reasons(self):
        config = {"reserved": [{"from": "01:00", "to": "07:00"}]}
        result = remote.evaluate("harry", config, probe(free_slots=0, free=10, versions=()),
                                 version="6000.3.24f1", session="s", is_local=False, now=at(3))
        self.assertFalse(result["eligible"])
        self.assertEqual(4, len(result["reasons"]))

    def test_person_at_interactive_host_lowers_score_but_keeps_it(self):
        quiet = remote.evaluate("eipoc", {"interactive": True}, probe(idle=3600), version=None, session=None, is_local=False)
        busy = remote.evaluate("eipoc", {"interactive": True}, probe(idle=5), version=None, session=None, is_local=False)
        self.assertTrue(busy["eligible"])
        self.assertLess(busy["score"], quiet["score"])
        avoided = remote.evaluate("eipoc", {"interactive": True, "avoidWhenActive": True}, probe(idle=5),
                                  version=None, session=None, is_local=False)
        self.assertFalse(avoided["eligible"])

    def test_own_mirror_preferred(self):
        mine = remote.evaluate("a", {}, probe(mirrors=[{"owner": {"session": "s"}}]), version=None, session="s", is_local=False)
        other = remote.evaluate("b", {}, probe(), version=None, session="s", is_local=False)
        self.assertGreater(mine["score"], other["score"])


class DecideTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(Path, "home", return_value=self.home))
        stack.enter_context(patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abc"}, clear=False))
        for key in ("UNITAP_HOST", "CODEX_THREAD_ID", "UNITAP_SESSION"):
            os.environ.pop(key, None)
        hosts = self.home / "hosts.json"
        hosts.write_text(json.dumps({"hosts": {"me": {}, "big": {"ssh": "u@big"}, "small": {"ssh": "u@small"}}}))
        os.environ["UNITAP_HOSTS_FILE"] = str(hosts)
        stack.enter_context(patch.object(remote, "local_host_name", return_value="me"))
        self.project = self.home / "proj"
        (self.project / "Packages").mkdir(parents=True)
        (self.project / "Packages" / "manifest.json").write_text("{}")

    def args(self, command="compile_check", host=None):
        return argparse.Namespace(command=command, host=host)

    def test_no_agent_session_stays_local(self):
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        os.environ.pop("STARSESSION_SESSION_ID", None)
        self.assertIsNone(remote.decide(self.args(), self.project))

    def test_local_room_stays_local(self):
        with patch.object(remote, "local_has_room", return_value=(True, "free")):
            self.assertIsNone(remote.decide(self.args(), self.project))

    def test_busy_local_picks_best_remote_and_placement_sticks(self):
        probes = {"big": probe(free_slots=4), "small": probe(free_slots=1)}
        with patch.object(remote, "local_has_room", return_value=(False, "full")), \
             patch.object(remote, "probe_hosts", return_value=probes):
            placement = remote.decide(self.args(), self.project)
        self.assertEqual("big", placement["host"])
        self.assertEqual("claude:abc", placement["session"])
        remote.save_placement("claude:abc", self.project, dict(placement, label="m1", root="/r/m1"))
        with patch.object(remote, "local_has_room", side_effect=AssertionError("must not re-decide")):
            again = remote.decide(self.args("status"), self.project)
        self.assertEqual(("big", "m1"), (again["host"], again["label"]))
        self.assertIsNone(remote.decide(self.args(host="local"), self.project))
        remote.drop_placement("claude:abc", self.project)
        self.assertIsNone(remote.decide(self.args("status"), self.project))

    def test_nothing_eligible_stays_local(self):
        probes = {"big": probe(ok=False), "small": probe(free_slots=0)}
        with patch.object(remote, "local_has_room", return_value=(False, "full")), \
             patch.object(remote, "probe_hosts", return_value=probes):
            self.assertIsNone(remote.decide(self.args(), self.project))

    def test_local_only_and_unknown_host(self):
        self.assertIsNone(remote.decide(self.args("hosts", host="big"), self.project))
        with self.assertRaises(remote.RemoteError):
            remote.decide(self.args(host="nowhere"), self.project)


class HelperMirrorTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.base = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "remote"
        stack.enter_context(patch.object(remote_helper, "BASE", self.base))
        stack.enter_context(patch.object(remote_helper, "unity_editors", return_value=[]))

    def test_claim_reuse_seed_and_release(self):
        first = remote_helper.claim({"key": "nek-unity", "session": "a", "projectRel": "."})
        self.assertTrue(first["created"])
        self.assertFalse(first["seeded"])
        root = Path(first["root"])
        (root / "Library" / "Unitap").mkdir(parents=True)
        (root / "Library" / "ArtifactDB").write_text("db")
        (root / ".tmp").mkdir()
        self.assertTrue(remote_helper.touch({"key": "nek-unity", "label": first["label"], "session": "a",
                                             "libraryReady": True, "lastSyncAt": time.time()})["ok"])
        self.assertEqual(first["label"], remote_helper.claim({"key": "nek-unity", "session": "a"})["label"])
        second = remote_helper.claim({"key": "nek-unity", "session": "b", "projectRel": ".", "clearOnSeed": [".tmp"]})
        self.assertNotEqual(first["label"], second["label"])
        self.assertTrue(second["seeded"])
        copy = Path(second["root"])
        self.assertTrue((copy / "Library" / "ArtifactDB").exists())
        self.assertFalse((copy / "Library" / "Unitap").exists())
        self.assertFalse((copy / ".tmp").exists())
        self.assertFalse(remote_helper.touch({"key": "nek-unity", "label": second["label"], "session": "a"})["ok"])
        self.assertTrue(remote_helper.release({"key": "nek-unity", "label": first["label"], "session": "a"})["released"])
        third = remote_helper.claim({"key": "nek-unity", "session": "c"})
        self.assertEqual(first["label"], third["label"])

    def test_folders_unitap_did_not_make_are_not_mirrors(self):
        (self.base / "rwd" / "rwd-remote-asset").mkdir(parents=True)
        claimed = remote_helper.claim({"key": "rwd", "session": "s"})
        self.assertNotEqual("rwd-remote-asset", claimed["label"])
        self.assertEqual([claimed["label"]], [m["label"] for m in remote_helper._mirrors("rwd")])

    def test_one_session_with_two_clones_gets_two_mirrors(self):
        a = remote_helper.claim({"key": "rwd", "session": "s", "sourcePath": "/w/rwd--a/rwd-unity"})
        b = remote_helper.claim({"key": "rwd", "session": "s", "sourcePath": "/w/rwd--b/rwd-unity"})
        self.assertNotEqual(a["label"], b["label"])
        again = remote_helper.claim({"key": "rwd", "session": "s", "sourcePath": "/w/rwd--a/rwd-unity"})
        self.assertEqual(a["label"], again["label"])

    def test_stale_owner_gives_mirror_up(self):
        first = remote_helper.claim({"key": "k", "session": "a"})
        owner = remote_helper._owner_path("k", first["label"])
        data = json.loads(owner.read_text())
        data["updatedAt"] = time.time() - remote_helper.OWNER_STALE_SECONDS - 5
        owner.write_text(json.dumps(data))
        self.assertEqual(first["label"], remote_helper.claim({"key": "k", "session": "b"})["label"])

    def test_mirror_editor_matches_project_at_mirror_root_or_below(self):
        editors = [{"pid": 1, "projectPath": "/b/k/m1"}, {"pid": 2, "projectPath": "/b/k/m2/rwd-unity"},
                   {"pid": 3, "projectPath": "/b/k/m10"}]
        self.assertEqual([1], [e["pid"] for e in remote_helper._mirror_editors("/b/k/m1", editors)])
        self.assertEqual([2], [e["pid"] for e in remote_helper._mirror_editors("/b/k/m2", editors)])

    def test_bad_key_refused(self):
        self.assertFalse(remote_helper.claim({"key": "../x", "session": "a"})["ok"])

    def test_reap_quits_only_forgotten_idle_editors(self):
        claims = {s: remote_helper.claim({"key": "k", "session": s, "sourcePath": f"/w/{s}"})
                  for s in ("fresh", "forgot", "busy")}
        roots = {s: c["root"] for s, c in claims.items()}
        for session, claimed in claims.items():
            if session != "fresh":
                owner = remote_helper._owner_path("k", claimed["label"])
                data = json.loads(owner.read_text())
                data["updatedAt"] = time.time() - remote_helper.OWNER_STALE_SECONDS - 5
                owner.write_text(json.dumps(data))
        editors = [{"pid": 10 + i, "projectPath": roots[s]} for i, s in enumerate(("fresh", "forgot", "busy"))]
        ps = [f"500 python3 {roots['busy']}/unitap/cli/unitap.py --json wait_result"]
        with patch.object(remote_helper, "unity_editors", return_value=editors), \
                patch.object(remote_helper, "_out", return_value="\n".join(ps)), \
                patch.object(remote_helper, "_quit_editor") as quit_editor:
            result = remote_helper.reap({})
        self.assertEqual([[11]], [m["pids"] for m in result["reaped"]])
        self.assertEqual(1, quit_editor.call_count)
        self.assertEqual(1, len(result["keptBusy"]))


class RelinkTests(unittest.TestCase):
    def test_absolute_links_into_the_source_anchor_point_into_the_mirror(self):
        tmp = Path(tempfile.mkdtemp())
        source, root = tmp / "src", tmp / "mirror"
        (root / "master" / "A").mkdir(parents=True)
        db = root / "proj" / "Assets" / "DB"
        db.mkdir(parents=True)
        os.symlink(str(source / "master" / "A"), db / "A")
        os.symlink("../relative", db / "rel")
        os.symlink("/nowhere/else", db / "outside")
        result = remote_helper.relink({"root": str(root), "projectRel": "proj", "source": str(source)})
        self.assertEqual(1, result["relinked"])
        self.assertEqual(str(root / "master" / "A"), os.readlink(db / "A"))
        self.assertTrue((db / "A").is_dir())
        self.assertEqual("../relative", os.readlink(db / "rel"))
        self.assertEqual(["Assets/DB/outside"], result["dangling"])

    def test_listed_links_are_created_once_and_then_left_alone(self):
        tmp = Path(tempfile.mkdtemp())
        root = tmp / "mirror"
        (root / "master" / "A").mkdir(parents=True)
        (root / "proj" / "Assets").mkdir(parents=True)
        params = {"root": str(root), "projectRel": "proj", "source": str(tmp / "src"),
                  "links": [["Assets/DB/A", "master/A"]]}
        self.assertEqual(1, remote_helper.relink(params)["relinked"])
        self.assertEqual(str(root / "master" / "A"), os.readlink(root / "proj" / "Assets" / "DB" / "A"))
        self.assertEqual(0, remote_helper.relink(params)["relinked"])

    def test_anchor_links_stay_out_of_rsync(self):
        tmp = Path(tempfile.mkdtemp())
        anchor = tmp / "rwd"
        project = anchor / "rwd-unity"
        (anchor / "rwd-master" / "A").mkdir(parents=True)
        db = project / "Assets" / "DB"
        db.mkdir(parents=True)
        os.symlink(str(anchor / "rwd-master" / "A"), db / "A@NORMAL@x")
        os.symlink("../relative", db / "rel")
        layout = {"anchor": anchor, "project": project, "excludes": [],
                  "items": [{"path": project, "rel": "rwd-unity", "dir": True}]}
        links = remote.anchor_links(layout)
        self.assertEqual([["Assets/DB/A@NORMAL@x", "rwd-master/A"]], links)
        listed = []

        def fake_rsync(args, timeout=3600):
            listing = next(a for a in args if a.startswith("--exclude-from="))
            listed.append(Path(listing.split("=", 1)[1]).read_text())

        with patch.object(remote, "_rsync", side_effect=fake_rsync):
            remote.sync_tree(layout, "u@h", "/r/m1", links)
        self.assertEqual(["/Assets/DB/A@NORMAL@x\n"], listed)


class GuiLaunchTests(unittest.TestCase):
    def test_wanted_only_over_ssh_on_mac(self):
        with patch.object(gui_launch.sys, "platform", "darwin"):
            self.assertTrue(gui_launch.wanted({"SSH_CONNECTION": "1 2 3 4"}))
            self.assertFalse(gui_launch.wanted({"SSH_AUTH_SOCK": "/x"}))
            self.assertFalse(gui_launch.wanted({"SSH_CONNECTION": "1", "UNITAP_GUI_LAUNCH": "0"}))
            self.assertTrue(gui_launch.wanted({"UNITAP_GUI_LAUNCH": "1"}))
        with patch.object(gui_launch.sys, "platform", "linux"):
            self.assertFalse(gui_launch.wanted({"SSH_CONNECTION": "1"}))


if __name__ == "__main__":
    unittest.main()
