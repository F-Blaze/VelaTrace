# Experimental hybrid routing benchmark

VelaTrace can now compare and select independently validated candidates from
two existing open-source engines: external Freerouting 2.1.0 (GPLv3) and external
[KiCadRoutingTools 0.22.1](https://github.com/drandyhaas/KiCadRoutingTools/tree/023d3f79027d5406e4ea8e68291f588c5c135673)
(MIT). KRT uses a Rust grid/A* routing core and Python orchestration. VelaTrace
adds strict output inspection, original-rule KiCad DRC, optional dangling-stub
repair and bounded selection by unique copper length, then via count.

This is a research command, not the live plugin's routing backend. It accepts
only the exact synthetic boards, DSNs and projects authored by VelaTrace's
fixture generator. Do not use it for confidential or real boards. There is no
AI request or API charge. Installing dependencies requires downloads; routing
runs locally. Python audit hooks disable Python networking and subprocesses in
the KRT child, and its environment excludes provider keys. These hooks are not
an OS sandbox for its native extension. Production privacy qualification remains
a release gate. No upstream source or binary is bundled in VelaTrace.

## Setup (Windows x86_64 research environment)

Use a reviewed development checkout for this experiment; it does not create a
signed production release or update an installed plugin. Choose a new research
directory and run these commands there. Python 3.11 or newer is required.

```powershell
git clone --branch v0.22.1 --depth 1 https://github.com/drandyhaas/KiCadRoutingTools.git krt
git -C krt rev-parse HEAD
# Must print 023d3f79027d5406e4ea8e68291f588c5c135673
python -m venv krt-venv
.\krt-venv\Scripts\python.exe -m pip install numpy==2.2.6 scipy==1.15.3 shapely==2.1.1
gh release download v0.22.1 --repo drandyhaas/KiCadRoutingTools --pattern grid_router-windows-x86_64.pyd --dir krt/rust_router
Copy-Item krt/rust_router/grid_router-windows-x86_64.pyd krt/rust_router/grid_router.pyd
Get-FileHash krt/rust_router/grid_router.pyd -Algorithm SHA256
```

The binary hash must be
`06d127d55c4edfa5135b80ac5c6686d9e0a3d0d1d2c167e58a8972198fc97baa`.
The wrapper also verifies a pinned aggregate of every Python source file,
`VERSION` and `rust_router/Cargo.toml`, normalized only for CRLF/LF. Changed or
added Python files, cached bytecode, and mismatched native binaries are refused.
Do not run the upstream GUI/plugin or build script as part of this workflow.
Only its file-in/file-out `py_router/route.py` is called, with isolated Python
mode and bytecode writing disabled. No auto-placement or pin-swap command is
enabled. The binary version is 0.22.0, as declared by this release's Cargo file.

From a VelaTrace development environment with its dependencies installed:

```powershell
python -m velatrace --benchmark NEW_RESULTS_DIRECTORY --benchmark-layers 4 6 8 --benchmark-obstacles --benchmark-seconds 180 --jar PATH_TO_FREEROUTING_2_1_JAR --java PATH_TO_JAVA_21_EXE --kicad-cli PATH_TO_KICAD_CLI_EXE --benchmark-krt PATH_TO_KRT --benchmark-krt-python PATH_TO_KRT_VENV_PYTHON_EXE
```

Add `--benchmark-dense` for eight crossed nets instead of four. The obstacle
variant adds two identical no-net copper pads to both PCB and DSN, so routes
must avoid the same top-layer copper under both engines. Each result directory
must be new. All source boards and projects are preserved.
Use `--benchmark-solvers hybrid-fast` to measure only fast mode without repeating
the other solvers. Selected solvers are recorded in the manifest.

## What is measured

Each solver has the same aggregate wall-time allowance, including its candidate
generation and KiCad checks:

| Solver | Candidate attempts |
| --- | --- |
| `baseline` | One unmodified Freerouting run |
| `portfolio` | Up to three layer policies, each followed by guarded DRC repair |
| `krt` | One KRT run with strict copper import |
| `hybrid` | KRT plus the three repaired Freerouting policies |
| `hybrid-fast` | Same fallback order; stops at the first independently accepted candidate |

Both hybrid modes offer the same guarded repair to the KRT candidate. Only
complete, independently accepted plans compete. Different strategies perform
different amounts of work; equal budgets do not imply equal CPU work. Python
numerical libraries are limited to one thread. Per-process timeouts bound the
child router, but synchronous validation callbacks can overrun an aggregate
deadline; late results cannot win.

Fast mode trades further optimization for lower latency. It skips later engine
attempts after success but still revalidates the selected plan under fresh
inputs. Failed or incomplete results advance to the next engine/policy. A
failed final check produces no winner; early stopping cannot bypass DRC.

KRT writes only into a disposable copy. Any modification to that staged input or
its original rules refuses the result. Generated output project/rule files are
never imported: validation always uses the original project. The PCB importer
permits only new straight segments and approved through-vias; changes to pads,
net assignments, footprints, planes, physical stackup and even generator
metadata refuse the entire output. Arcs and unsupported via semantics also
refuse it. Returned Y coordinates are converted once into VelaTrace's route
coordinates. Normal KiCad candidate validation follows; the external board is
never applied directly.

Manifests record versions, hashes, results, timings, DRC reports and source
preservation. Raw backend logs, SES files and KRT output boards are retained for
inspection. These fixtures measure connectivity and geometry, not return-path
continuity, impedance, crosstalk or timing. Improved results here cannot establish
superiority over all routing tools or replace engineering review.

## Native comparison, 2026-10-03

The eight-net obstacle corpus was run with installed KiCad and both pinned
engines, with a 180-second allowance per solver per board. Times below include
candidate checks and final winner validation. These are single-run observations,
not stable performance averages.

| Copper layers | Repaired Freerouting | KRT alone | Hybrid optimization |
| --- | --- | --- | --- |
| 4 | 464.587 mm, 16 vias, 42.44 s | 498.108 mm, 0 vias, 17.19 s | 469.072 mm, 16 vias, 54.09 s |
| 6 | 464.379 mm, 16 vias, 44.64 s | 498.108 mm, 0 vias, 16.17 s | 461.047 mm, 16 vias, 56.84 s |
| 8 | 460.382 mm, 16 vias, 40.72 s | 498.108 mm, 0 vias, 16.27 s | 452.133 mm, 16 vias, 62.47 s |

Each listed route had zero unconnected items and zero new/blocking DRC issues.
Eighteen pre-existing fixture footprint warnings remained reported. All source
PCB, DSN and project hashes stayed unchanged. The one-shot unmodified Freerouting
baseline had no accepted winner in this run; the independent validator or strict
parser rejected each candidate. This is a comparison of these pinned versions
under VelaTrace's strict checks, not a claim about every release or configuration.

The hybrid selected the shortest copper among its own successful attempts. Its
four-layer result was longer than the separate repaired-Freerouting run: the
engine attempts differ, so adding a backend does not guarantee a better result
in every run. KRT used only the top layer in these cases. Thus its success on
boards with 4/6/8 copper layers does not demonstrate inner-layer congestion,
reference-plane handling or SI performance. It does demonstrate a useful
low-via, lower-latency alternative for these particular geometries.

An earlier four-net obstacle smoke test also passed for KRT (248.484 mm, zero
vias, 10.50 s) and hybrid optimization (231.631 mm, eight vias, 60.33 s). Neither
these toy boards nor their timings establish general superiority.

A separate finalized fast-mode run on the same dense corpus stopped after one
accepted KRT candidate on each board: **20.41 s (4 layers), 20.64 s (6 layers),
19.42 s (8 layers)**. All three had 498.108 mm of copper, zero vias, zero
unconnected items and zero new/blocking DRC issues; source hashes were unchanged.
This policy avoids the extra engine attempts while retaining the final fresh
validation. It is not faster than the single KRT comparison because it also
offers guarded repair and performs its associated initial validation.

Regression validation for this milestone: **518 tests and 226 subtests passed**,
four optional native tests skipped; lint and the local secret scan passed.
The native comparisons above were run separately with actual installed tools.
