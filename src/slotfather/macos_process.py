"""Read-only libproc identities and targeted ps arguments on macOS.

ABI: the macOS SDK's sys/proc_info.h proc_bsdinfo / PROC_PIDTBSDINFO,
libproc.h proc_pidinfo. Full arguments come only from ps without -E.
Libraries load only when the native adapter is called, so importing this
module also works on Linux.
"""
from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
import sys
import time
from typing import NamedTuple


class NativeProcess(NamedTuple):
    pid: int
    ppid: int
    uid: int
    tty: str
    start: str
    cmd: str


class BsdInfo(ctypes.Structure):
    pbi_pid: int
    pbi_ppid: int
    pbi_uid: int
    pbi_status: int
    pbi_comm: bytes
    pbi_name: bytes
    e_tdev: int
    pbi_start_tvsec: int
    pbi_start_tvusec: int
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class ShortInfo(ctypes.Structure):
    pbsi_pid: int
    pbsi_ppid: int
    pbsi_status: int
    _fields_ = [
        ("pbsi_pid", ctypes.c_uint32), ("pbsi_ppid", ctypes.c_uint32),
        ("pbsi_pgid", ctypes.c_uint32), ("pbsi_status", ctypes.c_uint32),
        ("pbsi_comm", ctypes.c_char * 16), ("pbsi_flags", ctypes.c_uint32),
        ("pbsi_uid", ctypes.c_uint32), ("pbsi_gid", ctypes.c_uint32),
        ("pbsi_ruid", ctypes.c_uint32), ("pbsi_rgid", ctypes.c_uint32),
        ("pbsi_svuid", ctypes.c_uint32), ("pbsi_svgid", ctypes.c_uint32),
        ("pbsi_rfu", ctypes.c_uint32),
    ]


def short_info(libproc: ctypes.CDLL, pid: int) -> ShortInfo:
    """The SDK's proc_bsdshortinfo supplies parent links without privileged timestamps."""
    info = ShortInfo()
    if ctypes.sizeof(info) != 64:
        raise OSError(errno.ENOTSUP, "unsupported short BSD process ABI")
    ctypes.set_errno(0)
    size = int(libproc.proc_pidinfo(pid, 13, 0, ctypes.byref(info), ctypes.sizeof(info)))
    if size != ctypes.sizeof(info) or info.pbsi_pid != pid:
        raise OSError(ctypes.get_errno() or errno.EIO, f"short proc_pidinfo({pid}) failed")
    return info


def libraries() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    if sys.platform != "darwin" or ctypes.sizeof(BsdInfo) != 136:
        raise OSError(errno.ENOTSUP, "unsupported macOS process ABI")
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libc = ctypes.CDLL(None, use_errno=True)
    libproc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    libc.devname.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc.devname.restype = ctypes.c_char_p
    return libproc, libc


def pid_info(libproc: ctypes.CDLL, pid: int) -> BsdInfo:
    info = BsdInfo()
    ctypes.set_errno(0)
    size = int(libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)))
    if size != ctypes.sizeof(info):
        raise OSError(ctypes.get_errno() or errno.EIO, f"proc_pidinfo({pid}) failed")
    if info.pbi_pid != pid:
        raise OSError(errno.EIO, "libproc returned an inconsistent process identity")
    return info


def start_text(seconds: int, padded_day: bool = False) -> str:
    value = time.localtime(seconds)
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    day = f"{value.tm_mday:2d}" if padded_day else str(value.tm_mday)
    return (f"{weekdays[value.tm_wday]} {months[value.tm_mon - 1]} {day} "
            f"{value.tm_hour:02}:{value.tm_min:02}:{value.tm_sec:02} {value.tm_year}")


def argument_processes(pids: set[int]) -> dict[int, str]:
    """One targeted ps snapshot supplies full argv without reading any environment."""
    if not pids:
        return {}
    argv = ["/bin/ps", "-ww", "-p", ",".join(map(str, sorted(pids))),
            "-o", "pid=,command="]
    try:
        answer = subprocess.run(argv, capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as error:
        raise OSError(errno.ETIMEDOUT, "ps did not read protected process identities in time") from error
    if answer.returncode not in (0, 1) or answer.returncode and answer.stderr.strip():
        raise OSError(errno.EACCES, "ps could not read protected process identities")
    result: dict[int, str] = {}
    for line in answer.stdout.splitlines():
        number, _, command = line.strip().partition(" ")
        if number.isdigit() and command.strip():
            result[int(number)] = command.strip()
    return result


def snapshot(pid: int, required: set[int] | None = None) -> dict[int, NativeProcess]:
    """Native identities needed for one PID's membership; one ps supplies ancestry argv."""
    libproc, libc = libraries()
    result: dict[int, NativeProcess] = {}
    identities: dict[int, tuple[int, int]] = {}

    def observe(number: int) -> NativeProcess | None:
        try:
            info = pid_info(libproc, number)
        except OSError as error:
            if error.errno in (errno.ESRCH, errno.ENOENT):
                return None
            raise
        if info.pbi_status == 5:  # SZOMB
            return None
        tty = ""
        if info.e_tdev not in (0, 0xFFFFFFFF):
            name = libc.devname(ctypes.c_int(info.e_tdev).value, stat.S_IFCHR)
            tty = os.fsdecode(name) if name else "?"
        process = NativeProcess(number, info.pbi_ppid, info.pbi_uid, tty,
                                start_text(info.pbi_start_tvsec), os.fsdecode(info.pbi_name or info.pbi_comm))
        result[number] = process
        identities[number] = (info.pbi_start_tvsec, info.pbi_start_tvusec)
        return process

    for number in {os.getpid(), pid, *(required or set())}:
        observe(number)
    caller = result.get(os.getpid())
    if caller is None or (caller.ppid, caller.uid) != (os.getppid(), os.geteuid()):
        raise OSError(errno.EIO, "native process ABI did not identify its caller")
    chain: list[int] = []
    here = pid
    while here > 1 and here not in chain:
        process = result.get(here) or observe(here)
        if process is None:
            if not chain:
                break  # The requested PID is no longer live.
            raise OSError(errno.ESRCH, "an ancestor exited during the native observation")
        chain.append(here)
        here = process.ppid
    arguments = argument_processes(set(chain))
    for number in chain:
        command = arguments.get(number)
        prior = result[number]
        checked = pid_info(libproc, number)
        if command is None or checked.pbi_status == 5 or (checked.pbi_ppid, checked.pbi_uid) != (prior.ppid, prior.uid) or \
                identities[number] != (checked.pbi_start_tvsec, checked.pbi_start_tvusec):
            raise OSError(errno.EAGAIN, "process identity changed while reading arguments")
        result[number] = prior._replace(cmd=command)
    return result


def process_ids(libproc: ctypes.CDLL) -> list[int]:
    needed = int(libproc.proc_listpids(1, 0, None, 0))
    if needed <= 0:
        raise OSError(ctypes.get_errno() or errno.EIO, "proc_listpids size failed")
    capacity = needed // ctypes.sizeof(ctypes.c_int) + 128
    for _ in range(3):
        buffer = (ctypes.c_int * capacity)()
        length = int(libproc.proc_listpids(1, 0, buffer, ctypes.sizeof(buffer)))
        if length <= 0:
            raise OSError(ctypes.get_errno() or errno.EIO, "proc_listpids failed")
        if length < ctypes.sizeof(buffer):
            return [int(pid) for pid in buffer[:length // ctypes.sizeof(ctypes.c_int)] if pid > 0]
        capacity *= 2
    raise OSError(errno.EAGAIN, "process list kept growing during observation")


def basic_snapshot(required: set[int]) -> dict[int, tuple[int, str]]:
    """Parent links and legacy raw lstart, with complete identities for required PIDs.

    Callers must require all current/legacy holders and every queried PID. Other
    rows may have start="" (explicitly unknown) and are usable only as parent
    links. No holder may be reaped or matched through such an unknown identity.
    """
    libproc, _ = libraries()
    result: dict[int, tuple[int, str]] = {}
    for number in process_ids(libproc):
        try:
            info = pid_info(libproc, number)
        except OSError as error:
            if error.errno in (errno.ESRCH, errno.ENOENT):
                continue
            if error.errno not in (errno.EPERM, errno.EACCES) or number in required:
                raise
            try:
                brief = short_info(libproc, number)
            except OSError as short_error:
                if short_error.errno in (errno.ESRCH, errno.ENOENT):
                    continue
                raise
            if brief.pbsi_status != 5:
                result[number] = (brief.pbsi_ppid, "")
            continue
        if info.pbi_status != 5:
            result[number] = (info.pbi_ppid, start_text(info.pbi_start_tvsec, padded_day=True))
    if os.getpid() not in result or result[os.getpid()][0] != os.getppid():
        raise OSError(errno.EIO, "native identity table did not identify its caller")
    return result
