# External router and offline policy

VelaTrace runs the unmodified **Freerouting 2.1.0 JAR** as an external process.
It does not link, embed or redistribute GPL router code. Download the JAR from
the [official v2.1.0 release](https://github.com/freerouting/freerouting/releases/tag/v2.1.0).
The required SHA-256 is:

```text
2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def
```

Install **Eclipse Temurin Java 21**, JRE or JDK, from
[Adoptium](https://adoptium.net/temurin/releases/?version=21), and configure the
absolute path to `bin/java` (`bin/java.exe` on Windows). Java 21 is mandatory,
including when a newer Java is installed for other applications. The integration
was executed with Windows x64 Temurin **21.0.12.1+1**. Other OS/runtime combinations
still need native verification before release.

## Why the older router and exact Java major

Freerouting 2.4.1, 2.1.0 and other inspected releases start an update check even
with analytics disabled. The
[2.1.0 startup source](https://github.com/freerouting/freerouting/blob/v2.1.0/src/main/java/app/freerouting/Freerouting.java)
and [version checker](https://github.com/freerouting/freerouting/blob/v2.1.0/src/main/java/app/freerouting/management/VersionChecker.java)
make this visible. Disabling telemetry alone therefore does not meet VelaTrace's
privacy requirement.

The adapter uses Java 21's process-local security policy with **no socket or URL
permissions**, no subprocess execution permission, no ability to replace the
security manager/policy, and writes limited to the per-run scratch directory.
The JAR and runtime files are readable. API servers and GUI are disabled,
analytics is disabled with `-da`, and user settings are isolated. Inherited API
keys, proxies, `CLASSPATH`, Java option variables and Freerouting settings are not
passed to Java. No host firewall or system policy is changed.

This is a Java-managed network restriction for the hash-pinned official router,
not an OS sandbox for arbitrary hostile native code. Java native library loading
is needed by the desktop runtime. The security manager is deprecated and was
removed in Java 24; VelaTrace rejects every major other than 21 rather than
silently run without the restriction. Future router/runtime upgrades require
an independently verified replacement for this mechanism.

Startup runs an independent, MIT-licensed `OfflineProbe` under the same policy.
The probe checks that DNS, socket connections/listening, HTTP URL access,
subprocess execution, outside-directory writes, and disabling the policy are
denied. A failed probe stops routing. A fresh probe runs again for each route.
The actual native integration also observed the router's GitHub update request
fail with `AccessControlException` for `java.net.URLPermission` while producing
a valid SES. Nothing is sent to that endpoint.

## Process and constraint boundaries

`Freerouting(jar, java, work_directory=project_scratch)` supplies the `Router`
protocol. Call `check_startup()` during plugin startup. Missing Java/JAR,
wrong hashes/versions and policy failures produce actionable `CapabilityError`s.
Routing copies the DSN into a fresh isolated directory; it does not write the
source DSN or board. No adjacent user `.rules` file or stored router preferences
are loaded. The external process has a timeout, a 1 GiB heap limit, one optimizer
thread, at most 100 passes, and a retained console-log tail of at most 64 KiB.
Timeout kills the Java process. Per-run scratch data is removed on return/error.
The retained log may contain design names; it stays local and must never enter
provider prompts or telemetry.

Numeric `clearance` and `trace-width` constraints targeting **`all nets`** are
supported. Every DSN width/clearance rule is raised to at least the confirmed
minimum; existing stronger rules remain. The DSN must have a structure rule.
Quoted identifiers remain quoted and numeric tokens remain numeric. The stackup
and placements are unchanged. A candidate validator must independently check
these requirements and actual KiCad DRC before approval. Passing a router setting
or seeing a router success message is not a validation report.

Header keepouts and per-net targets currently raise an explicit capability error;
they are never silently dropped. The user must explicitly revise constraints, or
a later geometry adapter must implement and validate them. No autoplacement.

## Interchange evidence and reproduction

`tests/fixtures/routing/simple.dsn` is a synthetic two-pad net created for this
project. `freerouting-2.1.0.ses` is the actual output of the official router with
0.5 mm minimum width and 0.3 mm minimum clearance, using a DSN named
`simple-test.dsn`. No upstream PCB designs or router binaries are bundled.

The SES reader accepts the router's unchanged placement echo only after comparing
every reference's position, side and rotation with the trusted original DSN. An
empty `was_is` section is permitted; renaming is refused. Identical duplicate via
definitions emitted by this version are permitted; conflicting definitions are
refused. SES base design must equal the expected basename or its stem. DSN decimal
coordinates use their `unit`; SES integer coordinates use `resolution`. These
different scales have regression tests.

Run unit tests with `python -m unittest discover -s tests -v` and `PYTHONPATH=src`.
To also execute the installed router, set `VELATRACE_TEST_JAR` and
`VELATRACE_TEST_JAVA` to absolute paths, then run that command. The optional native
test covers both unchanged DSN rules (0.25 mm width) and raised rules (0.5 mm width,
0.3 mm clearance), parses the entire real SES, checks the denied update request,
and checks scratch cleanup. The placeholder board file in this adapter test is
not a real PCB; **this is not evidence of KiCad DRC or board-write safety**.

The independent probe source and class are installed as package data under
`src/velatrace/router_resources`. To reproduce its bytecode with a Java 21 JDK:

```text
javac --release 21 src/velatrace/router_resources/OfflineProbe.java
```

Expected class SHA-256:
`c27481d8f2e0505ec21b8ba375888343dfcc06406d9e62e4ab6c7c63929ef6de`.
The adapter verifies this digest before execution. It has no Freerouting imports
or dependencies.

## Requirements for the board safety adapter

The `ViaSpec` catalog must come from verified KiCad board drill, diameter and
layer-pair data. A DSN/SES padstack name such as `Via[0-1]_600:300_um` is not by
itself trusted proof of a drill. Match the DSN circle geometry to the board's
actual via settings before supplying that name to `parse_ses`. All library-out
padstacks, including unused emitted definitions, require a verified mapping.
Unsupported mapping or geometry must stop the whole write.

The router adapter provides no live PCB mutation or DRC assertion. The next
boundary must recheck exact board/DSN identity and current live geometry,
materialize a candidate, run KiCad DRC, verify constraints, back up before every
write, and use one undoable IPC commit. An incomplete or unknown result must stop.
