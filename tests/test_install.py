"""Public one-time installer checks; every invocation has its own HOME and queue trees."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = b"#!/bin/sh\nprintf slotfather-fixture\n"
DASHBOARD = b"#!/usr/bin/env python3\nprint('dashboard fixture')\n"
STATS = b"#!/usr/bin/env python3\nprint('stats fixture')\n"
SWEEP = b"#!/usr/bin/env python3\nprint('sweep fixture')\n"


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="slotfather-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.home / ".local/state"
        self.old = self.state / "machine-slots"
        self.legacy = self.state / "straylight-verification/slots"
        self.new = self.state / "slotfather"
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        (self.bin / "machine-slot").write_text("old command")
        self.source = self.tmp / "source"
        self.source.mkdir()
        if (ROOT / "install.py").exists():
            shutil.copyfile(ROOT / "install.py", self.source / "install.py")
        (self.source / "slotfather").write_bytes(PAYLOAD)
        (self.source / "slotfather-dashboard").write_bytes(DASHBOARD)
        (self.source / "slotfather-stats").write_bytes(STATS)
        (self.source / "slotfather-sweep").write_bytes(SWEEP)
        self.env = {**os.environ, "HOME": str(self.home)}

    def run_install(self, *options):
        # A test runner inside a virtual environment names its own interpreter, which the installer then accepts.
        if sys.prefix != sys.base_prefix and not options:
            options = ("--python", sys.executable)
        return subprocess.run([sys.executable, str(self.source / "install.py"), *options], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def queue(self, path, *, state="held", pid=99999999, start="gone"):
        (path / "queue").mkdir(parents=True, exist_ok=True)
        (path / ".lock").touch()
        (path / "queue/0000000001-fixture.json").write_text(json.dumps(
            {"pid": pid, "start": start, "count": 4, "label": "fixture", "state": state}))
        return path

    def live_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        start = subprocess.check_output(["ps", "-o", "lstart=", "-p", str(child.pid)], text=True).strip()
        return child.pid, start

    def contents(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_migrate_preserves_old_queue_and_capacity_and_retires_both_paths(self):
        self.queue(self.old)
        (self.old / "capacity.json").write_text('{"slots":6}\n')
        (self.old / "sequence").write_text("12")
        before = self.contents(self.old)
        result = self.run_install()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.contents(self.new))
        for old in (self.old, self.legacy):
            self.assertTrue(old.is_file())
            probe = subprocess.run([sys.executable, "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).mkdir(parents=True,exist_ok=True); print('WORK STARTED')",
                str(old / "queue")], capture_output=True, text=True)
            self.assertNotEqual(0, probe.returncode)
            self.assertNotIn("WORK STARTED", probe.stdout)
        self.assertEqual(PAYLOAD, (self.bin / "slotfather").read_bytes())
        self.assertFalse((self.bin / "machine-slot").exists())
        self.assertTrue(os.access(self.bin / "slotfather", os.X_OK))
        self.assertTrue(os.access(self.bin / "slotfather-dashboard", os.X_OK))
        self.assertTrue(os.access(self.bin / "slotfather-stats", os.X_OK))
        self.assertTrue(os.access(self.bin / "slotfather-sweep", os.X_OK))

    def test_installed_commands_name_the_installing_interpreter_instead_of_env(self):
        result = self.run_install("--python", sys.executable)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        installed = (self.bin / "slotfather-dashboard").read_bytes()
        self.assertEqual(f"#!{sys.executable}\nprint('dashboard fixture')\n".encode(), installed)
        self.assertEqual(PAYLOAD, (self.bin / "slotfather").read_bytes(), "only an env python3 shebang is pinned")
        run = subprocess.run([str(self.bin / "slotfather-dashboard")], capture_output=True, text=True)
        self.assertEqual("dashboard fixture\n", run.stdout)
        self.assertEqual(f"#!{sys.executable}\nprint('stats fixture')\n".encode(),
                         (self.bin / "slotfather-stats").read_bytes())

    def test_an_interpreter_older_than_the_minimum_is_refused_before_any_change(self):
        old_python = self.tmp / "python3.8"
        old_python.write_text("#!/bin/sh\necho 3.8\n")
        old_python.chmod(0o755)
        self.queue(self.old)
        before = self.contents(self.old)
        result = self.run_install("--python", str(old_python))
        self.assertEqual(2, result.returncode)
        self.assertIn("is Python 3.8; slotfather needs 3.9 or later", result.stderr)
        self.assertEqual(before, self.contents(self.old))
        self.assertFalse((self.bin / "slotfather").exists())
        self.assertEqual("old command", (self.bin / "machine-slot").read_text())

    def test_live_old_holders_and_waiters_refuse_without_changing_state(self):
        pid, start = self.live_process()
        for old in (self.old, self.legacy):
            for state in ("held", "waiting"):
                with self.subTest(path=old, state=state):
                    self.queue(old, state=state, pid=pid, start=start)
                    before = self.contents(old)
                    result = self.run_install()
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn("live", result.stderr)
                    self.assertEqual(before, self.contents(old))
                    self.assertEqual("old command", (self.bin / "machine-slot").read_text())
                    self.assertFalse(self.new.exists())
                    shutil.rmtree(old)

    def test_existing_new_queue_and_active_lease_are_untouched_and_old_config_is_backed_up(self):
        pid, start = self.live_process()
        self.queue(self.new, pid=pid, start=start)
        (self.new / "capacity.json").write_text('{"slots":9}\n')
        self.queue(self.old)
        (self.old / "capacity.json").write_text('{"slots":6}\n')
        before = self.contents(self.new)
        old_before = self.contents(self.old)
        result = self.run_install()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.contents(self.new))
        backups = list((self.state / "slotfather-migrations").glob("*/machine-slots"))
        self.assertEqual(1, len(backups), result.stdout)
        self.assertEqual(old_before, self.contents(backups[0]))
        self.assertIn("preserved", result.stdout)

    def test_repeated_install_keeps_guards_and_existing_state(self):
        first = self.run_install()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        (self.new / "capacity.json").write_text('{"slots":7}\n')
        before = self.contents(self.new)
        guards = (self.old.read_bytes(), self.legacy.read_bytes())
        result = self.run_install()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.contents(self.new))
        self.assertEqual(guards, (self.old.read_bytes(), self.legacy.read_bytes()))

    def test_malformed_old_entry_refuses_without_retiring_the_queue(self):
        self.queue(self.old)
        (self.old / "queue/0000000001-fixture.json").write_text("not json")
        before = self.contents(self.old)
        result = self.run_install()
        self.assertEqual(2, result.returncode)
        self.assertIn("cannot establish", result.stderr)
        self.assertEqual(before, self.contents(self.old))
        self.assertEqual("old command", (self.bin / "machine-slot").read_text())

    def test_old_command_symlink_is_removed_only_after_installing_the_new_executable(self):
        (self.bin / "machine-slot").unlink()
        (self.bin / "slotfather").write_bytes(b"previous slotfather")
        (self.bin / "machine-slot").symlink_to(self.bin / "slotfather")
        result = self.run_install()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.bin / "machine-slot").is_symlink())
        self.assertEqual(PAYLOAD, (self.bin / "slotfather").read_bytes())

    def test_manual_canonical_guards_are_recognized_with_a_live_new_queue(self):
        guard = b"Retired queue: use slotfather with ~/.local/state/slotfather. Update this checkout before running heavy work.\n"
        for old in (self.old, self.legacy):
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(guard)
        pid, start = self.live_process()
        self.queue(self.new, pid=pid, start=start)
        before = self.contents(self.new)
        result = self.run_install()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.contents(self.new))
        self.assertEqual(guard, self.old.read_bytes())

    def test_an_unrelated_file_at_a_retired_path_is_never_overwritten(self):
        self.old.parent.mkdir(parents=True)
        self.old.write_text("unrelated file")
        result = self.run_install()
        self.assertEqual(2, result.returncode)
        self.assertIn("unrecognized file", result.stderr)
        self.assertEqual("unrelated file", self.old.read_text())
        self.assertEqual("old command", (self.bin / "machine-slot").read_text())

    def test_a_locked_old_queue_refuses_without_changing_its_contents(self):
        self.queue(self.old)
        before = self.contents(self.old)
        with (self.old / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = self.run_install()
        self.assertEqual(2, result.returncode)
        self.assertIn("operation in progress", result.stderr)
        self.assertEqual(before, self.contents(self.old))
        self.assertEqual("old command", (self.bin / "machine-slot").read_text())


class SharedQueueInstallTests(unittest.TestCase):
    """`--user NAME [--bin DIR]` (#653): one queue for every process of a user whatever its HOME, such as the runner
    services of a CI VM, with the commands where those services' PATH looks."""

    run_install = InstallTests.run_install

    def setUp(self):
        InstallTests.setUp(self)
        shutil.copyfile(ROOT / "slotfather", self.source / "slotfather")
        self.system_bin = self.tmp / "usr-local-bin"

    def installer(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("slotfather_installer", self.source / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        install = module.install

        def quiet(*args, **kwargs):
            with contextlib.redirect_stdout(io.StringIO()):
                return install(*args, **kwargs)
        module.install = quiet
        return module

    def test_a_pinned_install_names_the_users_queue_in_the_installed_command(self):
        module = self.installer()
        module.install(self.home, self.source / "slotfather", sys.executable, self.system_bin, pinned=True)
        installed = (self.system_bin / "slotfather").read_text()
        self.assertIn(f'DEFAULT_DIR = {json.dumps(str(self.new))}\n', installed)
        self.assertTrue(installed.startswith(f"#!{sys.executable}\n"))
        self.assertFalse((self.bin / "slotfather").exists(), "--bin replaces ~/.local/bin")
        self.assertTrue(self.new.is_dir())
        # Any HOME, one queue: a job that moved HOME still shares it.
        env = {k: v for k, v in os.environ.items() if not k.startswith("SLOTFATHER")}
        env.update(HOME=str(self.tmp / "elsewhere"), SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_CORES="6",
                   SLOTFATHER_MEMORY_GIB="11.68")
        saved = subprocess.run([str(self.system_bin / "slotfather"), "slots", "memory", "5"], env=env,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual("2 slots (saved: one per 5 GiB of 11.7 GiB)", saved.stdout.strip(), saved.stderr)
        self.assertEqual({"slots": 2, "mode": "memory", "gib_per_slot": 5},
                         json.loads((self.new / "capacity.json").read_text()))
        self.assertFalse((self.tmp / "elsewhere").exists())
        stats = subprocess.run([str(self.system_bin / "slotfather-stats")], env=env, capture_output=True, text=True)
        self.assertEqual("stats fixture\n", stats.stdout, "the companions are installed beside it")

    def test_the_cgroup_the_leases_share_is_written_in_for_every_client(self):
        module = self.installer()
        module.install(self.home, self.source / "slotfather", sys.executable, self.system_bin, pinned=True,
                       cgroup="/ci.slice/ci-runners.slice")
        self.assertIn('DEFAULT_CGROUP = "/ci.slice/ci-runners.slice"  #', (self.system_bin / "slotfather").read_text())
        # A shell outside the slice (no limited cgroup of its own) counts the slice's 10 GiB.
        fixtures = ROOT / "tests" / "fixtures"
        env = {k: v for k, v in os.environ.items() if not k.startswith("SLOTFATHER")}
        env.update(SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_CORES="16", SLOTFATHER_PROC=str(fixtures / "proc" / "calm"),
                   SLOTFATHER_CGROUP_ROOT=str(fixtures / "cgroup" / "calm"))
        listed = subprocess.run([str(self.system_bin / "slotfather"), "slots"], env=env, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual("2 slots (automatic, by memory: one per 4 GiB of 10 GiB in /ci.slice/ci-runners.slice; "
                         "5 by 16 cores)", listed.stdout.strip(), listed.stderr)
        for bad in ("ci.slice", "/a/../b"):
            with self.subTest(cgroup=bad):
                self.assertIn("--cgroup takes", self.run_install("--cgroup", bad).stderr)

    def test_a_source_without_the_state_line_is_refused(self):
        (self.source / "slotfather").write_bytes(PAYLOAD)
        module = self.installer()
        with self.assertRaises(module.InstallError):
            module.install(self.home, self.source / "slotfather", sys.executable, self.system_bin, pinned=True)
        self.assertFalse((self.system_bin / "slotfather").exists())

    def test_give_hands_only_root_owned_paths_under_the_home_to_the_user(self):
        module = self.installer()
        module.install(self.home, self.source / "slotfather", sys.executable, self.system_bin, pinned=True)
        changed = []
        (self.new / "keep-mine").write_text("someone else's")
        user = type("User", (), {"pw_uid": 1001, "pw_gid": 1001})()
        module.give(self.home, user, self.system_bin, owner_of=lambda path: 4242 if "keep" in str(path) else 0,
                    chown=lambda path, uid, gid: changed.append((Path(path), uid, gid)))
        paths = {path for path, _, _ in changed}
        self.assertIn(self.new, paths)
        self.assertIn(self.state / ".slotfather-install.lock", paths)
        self.assertIn(self.old, paths)
        self.assertNotIn(self.new / "keep-mine", paths)
        self.assertFalse(any(self.system_bin in p.parents or p == self.system_bin for p in paths),
                         "commands outside the home stay root's")
        self.assertEqual({(1001, 1001)}, {(uid, gid) for _, uid, gid in changed})

    def test_user_errors_come_before_any_change(self):
        for options, message in ((("--user", "no-such-user-653"), "no user"), (("--bin", "relative/bin"), "absolute"),
                                 (("--user", "a", "--user", "b"), "usage")):
            with self.subTest(options=options):
                result = self.run_install(*options)
                self.assertEqual(2, result.returncode, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.new.exists())
        if os.getuid() != 0:
            result = self.run_install("--user", "root")
            self.assertIn("needs root", result.stderr)
            self.assertFalse(self.new.exists())

    @unittest.skipUnless(os.getuid() == 0 and os.environ.get("SLOTFATHER_TEST_USERADD") == "1" and shutil.which("useradd"),
                         "root in a disposable container with SLOTFATHER_TEST_USERADD=1")
    def test_root_installs_for_a_runner_user_who_then_owns_the_queue(self):
        name, home = "sfrunner653", self.tmp / "runner-home"
        subprocess.run(["useradd", "-d", str(home), "-m", name], check=True)
        self.addCleanup(subprocess.run, ["userdel", name])
        self.tmp.chmod(0o755)
        result = self.run_install("--user", name, "--bin", str(self.system_bin))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        import pwd
        user = pwd.getpwnam(name)
        queue = home / ".local/state/slotfather"
        self.assertEqual((user.pw_uid, user.pw_gid), (queue.stat().st_uid, queue.stat().st_gid))
        self.assertEqual(0, (self.system_bin / "slotfather").stat().st_uid)
        env = {"PATH": f"{self.system_bin}:/usr/bin:/bin", "HOME": "/tmp", "SLOTFATHER_LEGACY_DIRS": ""}
        run = subprocess.run(["setpriv", f"--reuid={user.pw_uid}", f"--regid={user.pw_gid}", "--clear-groups",
                              "slotfather", "run", "-n", "1", "--", "slotfather", "status"], env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertIn("1 of", run.stdout)
        self.assertEqual(user.pw_uid, (queue / "history.jsonl").stat().st_uid)
