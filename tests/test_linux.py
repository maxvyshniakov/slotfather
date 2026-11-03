#!/usr/bin/env python3
"""Memory-based capacity and the Linux memory guard (#653): /proc/meminfo and /proc/pressure/memory from fixtures
(tests/fixtures/proc, a 12 GiB VM like the CI runner VM110), the capacity modes and their math, `slots`, `available`
and `advise` under them, an older client reading a memory-mode capacity, the guard through real processes, the
lease found in /proc/PID/environ, machine samples, and on Linux itself the real ps and /proc."""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_slotfather import ROOT, TOOL, SlotCase
from test_usage import load

sf = load(TOOL, "slotfather_linux_core")
PROC = ROOT / "tests" / "fixtures" / "proc"
LINUX = sys.platform.startswith("linux")


def clean_environment(**values) -> dict:
    """os.environ without SLOTFATHER_ variables, plus `values`."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("SLOTFATHER_")}, **values}


class ProcParsingTests(unittest.TestCase):
    def test_meminfo_reads_every_counter_in_kilobytes(self):
        found = sf.meminfo((PROC / "calm" / "meminfo").read_text())
        self.assertEqual((12251784, 10871232, 0), (found["MemTotal"], found["MemAvailable"], found["SwapTotal"]))
        self.assertEqual(0, found["HugePages_Total"], "a count without a unit reads as it is")
        self.assertEqual({}, sf.meminfo(""))

    def test_proc_stat_gives_cpu_in_clock_ticks_past_a_command_name_with_spaces(self):
        stat = "4242 (bun (worker) 2) S 4200 4242 4200 0 -1 4194560 812 0 0 0 151 49 0 0 20 0 9 0 1234567 0 0\n"
        self.assertAlmostEqual(200 / os.sysconf("SC_CLK_TCK"), sf.proc_cpu_seconds(4242, stat))
        self.assertIsNone(sf.proc_cpu_seconds(4242, "4242 (short) S 1\n"))

    def test_psi_reads_avg10_of_some_and_full(self):
        self.assertEqual({"some": 23.5, "full": 4.1}, sf.psi((PROC / "stalled" / "pressure" / "memory").read_text()))
        self.assertEqual({"some": 1.2}, sf.psi("some avg10=1.20 avg60=0.40 avg300=0.12 total=8812345\n"),
                         "kernels before 5.x had no full line for some resources")
        self.assertIsNone(sf.psi(""))
        self.assertIsNone(sf.psi("some avg10=x avg60=0.40\n"))

    def test_linux_memory_gives_megabytes_and_falls_back_before_memavailable(self):
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_PROC=str(PROC / "calm")), clear=True):
            calm = sf.linux_memory()
        self.assertEqual((11964.6, 10616.4, 0.0, 0.0, 0.0), tuple(round(calm[k], 1) for k in (
            "total", "available", "swap_total", "swap_free", "psi_some")))
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_PROC=str(PROC / "old-kernel")), clear=True):
            old = sf.linux_memory()
        self.assertEqual(round((8912340 + 1840212 + 52340) / 1024, 1), round(old["available"], 1),
                         "MemFree + Cached + Buffers where the kernel has no MemAvailable")
        self.assertIsNone(old["psi_some"], "no /proc/pressure: no PSI")


class LinuxGuardTests(unittest.TestCase):
    def short(self, case: str, **values) -> str | None:
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_PROC=str(PROC / case), **values), clear=True):
            return sf.memory_short()

    def test_a_calm_machine_and_one_without_psi_hold_nothing_back(self):
        self.assertIsNone(self.short("calm"))
        self.assertIsNone(self.short("nopsi"))
        self.assertIsNone(self.short("old-kernel"))

    def test_little_available_memory_holds_new_leases(self):
        self.assertEqual("available memory 700 MB is under 1024 MB", self.short("tight"))
        self.assertIsNone(self.short("tight", SLOTFATHER_MIN_AVAILABLE_MB="512"))
        self.assertEqual("available memory 3072 MB is under 4096 MB",
                         self.short("stalled", SLOTFATHER_MIN_AVAILABLE_MB="4096"))

    def test_tasks_stalled_on_memory_hold_new_leases(self):
        self.assertEqual("memory pressure: PSI some avg10 23.5% (limit 10%)", self.short("stalled"))
        self.assertEqual("memory pressure: PSI full avg10 4.1% (limit 4%)",
                         self.short("stalled", SLOTFATHER_MAX_PSI_SOME="30", SLOTFATHER_MAX_PSI_FULL="4"))
        self.assertIsNone(self.short("stalled", SLOTFATHER_MAX_PSI_SOME="0"), "0 turns a limit off")
        self.assertIn("some avg10 40%", self.short("thrashing"))

    def test_swap_counts_only_where_it_is_on(self):
        self.assertEqual("free swap 100 MB is under 512 MB", self.short("zram"))
        self.assertIsNone(self.short("zram", SLOTFATHER_MIN_FREE_SWAP_MB="64"))
        self.assertIsNone(self.short("calm"), "no swap: nothing to run short of")

    def test_the_test_seam_still_wins_and_macos_reads_no_proc(self):
        seam = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-seam-"))
        self.addCleanup(shutil.rmtree, seam, ignore_errors=True)
        (seam / "memory").write_text("8000 1\n")
        self.assertIsNone(self.short("tight", SLOTFATHER_MEMORY_FILE=str(seam / "memory")))
        if not LINUX:
            with mock.patch.dict(os.environ, clean_environment(), clear=True):
                self.assertFalse(sf.linux())
                with mock.patch.object(sf, "sysctl", side_effect=lambda name: {
                        "vm.swapusage": "total = 2048.00M  used = 1900.00M  free = 148.00M  (encrypted)",
                        "kern.memorystatus_vm_pressure_level": "1"}.get(name)):
                    self.assertEqual("free swap 148 MB is under 512 MB", sf.memory_short())

    def test_machine_samples_carry_available_memory_and_psi(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-machine-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "queue").mkdir()
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_PROC=str(PROC / "stalled"),
                                                           SLOTFATHER_DIR=str(directory)), clear=True):
            found = sf.machine_facts(directory)
        self.assertEqual((None, 26, 3072, 23.5, 4.1, 0, 0), tuple(found[k] for k in (
            "pressure", "memory_free_pct", "memory_available_mb", "psi_some", "psi_full", "swap_total_mb",
            "swap_free_mb")))


CGROUP = ROOT / "tests" / "fixtures" / "cgroup"
SLICE = "/ci.slice/ci-runners.slice"


class CgroupTests(unittest.TestCase):
    """VM110's runners (#653): each service has MemoryMax=6G inside ci-runners.slice with MemoryMax=10G, which all nine
    share; a process in one of them sizes the queue by the slice and reads the slice's pressure."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-cgroup-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def environment(self, case: str = "calm", **values) -> dict:
        return clean_environment(**{"SLOTFATHER_PROC": str(PROC / "runner"), "SLOTFATHER_CGROUP_ROOT": str(CGROUP / case),
                                    "SLOTFATHER_DIR": str(self.dir), **values})

    def test_the_outermost_limited_cgroup_is_the_shared_one(self):
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            self.assertEqual("/ci.slice/ci-runners.slice/actions.runner.sid-kit-1.service", sf.own_cgroup())
            self.assertEqual(SLICE, sf.limited_cgroup())
            self.assertEqual(SLICE, sf.memory_cgroup())
            self.assertEqual(10.0, sf.memory_gib(), "the slice's 10 GiB, under the machine's 11.7")
        with mock.patch.dict(os.environ, self.environment(SLOTFATHER_CGROUP=SLICE + "/actions.runner.sid-kit-1.service"),
                             clear=True):
            self.assertEqual(6.0, sf.memory_gib(), "SLOTFATHER_CGROUP names the cgroup outright")
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_PROC=str(PROC / "calm"),
                                                           SLOTFATHER_CGROUP_ROOT=str(CGROUP / "calm")), clear=True):
            self.assertIsNone(sf.memory_cgroup(), "no /proc/self/cgroup: the machine")
            self.assertEqual(11.7, round(sf.memory_gib(), 1))

    def test_capacity_follows_the_slice_and_saves_it_for_every_client(self):
        with mock.patch.dict(os.environ, self.environment(SLOTFATHER_CORES="16"), clear=True):
            self.assertEqual(2, sf.capacity_basis()["slots"], "automatic: the lower of 5 by cores and 10 GiB / 4")
            saved = sf.slots_value(["memory", "5"])
        self.assertEqual({"slots": 2, "mode": "memory", "gib_per_slot": 5, "cgroup": SLICE}, saved)
        (self.dir / "capacity.json").write_text(json.dumps(saved))
        # A client outside the slice (an admin's shell, a Docker container) counts the same slice.
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_PROC=str(PROC / "calm"), SLOTFATHER_CORES="6",
                                                           SLOTFATHER_CGROUP_ROOT=str(CGROUP / "calm"),
                                                           SLOTFATHER_DIR=str(self.dir)), clear=True):
            found = sf.capacity_basis()
            self.assertEqual((2, 10.0, SLICE), (found["slots"], found["memory_gib"], found["cgroup"]))
            self.assertEqual(SLICE, sf.memory_cgroup())
            self.assertEqual(f"2 slots (saved: one per 5 GiB of 10 GiB in {SLICE})", sf.capacity_text(found))

    def test_the_guard_reads_the_slices_headroom_and_pressure(self):
        cases = {"calm": None, "cached": None,
                 "full": f"memory left under {SLICE}'s limit, 612 MB, is under 1024 MB",
                 "stalled": "memory pressure: PSI some avg10 31.2% (limit 10%)"}
        for case, reason in cases.items():
            with self.subTest(case=case), mock.patch.dict(os.environ, self.environment(case), clear=True):
                self.assertEqual(reason, sf.memory_short())
        with mock.patch.dict(os.environ, self.environment("cached"), clear=True):
            facts = sf.linux_memory()
        self.assertEqual((SLICE, 10240, 6349), (facts["cgroup"], round(facts["cgroup_max"]),
                                                round(facts["cgroup_available"])),
                         "the page cache the slice can drop (inactive_file) is not counted as used")

    def test_machine_memory_stays_a_second_guard_when_the_vm_is_ballooned(self):
        # This /proc has no self/cgroup: the slice is named, as the runners' drop-in may.
        with mock.patch.dict(os.environ, self.environment(SLOTFATHER_PROC=str(PROC / "tight"), SLOTFATHER_CGROUP=SLICE),
                             clear=True):
            self.assertEqual("available memory 700 MB is under 1024 MB", sf.memory_short())


class CapacityMathTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-capacity-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def basis(self, saved: dict | None = None, **values) -> dict:
        (self.dir / "capacity.json").unlink(missing_ok=True)
        if saved is not None:
            (self.dir / "capacity.json").write_text(json.dumps(saved))
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_DIR=str(self.dir), **values), clear=True):
            return sf.capacity_basis()

    def test_memory_counts_in_whole_gib_and_gives_at_least_one_slot(self):
        self.assertEqual([3, 2, 4, 1, 1, None], [sf.memory_slots(11.68, 4), sf.memory_slots(11.68, 5),
                                                 sf.memory_slots(11.68, 2.5), sf.memory_slots(1.6, 4),
                                                 sf.memory_slots(0.2, 4), sf.memory_slots(None, 4)])
        self.assertEqual(8, sf.memory_slots(32, 4))

    def test_the_automatic_capacity_is_the_lower_of_the_cpu_and_memory_budgets(self):
        cases = {("12", "32"): 4, ("6", "11.68"): 2, ("16", "11.68"): 3, ("12", "64"): 4, ("4", "2"): 1,
                 ("32", "16"): 4}
        for (machine, gib), slots in cases.items():
            with self.subTest(cores=machine, gib=gib):
                found = self.basis(SLOTFATHER_CORES=machine, SLOTFATHER_MEMORY_GIB=gib)
                self.assertEqual((slots, False, "automatic"), (found["slots"], found["saved"], found["mode"]))

    def test_the_memory_size_comes_from_proc_meminfo_on_linux(self):
        found = self.basis(SLOTFATHER_CORES="16", SLOTFATHER_PROC=str(PROC / "calm"))
        self.assertEqual((11.7, 3), (found["memory_gib"], found["slots"]))

    def test_saved_modes(self):
        vm = {"SLOTFATHER_CORES": "6", "SLOTFATHER_MEMORY_GIB": "11.68"}
        self.assertEqual((2, "memory"), tuple(self.basis({"slots": 2, "mode": "memory", "gib_per_slot": 5},
                                                         **vm)[k] for k in ("slots", "mode")))
        self.assertEqual(4, self.basis({"slots": 4, "mode": "memory", "gib_per_slot": 2.5}, **vm)["slots"],
                         "memory alone may give more slots than the cores' budget")
        self.assertEqual(2, self.basis({"slots": 2, "mode": "lower", "gib_per_slot": 2.5}, **vm)["slots"])
        self.assertEqual(3, self.basis({"slots": 2, "mode": "memory", "gib_per_slot": 5},
                                       SLOTFATHER_CORES="6", SLOTFATHER_MEMORY_GIB="16")["slots"],
                         "a memory mode follows the machine's memory; the saved count is for older clients")
        self.assertEqual((7, "fixed"), tuple(self.basis({"slots": 7}, **vm)[k] for k in ("slots", "mode")))

    def test_unknown_memory_falls_back_to_the_saved_count_or_the_cpu_budget(self):
        with mock.patch.object(sf, "memory_gib", return_value=None):
            self.assertEqual(3, self.basis({"slots": 3, "mode": "memory", "gib_per_slot": 4},
                                           SLOTFATHER_CORES="6")["slots"])
            self.assertEqual(2, self.basis(SLOTFATHER_CORES="6")["slots"])

    def test_an_invalid_saved_mode_is_an_error(self):
        for saved in ({"slots": 2, "mode": "memory"}, {"slots": 2, "mode": "memory", "gib_per_slot": 0},
                      {"slots": 2, "mode": "disk", "gib_per_slot": 4}, {"slots": 2, "mode": "lower", "gib_per_slot": True},
                      {"slots": 0, "mode": "memory", "gib_per_slot": 4}):
            with self.subTest(saved=saved), self.assertRaises(sf.SlotError):
                self.basis(saved, SLOTFATHER_CORES="6", SLOTFATHER_MEMORY_GIB="12")


class MemoryCapacityCommandTests(SlotCase):
    """`slotfather slots memory G | lower G` on a 6-core VM with 12 GiB, as VM110."""

    def setUp(self):
        super().setUp()
        self.env.update(SLOTFATHER_CORES="6", SLOTFATHER_MEMORY_GIB="11.68")

    def test_the_modes_are_saved_for_every_client_with_a_count_older_clients_read(self):
        self.assertEqual("2 slots (automatic)", self.tool("slots").stdout.strip())
        saved = self.tool("slots", "memory", "3")
        self.assertEqual(0, saved.returncode, saved.stderr)
        self.assertEqual("4 slots (saved: one per 3 GiB of 11.7 GiB)", saved.stdout.strip())
        self.assertEqual({"slots": 4, "mode": "memory", "gib_per_slot": 3},
                         json.loads((self.state / "capacity.json").read_text()))
        self.assertEqual("4", self.tool("available").stdout.strip())
        advice = json.loads(self.tool("advise", "--exclusive", SLOTFATHER_LOADAVG="0").stdout)
        self.assertEqual((4, 4, 1.5, "memory", 3, 11.7, None), tuple(advice[k] for k in (
            "jobs", "total", "cores_per_slot", "capacity_mode", "gib_per_slot", "memory_gib", "memory_short")))
        lower = self.tool("slots", "lower", "2.5")
        self.assertEqual("2 slots (saved: the lower of 2 by 6 cores and 4 by memory, one per 2.5 GiB of 11.7 GiB)",
                         lower.stdout.strip())
        self.assertEqual("2", self.tool("available").stdout.strip())
        self.assertEqual(0, self.tool("slots", "reset").returncode)
        self.assertEqual("3 slots (automatic, by memory: one per 4 GiB of 11.7 GiB; 5 by 16 cores)",
                         self.tool("slots", SLOTFATHER_CORES="16").stdout.strip())

    def test_invalid_memory_modes_never_change_the_saved_value(self):
        self.assertEqual(0, self.tool("slots", "memory", "5").returncode)
        for args in (("memory",), ("memory", "0"), ("memory", "-1"), ("memory", "x"), ("lower", "nan"),
                     ("lower", "inf"), ("memory", "5", "6"), ("cpu", "3")):
            with self.subTest(args=args):
                result = self.tool("slots", *args)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertEqual("2 slots (saved: one per 5 GiB of 11.7 GiB)", self.tool("slots").stdout.strip())

    def test_a_request_for_all_takes_the_memory_capacity(self):
        self.assertEqual(0, self.tool("slots", "memory", "3").returncode)
        result = self.tool("run", "-n", "all", "--", TOOL, "status")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("4 of 4 slots held", result.stdout)

    @unittest.skipUnless((ROOT / ".git").exists(), "needs the git history for the previous slotfather")
    def test_an_older_client_reads_the_saved_count(self):
        found = subprocess.run(["git", "-C", str(ROOT), "show", "0af7546:slotfather"], capture_output=True)
        if found.returncode:
            self.skipTest("0af7546 is not in this checkout")
        old = self.tmp / "slotfather-old"
        old.write_bytes(found.stdout)
        old.chmod(0o755)
        self.assertEqual(0, self.tool("slots", "memory", "5").returncode)
        listing = subprocess.run([str(old), "slots"], env=self.env, capture_output=True, text=True)
        self.assertEqual("2 slots (saved)", listing.stdout.strip(), listing.stderr)


class LinuxGuardProcessTests(SlotCase):
    """The guard through real processes, with /proc from a directory the test switches between fixtures."""

    def setUp(self):
        super().setUp()
        del self.env["SLOTFATHER_MEMORY_FILE"]
        self.proc = self.tmp / "proc"
        self.use("calm")
        self.env["SLOTFATHER_PROC"] = str(self.proc)

    def use(self, case: str):
        """Points the /proc the clients read at a fixture at once (a symlink renamed over the old one)."""
        pending = self.tmp / "proc.pending"
        pending.unlink(missing_ok=True)
        pending.symlink_to(PROC / case)
        os.replace(pending, self.proc)

    def test_little_available_memory_or_stalls_hold_new_leases_until_memory_recovers(self):
        holder = self.request("holder", 1)
        self.started("holder")
        self.use("tight")
        guarded = self.request("guarded", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) guarded" in self.status(), "the request to wait")
        self.assertIn("available memory 700 MB is under 1024 MB", self.output_line(guarded))
        self.use("stalled")
        self.wait_for(lambda: "PSI some avg10 23.5%" in self.output_line(guarded, timeout=5), "the stall reason")
        self.use("calm")
        self.started("guarded")
        for label, process in (("holder", holder), ("guarded", guarded)):
            self.release(label)
            self.finish(process)

    def test_with_nothing_running_one_lease_still_starts(self):
        self.use("tight")
        alone = self.request("alone", 1)
        self.started("alone")
        self.release("alone")
        self.finish(alone)


class LinuxEnvironmentTests(unittest.TestCase):
    def test_the_lease_is_found_in_proc_environ_as_a_whole_variable(self):
        proc = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-proc-"))
        self.addCleanup(shutil.rmtree, proc, ignore_errors=True)
        lease = "/state/queue/0000000007-4242.json"
        for pid, environ in ((101, f"HOME=/home/runner\0SLOTFATHER_LEASE={lease}\0PATH=/usr/bin\0"),
                             (102, f"SLOTFATHER_LEASE={lease}.old\0"), (103, f"X=SLOTFATHER_LEASE={lease}\0")):
            (proc / str(pid)).mkdir()
            (proc / str(pid) / "environ").write_bytes(environ.encode())
        with mock.patch.dict(os.environ, clean_environment(SLOTFATHER_PROC=str(proc)), clear=True):
            self.assertEqual({101}, sf.carrying([101, 102, 103, 104], lease))

    @unittest.skipUnless(LINUX, "Linux /proc")
    def test_a_real_process_carrying_the_lease_is_found(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 env={**os.environ, "SLOTFATHER_LEASE": "/probe/lease"})
        self.addCleanup(lambda: (child.kill(), child.wait()))
        with mock.patch.dict(os.environ, clean_environment(), clear=True):
            self.assertEqual({child.pid}, sf.carrying([child.pid, os.getpid()], "/probe/lease"))

    @unittest.skipUnless(LINUX, "Linux procps")
    def test_procps_gives_the_tables_the_queue_and_the_sampler_read(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        table = sf.processes()
        self.assertEqual(os.getpid(), table[child.pid][0])
        self.assertIsNotNone(sf.started_at(table[child.pid][1]), table[child.pid][1])
        usage = sf.usage_table()
        self.assertEqual((os.getpid(), os.getuid()), (usage[child.pid][0], usage[child.pid][2]))
        self.assertGreater(usage[child.pid][3], 0, "resident KB")
        self.assertIsNotNone(sf.proc_cpu_seconds(os.getpid()))
        self.assertIsNotNone(sf.cpu_ticks())
        self.assertIsNotNone(sf.memory_gib())


if __name__ == "__main__":
    unittest.main()


class ReviewCgroupTests(unittest.TestCase):
    """Review of 4cf0233: a cgroup's limit is the smallest on its path (P3), and a capacity with a cgroup follows that
    limit alone, not the machine's MemTotal a balloon shrinks (P1)."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-review-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def env(self, proc="runner", cgroup="calm", **values):
        return clean_environment(**{"SLOTFATHER_PROC": str(PROC / proc), "SLOTFATHER_CGROUP_ROOT": str(CGROUP / cgroup),
                                    "SLOTFATHER_DIR": str(self.dir), **values})

    def test_the_limit_of_a_cgroup_is_the_smallest_on_its_path(self):
        with mock.patch.dict(os.environ, self.env(cgroup="nested", SLOTFATHER_CGROUP=SLICE), clear=True):
            self.assertEqual(8 << 30, sf.cgroup_limit(SLICE))
            self.assertEqual(8.0, sf.memory_gib(), "ci.slice's 8 GiB bounds the slice's 10")
            self.assertEqual(8 * 1024, round(sf.linux_memory()["cgroup_max"]))

    def test_a_ballooned_vm_keeps_the_capacity_of_its_cgroup(self):
        (self.dir / "capacity.json").write_text(json.dumps({"slots": 2, "mode": "memory", "gib_per_slot": 5,
                                                            "cgroup": SLICE}))
        with mock.patch.dict(os.environ, self.env(proc="ballooned", SLOTFATHER_CORES="6"), clear=True):
            self.assertEqual(2, sf.capacity_basis()["slots"], "10 GiB of the slice, not 4 of MemTotal")
            os.environ["SLOTFATHER_MIN_AVAILABLE_MB"] = "4096"
            self.assertEqual("available memory 3072 MB is under 4096 MB", sf.memory_short(),
                             "MemTotal's MemAvailable stays in the admission guard")


class OwnerTests(unittest.TestCase):
    """Review of 4cf0233 (P2-b): a process of another user (root by the guest agent) must not write into the queue
    of the user it belongs to: changes are refused, reading commands create nothing."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-owner-")) / "state"
        self.addCleanup(shutil.rmtree, self.dir.parent, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, clean_environment(SLOTFATHER_DIR=str(self.dir),
                                                                SLOTFATHER_LEGACY_DIRS="",
                                                                SLOTFATHER_MEMORY_GIB="64"), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.foreign = mock.patch.object(sf.os, "geteuid", return_value=os.geteuid() + 4242)

    def files(self):
        return sorted(str(p.relative_to(self.dir.parent)) for p in self.dir.parent.rglob("*"))

    def test_changes_are_refused_for_another_users_queue(self):
        before = self.files()
        for argv in (["run", "--", "true"], ["slots", "3"], ["order", "window"], ["yield"]):
            with self.subTest(argv=argv), self.foreign, mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(2, sf.main(argv))
                self.assertIn("belongs to uid", err.getvalue())
        self.assertEqual(before, self.files())
        with self.foreign, self.assertRaises(sf.SlotError):
            sf.acquire(1, "library")

    def test_reading_commands_create_nothing(self):
        before = self.files()
        for argv in (["status"], ["status", "--json"], ["history"], ["slots"], ["order"], ["available"], ["advise"]):
            with self.subTest(argv=argv), self.foreign, mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(0, sf.main(argv))
        self.assertEqual(before, self.files())

    def test_the_owner_itself_is_unaffected(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, sf.main(["slots", "3"]))
        self.assertTrue((self.dir / "capacity.json").exists())

    @unittest.skipUnless(os.geteuid() == 0 and LINUX, "root on Linux (the container run)")
    def test_root_running_the_command_against_a_runner_queue(self):
        self.dir.mkdir(parents=True)
        os.chown(self.dir, 65534, 65534)
        env = {**os.environ}
        refused = subprocess.run([TOOL, "run", "--", "true"], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(2, refused.returncode)
        self.assertIn("sudo -u", refused.stderr)
        listed = subprocess.run([TOOL, "status"], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual([], list(self.dir.iterdir()))


class ShrinkingCapacityTests(SlotCase):
    """Review of 4cf0233 (P1): a memory capacity that shrinks under waiters (a VM's balloon takes memory) must not
    leave a request larger than the new capacity waiting for ever: it takes every slot when it starts, as a request
    above the total always has; `-n all` is the capacity at its grant."""

    def setUp(self):
        super().setUp()
        del self.env["SLOTFATHER_MEMORY_GIB"]
        self.proc = self.tmp / "proc"
        self.proc.mkdir()
        self.memory_total(16)
        self.env.update(SLOTFATHER_PROC=str(self.proc), SLOTFATHER_CORES="12")

    def memory_total(self, gib: int):
        pending = self.proc / "meminfo.pending"
        pending.write_text(f"MemTotal: {gib << 20} kB\nMemAvailable: {gib << 19} kB\nSwapTotal: 0 kB\n")
        os.replace(pending, self.proc / "meminfo")

    def shrink_under_waiters(self, order: str):
        self.env["SLOTFATHER_QUEUE"] = order
        saved = self.tool("slots", "memory", "4")
        self.assertEqual("4 slots (saved: one per 4 GiB of 16 GiB)", saved.stdout.strip(), saved.stderr)
        self.assertIn("no cgroup with a memory limit", saved.stderr, "a count that follows MemTotal says so")
        holder = self.request("holder", 1)
        self.started("holder")
        four = self.request("four", 4)
        self.wait_for(lambda: "waiting: 4 slot(s) four" in self.status(), "the 4-slot request to queue")
        whole = self.request("whole", "all")
        self.wait_for(lambda: "slot(s) whole" in self.status(), "the all-slot request to queue")
        self.memory_total(8)  # two slots now
        self.release("holder")
        self.started("four")
        self.assertIn("2 of 2 slots held", self.status())
        self.release("four")
        self.started("whole")
        self.assertIn("held: 2 slot(s) whole", self.status())
        self.memory_total(16)
        self.release("whole")
        for process in (holder, four, whole):
            self.finish(process)
        history = {json.loads(line)["label"]: json.loads(line) for line in
                   (self.state / "history.jsonl").read_text().splitlines()}
        self.assertEqual((2, 2), (history["four"]["count"], history["whole"]["count"]))

    def test_backfill(self):
        self.shrink_under_waiters("backfill")

    def test_window(self):
        self.shrink_under_waiters("window")

    def test_all_takes_the_capacity_at_its_grant_when_it_grew(self):
        self.assertEqual(0, self.tool("slots", "memory", "4").returncode)
        self.memory_total(8)
        holder = self.request("holder", 1)
        self.started("holder")
        whole = self.request("whole", "all")
        self.wait_for(lambda: "waiting: 2 slot(s) whole" in self.status(), "the all-slot request to queue")
        self.memory_total(16)
        self.release("holder")
        self.started("whole")
        self.assertIn("held: 4 slot(s) whole", self.status())
        self.release("whole")
        for process in (holder, whole):
            self.finish(process)
