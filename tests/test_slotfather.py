#!/usr/bin/env python3
"""Concurrency fixtures for slotfather, the machine-wide slot queue every project shares (#599).

Fake work records its start in a log and holds its lease until the test creates its release file, so the order
in which requests start is observable without timing guesses. Each test has its own state directory.
"""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import platform as sf_platform


import importlib.machinery
import importlib.util
import fcntl
import json
import os
import pathlib
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = str(ROOT / "slotfather")
WORK = 'echo "start $1" >> "$LOG"; while [ ! -e "$RELEASE/$1" ]; do sleep 0.05; done; echo "end $1" >> "$LOG"'


class SlotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state, self.release_dir, self.log = self.tmp / "state", self.tmp / "release", self.tmp / "log"
        self.release_dir.mkdir()
        self.log.touch()
        self.memory = self.tmp / "memory"
        self.memory.write_text("8000 1\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("SLOTFATHER", "STRAYLIGHT_SLOT"))}
        self.env.update(SLOTFATHER_DIR=str(self.state), SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_POLL="0.05", SLOTFATHER_POLL_MAX="0.5",
                        SLOTFATHER_OVERTAKE_WINDOW="0",
                        SLOTFATHER_CORES="12", SLOTFATHER_MEMORY_GIB="64", SLOTFATHER_MEMORY_FILE=str(self.memory),
                        LOG=str(self.log),
                        RELEASE=str(self.release_dir))
        self.processes = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        # Each request runs in its own session: this also stops fake work a killed holder left behind.
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):  # macOS: a group whose leader exited unreaped
                pass
            process.wait()
            if process.stdout:
                process.stdout.close()

    def request(self, label, count, *command, options=()):
        """Start `slotfather run -n count --label label -- <fake work label>` in the background."""
        argv = [TOOL, "run", "-n", str(count), "--label", label, *options, "--",
                *(command or ("sh", "-c", WORK, "work", label))]
        process = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
        self.processes.append(process)
        return process

    def tool(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([TOOL, *args], env={**self.env, **env}, capture_output=True, text=True, timeout=20)

    def release(self, label):
        (self.release_dir / label).touch()

    def events(self):
        return self.log.read_text().split("\n")[:-1]

    def status(self):
        return self.tool("status").stdout

    def wait_for(self, predicate, what, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}; events {self.events()}; status:\n{self.status()}")

    def output_line(self, process, timeout=20):
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        return process.stdout.readline() if ready else ""

    def started(self, label):
        self.wait_for(lambda: f"start {label}" in self.events(), f"{label} to start")

    def finish(self, process, timeout=20):
        output, _ = process.communicate(timeout=timeout)
        self.assertEqual(0, process.returncode, output)
        return output

    def holder(self):
        """A live process that is not a lease holder of this queue: (pid, its ps start time)."""
        process = subprocess.Popen(["sh", "-c", f'while [ ! -e "{self.release_dir}/fake" ]; do sleep 0.05; done'],
                                   start_new_session=True)
        self.processes.append(process)
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(process.pid)], capture_output=True,
                               text=True).stdout.strip()
        return process.pid, start

    def queue_entry(self, directory, pid, start, count, label, state="held", sequence=1):
        (directory / "queue").mkdir(parents=True, exist_ok=True)
        (directory / "queue" / f"{sequence:010d}-{pid}.json").write_text(json.dumps(
            {"pid": pid, "start": start, "count": count, "label": label, "cmd": "fake", "since": "-", "state": state}))


class FifoTests(SlotCase):
    def test_an_all_slot_request_is_not_overtaken_by_later_one_slot_requests(self):
        first = self.request("one", 1)
        self.started("one")
        gate = self.request("gate", "all")
        self.assertIn("waits for 4 slot(s)", self.output_line(gate), "the gate should wait for the 1-slot holder")
        later = self.request("later", 1)
        self.wait_for(lambda: "start later" in self.events() or "waiting: 1 slot(s) later" in self.status(),
                      "the later request to queue or start")
        self.assertEqual(["start one"], self.events(), "a later 1-slot request started ahead of the queued gate")
        self.release("one")
        self.started("gate")
        self.assertNotIn("start later", self.events())
        self.release("gate")
        self.started("later")
        self.release("later")
        for process in (first, gate, later):
            self.finish(process)
        self.assertEqual(["start one", "end one", "start gate", "end gate", "start later", "end later"], self.events())

    def test_available_is_the_free_slots_and_none_while_a_request_waits(self):
        self.assertEqual("4", self.tool("available").stdout.strip())
        one = self.request("one", 1)
        self.started("one")
        self.assertEqual("3", self.tool("available").stdout.strip())
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        self.assertEqual("0", self.tool("available").stdout.strip())
        self.release("one")
        self.started("gate")
        self.release("gate")
        for process in (one, gate):
            self.finish(process)

    def test_one_slot_requests_share_the_slots(self):
        workers = [self.request(f"w{i}", 1) for i in range(4)]
        for i in range(4):
            self.started(f"w{i}")
        self.assertIn("4 of 4 slots held", self.status())
        fifth = self.request("w4", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) w4" in self.status(), "the fifth request to queue")
        self.release("w2")
        self.started("w4")
        for label in ("w0", "w1", "w3", "w4"):
            self.release(label)
        for process in (*workers, fifth):
            self.finish(process)


class TotalTests(SlotCase):
    def test_the_total_is_one_slot_per_three_cores_and_at_least_one(self):
        del self.env["SLOTFATHER_CORES"]
        for cores, total in (("12", 4), ("16", 5), ("4", 1), ("2", 1)):
            with self.subTest(cores=cores):
                self.assertEqual(f"0 of {total} slots held", self.tool("status", SLOTFATHER_CORES=cores).stdout
                                 .splitlines()[-1])

    def test_a_request_above_the_total_takes_every_slot(self):
        # A script written for a bigger machine still runs: its request is the whole machine here.
        status = self.tmp / "status"
        result = self.tool("run", "-n", "9", "--", "sh", "-c", f'"{TOOL}" status > "{status}"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("4 of 4 slots held", status.read_text().splitlines()[-1])


class CapacityTests(SlotCase):
    def test_saved_capacity_is_shared_by_new_processes_and_reset_restores_auto(self):
        self.assertEqual("4 slots (automatic)", self.tool("slots").stdout.strip())
        changed = self.tool("slots", "6")
        self.assertEqual(0, changed.returncode, changed.stderr)
        self.assertEqual("6 slots (saved)", changed.stdout.strip())
        self.assertEqual("6 slots (saved)", self.tool("slots", SLOTFATHER_TOTAL="99").stdout.strip())
        self.assertEqual("6", self.tool("available").stdout.strip())
        self.assertEqual(6, json.loads(self.tool("advise", "--exclusive", SLOTFATHER_LOADAVG="0").stdout)["jobs"])
        result = self.tool("run", "-n", "all", "--", TOOL, "status")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("6 of 6 slots held", result.stdout)
        reset = self.tool("slots", "reset")
        self.assertEqual(0, reset.returncode, reset.stderr)
        self.assertEqual("4 slots (automatic)", self.tool("slots").stdout.strip())

    def test_invalid_capacity_never_changes_saved_value(self):
        self.assertEqual(0, self.tool("slots", "6").returncode)
        for args in (("0",), ("-1",), ("1.5",), ("all",), ("many",), ("6", "7")):
            with self.subTest(args=args):
                result = self.tool("slots", *args)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertEqual("6 slots (saved)", self.tool("slots").stdout.strip())

    def test_capacity_changes_are_refused_until_holders_and_waiters_leave(self):
        holder = self.request("holder", "all")
        self.started("holder")
        waiter = self.request("waiter", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) waiter" in self.status(), "waiter to queue")
        for value in ("6", "2", "reset"):
            result = self.tool("slots", value)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("queue is busy", result.stderr)
            self.assertIn("4 of 4 slots held", self.status())
        self.release("holder")
        self.finish(holder)
        self.started("waiter")
        self.assertIn("4 of 4 slots held", self.status())
        self.release("waiter")
        self.finish(waiter)
        self.assertEqual(0, self.tool("slots", "6").returncode)

    def test_a_live_waiter_alone_also_blocks_capacity_changes(self):
        pid, start = self.holder()
        self.queue_entry(self.state, pid, start, 4, "waiting-only", state="waiting")
        self.assertIn("queue is busy", self.tool("slots", "6").stderr)
        self.assertEqual("4 slots (automatic)", self.tool("slots").stdout.strip())

    def test_a_crashed_entry_does_not_block_capacity_changes(self):
        self.queue_entry(self.state, 99999999, "gone", 4, "crashed")
        self.assertEqual(0, self.tool("slots", "6").returncode)
        self.assertEqual("6 slots (saved)", self.tool("slots").stdout.strip())

    def test_legacy_holders_and_waiters_block_capacity_changes(self):
        legacy = self.tmp / "legacy"
        self.env["SLOTFATHER_LEGACY_DIRS"] = str(legacy)
        pid, start = self.holder()
        for state in ("held", "waiting"):
            self.queue_entry(legacy, pid, start, 4, "legacy", state=state)
            self.assertIn("queue is busy", self.tool("slots", "6").stderr)


class NestingTests(SlotCase):
    def test_nested_runs_neither_deadlock_nor_count_twice(self):
        status = self.tmp / "inner-status"
        inner = [TOOL, "run", "-n", "all", "--label", "inner", "--", "sh", "-c", f'"{TOOL}" status > "{status}"']
        outer = self.request("outer", 2, TOOL, "run", "-n", "1", "--label", "middle", "--", *inner)
        self.finish(outer)
        lines = status.read_text().splitlines()
        self.assertEqual("2 of 4 slots held", lines[-1])
        self.assertEqual(["outer"], [line.split()[3] for line in lines[:-1]], lines)

    def test_a_nested_run_without_the_lease_variable_finds_its_ancestor(self):
        status = self.tmp / "inner-status"
        inner = ["env", "-u", "SLOTFATHER_LEASE", TOOL, "run", "-n", "4", "--", "sh", "-c",
                 f'"{TOOL}" status > "{status}"']
        self.finish(self.request("outer", 1, *inner))
        self.assertIn("1 of 4 slots held", status.read_text())

    def test_the_command_sees_its_lease(self):
        seen = self.tmp / "seen"
        self.finish(self.request("lease", 2, "sh", "-c", f'echo "$SLOTFATHER_COUNT $SLOTFATHER_LEASE" > "{seen}"'))
        count, path = seen.read_text().split()
        self.assertEqual("2", count)
        self.assertTrue(path.startswith(str(self.state / "queue")), path)


class OvertakeTests(SlotCase):
    """A waiter that does not fit lets later requests that fit start first until it has waited the window."""

    def queue_gate_behind_one(self, window):
        self.env["SLOTFATHER_OVERTAKE_WINDOW"] = window
        one = self.request("one", 1)
        self.started("one")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        return one, gate

    def test_within_the_window_a_smaller_request_passes_a_blocked_gate(self):
        one, gate = self.queue_gate_behind_one("600")
        self.assertIn("requests that fit may pass gate until", self.output_line(gate))
        self.assertEqual("3", self.tool("available").stdout.strip(), "the waiting gate holds no slots back yet")
        later = self.request("later", 1)
        self.started("later")
        self.release("one")
        self.wait_for(lambda: "end one" in self.events(), "one to end")
        self.assertNotIn("start gate", self.events(), "the gate needs every slot, and later still holds one")
        self.release("later")
        self.started("gate")
        self.release("gate")
        for process in (one, gate, later):
            self.finish(process)
        self.assertEqual(["start one", "start later", "end one", "end later", "start gate", "end gate"],
                         self.events())

    def test_past_the_window_a_blocked_gate_holds_back_later_requests(self):
        one, gate = self.queue_gate_behind_one("1")
        time.sleep(2.1)  # queue stamps have whole seconds
        self.assertEqual("0", self.tool("available").stdout.strip())
        later = self.request("later", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) later" in self.status(), "the later request to queue")
        self.release("one")
        self.started("gate")
        self.assertNotIn("start later", self.events())
        self.release("gate")
        self.started("later")
        self.release("later")
        for process in (one, gate, later):
            self.finish(process)

    def test_an_all_slot_request_that_waits_says_what_all_is_for(self):
        one, gate = self.queue_gate_behind_one("0")
        self.release("one")
        self.release("gate")
        self.finish(one)
        self.assertEqual(1, self.finish(gate).count("slot: -n all holds every later request behind it"))


class HistoryTests(SlotCase):
    def history(self):
        lines = (self.state / "history.jsonl").read_text().splitlines()
        return {item["label"]: item for item in map(json.loads, lines)}

    def test_each_request_that_leaves_the_queue_is_recorded(self):
        whole = self.request("whole", "all")
        self.started("whole")
        late = self.request("late", 1, options=("--wait", "0.3"))
        self.assertEqual(75, late.wait(timeout=20))
        self.release("whole")
        self.finish(whole)
        self.assertEqual(3, self.tool("run", "--label", "fails", "--", "sh", "-c", "exit 3").returncode)
        found = self.history()
        self.assertEqual(("released", 4, 0, os.getcwd()), (found["whole"]["outcome"], found["whole"]["count"],
                                                         found["whole"]["status"], found["whole"]["cwd"]))
        self.assertGreaterEqual(found["whole"]["ran"], 0)
        self.assertEqual(("left", None, None), (found["late"]["outcome"], found["late"]["granted"],
                                                found["late"]["ran"]))
        self.assertEqual(("released", 3, "sh -c exit 3"), (found["fails"]["outcome"], found["fails"]["status"],
                                                           found["fails"]["cmd"]))
        listing = self.tool("history").stdout
        self.assertRegex(listing, r"left: 1 slot\(s\) late waited \d+s cmd=sh")
        # A command that ends within 5 ms records a wall of 0.00 s and shows its CPU seconds instead of cores.
        self.assertRegex(listing, r"released: 1 slot\(s\) fails waited \d+s ran \d+s exit 3 used (?:[\d.]+ cores|\d+ s CPU), \d+ MB "
                                  r"cmd=sh -c exit 3")
        self.assertEqual(1, len(self.tool("history", "1").stdout.splitlines()))

    def test_a_reaped_lease_is_recorded(self):
        (self.state / "queue").mkdir(parents=True)
        (self.state / "queue" / "0000000001-1.json").write_text(json.dumps(
            {"pid": os.getpid(), "start": "Thu Jan  1 00:00:00 1970", "count": 2, "label": "crashed", "cmd": "fake",
             "since": "2026-01-01T00:00:00Z", "granted": "2026-01-01T00:01:00Z", "state": "held"}))
        self.status()
        item = self.history()["crashed"]
        self.assertEqual(("reaped", 60), (item["outcome"], item["waited"]))
        self.assertGreater(item["ran"], 0)


class StaleLeaseTests(SlotCase):
    def test_a_crashed_holder_is_reaped_and_the_queue_moves_on(self):
        crashed = self.request("crashed", 4)
        self.started("crashed")
        waiter = self.request("next", 4)
        self.wait_for(lambda: "waiting: 4 slot(s) next" in self.status(), "the next request to queue")
        os.kill(crashed.pid, signal.SIGKILL)  # the holder dies without releasing; its fake work keeps running
        self.started("next")
        self.release("next")
        self.finish(waiter)
        self.assertIn("reaped held lease of 4 for crashed", (self.state / "queue.log").read_text())
        self.release("crashed")

    def test_a_reused_pid_does_not_keep_a_dead_holders_lease(self):
        pid, start = self.holder()
        self.queue_entry(self.state, pid, "Thu Jan  1 00:00:00 1970", 4, "earlier-holder")
        self.assertIn("0 of 4 slots held", self.status())
        self.assertIn("reaped held lease of 4 for earlier-holder", (self.state / "queue.log").read_text())
        # The same pid with its own start time is the holder itself and keeps the lease.
        self.queue_entry(self.state, pid, start, 3, "live-holder", sequence=2)
        self.assertIn("3 of 4 slots held", self.status())

    def test_a_terminated_waiter_leaves_the_queue(self):
        holder = self.request("holder", 4)
        self.started("holder")
        waiter = self.request("quitter", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) quitter" in self.status(), "the waiter to queue")
        waiter.terminate()
        waiter.wait(timeout=10)
        self.assertNotIn("quitter", self.status())
        self.release("holder")
        self.finish(holder)

    def test_a_wait_limit_gives_up_and_leaves_the_queue(self):
        holder = self.request("holder", "all")
        self.started("holder")
        result = self.tool("run", "-n", "1", "--label", "impatient", "--wait", "0.3", "--", "true")
        self.assertEqual(75, result.returncode, result.stdout + result.stderr)
        self.assertIn("no 1 slot(s) within 0.3 s", result.stderr)
        self.assertNotIn("impatient", self.status())
        self.release("holder")
        self.finish(holder)


class PollingTests(SlotCase):
    def count_ps(self):
        """Puts a ps that counts its runs first on PATH for requests started from now; returns the count file."""
        tools, count = self.tmp / "tools", self.tmp / "ps-runs"
        tools.mkdir()
        (tools / "ps").write_text(f'#!/bin/sh\necho run >> "{count}"\nexec /bin/ps "$@"\n')
        (tools / "ps").chmod(0o755)
        count.touch()
        self.env = {**self.env, "PATH": f"{tools}:{self.env['PATH']}"}
        return count

    def test_a_long_waiter_looks_at_the_queue_less_and_less_often(self):
        holder = self.request("holder", "all")
        self.started("holder")
        count = self.count_ps()
        self.env["SLOTFATHER_POLL_MAX"] = "1"
        waiter = self.request("waiter", 1)
        self.wait_for(lambda: count.read_text(), "the waiter's first look")
        time.sleep(3)
        looks = len(count.read_text().split())
        # Every 0.05 s would be about 60 looks; growing by half each time to 1 s it is about 10.
        self.assertLess(looks, 20, "a waiter whose reason to wait does not change should back off")
        self.release("holder")
        self.started("waiter")
        self.release("waiter")
        for process in (holder, waiter):
            self.finish(process)

    def test_a_release_hands_its_slots_on_without_waiting_for_the_next_look(self):
        holder = self.request("holder", "all")
        self.started("holder")
        waiter = self.request("waiter", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) waiter" in self.status(), "the waiter to queue")
        # A stopped waiter takes no look of its own, so only the release can grant it.
        # Stop only outside its critical section; otherwise the test freezes the
        # queue lock and neither release nor status can make progress.
        with (self.state / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.kill(waiter.pid, signal.SIGSTOP)
            os.waitpid(waiter.pid, os.WUNTRACED)
        try:
            self.release("holder")
            self.wait_for(lambda: "held: 1 slot(s) waiter" in self.status(), "the release to grant the waiter",
                          timeout=5)
        finally:
            os.kill(waiter.pid, signal.SIGCONT)
        self.started("waiter")
        self.release("waiter")
        for process in (holder, waiter):
            self.finish(process)


class MemoryGuardTests(SlotCase):
    def test_new_leases_wait_while_swap_is_short_and_start_when_it_recovers(self):
        holder = self.request("holder", 1)
        self.started("holder")
        self.memory.write_text("100 1\n")
        guarded = self.request("guarded", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) guarded" in self.status(), "the request to wait")
        self.assertIn("free swap 100 MB is under 512 MB", self.output_line(guarded))
        self.memory.write_text("4000 1\n")
        self.started("guarded")
        for label, process in (("holder", holder), ("guarded", guarded)):
            self.release(label)
            self.finish(process)

    def test_memory_pressure_warning_also_holds_new_leases(self):
        holder = self.request("holder", 1)
        self.started("holder")
        self.memory.write_text("8000 2\n")
        guarded = self.request("guarded", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) guarded" in self.status(), "the request to wait")
        self.assertIn("memory pressure level 2", self.output_line(guarded))
        self.release("holder")
        self.finish(holder)
        self.started("guarded")
        self.release("guarded")
        self.finish(guarded)

    def test_a_host_with_swap_turned_off_has_no_swap_floor(self):
        self.assertIsNone(sf_platform.swap_free_mb("total = 0.00M  used = 0.00M  free = 0.00M  (encrypted)"))
        self.assertEqual(148.0, sf_platform.swap_free_mb("total = 2048.00M  used = 1900.00M  free = 148.00M  (encrypted)"))
        self.assertEqual(1536.0, sf_platform.swap_free_mb("total = 2.00G  used = 512.00M  free = 1.50G  (encrypted)"))
        self.assertIsNone(sf_platform.swap_free_mb(""))

    def test_with_nothing_running_a_short_swap_does_not_block(self):
        self.memory.write_text("0 4\n")
        lone = self.request("lone", 4)
        self.started("lone")
        self.release("lone")
        self.finish(lone)


class CommandLineTests(SlotCase):
    def test_an_interpreter_older_than_the_minimum_says_how_to_reinstall(self):
        result = subprocess.run([sys.executable, "-c", "import runpy, sys; sys.version_info = (3, 8, 0); "
                                 f"runpy.run_path({TOOL!r}, run_name='__main__')", "status"],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(1, result.returncode)
        self.assertIn("slotfather needs Python 3.9 or later", result.stderr)
        self.assertIn("install.py", result.stderr)

    def test_bypass_runs_without_a_lease(self):
        holder = self.request("holder", 4)
        self.started("holder")
        result = self.tool("run", "-n", "4", "--", "true", SLOTFATHER_POLICY="bypass")
        self.assertEqual(0, result.returncode, result.stderr)
        self.release("holder")
        self.finish(holder)

    def test_the_command_exit_status_is_returned(self):
        self.assertEqual(3, self.tool("run", "-n", "1", "--", "sh", "-c", "exit 3").returncode)
        self.assertEqual(128 + signal.SIGTERM, self.tool("run", "--", "sh", "-c", "kill -TERM $$").returncode)
        self.assertEqual(127, self.tool("run", "--", str(self.tmp / "missing")).returncode)

    def test_bad_requests_are_refused(self):
        for args in (("run", "-n", "0", "--", "true"), ("run", "-n", "many", "--", "true"), ("run", "-n", "1"),
                     ("run", "--wait", "soon", "--", "true"), ("nonsense",)):
            with self.subTest(args=args):
                self.assertEqual(2, self.tool(*args).returncode)


class LegacyQueueTests(SlotCase):
    """The queue it replaces (Straylight's ~/.local/state/straylight-verification/slots): leases held there count as
    used, and a run under one of them inherits it, until no old checkout runs."""

    def setUp(self):
        super().setUp()
        self.legacy = self.tmp / "legacy"
        self.env["SLOTFATHER_LEGACY_DIRS"] = str(self.legacy)

    def test_a_lease_held_in_the_old_queue_is_counted(self):
        pid, start = self.holder()
        self.queue_entry(self.legacy, pid, start, 3, "old-gate")
        self.assertIn("legacy: 3 slot(s) old-gate", self.status())
        two = self.request("two", 2)
        self.wait_for(lambda: "waiting: 2 slot(s) two" in self.status(), "the request to wait for the old lease")
        self.release("fake")
        self.started("two")
        self.release("two")
        self.finish(two)

    def test_a_run_under_an_old_queue_lease_inherits_it(self):
        status = self.tmp / "status"
        # An old client holds every slot in its queue under its own pid, then runs a new request.
        script = (f'mkdir -p "{self.legacy}/queue"; '
                  f'echo "{{\\"pid\\": $$, \\"start\\": \\"\\", \\"count\\": 4, \\"label\\": \\"old\\", '
                  f'\\"cmd\\": \\"x\\", \\"since\\": \\"-\\", \\"state\\": \\"held\\"}}" > "{self.legacy}/queue/0000000001-$$.json"; '
                  f'"{TOOL}" run -n all -- "{TOOL}" status > "{status}"')
        result = subprocess.run(["sh", "-c", script], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["legacy: 4 slot(s) old", "4 of 4 slots held"],
                         [line.split(" pid=")[0] for line in status.read_text().splitlines()])


class AdviceTests(SlotCase):
    """`slotfather advise`: a job count from the free slots and the load of processes that take no lease. The
    load is the current one, the lower of load1 and load5: load15 still holds work whose leases ended minutes ago."""

    def advise(self, *args, load="0.0"):
        result = self.tool("advise", *args, SLOTFATHER_CORES="12", SLOTFATHER_LOADAVG=load)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_an_idle_machine_gives_every_slot(self):
        advice = self.advise("--exclusive")
        self.assertEqual((4, 4, 3.0, 0, 0.0), (advice["jobs"], advice["total"], advice["cores_per_slot"],
                                               advice["held"], advice["unleased_load"]))

    def test_load_without_a_lease_takes_its_cores_out(self):
        # 7.5 of 12 cores busy and no lease held: 4.5 cores, one and a half slots, are left; half a slot counts.
        advice = self.advise("--exclusive", load="7.5")
        self.assertEqual((7.5, 2), (advice["unleased_load"], advice["jobs"]))
        self.assertEqual(1, self.advise("--exclusive", load="14")["jobs"], "at least one job")

    def test_load_from_leases_that_just_ended_does_not_shrink_the_job_count(self):
        # Review of #599: right after lands whose smokes released their leases, load15 is still 8 with nothing held.
        advice = self.advise("--exclusive", load="0.5 2.0 8.0")
        self.assertEqual((4, 0.5, 0.5, 8.0), (advice["jobs"], advice["load"], advice["unleased_load"],
                                              advice["load15"]))
        # Load that has lasted minutes counts, whatever load15 says; a spike of a minute does not.
        self.assertEqual(2, self.advise("--exclusive", load="7.5 7.5 2.0")["jobs"])
        self.assertEqual(4, self.advise("--exclusive", load="9.0 0.5 0.4")["jobs"])

    def test_held_slots_explain_their_share_of_the_load(self):
        holder = self.request("holder", 2)
        self.started("holder")
        # The holder's 2 slots account for 6 of the 7.5: 1.5 is load without a lease.
        exclusive = self.advise("--exclusive", load="7.5")
        self.assertEqual((2, 1.5, 4), (exclusive["held"], exclusive["unleased_load"], exclusive["jobs"]))
        # A request that runs beside the holder gets at most the free slots and the idle cores.
        beside = self.advise(load="7.5")
        self.assertEqual((2, 2), (beside["free"], beside["jobs"]))
        self.assertEqual(1, self.advise("--max", "1", load="7.5")["jobs"])
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        self.assertEqual((0, 1), (self.advise(load="7.5")["free"], self.advise(load="7.5")["jobs"]))
        self.release("holder")
        self.release("gate")
        self.finish(holder)
        self.finish(gate)


if __name__ == "__main__":
    unittest.main()
