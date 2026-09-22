# VelaTrace build status

This is an unfinished development checkout, not a signed release. Target upstream:
F-Blaze/VelaTrace. Remote setup remains pending by the user's explicit choice.

## Reviewed work

- Phase 0: IPC feasibility, MIT from first commit (1157429), Python packaging,
  read-only PCB/XML readers and security workflow scaffolding.
- Phase 1: connectivity-aware audit, function correction/confirmation, actual-prompt
  classification token estimate and consent, deterministic flags/math, flagged-only
  pricing, call limits, injection boundary and endpoint-only provider transport.
- Phase 2: numeric scoped constraints, fresh DSN handoff, strict SES parsing, pinned
  external offline Freerouting, candidate DRC and backed-up single-commit IPC writer.
  Write safety was reviewed and committed as 8f1f0d2.

## Current work and remaining phases

Phase 3 ui-design is active: one external Audit/Routing window and IPC launcher.
Next, in order: privacy-disclosure, test, docs, release-review. Each marker is
assigned to a sub-agent and reviewed before the next phase begins.

## Verification

- 58 unit/integration tests: 57 pass, optional installed-router test skipped in the
  latest run. Earlier full 42-test suite included and passed that real router test.
- Official Freerouting 2.1.0 plus Java 21: default and constrained routing passed;
  runtime network-denial probe and actual blocked update request verified.
- Exact pinned kicad-python 0.8.0 constructors/signatures checked offline.
- Saved/live backup refusal, partial-create rollback, uncertain commit blocking,
  project-rule changes and one-commit application tested using failure injection.
- Wheel includes MIT license and hash-verified independent offline probe.
- Qt 6.10.2 offscreen rendering works. UI implementation/review remains underway.

No live provider request or live KiCad IPC/DRC test has been performed. KiCad is not
installed. Java and Freerouting are local test tools under the workspace work/
folder, not bundled in the deliverable. No API key was requested or stored.

## Explicit capability and release gates

KiCad 9/10 require a fresh manual DSN export. No docking or transient overlay API
is available; the external window and User.9 graphics are the fallbacks. KiCad owns
canvas layer colors. Routing currently supports initially unrouted boards,
straight traces, through vias and global numeric width/clearance rules. Existing
routing, hierarchical schematic context and unsupported header/per-net constraints
are refused. These limitations are not claims of completed support.

Live candidate DRC, SaveCopy serialization, server default attributes, reads during
an IPC commit and real single-step Undo remain mandatory release acceptance tests.
The first-use persistent disclosure UI, final tests/docs and release review remain
pending. GitHub protections, reviewed PRs, secret scanning/push protection, remote
CodeQL evidence and a signed release tag have not been established. No main branch,
remote or release tag exists; development commits are on bootstrap/foundation.

## Developer resume

Run `python -m unittest discover -s tests -v` with PYTHONPATH=src. The workspace
work/test-runtime contains test tools and the pinned IPC SDK; work/ui-runtime
contains PySide6-Essentials 6.10.2. Native router test paths are documented in
freerouting.md. Pip directory ACLs may require approved read access in this build
sandbox; this is a host-specific testing issue.

Do not call this production-ready, install from main, bypass reviews, create an
unsigned release, or replace unsupported operations with fabricated success.
