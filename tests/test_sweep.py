#!/usr/bin/env python3
"""`slotfather sweep` (#662): the classes decided on fake process tables, containers and memory from fake facts,
the settings, the launchd agent and the stats; and through real short-lived processes the tests start themselves
(an orphan by a double fork, a process whose working directory was removed, one that ignores SIGTERM, and leased
processes that must be spared). Every live sweep here is limited to the test's own pids with --pid."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import platform as sf_platform


import json
import os
import pathlib
import plistlib
import signal
import subprocess
import sys
import textwrap
import time
import unittest
import unittest.mock

from test_slotfather import TOOL, SlotCase
from test_usage import load

sweep = load(TOOL + "-sweep", "slotfather_sweep_under_test")

NOW = 1_800_000_000.0
UID = 501
HOME = os.path.expanduser("~")
SETTINGS = {**sweep.DEFAULTS, "classes": list(sweep.CLASSES)}


def proc(pid, ppid, cmd, age=36000.0, uid=UID, tty="", rss=10240):
    return sweep.Proc(pid, ppid, uid, rss, tty, f"start-{pid}", NOW - age, cmd)


def snap(procs, cwds=None, managed=(), gone=(), me=(999,)):
    """A snapshot of `procs`; directories listed in `gone` (and below them) do not exist."""
    def isdir(path):
        return not any(path == g or path.startswith(g.rstrip("/") + "/") for g in gone)
    return sweep.Snapshot({p.pid: p for p in procs}, cwds or {}, managed, moment=NOW, uid=UID, me=me, isdir=isdir)


def plan(s, settings=None, **options):
    return {i["pid"]: i for i in sweep.plan(s, settings or SETTINGS, **options)}


SHELL = proc(50, 1, "/usr/bin/some-daemon")  # a live parent that is no session


class KindTests(unittest.TestCase):
    def test_commands_of_the_incident_and_the_kinds_they_get(self):
        cases = {
            "node /w/repo-kit-wt-656/src/cli.mjs verify": ("runner", None),
            "tail -n +1 -f /Users/m/.claude/tasks/abc.output": ("tail-f", None),
            "tail -F /var/log/x": ("tail-f", None),
            "tail -n 20 /var/log/x": (None, None),
            "bun run --cwd apps/storybook storybook dev --port 6012 --no-open": (None, "storybook-dev"),
            "node /w/node_modules/.bin/vite --port 5173": (None, "vite-dev"),
            "node /w/node_modules/vitest/vitest.mjs run": ("vitest", None),
            "/Users/m/.agent-browser/bin/agent-browser daemon": ("agent-browser", None),
            "/Users/m/Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/"
            "Contents/MacOS/Google Chrome for Testing --headless": ("playwright", None),
            "/usr/lib/chromium/chromium --headless=new --remote-debugging-pipe": ("chromium", None),
            "cargo test -p core": ("cargo-test", None),
            "/w/target/debug/deps/core-0123456789abcdef --test-threads 4": ("cargo-test", None),
            "swift test --parallel": ("swift-test", None),
            "python3 -m pytest tests": ("pytest", None),
            "bun test": ("runner", None),
            "sh ./verify.sh --checks a,b": ("runner", None),
            "sh -c while test -e /x; do sleep 1; done": (None, None),
            "npm exec agentation-mcp server --port 4747": (None, None),
            "node server.js": (None, None),
        }
        for cmd, (orphan, dev) in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(orphan, sweep.orphan_kind(cmd))
                self.assertEqual(dev, sweep.dev_server_kind(cmd))

    def test_sessions_and_programs(self):
        self.assertTrue(sweep.is_session(proc(1, 1, "/Users/m/.local/share/claude/versions/2.1.293 --resume")))
        self.assertTrue(sweep.is_session(proc(1, 1, "/Users/m/Library/Application Support/Claude/claude-code/2.1/"
                                                      "x/claude.app/Contents/MacOS/claude --output-format x")))
        self.assertTrue(sweep.is_session(proc(1, 1, "tmux new -s work")))
        self.assertTrue(sweep.is_session(proc(1, 1, "vim notes.md", tty="ttys003")))
        self.assertFalse(sweep.is_session(proc(1, 1, "node /w/claude/index.js")))
        self.assertEqual("/Applications/Google Chrome.app/Contents/MacOS/Google",
                         sweep.program("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --x"))
        self.assertEqual("node", sweep.program("node /x/Foo.app/Contents/a.js"))

    def test_ps_lines_of_macos_and_procps(self):
        found = sweep.parse_table(
            "  100     1   501  20480 ??       Wed Nov  4 01:00:00 2026     tail -f /x/a.output\n"
            "  101   100     0   1024 pts/0    Wed Nov  4 01:00:01 2026 /bin/bash\n"
            "  102   100  1000   1024 ?        Wed Nov  4 01:00:02 2026 \n"
            "garbage\n")
        self.assertEqual(sweep.Proc(100, 1, 501, 20480, "", "Wed Nov 4 01:00:00 2026",
                                    sf_platform.started_at("Wed Nov 4 01:00:00 2026"), "tail -f /x/a.output"), found[100])
        self.assertEqual(("pts/0", "/bin/bash"), (found[101].tty, found[101].cmd))
        self.assertEqual(("", ""), (found[102].tty, found[102].cmd))
        self.assertEqual({100, 101, 102}, set(found))


class PlanTests(unittest.TestCase):
    def test_a_removed_working_directory_or_a_path_into_a_removed_directory_is_stopped_once_old_enough(self):
        gone = f"{HOME}/sweep-fixture-wt-656"
        s = snap([SHELL,
                  proc(100, 50, "node cli.mjs verify", age=700),
                  proc(101, 50, "sleep 300", age=60),
                  proc(102, 50, f"node {gone}/src/cli.mjs verify", age=700),
                  proc(103, 50, f"node {HOME}/sweep-fixture-new/out.json", age=700),
                  proc(104, 50, "node /opt/gone/x.js", age=700),
                  proc(105, 50, f"sh -c ls {HOME}/*-wt-shoot/static $HOME/x {gone}/$name/y", age=700),
                  proc(106, 50, f"zsh -c cd {HOME}/wt\\012d=/tmp\\012tools/lane check", age=700)],
                 cwds={100: sweep.Cwd(f"{gone}", True), 101: sweep.Cwd(f"{gone}", True),
                       103: sweep.Cwd("/w", False)},
                 gone=(gone, "/opt/gone", f"{HOME}/wt\\012d="))  # what the glued words would name
        found = plan(s)
        self.assertEqual({100, 102}, set(found), "young (101), an existing parent (103), a path outside home and "
                                                 "the temporary directories (104), globs and variables (105), macOS ps's \\012 "
                                                 "between words (106) are left")
        self.assertEqual(("removed", "would-stop"), (found[100]["class"], found[100]["action"]))
        self.assertIn(f"cwd {gone} is gone", found[100]["reason"])
        self.assertIn(f"{gone}/src/cli.mjs is in a directory that is gone", found[102]["reason"])
        self.assertEqual("stop", plan(s, live=True)[100]["action"])

    def test_orphans_of_dev_kinds_past_the_age_and_nothing_else(self):
        s = snap([SHELL,
                  proc(100, 1, "node /w/node_modules/vitest/vitest.mjs run", age=3 * 3600),
                  proc(101, 1, "tail -n +1 -f /Users/m/.claude/tasks/a.output", age=16 * 3600),
                  proc(102, 1, "node server.js", age=99 * 3600),
                  proc(103, 1, "node /w/node_modules/vitest/vitest.mjs run", age=1800),
                  proc(104, 50, "node /w/node_modules/vitest/vitest.mjs run", age=99 * 3600),
                  proc(105, 1, "bun run storybook dev --port 6012", age=3 * 3600),
                  proc(106, 100, "/w/node_modules/@esbuild/darwin-arm64/bin/esbuild --service=0.25 --ping")])
        found = plan(s)
        self.assertEqual({100, 101}, set(found))
        self.assertEqual([("orphan", "vitest", "would-stop", [100, 106]), ("orphan", "tail-f", "would-stop", [101])],
                         [(i["class"], i["kind"], i["action"], i["tree"]) for i in found.values()])
        self.assertIn("vitest adopted by pid 1", found[100]["reason"])

    def test_never_a_terminal_a_session_a_service_another_user_the_system_an_app_or_the_sweep(self):
        gone = f"{HOME}/sweep-fixture-gone"
        procs = [proc(100, 1, "vim notes.md", tty="ttys001"),
                 proc(101, 1, "/Users/m/.local/share/claude/versions/2.1.293"),
                 proc(102, 1, "node /w/node_modules/vitest/vitest.mjs run"),
                 proc(103, 1, "node /w/node_modules/vitest/vitest.mjs run", uid=0),
                 proc(104, 1, "/usr/libexec/trustd"),
                 proc(105, 1, "/Applications/Notes.app/Contents/MacOS/Notes"),
                 proc(999, 1, "python3 slotfather-sweep")]
        s = snap(procs, cwds={p.pid: sweep.Cwd(gone, True) for p in procs}, managed={102}, gone=(gone,))
        self.assertEqual({}, plan(s))
        self.assertEqual("a launchd or systemd job", sweep.exempt(s.procs[102], s))
        self.assertEqual("another user's", sweep.exempt(s.procs[103], s))

    def test_a_live_lease_spares_its_tree_its_carriers_and_trees_holding_them(self):
        gone = f"{HOME}/sweep-fixture-gone"
        procs = [SHELL, proc(100, 50, "sleep 300"), proc(101, 50, "sh -c x"), proc(102, 101, "sleep 300"),
                 proc(103, 1, "node /w/verify.mjs"), proc(104, 50, "sleep 1"),
                 proc(105, 50, "sh -c y"), proc(106, 105, "tmux new -s z")]
        s = snap(procs, cwds={p.pid: sweep.Cwd(gone, True) for p in procs}, gone=(gone,))
        asked = []

        def carriers(pids):
            asked.append(pids)
            return {103: "lane-650"} if 103 in pids else {}
        found = plan(s, protected={100: "gate", 102: "lane-7"}, carriers=carriers, live=True)
        self.assertEqual("spared", found[100]["action"])
        self.assertIn("inside the live lease gate", found[100]["reason"])
        self.assertIn("inside the live lease lane-7", found[101]["reason"], "a leased child spares its parent")
        self.assertEqual("spared", found[102]["action"])
        self.assertIn("inside the live lease lane-650", found[103]["reason"], "an orphan carrying the lease")
        self.assertEqual("stop", found[104]["action"])
        self.assertIn("a session (pid 106) in its tree", found[105]["reason"])
        self.assertNotIn(100, asked[0], "the tree's members need no environment check")

    def test_a_dev_server_without_a_live_session_is_reported_and_stopped_only_with_dev_servers(self):
        procs = [proc(100, 1, "bun run storybook dev --port 6012", age=27 * 3600),
                 proc(101, 100, "node /w/node_modules/.bin/storybook dev --port 6012", age=27 * 3600),
                 proc(110, 1, "/bin/zsh", tty="ttys002"),
                 proc(111, 110, "node /w2/node_modules/.bin/vite", age=27 * 3600),
                 proc(120, 1, "/Applications/Claude.app/Contents/MacOS/Claude"),
                 proc(121, 120, "node /w3/node_modules/.bin/vite", age=27 * 3600),
                 proc(130, 1, "/Applications/WebStorm.app/Contents/MacOS/webstorm"),
                 proc(131, 130, "node /w4/node_modules/.bin/vite", age=27 * 3600),
                 proc(140, 1, "node /w5/node_modules/.bin/vite", age=27 * 3600),
                 proc(141, 1, "/Users/m/.local/share/claude/versions/2.1.293"),
                 proc(150, 1, "node /w6/node_modules/.bin/vite", age=3600)]
        cwds = {100: sweep.Cwd("/w", False), 121: sweep.Cwd("/w3", False), 140: sweep.Cwd("/w5", False),
                141: sweep.Cwd("/w5", False)}
        found = plan(snap(procs, cwds))
        self.assertEqual({100, 121}, set(found), "a terminal (111) or an IDE (131) above, a session in its checkout "
                                                 "(140) and a young server (150) are left")
        self.assertEqual(("dev-server", "storybook-dev", "report", [100, 101]),
                         (found[100]["class"], found[100]["kind"], found[100]["action"], found[100]["tree"]))
        self.assertEqual("report", plan(snap(procs, cwds), live=True)[121]["action"],
                         "the agents' desktop apps are no live session for the servers they started")
        self.assertEqual("stop", plan(snap(procs, cwds), live=True, dev_servers=True)[100]["action"])
        self.assertEqual("would-stop", plan(snap(procs, cwds), dev_servers=True)[100]["action"])

    def test_a_browser_daemon_is_spared_while_a_session_works_in_its_checkout(self):
        procs = [proc(100, 1, "/Users/m/.agent-browser/bin/agent-browser daemon"),
                 proc(101, 1, "/Users/m/.local/share/claude/versions/2.1.293"),
                 proc(102, 1, "/Users/m/.agent-browser/bin/agent-browser daemon")]
        cwds = {100: sweep.Cwd("/kit/wt-a", False), 101: sweep.Cwd("/kit/wt-a", False),
                102: sweep.Cwd("/kit/wt-b", False)}
        found = plan(snap(procs, cwds), live=True)
        self.assertEqual("spared", found[100]["action"])
        self.assertIn("a session works in its checkout /kit/wt-a", found[100]["reason"])
        self.assertEqual("stop", found[102]["action"])

    def test_a_candidate_inside_one_that_stops_goes_with_it_and_settings_limit_the_classes(self):
        gone = f"{HOME}/sweep-fixture-gone"
        procs = [SHELL, proc(100, 50, "sh -c verify"), proc(101, 100, "node x.js"),
                 proc(102, 1, "tail -f /x/a.output")]
        s = snap(procs, cwds={100: sweep.Cwd(gone, True), 101: sweep.Cwd(gone, True)}, gone=(gone,))
        found = plan(s)
        self.assertEqual({100, 102}, set(found))
        self.assertEqual([100, 101], found[100]["tree"])
        self.assertEqual({102}, set(plan(s, {**SETTINGS, "classes": ["orphan"]})))
        self.assertEqual({101}, set(plan(s, only={101})))
        self.assertEqual({}, plan(s, {**SETTINGS, "orphan_age": 99 * 3600, "removed_age": 99 * 3600}))

    def test_under_a_live_session_is_said(self):
        gone = f"{HOME}/sweep-fixture-gone"
        procs = [proc(40, 1, "/Users/m/Library/Application Support/Claude/claude-code/2.1/x/claude.app/Contents/"
                                "MacOS/claude --output-format stream-json"),
                 proc(100, 40, "/bin/zsh -c source snapshot.sh && eval 'while :; do sleep 20; done'")]
        found = plan(snap(procs, cwds={100: sweep.Cwd(gone, True)}, gone=(gone,)))
        self.assertEqual(40, found[100]["session"])
        self.assertIn("under live session pid 40 (claude)", found[100]["reason"])


class SessionTests(unittest.TestCase):
    CLAUDE = ("/Users/m/Library/Application Support/Claude/claude-code/2.1/x/claude.app/Contents/MacOS/claude "
              "--output-format stream-json")

    def test_a_live_sessions_children_are_reported_unless_a_sweep_saw_their_cwd_gone_six_hours(self):
        gone, wt = f"{HOME}/sweep-fixture-gone", f"{HOME}/sweep-fixture-wt"
        procs = [proc(40, 1, self.CLAUDE),
                 proc(100, 40, f"node /w/node_modules/vitest/vitest.mjs --root {wt}/scratch/x", age=7 * 3600),
                 proc(101, 40, "/bin/zsh -c until ! pgrep -f x; do sleep 20; done", age=30 * 3600),
                 proc(102, 40, "/bin/zsh -c until ! pgrep -f y; do sleep 20; done", age=30 * 3600),
                 proc(103, 40, "/bin/zsh -c tail -f z", tty="ttys001", age=9 * 3600),
                 proc(104, 40, "/bin/zsh -c until ! pgrep -f v; do sleep 20; done", age=30 * 3600),
                 proc(105, 104, "ugrep -E FAIL", age=30 * 3600),
                 proc(110, 1, "/bin/zsh -c until ! pgrep -f w; do sleep 20; done", age=1 * 3600)]
        cwds = {100: sweep.Cwd("/w", False), 101: sweep.Cwd(gone, True), 102: sweep.Cwd(gone, True),
                104: sweep.Cwd(gone, True), 105: sweep.Cwd(gone, True), 110: sweep.Cwd(gone, True)}
        seen = {sweep.gone_key(procs[2]): NOW - 7 * 3600, sweep.gone_key(procs[3]): NOW - 5 * 3600}
        found = plan(snap(procs, cwds, gone=(gone, f"{wt}/scratch")), live=True, gone_since=seen)
        self.assertEqual({100: "spared", 101: "stop", 102: "spared", 104: "spared", 110: "stop"},
                         {pid: i["action"] for pid, i in found.items()},
                         "a path into a removed directory under a session: reported (100); a sweep saw its cwd gone "
                         "7 h ago: stopped (101); 5 h ago (102) or never before (104), however old the process: "
                         "reported; no session above it: stopped (110)")
        self.assertEqual((7 * 3600, 5 * 3600, 0), (found[101]["gone_for"], found[102]["gone_for"],
                                                   found[104]["gone_for"]))
        self.assertEqual([104, 105], found[104]["tree"], "its child (105) is listed with it, not on its own")
        self.assertTrue(found[100]["under_session"] and found[102]["under_session"])
        self.assertNotIn("under_session", found[101])
        self.assertIn("under live session pid 40 (claude); report only: a live session's child", found[102]["reason"])
        relaxed = plan(snap(procs, cwds, gone=(gone, f"{wt}/scratch")), {**SETTINGS, "session_removed_age": 0})
        self.assertEqual("would-stop", relaxed[104]["action"])
        self.assertEqual("spared", relaxed[100]["action"], "only a gone working directory, never a path in argv")

    def test_children_of_a_session_that_works_in_the_same_removed_directory_are_spared(self):
        gone = f"{HOME}/sweep-fixture-wt-gone"
        procs = [proc(40, 1, self.CLAUDE), proc(100, 40, "node mcp-server.js", age=30 * 3600),
                 proc(101, 40, "node language-server.js", age=30 * 3600)]
        cwds = {40: sweep.Cwd(gone, True), 100: sweep.Cwd(gone, True), 101: sweep.Cwd(f"{gone}/src", True)}
        found = plan(snap(procs, cwds, gone=(gone,)), {**SETTINGS, "session_removed_age": 0}, live=True)
        self.assertEqual({100: "spared", 101: "spared"}, {pid: i["action"] for pid, i in found.items()})
        self.assertIn("the session works in the same removed directory", found[100]["reason"])

    def test_the_state_keeps_when_a_cwd_was_first_seen_gone(self):
        gone = f"{HOME}/sweep-fixture-gone"
        procs = [proc(100, 1, "sleep 1"), proc(101, 1, "sleep 2"), proc(102, 1, "sleep 3", uid=0)]
        s = snap(procs, {100: sweep.Cwd(gone, True), 101: sweep.Cwd(gone, True), 102: sweep.Cwd(gone, True)})
        first = sweep.track_gone(s, {sweep.gone_key(procs[0]): NOW - 100, "999 old": NOW - 9})
        self.assertEqual({sweep.gone_key(procs[0]): NOW - 100, sweep.gone_key(procs[1]): NOW}, first,
                         "kept from before, new ones now, gone processes and other users' dropped")

    def test_a_pgrep_wait_that_matches_its_own_command_line(self):
        shell = "/bin/zsh -c source snap.sh && eval 'until ! pgrep -f {} >/dev/null; do sleep 20; done; tail x'"
        self.assertEqual("lane check 425-parity-table-1", sweep.self_matching(shell.format('"lane check 425-parity-table-1"')))
        self.assertEqual("build-storybooks.sh", sweep.self_matching(shell.format("build-storybooks.sh")))
        self.assertEqual("verify", sweep.self_matching("sh -c while pgrep -lf 'verify' > /dev/null; do sleep 1; done"))
        self.assertIsNone(sweep.self_matching(shell.format('"[l]ane check 7"')), "the bracket trick matches no itself")
        self.assertIsNone(sweep.self_matching("sh -c until ! pgrep node; do sleep 1; done"), "pgrep without -f")
        self.assertIsNone(sweep.self_matching("pgrep -f lane"), "no loop")
        procs = [proc(40, 1, self.CLAUDE), proc(100, 40, shell.format('"lane check 7"'), age=3600),
                 proc(101, 100, "sleep 20"), proc(102, 1, shell.format("x"), uid=0)]
        procs.append(proc(103, 40, shell.format('"lane check 7"'), age=60))
        procs.append(proc(104, 103, 'pgrep -f lane check 7'))
        waits = sweep.self_matching_waits(snap(procs))
        self.assertEqual([(100, "lane check 7", 3600, 40, [103]), (103, "lane check 7", 60, 40, [100])],
                         [(w["pid"], w["pattern"], w["age"], w["session"], w["matched_by"]) for w in waits],
                         "each finds the other; not itself, its children or a pgrep run")
        self.assertTrue(waits[0]["cmd"].startswith("until ! pgrep -f"), "the loop, not the shell's setup")


class ReviewTests(unittest.TestCase):
    """The independent review of dd27cd7: lists and output paths, unreadable directories, mount namespaces,
    services, the runner kind, settings and stop()."""

    def test_lists_and_output_paths_are_no_removed_directory(self):
        gone = f"{HOME}/sweep-fixture-gone"
        isdir = (lambda path: not path.startswith(gone))
        within = sweep.roots()
        for cmd in (f"java -cp {HOME}/a.jar:{HOME}/b.jar Main", f"env NODE_PATH={HOME}/a:{HOME}/b node x.js",
                    f"PATH={HOME}/.local/bin:/usr/bin make", f"tool --paths={HOME}/x,{HOME}/y"):
            self.assertEqual([], sweep.gone_paths(cmd, isdir, within), cmd)
        self.assertEqual([f"{gone}/a/b"], sweep.gone_paths(f"x -cp {HOME}/a.jar:{gone}/a/b", isdir, within))
        s = snap([SHELL, proc(100, 50, f"java -jar app.jar --out {gone}/new/dir/f", age=99 * 3600),
                  proc(101, 50, f"/usr/bin/nohup node server.js --data {gone}/db", age=99 * 3600),
                  proc(102, 50, f"node {gone}/node_modules/vitest/vitest.mjs run", age=99 * 3600),
                  proc(103, 50, "java -Dgradle GradleDaemon", age=99 * 3600)],
                 cwds={103: sweep.Cwd(gone, True)}, gone=(gone,))
        self.assertEqual({102: "removed", 103: "removed"}, {p: i["class"] for p, i in plan(s).items()},
                         "a path in argv counts only for dev kinds (102); a gone cwd for any process (103)")

    def test_an_unreadable_directory_is_unknown_never_gone(self):
        locked = pathlib.Path(self.tmp_dir()) / "locked"
        (locked / "inside").mkdir(parents=True)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        if os.geteuid() == 0:
            self.skipTest("root reads every directory")
        self.assertIsNone(sweep.dir_state(str(locked / "inside")))
        self.assertIsNone(sweep.removed_dir(str(locked / "inside"), 5))
        self.assertTrue(sweep.removed_dir(str(locked.parent / "missing"), None))
        self.assertFalse(sweep.dir_state(str(locked.parent / "missing")))
        self.assertTrue(sweep.dir_state(str(locked.parent)))
        self.assertFalse(sweep.removed_dir(str(locked.parent), os.stat(locked.parent).st_ino))
        self.assertTrue(sweep.removed_dir(str(locked.parent), os.stat(locked.parent).st_ino + 1),
                        "another directory at the path now")
        self.assertEqual([], sweep.gone_paths(f"vitest --root {locked}/inside/x", sweep.dir_state,
                                              (str(locked.parent) + "/",)))
        s = snap([SHELL, proc(100, 50, "node verify.mjs")], {100: sweep.Cwd(str(locked / "inside"), None)})
        self.assertEqual({}, plan(s))

    def tmp_dir(self):
        import tempfile
        path = tempfile.mkdtemp(prefix="sweep-review-")
        self.addCleanup(__import__("shutil").rmtree, path, ignore_errors=True)
        return path

    def test_linux_cwds_come_from_the_same_mount_namespace_and_the_kernels_word_only(self):
        fake = pathlib.Path(self.tmp_dir())
        real = fake / "real-dir"
        real.mkdir()

        def process(pid, cwd, ns="mnt:[4026531840]"):
            (fake / str(pid) / "ns").mkdir(parents=True)
            (fake / str(pid) / "ns" / "mnt").symlink_to(ns)
            (fake / str(pid) / "cwd").symlink_to(cwd)
        process("self", "/")
        process(100, "/home/u/wt (deleted)")
        process(101, str(real))
        process(102, "/__w/repo/repo", ns="mnt:[4026532999]")
        process(103, "/home/u/gone-but-not-said")
        procs = {pid: proc(pid, 1, "x") for pid in (100, 101, 102, 103)}
        with unittest.mock.patch.dict(os.environ, {"SLOTFATHER_PROC": str(fake)}):
            cwds, notes, foreign = sweep.proc_cwds(procs, UID)
        self.assertEqual(sweep.Cwd("/home/u/wt", True), cwds[100])
        self.assertEqual(sweep.Cwd(str(real), False), cwds[101])
        self.assertEqual({102}, foreign)
        self.assertNotIn(102, cwds)
        self.assertEqual(sweep.Cwd("/home/u/gone-but-not-said", None), cwds[103],
                         "a path the host cannot see is unknown, not gone")
        self.assertIn("1 process(es) in another mount namespace", notes[0])
        s = sweep.Snapshot({102: proc(102, 1, "node verify.mjs")}, {}, moment=NOW, uid=UID, foreign={102})
        self.assertEqual("in another mount namespace", sweep.exempt(s.procs[102], s))

    def test_services_of_the_user_manager_and_the_gui_domain(self):
        self.assertEqual((True, "app.service"), sweep.service_of(
            ["0::/user.slice/user-1000.slice/user@1000.service/app.slice/app.service"]))
        self.assertEqual((False, "actions.runner.x.service"), sweep.service_of(
            ["0::/ci.slice/ci-runners.slice/actions.runner.x.service"]))
        self.assertIsNone(sweep.service_of(["0::/user.slice/user-1000.slice/session-3.scope"]))
        asked = []

        def run(command, **_):
            asked.append(command)
            return subprocess.CompletedProcess(command, 0, "4242\n", "")
        self.assertEqual(4242, sweep.main_pid(True, "app.service", run))
        self.assertEqual(["systemctl", "--user", "show", "--property", "MainPID", "--value", "app.service"], asked[0])
        sweep.main_pid(False, "x.service", run)
        self.assertNotIn("--user", asked[1])
        self.assertEqual(-1, sweep.main_pid(True, "x.service", lambda c, **_: subprocess.CompletedProcess(c, 1, "", "")))
        printed = ("gui/501 = {\n\tservices = {\n\t\t    1329      - \tapplication.bt\n\t\t       0      0 \tidle\n"
                   "\t\t   65817   (pe) \tcom.apple.x\n\t}\n\tother = {\n\t\t  77 x\n\t}\n}\n")
        self.assertEqual({1329, 65817}, sweep.launchd_pids(printed, "print"))
        self.assertEqual({57462}, sweep.launchd_pids("PID\tStatus\tLabel\n57462\t-15\tcom.x\n-\t0\tcom.y\n", "list"))

    def test_a_runner_is_known_by_its_script_not_any_argument(self):
        for cmd in ("python3 -m http.server --directory /Users/m/tests", "node server.js --root /srv/e2e",
                    "bun run --cwd apps/x build", "python3 tool.py --suite test"):
            self.assertIsNone(sweep.orphan_kind(cmd), cmd)
        for cmd in ("node /w/src/cli.mjs verify", "npm run test", "npm test", "bun test", "node --test",
                    "node --import tsx /w/a.test.ts", "make smoke", "sh -c ./verify.sh --checks x",
                    "deno run -A e2e.ts"):
            self.assertEqual("runner", sweep.orphan_kind(cmd), cmd)


class StopTests(unittest.TestCase):
    """stop() with a fake process table: pids whose start time changed are never signalled."""

    def run_stop(self, looks, grace=1.0):
        s = snap([proc(100, 1, "x"), proc(101, 100, "y")])
        sent, clock = [], [0.0]
        answers = iter(looks)
        last = [None]

        def look(pids):
            try:
                last[0] = next(answers)
            except StopIteration:
                pass
            return {p: v for p, v in last[0].items() if p in pids}

        def now():
            return clock[0]

        def pause(seconds):
            clock[0] += seconds
        result = sweep.stop({"tree": [100, 101]}, s, grace, now, pause, look, lambda pid, sig: sent.append((pid, sig)))
        return result, sent

    def test_a_start_time_changed_before_the_term_is_not_signalled(self):
        result, sent = self.run_stop([{100: ("other", False), 101: ("start-101", False)}, {}])
        self.assertEqual(([101], [(101, signal.SIGTERM)]), (result["signalled"], sent))
        result, sent = self.run_stop([{100: ("other", False)}])
        self.assertEqual(("gone", []), (result["result"], sent))

    def test_a_pid_reused_between_term_and_kill_gets_no_kill(self):
        same = {100: ("start-100", False), 101: ("start-101", False)}
        reused = {100: ("reused", False), 101: ("start-101", False)}
        result, sent = self.run_stop([same, same, reused, reused, reused, reused, reused, reused, {}])
        self.assertIn((101, signal.SIGKILL), sent)
        self.assertNotIn((100, signal.SIGKILL), sent, "pid 100 is another process now")
        self.assertEqual("killed", result["result"])

    def test_term_suffices_and_zombies_count_as_gone(self):
        same = {100: ("start-100", False), 101: ("start-101", False)}
        result, sent = self.run_stop([same, {100: ("start-100", True)}])
        self.assertEqual(("stopped", [(100, signal.SIGTERM), (101, signal.SIGTERM)]), (result["result"], sent))


class ReportTests(unittest.TestCase):
    def test_containers_in_a_restart_loop(self):
        def container(name, restarts, status="restarting", restarting=True, started="2027-01-15T07:55:00Z",
                      policy="unless-stopped", created="2027-01-10T00:00:00Z"):
            return {"Id": "abcdef0123456789", "Name": "/" + name, "RestartCount": restarts, "Created": created,
                    "State": {"Status": status, "Restarting": restarting, "StartedAt": started, "ExitCode": 1},
                    "HostConfig": {"RestartPolicy": {"Name": policy}},
                    "Config": {"Labels": {"com.docker.compose.project": "sirv-demo"}}}
        moment = sweep.docker_time("2027-01-15T08:00:00Z")
        found = sweep.restart_loops([
            container("sirv-demo-app-1", 412),
            container("steady", 5, "running", False, started="2027-01-14T00:00:00Z"),
            container("few", 2),
            container("no-policy", 50, policy="no"),
            container("new", 50, created="2027-01-15T07:50:00Z"),
            container("again", 9, "running", False, started="2027-01-15T07:59:00Z"),
        ], SETTINGS, moment)
        self.assertEqual(["sirv-demo-app-1", "again"], [c["name"] for c in found])
        self.assertEqual(("report", "sirv-demo", 412, "unless-stopped"),
                         (found[0]["action"], found[0]["project"], found[0]["restarts"], found[0]["policy"]))

    def test_memory_holders_only_while_memory_is_short_with_their_class(self):
        s = snap([proc(100, 1, "tail -f x", rss=900 * 1024), proc(101, 50, "node gate.mjs", rss=2000 * 1024),
                  proc(102, 1, "Finder", rss=10 * 1024, uid=0)])
        items = [{"class": "orphan", "tree": [100]}]
        calm = sweep.memory_report({"swap_total_mb": 8192, "swap_free_mb": 4000, "short": None}, s, SETTINGS, items,
                                   {101: "lane-7"})
        self.assertEqual((None, []), (calm["short"], calm["top"]))
        short = sweep.memory_report({"swap_total_mb": 8192, "swap_free_mb": 1030, "short": None}, s,
                                    {**SETTINGS, "memory_top": 2}, items, {101: "lane-7"})
        self.assertIn("free swap 1030 of 8192 MB is under 1638 MB", short["short"], "20% of 8 GB")
        self.assertEqual([(101, 2000, "lease lane-7"), (100, 900, "orphan")],
                         [(t["pid"], t["rss_mb"], t["class"]) for t in short["top"]])
        guard = sweep.memory_report({"swap_total_mb": None, "swap_free_mb": None, "short": "memory pressure level 4"},
                                    s, SETTINGS, [], {})
        self.assertEqual(3, len(guard["top"]))

    def test_the_text_report(self):
        report = {"at": NOW, "live": False, "dev_servers": False, "only": None, "notes": ["lsof failed"],
                  "found": {"removed": 1, "orphan": 0, "dev-server": 0, "container": 1},
                  "items": [{"class": "removed", "action": "would-stop", "pid": 100, "age": 3600, "rss_mb": 12.0,
                             "kind": "runner", "cmd": "node cli.mjs verify", "reason": "cwd /w is gone",
                             "tree": [100, 101]}],
                  "containers": {"available": True, "items": [{"name": "app-1", "project": "demo",
                                                               "status": "restarting", "restarts": 9,
                                                               "policy": "always", "exit_code": 1}]},
                  "memory": {"swap_total_mb": 8192, "swap_free_mb": 1000, "short": "free swap low",
                             "top": [{"pid": 100, "rss_mb": 12, "class": "removed", "cmd": "node cli.mjs verify"}]},
                  "would_stop": 1, "spared": 0, "stopped": {}, "failed": 0, "freed_mb": 0}
        text = sweep.text(report)
        self.assertRegex(text, r"removed +would stop pid 100 +1h00m +12 MB  runner +node cli\.mjs verify")
        self.assertIn("cwd /w is gone; 2 process(es)", text)
        self.assertIn("container  report     app-1 (demo): restarting, 9 restarts", text)
        self.assertIn("memory short: free swap low; swap 7192 of 8192 MB used; largest:", text)
        self.assertIn("note: lsof failed", text)
        self.assertIn("dry run: nothing was signalled; 1 would be stopped with --live", text)
        self.assertEqual("", sweep.text(report, quiet=True), "quiet prints only what was stopped")


class SweepCase(SlotCase):
    def sweep(self, *args, ok=True):
        result = self.tool("sweep", *args)
        if ok:
            self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        return result

    def configure(self, *pairs):
        result = self.tool("sweep", "config", *pairs)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def report(self, *args):
        result = self.sweep("--json", *args)
        return json.loads(result.stdout)

    def alive(self, pid):
        """Running (not a zombie) by ps."""
        found = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(found) and not found.startswith("Z")

    def wait_gone(self, pid, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.alive(pid):
            time.sleep(0.05)
        return not self.alive(pid)

    def pid_file(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip().isdigit():
                return int(path.read_text().strip())
            time.sleep(0.05)
        self.fail(f"no pid in {path}")

    def orphan(self, name, extra_env=None, wrap=()):
        """A python `verify.py` that sleeps, double-forked so that launchd/init adopts it; returns its pid."""
        work = self.tmp / name
        work.mkdir()
        (work / "verify.py").write_text("import time\ntime.sleep(120)\n")
        pid_path = work / "pid"
        launcher = textwrap.dedent(f"""
            import os, sys
            if os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    with open({str(pid_path)!r} + ".tmp", "w") as f:
                        f.write(str(os.getpid()))
                    os.replace({str(pid_path)!r} + ".tmp", {str(pid_path)!r})
                    os.execv(sys.executable, [sys.executable, {str(work / 'verify.py')!r}])
                os._exit(0)
            os.wait()
        """)
        subprocess.run([*wrap, sys.executable, "-c", launcher], cwd=work, env={**self.env, **(extra_env or {})},
                       check=True, timeout=20, start_new_session=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # the orphan keeps no pipe open
        pid = self.pid_file(pid_path)
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if self.alive(pid) else None)
        return pid

    def in_removed_directory(self, name, script="exec sleep 120"):
        """A process (its own session, no terminal) whose working directory the test then removes."""
        work = self.tmp / name
        work.mkdir()
        process = subprocess.Popen(["sh", "-c", script], cwd=work, start_new_session=True)
        self.processes.append(process)
        time.sleep(0.3)
        work.rmdir()
        return process


class LiveTests(SweepCase):
    def setUp(self):
        super().setUp()
        # session_removed_age 0: the test may itself run under a live session (an agent's shell), whose children
        # would otherwise be reported only.
        self.configure("removed_age", "0", "orphan_age", "0", "classes", "removed,orphan,dev-server", "grace", "2",
                       "session_removed_age", "0")

    def test_an_orphan_from_a_double_fork_is_reported_then_stopped_with_live(self):
        pid = self.orphan("orphan")
        dry = self.report("--pid", str(pid))
        self.assertEqual([(pid, "orphan", "runner", "would-stop")],
                         [(i["pid"], i["class"], i["kind"], i["action"]) for i in dry["items"]], dry)
        self.assertTrue(self.alive(pid), "a dry run signals nothing")
        live = self.report("--live", "--pid", str(pid))
        self.assertEqual("stopped", live["items"][0]["action"], live)
        self.assertTrue(self.wait_gone(pid))
        self.assertEqual({"removed": 0, "orphan": 1, "dev-server": 0}, live["stopped"])
        log = (self.state / "queue.log").read_text()
        self.assertRegex(log, rf"sweep stopped orphan pid {pid} \(runner, age \d+s, 1 process\(es\)\)")
        lines = [json.loads(line) for line in (self.state / "sweep.jsonl").read_text().splitlines()]
        self.assertEqual([False, True], [line["live"] for line in lines])

    def test_a_process_in_a_removed_directory_is_stopped_and_one_ignoring_term_is_killed(self):
        plain = self.in_removed_directory("wt-656")
        stubborn = self.in_removed_directory("wt-657", 'trap "" TERM; while :; do sleep 0.1; done')
        dry = self.report("--pid", str(plain.pid), "--pid", str(stubborn.pid))
        self.assertEqual({plain.pid: "would-stop", stubborn.pid: "would-stop"},
                         {i["pid"]: i["action"] for i in dry["items"]}, dry)
        self.assertRegex(dry["items"][0]["reason"], r"cwd .*wt-656 is gone")
        live = self.report("--live", "--pid", str(plain.pid), "--pid", str(stubborn.pid))
        results = {i["pid"]: i["action"] for i in live["items"]}
        self.assertEqual({plain.pid: "stopped", stubborn.pid: "killed"}, results, live)
        self.assertEqual(-signal.SIGTERM, plain.wait(timeout=5))
        self.assertEqual(-signal.SIGKILL, stubborn.wait(timeout=5))

    def test_a_leased_process_and_a_leased_orphan_are_spared(self):
        work = self.tmp / "leased"
        work.mkdir()
        pid_path = self.tmp / "leased.pid"
        holder = subprocess.Popen([TOOL, "run", "-n", "1", "--label", "leased", "--", "sh", "-c",
                                   f'echo $$ > "{pid_path}"; exec sleep 120'], cwd=work, env=self.env,
                                  start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(holder)
        pid = self.pid_file(pid_path)
        work.rmdir()
        found = self.report("--live", "--pid", str(pid), "--pid", str(holder.pid))
        self.assertEqual({pid: "spared", holder.pid: "spared"}, {i["pid"]: i["action"] for i in found["items"]},
                         found)
        self.assertIn("inside the live lease leased", found["items"][0]["reason"])
        self.assertTrue(self.alive(pid) and self.alive(holder.pid))
        lease = json.loads(self.tool("status", "--json").stdout)["entries"][0]["lease"]
        orphan = self.orphan("leased-orphan", {"SLOTFATHER_LEASE": lease})
        if not sf_platform.carrying([orphan], lease):
            self.skipTest("this host does not show the orphan's environment (an Apple platform python)")
        found = self.report("--live", "--pid", str(orphan))
        self.assertEqual([(orphan, "spared")], [(i["pid"], i["action"]) for i in found["items"]], found)
        self.assertIn("inside the live lease leased", found["items"][0]["reason"])
        self.assertTrue(self.alive(orphan))


class SessionLiveTests(SweepCase):
    def setUp(self):
        super().setUp()
        self.configure("removed_age", "0", "classes", "removed,orphan", "grace", "2", "session_removed_age", "1h")

    def test_a_live_sessions_children_are_spared_until_their_cwd_has_been_gone_long_enough(self):
        """A stand-in session: python run as `claude` (a symlink), with two children: one whose working directory
        is removed, and a verify.py in an existing directory whose command names a removed one."""
        bin_dir, gone, kept, named = (self.tmp / n for n in ("bin", "wt-gone", "kept", "named"))
        for d in (bin_dir, gone, kept, named):
            d.mkdir()
        (bin_dir / "claude").symlink_to(sys.executable)
        (kept / "verify.py").write_text("import time\ntime.sleep(120)\n")  # a runner: its argv path counts
        pids = self.tmp / "children"
        session = subprocess.Popen([str(bin_dir / "claude"), "-c", textwrap.dedent(f"""
            import subprocess, sys, time
            a = subprocess.Popen(["sleep", "120"], cwd={str(gone)!r})
            b = subprocess.Popen([{sys.executable!r}, {str(kept / "verify.py")!r}, {str(named / "check.log")!r}],
                                 cwd={str(kept)!r})
            open({str(pids)!r} + ".tmp", "w").write(f"{{a.pid}} {{b.pid}}")
            import os; os.replace({str(pids)!r} + ".tmp", {str(pids)!r})
            time.sleep(120)
        """)], start_new_session=True)
        self.processes.append(session)
        deadline = time.monotonic() + 10
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        in_gone, naming = map(int, pids.read_text().split())
        time.sleep(0.3)
        gone.rmdir()
        named.rmdir()
        found = self.report("--live", "--pid", str(in_gone), "--pid", str(naming))
        self.assertEqual({in_gone: "spared", naming: "spared"}, {i["pid"]: i["action"] for i in found["items"]}, found)
        self.assertTrue(all(i["under_session"] and i["session"] == session.pid for i in found["items"]))
        self.assertEqual(2, found["under_session"])
        text = self.sweep("--pid", str(in_gone), "--no-log").stdout
        self.assertIn(f"under live session pid {session.pid}: 1 child(ren) reported only", text)
        self.assertTrue(self.alive(in_gone) and self.alive(naming))
        self.configure("session_removed_age", "0")
        found = self.report("--live", "--pid", str(in_gone), "--pid", str(naming))
        self.assertEqual({in_gone: "stopped", naming: "spared"}, {i["pid"]: i["action"] for i in found["items"]},
                         found)
        self.assertTrue(self.wait_gone(in_gone))
        self.assertTrue(self.alive(naming), "a path in argv never lets a session's child be stopped")

    def test_two_waits_for_one_pattern_never_end_and_get_a_note(self):
        """Linux's pgrep finds each loop itself; macOS's skips its own ancestors but finds the other loop."""
        marker = f"sweep-wait-{os.getpid()}-{time.monotonic_ns()}"
        waits = [subprocess.Popen(["sh", "-c", f'until ! pgrep -f "{marker}" >/dev/null; do sleep 0.2; done'],
                                  start_new_session=True) for _ in range(2)]
        self.processes.extend(waits)
        time.sleep(1)
        self.assertEqual([None, None], [w.poll() for w in waits], "neither loop ends")
        pids = [w.pid for w in waits]
        found = self.report(*(a for pid in pids for a in ("--pid", str(pid))))
        self.assertEqual([(pids[0], marker, [pids[1]]), (pids[1], marker, [pids[0]])],
                         [(w["pid"], w["pattern"], w["matched_by"]) for w in found["self_matching"]])
        text = self.sweep(*(a for pid in pids for a in ("--pid", str(pid)))).stdout
        self.assertIn(f"note: self-matching wait: pgrep -f '{marker}' matches its own command line; 2 wait(s), "
                      f"oldest", text)
        self.assertIn(f"pids {pids[0]}, {pids[1]}: until ! pgrep -f", text)
        self.assertTrue(all(self.alive(pid) for pid in pids), "a note only")


class StateTests(SweepCase):
    def test_a_sweep_records_when_it_first_saw_a_cwd_gone_and_forgets_it_once_the_process_is_gone(self):
        process = self.in_removed_directory("wt-state")
        key_prefix = f"{process.pid} "
        self.sweep("--pid", str(process.pid))

        def recorded():
            data = json.loads((self.state / "sweep-state.json").read_text())["gone_since"]
            return {k: v for k, v in data.items() if k.startswith(key_prefix)}
        first = recorded()
        self.assertEqual(1, len(first), first)
        time.sleep(1.1)
        self.sweep("--pid", str(process.pid))
        self.assertEqual(first, recorded(), "the first sighting is kept")
        self.sweep("--pid", str(process.pid), "--no-log")
        process.kill()
        process.wait()
        self.sweep("--pid", str(process.pid))
        self.assertEqual({}, recorded())


class CommandTests(SweepCase):
    def test_settings_are_shown_saved_checked_and_reset(self):
        self.assertIn("orphan_age         2h00m", self.configure())
        self.assertIn("(defaults)", self.configure())
        shown = self.configure("orphan_age", "3h", "classes", "removed,container", "grace", "5")
        self.assertIn("orphan_age         3h00m", shown)
        self.assertIn("classes            removed,container", shown)
        saved = json.loads((self.state / "sweep.json").read_text())
        self.assertEqual((10800.0, ["removed", "container"], 5.0),
                         (saved["orphan_age"], saved["classes"], saved["grace"]))
        for bad in (("orphan_age", "soon"), ("classes", "everything"), ("nope", "1"), ("grace",)):
            result = self.tool("sweep", "config", *bad)
            self.assertEqual(2, result.returncode, bad)
        self.assertEqual(10800.0, json.loads((self.state / "sweep.json").read_text())["orphan_age"])
        self.assertIn("(defaults)", self.configure("reset"))
        self.assertFalse((self.state / "sweep.json").exists())
        (self.state / "sweep.json").write_text("{not json")
        result = self.tool("sweep", "config")
        self.assertIn("sweep.json ignored", result.stderr)
        self.assertIn("orphan_age         1h00m", self.configure("orphan_age", "1h"), "a save replaces a broken file")
        (self.state / "sweep.json").write_text(json.dumps({"orphan_age": 60, "from_the_future": 1}))
        result = self.tool("sweep", "config")
        self.assertIn("unknown setting(s) from_the_future ignored", result.stderr)
        self.assertIn("orphan_age         1m00s", result.stdout, "the known keys still count")

    def test_settings_that_cannot_be_read_stop_nothing_and_fail_the_live_sweep(self):
        work = self.tmp / "gone"
        work.mkdir()
        victim = subprocess.Popen(["sh", "-c", "exec sleep 120"], cwd=work, start_new_session=True)
        self.processes.append(victim)
        time.sleep(0.3)
        work.rmdir()
        (self.state).mkdir(parents=True, exist_ok=True)
        (self.state / "sweep.json").write_text(json.dumps({"removed_age": "soon", "session_removed_age": 0}))
        result = self.tool("sweep", "--live", "--quiet", "--pid", str(victim.pid))
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("the live sweep stopped nothing", result.stderr)
        self.assertTrue(self.alive(victim.pid))
        found = json.loads(self.tool("sweep", "--live", "--json", "--pid", str(victim.pid)).stdout)
        self.assertEqual((False, True), (found["live"], found["live_refused"]))
        self.assertIn("removed_age must be a number", found["settings_error"])

    def test_a_dry_run_over_the_whole_machine_signals_nothing_and_logs_one_line(self):
        self.configure("classes", "removed,orphan,dev-server,memory")
        before = self.processes_now()
        result = self.sweep()
        self.assertIn("dry run: nothing was signalled", result.stdout)
        self.assertEqual(1, len((self.state / "sweep.jsonl").read_text().splitlines()))
        self.assertLessEqual(before - self.processes_now(), set(), "no process of the test's was stopped")
        found = self.report("--no-log")
        self.assertFalse(found["live"])
        self.assertTrue(all(i["action"] in ("would-stop", "report", "spared") for i in found["items"]))
        self.assertEqual(1, len((self.state / "sweep.jsonl").read_text().splitlines()), "--no-log")
        self.assertEqual(2, self.tool("sweep", "--live", "--no-log").returncode)
        self.assertEqual(2, self.tool("sweep", "--bogus").returncode)

    def processes_now(self):
        return {p.pid for p in self.processes if p.poll() is None}

    def test_the_launchd_agent(self):
        found = plistlib.loads(self.tool("sweep", "--plist").stdout.encode())
        self.assertEqual(("com.slotfather.sweep", 3600, True), (found["Label"], found["StartInterval"],
                                                                found["RunAtLoad"]))
        self.assertEqual(["sweep", "--quiet"], found["ProgramArguments"][2:])
        self.assertTrue(found["ProgramArguments"][1].endswith("__main__.py"))
        self.assertEqual(str(self.state), found["EnvironmentVariables"]["SLOTFATHER_DIR"])
        live = plistlib.loads(self.tool("sweep", "--plist", "--live", "--dev-servers", "--interval", "1800")
                              .stdout.encode())
        self.assertEqual((["sweep", "--quiet", "--live", "--dev-servers"], 1800),
                         (live["ProgramArguments"][2:], live["StartInterval"]))

    def test_stats_count_the_sweeps(self):
        self.state.mkdir(parents=True, exist_ok=True)
        lines = [{"v": 1, "at": NOW, "live": False, "found": {"removed": 2, "orphan": 1}, "stopped": {},
                  "failed": 0, "freed_mb": 0},
                 {"v": 1, "at": NOW + 3600, "live": True, "found": {"removed": 2, "orphan": 1, "container": 1},
                  "stopped": {"removed": 2, "orphan": 1}, "failed": 1, "freed_mb": 1200.5}]
        (self.state / "sweep.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines) + "torn")
        result = self.tool("stats", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        found = json.loads(result.stdout)["sweeps"]
        self.assertEqual((2, 1, {"removed": 2, "orphan": 1, "dev-server": 0}, 1, 1200.5),
                         (found["runs"], found["live"], found["stopped"], found["failed"], found["freed_mb"]))
        text = self.tool("stats").stdout
        self.assertIn("sweeps: 2 (1 live); stopped 2 removed, 1 orphan, 0 dev-server, 1 failed, 1200 MB resident "
                      "freed; the last live run at", text)
        self.assertIn("found 2 removed, 1 orphan, 0 dev-server, 1 container", text)


if __name__ == "__main__":
    unittest.main()
