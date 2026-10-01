# HTML audit report

`velatrace.report` turns an audit into one shareable HTML file: inline CSS, no JavaScript, no
external fonts/scripts/images, so it opens offline, prints cleanly and follows the light/dark
setting of the viewer.

```python
from velatrace.report import render_report, save_report

html = render_report(snapshot, findings, project_name="My board", version="0.1.0a1")
save_report("audit.html", html)  # UTF-8, written atomically
```

Contents: header (project, date, part count, version), the same summary line as
`audit_rules.summarize()` (counts per severity plus estimated saving per board), findings grouped
Errors / Warnings / Savings / Info (title, refs, nets, evidence, fix, cost delta), a collapsed BOM
table (ref, value, footprint) and a footer linking to the project.

Every board-derived string (references, values, footprints, net names, finding text) is passed
through `html.escape`; the report never executes anything from the design. Pass `generated_at`
for byte-for-byte reproducible output.
