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

    def run_install(self, *options: str) -> subprocess.CompletedProcess[str]:
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
        if (ROOT / "src/slotfather").is_dir():
            shutil.copytree(ROOT / "src", self.source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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
        configs = list((self.system_bin.parent / "lib/slotfather/releases").glob("*/slotfather/config.py"))
        defaults = configs[0].read_text() if configs else installed
        self.assertIn(f'DEFAULT_DIR = {json.dumps(str(self.new))}\n', defaults)
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
        self.assertEqual(0, stats.returncode, stats.stderr)
        if configs:
            self.assertIn("slotfather stats · 0 requests", stats.stdout)
        else:
            self.assertEqual("stats fixture\n", stats.stdout, "the companions are installed beside it")

    def test_the_cgroup_the_leases_share_is_written_in_for_every_client(self):
        module = self.installer()
        module.install(self.home, self.source / "slotfather", sys.executable, self.system_bin, pinned=True,
                       cgroup="/ci.slice/ci-runners.slice")
        configs = list((self.system_bin.parent / "lib/slotfather/releases").glob("*/slotfather/config.py"))
        defaults = configs[0].read_text() if configs else (self.system_bin / "slotfather").read_text()
        self.assertIn('DEFAULT_CGROUP = "/ci.slice/ci-runners.slice"  #', defaults)
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
        config = self.source / "src/slotfather/config.py"
        if config.is_file():
            config.write_text(config.read_text().replace('DEFAULT_DIR = "~/.local/state/slotfather"\n', ''))
        else:
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


class PackageInstallTests(unittest.TestCase):
    """Installed entry points must keep working after their source checkout disappears."""

    tmp: Path
    home: Path
    state: Path
    old: Path
    legacy: Path
    new: Path
    bin: Path
    source: Path
    env: dict[str, str]

    setUp = InstallTests.setUp
    run_install = InstallTests.run_install

    def package(self) -> Path:
        package = self.source / 'src' / 'slotfather'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'config.py').write_text('DEFAULT_DIR = "~/.local/state/slotfather"\nDEFAULT_CGROUP = None  # installer default\n')
        (package / 'cli.py').write_text(
            'from pathlib import Path\nimport json\nfrom . import config\n'
            'def main():\n'
            '    print(json.dumps({"state": config.DEFAULT_DIR, "cgroup": config.DEFAULT_CGROUP, '
            '"version": json.loads(Path(__file__).with_name("version.json").read_text())}))\n'
            '    return 0\n')
        for name in ('dashboard', 'stats', 'sweep'):
            (package / (name + '.py')).write_text('def main():\n    print("package ' + name + '")\n    return 0\n')
        return package

    def test_package_install_is_self_contained_and_reports_identical_source_identity(self):
        self.package()
        result = self.run_install('--python', sys.executable)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        shutil.rmtree(self.source)
        run = subprocess.run([str(self.bin / 'slotfather'), 'version', '--json'], env=self.env,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stderr)
        info = json.loads(run.stdout)
        self.assertEqual('~/.local/state/slotfather', info['state'])
        self.assertEqual(64, len(info['version']['source_sha256']))
        self.assertIn('commit', info['version'])
        self.assertNotIn(str(self.source), (self.bin / 'slotfather').read_text())
        for name in ('dashboard', 'stats', 'sweep'):
            companion = subprocess.run([str(self.bin / ('slotfather-' + name))], env=self.env,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual('package ' + name + '\n', companion.stdout, companion.stderr)

    def test_ssh_install_streams_the_same_source_and_quotes_remote_paths(self):
        self.package()
        local = self.run_install('--python', sys.executable)
        self.assertEqual(0, local.returncode, local.stdout + local.stderr)
        read = subprocess.run([str(self.bin / 'slotfather')], env=self.env,
                              capture_output=True, text=True, check=True)
        identity = json.loads(read.stdout)['version']
        remote_home = self.tmp / 'remote home'
        remote_home.mkdir()
        remote_bin = remote_home / 'custom bin'
        commands = self.tmp / 'ssh-bin'
        commands.mkdir()
        fake_ssh = commands / 'ssh'
        fake_ssh.write_text(f'#!{sys.executable}\n'
                            'import os, subprocess, sys\n'
                            'assert sys.argv[1:3] == ["--", "fixture-host"]\n'
                            f'env = {{**os.environ, "HOME": {str(remote_home)!r}, "TMPDIR": {str(remote_home)!r}}}\n'
                            'sys.exit(subprocess.run(["sh", "-c", sys.argv[3]], env=env).returncode)\n')
        fake_ssh.chmod(0o755)
        self.env['PATH'] = str(commands) + os.pathsep + self.env.get('PATH', '')
        result = self.run_install('--host', 'fixture-host', '--python', sys.executable, '--bin', str(remote_bin))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        remote = subprocess.run([str(remote_bin / 'slotfather')], env=self.env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(identity, json.loads(remote.stdout)['version'])
        self.assertEqual([], list(remote_home.glob('slotfather-install.*')), 'remote transport cleans its own temporary tree')

    def test_ssh_options_or_shell_syntax_are_refused_before_launching_ssh(self):
        self.package()
        for host in ('-oProxyCommand=bad', 'host;echo bad', 'host with space'):
            with self.subTest(host=host):
                result = self.run_install('--host', host)
                self.assertEqual(2, result.returncode)
                self.assertIn('--host takes', result.stderr)

    def test_a_real_pre_package_client_keeps_its_lease_through_upgrade(self):
        import gzip
        import hashlib
        import time
        payload = gzip.decompress((ROOT / 'tests/fixtures/upgrade/slotfather-556f912.py.gz').read_bytes())
        self.assertEqual('c320a2ca95e4da03fa73b5b86c836264b5a9716de0d6fbbcfacc5d8cd57be8b8', hashlib.sha256(payload).hexdigest())
        old = self.tmp / 'old-slotfather'
        old.write_bytes(payload)
        old.chmod(0o755)
        for name in ('slotfather', 'slotfather-dashboard', 'slotfather-stats', 'slotfather-sweep'):
            shutil.copyfile(ROOT / name, self.source / name)
        shutil.copytree(ROOT / 'src', self.source / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        memory = self.tmp / 'memory'
        memory.write_text('8000 1\n')
        env = {key: value for key, value in self.env.items() if not key.startswith('SLOTFATHER')}
        env.update(SLOTFATHER_DIR=str(self.new), SLOTFATHER_LEGACY_DIRS='', SLOTFATHER_CORES='12',
                   SLOTFATHER_MEMORY_GIB='64', SLOTFATHER_MEMORY_FILE=str(memory), SLOTFATHER_LOADAVG='0',
                   SLOTFATHER_POLL='0.05', SLOTFATHER_POLL_MAX='0.1')
        release = self.tmp / 'release-old'
        command = 'import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);\nwhile not p.exists(): time.sleep(0.02)'
        holder = subprocess.Popen([sys.executable, str(old), 'run', '-n', '1', '--label', 'old-client', '--',
                                   sys.executable, '-c', command, str(release)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                entries = list((self.new / 'queue').glob('*.json'))
                if entries and json.loads(entries[0].read_text()).get('state') == 'held':
                    break
                if holder.poll() is not None:
                    self.fail('old holder exited: ' + str(holder.communicate()))
                time.sleep(0.02)
            else:
                self.fail('old client did not acquire its lease')
            before = {p.name: p.read_bytes() for p in (self.new / 'queue').glob('*.json')}
            result = self.run_install('--python', sys.executable)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (self.new / 'queue').glob('*.json')})
            current = subprocess.run([str(self.bin / 'slotfather'), 'status', '--json'], env=env,
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(0, current.returncode, current.stderr)
            self.assertEqual(['old-client'], [entry['label'] for entry in json.loads(current.stdout)['entries']])
            blocked = subprocess.run([str(self.bin / 'slotfather'), 'run', '-n', 'all', '--wait', '0.1', '--',
                                      sys.executable, '-c', 'pass'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(75, blocked.returncode, blocked.stdout + blocked.stderr)
            release.touch()
            output, error = holder.communicate(timeout=10)
            self.assertEqual(0, holder.returncode, output + error)
            current = subprocess.run([str(self.bin / 'slotfather'), 'status', '--json'], env=env,
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual([], json.loads(current.stdout)['entries'])
            history = [json.loads(line) for line in (self.new / 'history.jsonl').read_text().splitlines()]
            old_history = next(row for row in history if row.get('label') == 'old-client')
            self.assertEqual('released', old_history['outcome'])
        finally:
            release.touch()
            if holder.poll() is None:
                import signal
                os.killpg(holder.pid, signal.SIGTERM)
            holder.communicate(timeout=10)

    def test_installing_identical_runtime_after_commit_refreshes_identity_without_rewriting_old_release(self):
        self.package()
        first = self.run_install('--python', sys.executable)
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        read = subprocess.run([str(self.bin / 'slotfather')], env=self.env,
                              capture_output=True, text=True, check=True)
        original = json.loads(read.stdout)['version']
        supplied = {**original, 'commit': 'a' * 40, 'dirty': False}
        (self.source / '.slotfather-source.json').write_text(json.dumps(supplied))
        installed = self.run_install('--python', sys.executable)
        self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
        read = subprocess.run([str(self.bin / 'slotfather')], env=self.env,
                              capture_output=True, text=True, check=True)
        self.assertEqual(supplied, json.loads(read.stdout)['version'])
        versions = [json.loads(path.read_text()) for path in
                    (self.home / '.local/lib/slotfather/releases').glob('*/slotfather/version.json')]
        self.assertCountEqual([original, supplied], versions)

    def test_system_release_is_readable_by_the_service_user_and_not_writable_by_other_users(self):
        self.package()
        result = self.run_install('--python', sys.executable, '--bin', str(self.tmp / 'system-bin'))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        releases = self.tmp / 'lib/slotfather/releases'
        release = next(releases.iterdir())
        self.assertEqual(0o755, release.stat().st_mode & 0o777)
        for path in release.rglob('*'):
            self.assertEqual(0o755 if path.is_dir() else 0o644, path.stat().st_mode & 0o777,
                             f'{path}: service users must read code but cannot replace it')
