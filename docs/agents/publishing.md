# Publishing

An explicit push request publishes the current `main` branch to its configured
upstream, `origin/main` in `maxvyshniakov/slotfather`. Fetch and inspect the full
outgoing range first, then push the verified commit with an explicit refspec:

```sh
git push origin <verified-sha>:refs/heads/main
```

Never force a push or skip a pre-push hook. There is currently no pre-push gate;
run the quality and test commands in [checks](checks.md) before publishing. Reuse
results when their inputs are unchanged, and do not run a gate twice if a hook
is later configured to run it.

The `CI` workflow in `.github/workflows/ci.yml` runs the same pinned Ruff/Pyright
budget check and full unittest suite on Ubuntu and macOS. It checks out full Git
history because the quality gate rejects shallow history. Follow both matrix
jobs to successful completion on the exact pushed SHA; a green local test run
does not substitute for either CI job. Fix code failures in new commits; rerun
only infrastructure failures.

A push does not install the CLI, change its immutable installed package, migrate
the live queue or deploy to another machine. Those actions require their own
explicit scope and the compatibility rules in [installation](../installation.md).
