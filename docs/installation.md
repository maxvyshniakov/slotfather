# Install

```sh
gh repo clone maxvyshniakov/slotfather
cd slotfather
python3 install.py
```

The four commands are thin entry points into a complete, immutable package release. A normal
user install keeps releases under `~/.local/lib/slotfather/releases/`; an external system
`--bin /usr/local/bin` uses `/usr/local/lib/slotfather/releases/`, keeping modules loaded by
root-owned commands outside the service user's writable home. Each entry point pins its
release, so moving or deleting the source checkout does not break the install. Older releases
remain available to processes that started before an update.

The installed commands run on the interpreter that ran the installer: their `#!/usr/bin/env python3` line becomes that interpreter's path, as invoked, so an agent inside a project's virtual environment does not run slotfather on the project's Python. The installer refuses an interpreter inside a virtual environment, and one older than 3.9; name another with `python3 install.py --python /usr/bin/python3`. Re-run it to switch interpreters.

## Request-format cutover

Update callers before replacing the installed runtime: priority is a top-level
request field and ticket is optional; reserved metadata aliases are no longer a
runtime input. Stop submitting old-format work and let its existing holders and
waiters finish with their original client. Do not stop another agent's work to
force an upgrade.

The installer holds the installation lock and the current queue lock while it
checks process identities and publishes commands. It refuses a live entry with
no top-level priority, an invalid priority, or `class`, `priority` or `ticket` in
metadata. The refusal names the entry and asks for the old callers to drain;
registered queue data, command files and retired-path guards remain untouched.
Failed process inspection or an unavailable start identity also refuses the
cutover. A PID whose start time differs is a stale record, not that old owner.
Current-format active leases remain installable and unchanged.

This check covers registered requests. Updating callers and stopping old binary
launches first prevents an old process from arriving after the guarded snapshot.
A reinstall cannot replace code already loaded by a running Python process.

After the old clients drain, installation converts `history.jsonl` and
`history.jsonl.1` once: metadata priority/class becomes top-level priority
(`normal` when absent), metadata ticket becomes an optional top-level ticket,
and the reserved metadata keys are removed. Existing top-level values win.
Measurements and custom tags are preserved. Both files are checked before any
conversion or package preparation. Originals are saved under
`~/.local/state/slotfather-migrations/<id>/history/` before atomic file
replacements; the installer prints that backup path. A repeated install does
not rewrite already canonical history or create another history backup.

## Retired queue names

The installer also performs the one-time rename migration. It refuses live holders or waiters in either retired queue, preserves the entire earlier machine queue (including saved capacity) when the new queue is absent, and otherwise leaves the existing new queue untouched. Retired contents remain in named `~/.local/state/slotfather-migrations/` backups. Different old and new configurations are both preserved; the existing new configuration stays effective.

Both retired paths, `~/.local/state/machine-slots` and `~/.local/state/straylight-verification/slots`, become regular file guards. Atomic directory/file exchanges ensure old clients cannot recreate an uncoordinated queue between the move and guard creation. Existing intentional guards make a repeated install safe, including while the new queue has active leases. Unrelated files at those paths are refused and preserved. The installer uses the default state locations; coordinate custom queue paths separately.

The package is prepared and checked before the entry points are replaced atomically; the old
command name is removed last. If a migration stops after a directory exchange, retain its guards and printed backup paths: the queue contents remain preserved for recovery. This is an explicit installation step, not compatibility logic in normal command execution.

Put `~/.local/bin` on your `PATH`. All participating projects must use the same state directory; saved capacity is shared through that directory. Updating the executable preserves the queue and current-format running leases; old-format live requests must drain first.

## Version and remote updates

```sh
slotfather version
slotfather version --json
python3 install.py --host build-host
python3 install.py --host root@build-host --user runner --bin /usr/local/bin
```

`--host` uses the named SSH alias or `user@hostname`, streams the exact local runtime source,
and runs the same installer remotely. No remote Git credentials or checkout are needed. Other
options apply on the destination, including `--python`; installing for another user still
requires a root SSH destination. The transport verifies the source digest and removes only its
own remote temporary directory.

`version` reports the source commit, whether the source had uncommitted runtime changes, and a
SHA-256 digest of the runtime files. Compare all three fields across machines; a dirty source
is identified explicitly and cannot be mistaken for the committed revision. Installed metadata
is saved with the release, so later edits to a checkout do not change an installed version.
The upgrade test runs the real pre-package client from a frozen, checksum-verified fixture.
Installation refuses without changing its lease or published commands, the original client
finishes and releases successfully, and installation then succeeds after the queue drains.

### For a service user: one queue for several CI runners

`python3 install.py --user NAME [--bin DIR] [--cgroup PATH]` installs for NAME (another user needs root): the queue is NAME's `~/.local/state/slotfather`, written into the installed package configuration as its default state directory, so every process that runs these commands shares that one queue whatever its `HOME` (a job that moves `HOME` still queues with the rest; `SLOTFATHER_DIR` still overrides). `--bin DIR` puts the commands in DIR instead of `~/.local/bin`, such as `/usr/local/bin`, which a systemd service's and the GitHub runner's `PATH` already hold. Under root, the paths the installer creates in NAME's home (the queue, the retired-path guards, the install lock) are given to NAME; commands outside that home stay root's, so jobs cannot rewrite them (a failure part-way still hands over what was created). `--cgroup PATH` writes in the cgroup v2 path the leases share (see Linux), so every client of the installed command, also a shell outside that cgroup, sizes and guards by it. Another user's queue is read only: run by any other user (root through the guest agent, say), `run`, `yield` and saving `slots` or `order` are refused with the user to run them as (`sudo -u NAME slotfather ...`), and `status`, `history`, `stats`, `available`, `advise`, the dashboard and `slots`/`order` without changes create and remove nothing (a dead holder's entry is left out, not reaped).

The CI runner VM110 (Ubuntu, 6 vCPU, 12 GiB, no swap, nine `actions.runner.*` services as `runner`, #653), from a checkout of this repository on the VM:

```sh
sudo python3 install.py --user runner --bin /usr/local/bin --cgroup /ci.slice/ci-runners.slice
sudo -u runner slotfather slots memory 5   # 2 slots (saved: one per 5 GiB of 10 GiB in /ci.slice/ci-runners.slice)
sudo -u runner sh -c 'cd /tmp && slotfather run -n 1 -- slotfather status'
slotfather status                          # as root: read only
```

`slots` refuses changes while any lease is held or waits, so save it while the runners are idle. If the runners' shared drop-in sets `SLOTFATHER_DIR`, point it at this same `/home/runner/.local/state/slotfather` (the pinned default; another directory would need its own `slots` save); `SLOTFATHER_CGROUP=/ci.slice/ci-runners.slice` there is optional, since the installed command names the slice. Sid-Kit's and Data Grid's `scripts/heavy.sh` find `slotfather` on `PATH` and queue their heavy steps from then on; the runner services need no restart (each job looks the command up when it starts).

Linux development acceptance runs locally in OrbStack/Docker, including the root service-user
installation test. It does not contact or update the homelab runner.

VM110 intentionally has no inbound SSH service. Its separate, explicitly authorized Proxmox
deployment uses QGA and a pinned Git bundle; keep that route rather than opening SSH for
`--host`. The homelab's `slotfather_commit` pin and `configure-ci-runner-guards.yml` playbook
own that live delivery. They require a published commit and preserve the runner's queue,
cgroup configuration and two-slot memory capacity.
