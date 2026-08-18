# Contributing to mavlink-mcp

Thanks for your interest in contributing!

## Getting started

1. Fork and clone the repository.
2. Install in development mode: `pip install -e ".[dev]"`
3. Run the test suite: `pytest` (no SITL or drone required — tests use a mock adapter).

## Pull requests

- Keep PRs focused: one feature or fix per PR.
- Add or update tests for any behavior change.
- Make sure `pytest` passes locally and CI is green.
- Use clear commit messages (Conventional Commits style is appreciated, e.g. `feat:`, `fix:`, `docs:`).
- **Cite your sources.** For anything protocol- or behavior-related, link the public spec or official documentation you worked from (MAVLink docs, MAVSDK docs, PX4/ArduPilot docs) in the PR description. This keeps an audit trail showing the project is built from open standards only.

## Clean-contribution rule (important)

This project is built **exclusively from public, open sources**: open protocol specs, official public documentation, permissively-licensed libraries, and hardware the contributor owns. To keep it that way, we will decline contributions that contain or derive from:

- non-public information (anything under NDA, leaked documents, vendor-internal material);
- firmware dumps, decompiled/disassembled proprietary code, or anything obtained by bypassing technical protection measures;
- code copied from GPL/AGPL projects or from proprietary SDKs (including their headers);
- material available only behind a vendor developer login whose EULA restricts open-source use.

If you are unsure whether something is safe to contribute, open an issue and ask first.

## Dependency policy

Runtime dependencies must be permissively licensed (MIT/BSD/Apache). In particular, the core must not depend on pymavlink (LGPL-3) — the MAVLink backend is MAVSDK-Python (BSD-3). Adapters with non-permissive dependencies belong in separate repositories.

## Developer Certificate of Origin (DCO)

By contributing, you certify that you have the right to submit your work under the MIT license ([developercertificate.org](https://developercertificate.org)). Please sign off each commit:

```bash
git commit -s -m "feat: my change"
```

This adds a `Signed-off-by: Your Name <you@example.com>` line to the commit message.

## Reporting issues

Open a GitHub issue with reproduction steps, your autopilot + version (PX4/ArduPilot, SITL or hardware), your MAVSDK version, and the MCP client you are using.
