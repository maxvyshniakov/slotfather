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
import re
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

    def test_table_headers_share_the_border_and_empty_messages_start_under_them(self):
        text = self.snapshot(width=220, height=50)
        lines = text.splitlines()
        for title, message in (("RUNNING", "Nothing running"), ("QUEUE", "Empty ·"),
                               ("EVENTS", "No grants")):
            row = next(i for i, line in enumerate(lines) if title in line)
            self.assertEqual(lines[row].index(title), lines[row + 2].index(message))
            self.assertIn("╭", lines[row])
            left, right = lines[row].index("╭"), lines[row].index("╮")
            self.assertFalse(lines[row + 1][left + 1:right].strip(), "top content padding")
            bottom = next(i for i in range(row + 1, len(lines)) if lines[i][left:left + 1] == "╰")
            self.assertFalse(lines[bottom - 1][left + 1:right].strip(), "bottom content padding")
            if title != "EVENTS":
                self.assertIn("pid", lines[row], "column names belong on the top border")

    def test_small_terminal_fallback_preserves_its_footer(self):
        for height in (5, 7):
            with self.subTest(height=height):
                text = self.snapshot(width=40, height=height)
                self.assertIn("slotfather", text.splitlines()[-1])
                self.assertIn("live", text.splitlines()[-1])

    def test_an_idle_queue_reads_open_with_every_slot_free(self):
        text = self.snapshot()
        self.assertIn("SLOTS", text.splitlines()[0])
        self.assertIn("free", text)
        self.assertIn("slotfather", text.splitlines()[-1])
        self.assertNotIn("slotfather", "\n".join(text.splitlines()[:-1]))
        self.assertNotIn(str(self.state), text)
        self.assertNotIn("OPEN", text)
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
        self.assertNotIn("QUEUED", text)
        self.assertNotIn("next is", text)
        self.assertNotIn("next:", text)
        self.assertIn("free", text.split("QUEUE", 1)[1].splitlines()[0])
        self.assertNotIn("2/4 held", text)
        running, queue = text.index("RUNNING"), text.index("QUEUE ")
        self.assertLess(running, text.index("│▶ holder"), "the holder is listed under RUNNING")
        self.assertEqual(1, text.count("▶"), "only the selected row has a cursor")
        self.assertRegex(text[queue:], r"1  gate +4 .* next")
        self.assertRegex(text[queue:], r"2  small +1 .* 4 ")
        for label in ("holder", "gate", "small"):
            self.release(label)
        for process in (holder, gate, small):
            self.finish(process)

    def test_within_the_overtaking_window_smaller_requests_pass_a_blocked_head(self):
        self.env["SLOTFATHER_OVERTAKE_WINDOW"] = "600"
        holder = self.request("holder", 2)
        self.started("holder")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        text = self.snapshot()
        self.assertNotIn("PASSING", text)
        self.assertIn("pass until", text)
        for label in ("holder", "gate"):
            self.release(label)
        for process in (holder, gate):
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

    def test_clicking_a_queue_row_updates_selected_in_the_live_terminal(self):
        holder = self.request("click-owner", "all")
        self.started("click-owner")
        waiter = self.request("click-target", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) click-target" in self.status(), "the click target to queue")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        process = subprocess.Popen([TOOL, "dashboard", "--interval", "60"], stdin=slave, stdout=slave,
                                   stderr=slave, env=self.env, start_new_session=True)
        self.processes.append(process)
        os.close(slave)
        screen = bytearray()
        def read_until(predicate):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        screen.extend(os.read(master, 65536))
                    except OSError:
                        break
                plain = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", screen.decode("utf-8", "replace"))
                if predicate(plain):
                    return
            self.fail("live terminal did not render the expected selection")
        read_until(lambda text: "lane      click-owner" in text and "status    running" in text)
        # SLOTS is six rows, RUNNING is five; queue data begins at zero-based row 13.
        os.write(master, b"\x1b[<0;10;14M\x1b[<0;10;14m")
        read_until(lambda text: "lane      click-target" in text and "status    waiting" in text)
        os.write(master, b"m")
        read_until(lambda text: "text selection" in text)
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", screen)
        os.write(master, b"m")
        read_until(lambda text: screen.count(b"\x1b[?1000h\x1b[?1006h") >= 2)

        os.write(master, b"q")
        read_until(lambda text: process.poll() is not None)
        self.assertEqual(0, process.wait(timeout=3))
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", screen)
        for label in ("click-owner", "click-target"):
            self.release(label)
        for request in (holder, waiter):
            self.finish(request)

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
            # Ghostty's terminfo has REP, which the ncurses Python links sent as a lone last byte of █ or ─.
            os.execve(TOOL, [TOOL], {**self.env, "TERM": "xterm-ghostty"})
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
                    screen += os.read(terminal, 65536)
            done, code = os.waitpid(pid, os.WNOHANG)
            status = code if done else None
        os.close(terminal)
        if status is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail("the dashboard did not quit on q")
        self.assertIn(b"RUNNING", screen)
        self.assertIn(b"\x1b[?1000h\x1b[?1006h", screen)
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", screen)
        self.assertEqual(0, os.waitstatus_to_exitcode(status))
        text = screen.decode("utf-8")  # strict: a stray byte of a multibyte character fails here
        self.assertIn("▉", text)
        self.assertNotRegex(text, r"\x1b\[\d+b", "no REP: a terminal repeats bytes, not characters")
        self.assertTrue(text.rstrip().endswith("\x1b[?1049l"), "quitting leaves the alternate screen")

    def test_the_queue_code_holds_no_terminal_code(self):
        source = pathlib.Path(TOOL).read_text()
        for word in ("curses", "lsof", "Canvas"):
            self.assertNotIn(word, source)


class MouseInputTests(unittest.TestCase):
    def test_click_and_wheel_follow_the_visible_rows_of_an_overflowing_table(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        holders = [dict(label=f"job-{i}", kind="held", count=1, pid=100+i, style="h0",
                        cpu=0, since=now, granted=now) for i in range(8)]
        model = dict(holders=holders, slots=8, cores=8)
        scroll = {}
        def render():
            canvas = tool.Canvas(100, 7)
            canvas.scroll = scroll
            tool.draw_running(canvas, 0, 0, 7, 100, model, 0, now)
            return canvas
        canvas = render()
        self.assertEqual(1, tool.mouse_action(canvas, ("mouse", 0, 20, 3, True), scroll, 0))
        self.assertEqual(0, tool.mouse_action(canvas, ("mouse", 65, 20, 3, True), scroll, 0),
                         "wheel scrolls the viewport without changing selection")
        canvas = render()
        lines = canvas.render(False).splitlines()
        self.assertIn("job-3", lines[2])
        self.assertIn("job-5", lines[4], "counters must not take a data row")
        self.assertIn("↑ 3 above", lines[1])
        self.assertIn("↓ 2 more", lines[5])
        self.assertNotIn("more", lines[-1], "overflow belongs inside the table")
        self.assertEqual(3, tool.mouse_action(canvas, ("mouse", 0, 20, 2, True), scroll, 0))
        self.assertEqual(0, tool.mouse_action(canvas, ("mouse", 0, 20, 1, True), scroll, 0),
                         "clicking padding must not select a hidden row")
        self.assertEqual(0, tool.mouse_action(canvas, ("mouse", 0, 20, 2, False), scroll, 0))

    def test_a_delayed_mouse_release_cannot_become_the_copy_shortcut(self):
        tool = load_dashboard()
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[<0;14;10")
            self.assertEqual([], tool.read_keys(read))
            os.write(write, b"m")
            self.assertEqual([("mouse", 0, 13, 9, False)], tool.read_keys(read))
        finally:
            os.close(read)
            os.close(write)

    def test_sgr_mouse_reports_do_not_leak_into_keyboard_shortcuts(self):
        tool = load_dashboard()
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[<0;14;10M\x1b[<0;14;10m\x1b[<65;22;18Mq")
            self.assertEqual([("mouse", 0, 13, 9, True), ("mouse", 0, 13, 9, False),
                              ("mouse", 65, 21, 17, True), "q"], tool.read_keys(read))
        finally:
            os.close(read)
            os.close(write)


class DetailLayoutTests(unittest.TestCase):
    def test_process_table_keeps_space_even_with_a_long_command(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        item = dict(label="build", kind="held", count=2, pid=12345, style="h0", cpu=150,
                    rss=102400, procs=1, granted=now, since=now, queue="",
                    cmd="verify.sh " + "a-very-long-check-name," * 80,
                    meta="lane=build,ticket=42,phase=check," * 12,
                    tree=[(12345, 150, 102400, "worker")])
        canvas = tool.Canvas(70, 20)
        tool.draw_detail(canvas, 0, 0, 20, 70, {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        lines = canvas.render(False).splitlines()
        self.assertIn("LANE DETAILS", lines[0])
        lane = next(i for i,line in enumerate(lines) if "lane      build" in line)
        self.assertIn("status    running", lines[lane + 1])
        command = next(i for i,line in enumerate(lines) if "command" in line)
        meta = next(i for i,line in enumerate(lines) if "meta" in line)
        cwd = next(i for i,line in enumerate(lines) if "cwd" in line)
        self.assertLessEqual(meta-command, 3)
        self.assertLessEqual(cwd-meta, 3)
        self.assertIn("\x1b[0m 1.5 of 4 cores", canvas.lines(True)[lane + 2])
        header = next(line for line in lines if "process" in line and "cpu" in line)
        self.assertEqual(3, header.index("process"))
        self.assertNotIn("processes", header)
        self.assertLess(header.index("process"), header.index("cpu"))
        self.assertLess(header.index("memory"), header.index("pid"))
        row = next(line for line in lines if "worker" in line)
        self.assertEqual(3, row.index("worker"))
        self.assertTrue(row.rstrip(" │").endswith("12345"))
        item["tree"] = [(1000+i, 0, 1024, f"worker{i}") for i in range(10)]
        canvas = tool.Canvas(70, 20)
        canvas.scroll = {"processes": 3}
        tool.draw_detail(canvas, 0, 0, 20, 70, {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        text = canvas.render(False)
        self.assertIn("worker3", text)
        self.assertNotIn("worker0", text)
        self.assertIn("more", text)
        self.assertNotIn("more", text.splitlines()[-1])



class QueuePolicyTests(unittest.TestCase):
    def test_backfill_policy_is_structured_as_deadlines_not_a_sentence(self):
        tool = load_dashboard()
        since = datetime.datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        model = dict(holders=[], waiters=[dict(count=3, since=since, passable=False, label="head", pid=100)], free=1,
                     order="backfill", start=since+datetime.timedelta(minutes=30), limit=1200)
        self.assertEqual({"start est": "10:30:00", "backfill by": "10:30:00", "pass until": "10:20:00"},
                         tool.queue_policy(model))
        canvas = tool.Canvas(140, 7)
        tool.draw_queue(canvas, 0, 0, 7, 140, model, 0, since)
        lines = canvas.render(False).splitlines()
        for title, value in (("start est", "10:30:00"), ("backfill by", "10:30:00"), ("pass until", "10:20:00")):
            column = lines[0].index(title)
            self.assertEqual(value, lines[2][column:column+8])
        model["waiters"] = []
        self.assertEqual({}, tool.queue_policy(model))


class SlotBarTests(unittest.TestCase):
    def test_free_label_is_right_aligned_and_leaves_room_for_the_last_owner(self):
        tool = load_dashboard()
        canvas = tool.Canvas(120, 3)
        tool.draw_slots(canvas, 0, 2, 116, dict(slots=6, used=1, waiters=[],
                        holders=[dict(count=1, kind="held", style="h0", label="lane-624-flows-are-graphs")]))
        lines = canvas.render(False).splitlines()
        self.assertFalse(lines[1].strip(), "gap between slots and labels")
        self.assertIn("lane-624-flows-are-graphs", lines[2])
        self.assertEqual(lines[0].rindex("░") + 1, lines[2].index("free") + 4)

    def test_each_slot_is_solid_with_only_a_gap_between_slots(self):
        tool = load_dashboard()
        canvas = tool.Canvas(120, 3)
        tool.draw_slots(canvas, 0, 2, 116, dict(slots=3, used=2, waiters=[],
                        holders=[dict(count=2, kind="held", style="h0", label="build")]))
        line = canvas.render(False).splitlines()[0]
        self.assertEqual("█" * 38 + "  " + "█" * 37 + "  " + "░" * 37, line[2:118])
        self.assertNotIn("▉", line)


class MemoryBarTests(unittest.TestCase):
    def test_machine_has_usage_bars_and_a_chart_spanning_the_inner_width(self):
        tool = load_dashboard()
        now = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)
        canvas = tool.Canvas(90, 24)
        tool.draw_machine(canvas, 0, 0, 24, 90, dict(slots=4, cores=12, averages=(1,2,3),
                          advice=dict(unleased_load=0, jobs=2), guard=None,
                          memory=dict(level=1, used=768, total=1024, swap_total=512, swap_free=128),
                          history=[(now-datetime.timedelta(hours=1), now, 4)]))
        lines = canvas.render(False).splitlines()
        text = "\n".join(lines)
        self.assertIn("used 768 MB of 1.0 GB", text)
        self.assertIn("used 384 MB of 512 MB", text)
        self.assertNotIn("load avg", text)
        self.assertNotIn("pressure", text)
        first_chart = next(i for i, line in enumerate(lines) if "█" in line)
        self.assertFalse(lines[first_chart - 1][1:-1].strip(), "gap above chart")
        self.assertFalse(lines[-4][1:-1].strip(), "one gap row before legend")
        chart = lines[first_chart]
        self.assertEqual("█" * 84, chart[3:87])
        self.assertEqual("  ", chart[1:3])
        self.assertEqual("  ", chart[87:89])
        self.assertIn("held/4", lines[-3])
        self.assertIn("s/col", lines[-3])
        self.assertIn("avg 100%", lines[-3])
        self.assertIn("peak 4/4", lines[-3])
        self.assertIn("full 100%", lines[-3])
        self.assertFalse(lines[-2][1:-1].strip(), "one padding row after legend")
        self.assertIn("╰", lines[-1])


    def test_memory_bar_colour_reflects_pressure_without_a_separate_pressure_row(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        for level, colour in ((1,75), (2,179), (4,167)):
            with self.subTest(level=level):
                canvas = tool.Canvas(90,24)
                tool.draw_machine(canvas,0,0,24,90,dict(slots=4,cores=12,averages=(1,2,3),
                                  advice=dict(unleased_load=0,jobs=2),guard=None,
                                  memory=dict(level=level,used=768,total=1024,swap_total=512,swap_free=128),
                                  history=[(now-datetime.timedelta(hours=1),now,4)]))
                row = next(i for i,line in enumerate(canvas.lines(False)) if "memory" in line)
                self.assertIn(f"\x1b[0;38;5;{colour}m", canvas.lines(True)[row])
                self.assertNotIn("pressure", canvas.render(False))

    def test_full_capacity_is_orange_even_in_a_one_row_histogram(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        canvas=tool.Canvas(90,10)
        tool.draw_machine(canvas,0,0,10,90,dict(slots=4,cores=12,averages=(1,2,3),
                          advice=dict(unleased_load=0,jobs=2),guard=None,
                          memory=dict(level=1,used=768,total=1024,swap_total=512,swap_free=128),
                          history=[(now-datetime.timedelta(hours=1),now,4)]))
        rows=[i for i,line in enumerate(canvas.lines(False)) if "█" in line]
        self.assertEqual(1,len(rows))
        self.assertIn("38;2;255;175;95m",canvas.lines(True)[rows[0]])

    def test_macos_memory_counts_active_wired_and_compressor_physical_pages(self):
        tool = load_dashboard()
        sample = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages active: 100.
Pages inactive: 1000.
Pages wired down: 50.
Pages occupied by compressor: 25.
"""
        self.assertEqual(2.734375, tool.macos_used_memory(sample))
        self.assertIsNone(tool.macos_used_memory("no vm statistics"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tool = load_dashboard()

    def test_oldest_clock_bucket_retains_its_peak_until_the_next_scroll(self):
        seconds = int(datetime.datetime(2026,1,1,12,tzinfo=datetime.timezone.utc).timestamp())
        edge = datetime.datetime.fromtimestamp(seconds // 43 * 43, datetime.timezone.utc)
        oldest = edge-datetime.timedelta(seconds=3612)
        events = [dict(when=oldest+datetime.timedelta(seconds=5),delta=4),
                  dict(when=oldest+datetime.timedelta(seconds=6),delta=-4)]
        earlier = self.tool.held_history(events,0,edge-datetime.timedelta(seconds=10))
        later = self.tool.held_history(events,0,edge-datetime.timedelta(seconds=5))
        self.assertEqual(self.tool.history_buckets(earlier,84),self.tool.history_buckets(later,84))
        self.assertEqual(4,self.tool.history_buckets(later,84)[0])
        self.assertEqual(0,self.tool.history_summary(later,4)["peak"],
                         "the trailing-hour summary must exclude the older retained chart peak")

    def test_a_historical_peak_keeps_its_bucket_between_clock_boundaries(self):
        base = datetime.datetime(2026, 1, 1, 10, tzinfo=datetime.timezone.utc)
        rise = base + datetime.timedelta(seconds=601.2)
        fall = base + datetime.timedelta(seconds=601.4)
        def buckets(seconds):
            now = base + datetime.timedelta(seconds=seconds)
            steps = [(now - datetime.timedelta(hours=1), rise, 0),
                     (rise, fall, 4), (fall, now, 0)]
            return self.tool.history_buckets(steps, 6)
        self.assertEqual(buckets(3601), buckets(3602),
                         "refreshing within one time bucket must not move or widen a past peak")
        self.assertEqual([4, 0, 0, 0, 0, 0], buckets(3601))

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
