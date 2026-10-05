## What and why

<!-- The problem, the behaviour change, and any limits that remain. Link the issue. -->

## Checklist

- [ ] `python -m ruff check src tests launch.py` and `python -m pytest` pass
- [ ] New or changed audit rule: one positive and one negative test, and the table in `docs/behavior-and-limits.md` (or `docs/bom.md`) updated
- [ ] Rules stay deterministic, local and conservative (silent when unsure)
- [ ] No new network call, subprocess, filesystem write outside the project, telemetry or secret handling. If there is one, the PR explains the trust boundary and destinations (see CONTRIBUTING.md)
- [ ] Board writes still go through the backup / refusal / single-transaction boundary
- [ ] Fixtures are authored for this project or have a compatible license; no keys or confidential boards
- [ ] Docs and screenshots updated if behaviour or UI changed

## Notes for the reviewer

<!-- Anything risky, untested (for example live KiCad) or deliberately left out. -->
