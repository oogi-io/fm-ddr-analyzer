<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/banner-dark.png">
    <img src="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/banner-light.png" alt="fmsonar" width="380">
  </picture>
</p>

fmsonar answers "where is this used?" for a FileMaker solution. The only complete map
of a solution is its design export, hundreds of megabytes of XML; fmsonar reads it
once into a single file, and from then on every question is instant: in the browser,
in the Terminal, or through your AI assistant.

*[fmsonar.com](https://fmsonar.com) · [![PyPI](https://img.shields.io/pypi/v/fmsonar)](https://pypi.org/project/fmsonar/) `pipx install fmsonar` · MIT*

## Try it in the browser

1. In FileMaker Pro, with **Use advanced tools** on (Settings → General, then restart):
   Tools → Database Design Report, format XML, all files. FileMaker writes a folder with
   `Summary.xml` and one XML file per database.
2. Open [fmsonar.com](https://fmsonar.com) and drop that folder on the page.

Parsing runs in your browser; nothing is uploaded
([SECURITY.md](https://github.com/oogi-io/fm-ddr-analyzer/blob/main/SECURITY.md)).
Search names and code, follow references both ways, read scripts, see call chains.
The browser reads DDRs; a Save a Copy as XML file is refused with a message. Self-host
by serving `fm_ddr/web/index.html` as a static file.

## Install the CLI

Needs Python 3.10 or later and [pipx](https://pipx.pypa.io) (`brew install pipx` on
macOS; pipx installs on Windows too).

```bash
pipx install fmsonar
fmsonar build /path/to/DDR/Summary.xml -o solution.db
fmsonar where solution.db "CUS__Customer::Name_First"
```

Output, here from the lab file in `tests/fixtures`:

```
# CUS__Customer::Name_First (field) — 8 reference(s)
context        source_kind    source_parent_name  source_name  ambiguous
-------------  -------------  ------------------  -----------  ---------
calc           field          CUS__Customer       Name_Full_c  0
calc           layout_object  Lab Layout          labWeb       0
calc           layout_object  tabFirst                         0
calc           script_step    Lab Mailer          Send Mail    0
layout_object  layout_object  Customer Detail                  0
layout_object  layout_object  Lab Layout          nameFirst    0
step_target    script_step    Caller              Set Field    0
validation     field          CUS__Customer       Name_Last    0
```

Source is what uses the field, context is how. A layout object with no name in
FileMaker has none here either.

`solution.db` is one file, no server: fmsonar reads it, and so does any SQLite tool.
It holds the whole schema, so treat it like the export itself.

| Command (`DB` is your `solution.db`) | Answers |
|---|---|
| `where DB "name"` | the uses of a field, script, layout, table occurrence or custom function |
| `search DB "text"` | full-text search across calculations, script steps and names |
| `investigate DB "Script"` | callers, callees, layout launch points, globals, full body |
| `report DB -o out.html` | a self-contained interactive viewer |
| `sql DB "SELECT * FROM v_unused_fields"` | anything else; recipes in [QUERIES.md](https://github.com/oogi-io/fm-ddr-analyzer/blob/main/QUERIES.md) |

`fmsonar --help` lists the rest. Building the same source again is a no-op
(`Unchanged:`). Update with `pipx upgrade fmsonar`.

## Two inputs, one index

Use the DDR for the browser. Use Save a Copy as XML when nobody should have to open
FileMaker Pro: a nightly job, CI, or an agent that exports and indexes on its own.

| | Database Design Report | Save a Copy as XML |
|---|---|---|
| Made with | FileMaker Pro: Tools → Database Design Report → XML | FileMaker 26, "Include details for analysis tools" on: Tools → Save a Copy as XML, the script step of that name, or FileMaker Server's FMDeveloperTool on a closed file |
| Read by | the browser and the CLI | the CLI |
| References into other files | resolved, from a DDR of every file | resolved when exported from Pro with every file open; empty from FMDeveloperTool |

Both give the same database layout and the same commands; `ddr_run.source_format`
records which export fed it. Where their content differs, and the headless
FMDeveloperTool route, are in
[COVERAGE.md](https://github.com/oogi-io/fm-ddr-analyzer/blob/main/COVERAGE.md),
section "SaXML next to the DDR".

## Your AI assistant

```bash
fmsonar install-skill
```

Claude Code then knows the schema, the query recipes and how to investigate. Point it
at a DDR or an export; the index lands next to it. `fmsonar install-skill --check`
tells you when the installed skill differs from the tool's. Other assistants read
[AGENTS.md](https://github.com/oogi-io/fm-ddr-analyzer/blob/main/AGENTS.md).

## What is in the index

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/schema-dark.svg">
    <img src="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/schema-light.svg" alt="entities and refs, with the Set Field row from the example above" width="840">
  </picture>
</p>

Two tables carry the answers. `entities` has one row per named thing: a table, a
field, a layout, a script, a script step, a layout object, a custom function. `refs`
has one row per use, pointing from the thing that uses to the thing used, with a
`context` that says how. The `Set Field` step in the example above is one such row:
from the step to the field, context `step_target`. `where` lists the rows that point
at a thing; `v_unused_fields` lists the fields nothing enabled points at. The ids in
the picture are fmsonar's row numbers; FileMaker's own ids are in `fm_id`. A full-text
table covers what no row captures: names inside `ExecuteSQL` strings, scripts and
layouts called by name. The kinds, the contexts and the gaps are in
[COVERAGE.md](https://github.com/oogi-io/fm-ddr-analyzer/blob/main/COVERAGE.md).

## Cost

A 510 MB nine-file DDR builds in about 26 s. Its 416 MB main file parses in the browser
in about 7 s using 80 MB. A 448 MB Save a Copy as XML export of one file builds in
about 28 s using 230 MB.

---

[Thomas De Smet](https://oogi.io) · [tdesmet@oogi.io](mailto:tdesmet@oogi.io) · MIT ·
FileMaker and Claris are trademarks of Claris International Inc.; fmsonar is independent.
