# Install

```sh
gh repo clone maxvyshniakov/slotfather
cd slotfather
python3 install.py
```

The installed commands run on the interpreter that ran the installer: their `#!/usr/bin/env python3` line becomes that interpreter's path, as invoked, so an agent inside a project's virtual environment does not run slotfather on the project's Python. The installer refuses an interpreter inside a virtual environment, and one older than 3.9; name another with `python3 install.py --python /usr/bin/python3`. Re-run it to switch interpreters.

The installer also performs the one-time rename migration. It refuses live holders or waiters in either retired queue, preserves the entire earlier machine queue (including saved capacity) when the new queue is absent, and otherwise leaves the existing new queue untouched. Retired contents remain in named `~/.local/state/slotfather-migrations/` backups. Different old and new configurations are both preserved; the existing new configuration stays effective.

Both retired paths, `~/.local/state/machine-slots` and `~/.local/state/straylight-verification/slots`, become regular file guards. Atomic directory/file exchanges ensure old clients cannot recreate an uncoordinated queue between the move and guard creation. Existing intentional guards make a repeated install safe, including while the new queue has active leases. Unrelated files at those paths are refused and preserved. The installer uses the default state locations; coordinate custom queue paths separately.

The executable is replaced atomically and the old command name is removed last. If a migration stops after a directory exchange, retain its guards and printed backup paths: the queue contents remain preserved for recovery. This is an explicit installation step, not compatibility logic in normal command execution.

Put `~/.local/bin` on your `PATH`. All participating projects must use the same state directory; saved capacity is shared through that directory. Updating the executable preserves the queue and running leases.

### For a service user: one queue for several CI runners

`python3 install.py --user NAME [--bin DIR] [--cgroup PATH]` installs for NAME (another user needs root): the queue is NAME's `~/.local/state/slotfather`, written into the installed `slotfather` as its default state directory, so every process that runs these commands shares that one queue whatever its `HOME` (a job that moves `HOME` still queues with the rest; `SLOTFATHER_DIR` still overrides). `--bin DIR` puts the commands in DIR instead of `~/.local/bin`, such as `/usr/local/bin`, which a systemd service's and the GitHub runner's `PATH` already hold. Under root, the paths the installer creates in NAME's home (the queue, the retired-path guards, the install lock) are given to NAME; commands outside that home stay root's, so jobs cannot rewrite them (a failure part-way still hands over what was created). `--cgroup PATH` writes in the cgroup v2 path the leases share (see Linux), so every client of the installed command, also a shell outside that cgroup, sizes and guards by it. Another user's queue is read only: run by any other user (root through the guest agent, say), `run`, `yield` and saving `slots` or `order` are refused with the user to run them as (`sudo -u NAME slotfather ...`), and `status`, `history`, `stats`, `available`, `advise`, the dashboard and `slots`/`order` without changes create and remove nothing (a dead holder's entry is left out, not reaped).

The CI runner VM110 (Ubuntu, 6 vCPU, 12 GiB, no swap, nine `actions.runner.*` services as `runner`, #653), from a checkout of this repository on the VM:

```sh
sudo python3 install.py --user runner --bin /usr/local/bin --cgroup /ci.slice/ci-runners.slice
sudo -u runner slotfather slots memory 5   # 2 slots (saved: one per 5 GiB of 10 GiB in /ci.slice/ci-runners.slice)
sudo -u runner sh -c 'cd /tmp && slotfather run -n 1 -- slotfather status'
slotfather status                          # as root: read only
```

`slots` refuses changes while any lease is held or waits, so save it while the runners are idle. If the runners' shared drop-in sets `SLOTFATHER_DIR`, point it at this same `/home/runner/.local/state/slotfather` (the pinned default; another directory would need its own `slots` save); `SLOTFATHER_CGROUP=/ci.slice/ci-runners.slice` there is optional, since the installed command names the slice. Sid-Kit's and Data Grid's `scripts/heavy.sh` find `slotfather` on `PATH` and queue their heavy steps from then on; the runner services need no restart (each job looks the command up when it starts).
