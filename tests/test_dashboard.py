#!/usr/bin/env python3
"""The dashboard against real queues: `dashboard --once` prints the same canvas the live view draws, so these tests
read its text. Log history is checked on slotfather-dashboard itself."""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import importlib.machinery
import importlib.util
import os
import pathlib
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest

from test_slotfather import TOOL, SlotCase


def load_dashboard():
    loader = importlib.machinery.SourceFileLoader("slotfather_dashboard", TOOL + "-dashboard")
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class DashboardTests(SlotCase):
    def snapshot(self, width=140, height=40):
        result = self.tool("dashboard", "--once", "--width", str(width), "--height", str(height))
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_an_idle_queue_reads_open_with_every_slot_free(self):
        text = self.snapshot()
        self.assertIn("OPEN", text)
        self.assertIn("4 of 4 slots free · a new request starts now", text)
        self.assertIn("Nothing running · all 4 slots free", text)
        self.assertNotIn("\x1b[", text, "a snapshot to a pipe is plain text")

    def test_a_blocked_head_explains_why_a_smaller_request_behind_it_waits(self):
        holder = self.request("holder", 2)
        self.started("holder")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        small = self.request("small", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) small" in self.status(), "the small request to queue")
        text = self.snapshot()
        self.assertIn("QUEUED", text)
        self.assertIn("next is gate, which needs 4 slots with 2 free · 1 smaller request that would fit", text)
        self.assertIn("next: gate ×4", text)
        self.assertIn("2/4 held", text)
        running, queue = text.index("RUNNING"), text.index("QUEUE ")
        self.assertLess(running, text.index("│ █ holder"), "the holder is listed under RUNNING")
        self.assertRegex(text[queue:], r"1 gate +4 .* next")
        self.assertRegex(text[queue:], r"2 small +1 .* 4 ")
        for label in ("holder", "gate", "small"):
            self.release(label)
        for process in (holder, gate, small):
            self.finish(process)

    def test_every_terminal_size_draws_without_error(self):
        holder = self.request("a-rather-long-label-for-a-holder", 1)
        self.started("a-rather-long-label-for-a-holder")
        for width, height in ((20, 10), (49, 13), (50, 14), (80, 24), (109, 30), (110, 30), (200, 60)):
            text = self.snapshot(width, height)
            self.assertEqual(height, len(text.splitlines()), f"{width}x{height}")
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), f"{width}x{height}")
        self.release("a-rather-long-label-for-a-holder")
        self.finish(holder)

    def test_control_bytes_in_labels_never_reach_the_terminal(self):
        holder = self.request("evil\x1b[2Jlabel", 1)
        self.wait_for(lambda: "evil" in self.status(), "the holder to start")
        text = self.snapshot()
        self.assertNotIn("\x1b", text)
        self.assertIn("evil [2Jlabel", text)
        self.release("evil\x1b[2Jlabel")
        self.finish(holder)

    def test_options_are_checked(self):
        for args in (["--interval", "0"], ["--width", "4"], ["--height", "x"], ["--bogus"]):
            result = self.tool("dashboard", *args)
            self.assertEqual(2, result.returncode, args)
        result = self.tool("dashboard")
        self.assertEqual(2, result.returncode)
        self.assertIn("dashboard --once prints one snapshot", result.stderr)


class SeparationTests(SlotCase):
    def test_the_queue_runs_without_the_dashboard_beside_it(self):
        alone = self.tmp / "alone"
        alone.mkdir()
        shutil.copy(TOOL, alone / "slotfather")
        run = lambda *args: subprocess.run([str(alone / "slotfather"), *args], env=self.env, capture_output=True,
                                           text=True, timeout=20)
        self.assertEqual(0, run("run", "--", "true").returncode)
        result = run("dashboard", "--once")
        self.assertEqual(2, result.returncode)
        self.assertIn("the dashboard is not installed beside slotfather", result.stderr)

    def test_plain_slotfather_opens_the_dashboard_in_a_terminal_and_prints_usage_without_one(self):
        result = self.tool()
        self.assertEqual(2, result.returncode)
        self.assertIn("usage: slotfather", result.stderr)
        pid, terminal = pty.fork()
        if pid == 0:
            os.execve(TOOL, [TOOL], {**self.env, "TERM": "xterm-256color"})
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        screen, deadline = b"", time.monotonic() + 15
        while b"RUNNING" not in screen and time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.1)[0]:
                with contextlib.suppress(OSError):
                    screen += os.read(terminal, 65536)
        os.write(terminal, b"q")
        status = None
        while status is None and time.monotonic() < deadline + 10:
            # Keep reading: a dashboard blocked on a full terminal never reads the q.
            if select.select([terminal], [], [], 0.1)[0]:
                with contextlib.suppress(OSError):
                    os.read(terminal, 65536)
            done, code = os.waitpid(pid, os.WNOHANG)
            status = code if done else None
        os.close(terminal)
        if status is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail("the dashboard did not quit on q")
        self.assertIn(b"RUNNING", screen)
        self.assertEqual(0, os.waitstatus_to_exitcode(status))

    def test_the_queue_code_holds_no_terminal_code(self):
        source = pathlib.Path(TOOL).read_text()
        for word in ("curses", "lsof", "Canvas"):
            self.assertNotIn(word, source)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tool = load_dashboard()

    def test_held_slots_are_walked_back_from_now_and_a_waiter_giving_up_holds_nothing(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-history-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "queue.log").write_text(
            "2026-01-01T10:00:00Z granted 2 to build (pid 10)\n"
            "2026-01-01T10:10:00Z granted 1 to tests (pid 11)\n"
            "2026-01-01T10:20:00Z released 1 (0000000003-12.json)\n"
            "2026-01-01T10:30:00Z released 2 (0000000001-10.json)\n"
            "2026-01-01T10:40:00Z reaped held lease of 1 for tests (pid 11 exited)\n")
        events = self.tool.queue_events(directory)
        self.assertEqual(["granted", "granted", "left", "released", "reaped"], [e["verb"] for e in events])
        self.assertEqual("build", events[3]["label"], "a release is named by the grant to its pid")
        now = datetime.datetime(2026, 1, 1, 10, 50, tzinfo=datetime.timezone.utc)
        steps = self.tool.held_history(events, 0, now, window=3600)
        self.assertEqual([0, 2, 3, 1, 0], [level for _, _, level in steps])
        summary = self.tool.history_summary(steps, 4)
        self.assertEqual(3, summary["peak"])
        self.assertAlmostEqual((2 + 3 + 3 + 1) * 600 / 3600 / 4, summary["average"])
        self.assertEqual([0, 2, 3, 3, 1, 0], self.tool.history_buckets(steps, 6))


if __name__ == "__main__":
    unittest.main()
