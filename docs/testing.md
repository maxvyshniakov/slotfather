# Verification

```sh
slotfather run -n 1 -- python3 -m unittest discover -s tests -v
```

Tests use isolated state directories and real child processes. The suite covers
queue admission, scheduling, capacity, nesting, stale leases, metadata, usage,
installation, process cleanup, dashboard input and the replay tools. Diagnostic
environment seams are listed in [configuration](configuration.md).

On Linux, procps-compatible `ps` is required. From macOS, use a disposable Python
container for Linux acceptance; a linked worktree also needs its main checkout’s
`.git` mounted at the same path. Root-only service-user tests require an isolated
container and `SLOTFATHER_TEST_USERADD=1`.

```sh
slotfather run -n 1 -- docker run --rm --user 1000:1000 -e HOME=/tmp/h \
  -v "${PWD}:${PWD}:ro" -w "$PWD/tests" python:3.12 \
  sh -c 'mkdir -p "$HOME" && git config --global --add safe.directory "*" && python3 -m unittest discover -s .'
```

For the demo’s moving data and fractional chart geometry:

```sh
slotfather run -n 1 -- python3 -m unittest discover -s tests -p test_dashboard_demo.py
```

Media review is separate from text assertions: examine the exported frames and
verify their run manifests through screen-kit. [Media workflow](media.md).
