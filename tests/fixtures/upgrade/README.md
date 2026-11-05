# Upgrade compatibility fixture

`slotfather-556f912.py.gz` is the unmodified installed-command source at commit
`556f912dd1354e6142b1204db9e0ca220495430e`, before the package extraction.
Its decompressed SHA-256 is `c320a2ca95e4da03fa73b5b86c836264b5a9716de0d6fbbcfacc5d8cd57be8b8`.
The installer test runs this real old-format client and proves that a breaking
request-format upgrade refuses while it holds a lease. The refusal leaves its
queue entry and published commands unchanged. The old client then finishes and
releases with its own code; installation succeeds after that drain. Its history
is converted by the finite installer migration, rather than a runtime fallback.
The compressed copy keeps the test self-contained in shallow clones and source archives.
