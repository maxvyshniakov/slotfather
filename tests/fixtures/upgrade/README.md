# Upgrade compatibility fixture

`slotfather-556f912.py.gz` is the unmodified installed-command source at commit
`556f912dd1354e6142b1204db9e0ca220495430e`, before the package extraction.
Its decompressed SHA-256 is `c320a2ca95e4da03fa73b5b86c836264b5a9716de0d6fbbcfacc5d8cd57be8b8`.
The installer test runs this real old client, upgrades while it holds a lease,
and verifies that the new client sees its lease and that the old client can release it.
The compressed copy keeps the test self-contained in shallow clones and source archives.
