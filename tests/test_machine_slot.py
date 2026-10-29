#!/usr/bin/env python3
"""Concurrency fixtures for machine-slot, the machine-wide slot queue every project shares (#599).

Fake work records its start in a log and holds its lease until the test creates its release file, so the order
in which requests start is observable without timing guesses. Each test has its own state directory.
"""
from __future__ import annotations

import json
import os
import pathlib
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = str(ROOT / "machine-slot")
WORK = 'echo "start $1" >> "$LOG"; while [ ! -e "$RELEASE/$1" ]; do sleep 0.05; done; echo "end $1" >> "$LOG"'


class SlotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="machine-slot-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state, self.release_dir, self.log = self.tmp / "state", self.tmp / "release", self.tmp / "log"
        self.release_dir.mkdir()
        self.log.touch()
        self.memory = self.tmp / "memory"
        self.memory.write_text("8000 1\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("MACHINE_SLOT", "STRAYLIGHT_SLOT"))}
        self.env.update(MACHINE_SLOT_DIR=str(self.state), MACHINE_SLOT_LEGACY_DIRS="", MACHINE_SLOT_POLL="0.05",
                        MACHINE_SLOT_TOTAL="4", MACHINE_SLOT_MEMORY_FILE=str(self.memory), LOG=str(self.log),
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
        """Start `machine-slot run -n count --label label -- <fake work label>` in the background."""
        argv = [TOOL, "run", "-n", str(count), "--label", label, *options, "--",
                *(command or ("sh", "-c", WORK, "work", label))]
        process = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
        self.processes.append(process)
        return process

    def tool(self, *args, **env):
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
        del self.env["MACHINE_SLOT_TOTAL"]
        for cores, total in (("12", 4), ("16", 5), ("4", 1), ("2", 1)):
            with self.subTest(cores=cores):
                self.assertEqual(f"0 of {total} slots held", self.tool("status", MACHINE_SLOT_CORES=cores).stdout
                                 .splitlines()[-1])

    def test_a_request_above_the_total_takes_every_slot(self):
        # A script written for a bigger machine still runs: its request is the whole machine here.
        status = self.tmp / "status"
        result = self.tool("run", "-n", "9", "--", "sh", "-c", f'"{TOOL}" status > "{status}"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("4 of 4 slots held", status.read_text().splitlines()[-1])


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
        inner = ["env", "-u", "MACHINE_SLOT_LEASE", TOOL, "run", "-n", "4", "--", "sh", "-c",
                 f'"{TOOL}" status > "{status}"']
        self.finish(self.request("outer", 1, *inner))
        self.assertIn("1 of 4 slots held", status.read_text())

    def test_the_command_sees_its_lease(self):
        seen = self.tmp / "seen"
        self.finish(self.request("lease", 2, "sh", "-c", f'echo "$MACHINE_SLOT_COUNT $MACHINE_SLOT_LEASE" > "{seen}"'))
        count, path = seen.read_text().split()
        self.assertEqual("2", count)
        self.assertTrue(path.startswith(str(self.state / "queue")), path)


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

    def test_with_nothing_running_a_short_swap_does_not_block(self):
        self.memory.write_text("0 4\n")
        lone = self.request("lone", 4)
        self.started("lone")
        self.release("lone")
        self.finish(lone)


class CommandLineTests(SlotCase):
    def test_bypass_runs_without_a_lease(self):
        holder = self.request("holder", 4)
        self.started("holder")
        result = self.tool("run", "-n", "4", "--", "true", MACHINE_SLOT_POLICY="bypass")
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
        self.env["MACHINE_SLOT_LEGACY_DIRS"] = str(self.legacy)

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
    """`machine-slot advise`: a job count from the free slots and the load of processes that take no lease. The
    load is the current one, the lower of load1 and load5: load15 still holds work whose leases ended minutes ago."""

    def advise(self, *args, load="0.0"):
        result = self.tool("advise", *args, MACHINE_SLOT_CORES="12", MACHINE_SLOT_LOADAVG=load)
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
