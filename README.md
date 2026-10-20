<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/banner-dark.png">
    <img src="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/banner-light.png" alt="fmsonar" width="380">
  </picture>
</p>

fmsonar reads a FileMaker solution's design export into a SQLite index and answers
"where is this used?" in milliseconds. Explore it in the browser, query it from the
shell, or let your AI assistant query it for you.

*[fmsonar.com](https://fmsonar.com) · [![PyPI](https://img.shields.io/pypi/v/fmsonar)](https://pypi.org/project/fmsonar/) `pipx install fmsonar` · MIT*

## Why

- A solution's only complete map is its design export: a Database Design Report, or
  since FileMaker 26 a Save a Copy as XML file. Both are hundreds of megabytes of XML.
  Finding every use of one field by hand means searching that file and hoping you did
  not miss a trigger, a button parameter or a calculated name.
- An AI assistant can read the raw XML, but every question costs a fresh pass. One
  parse into an index makes every question after it instant, at roughly a third of the
  time and tokens.
- The export is a client's whole schema. fmsonar.com is a page, not a service: parsing
  runs in your browser and nothing is uploaded. See [SECURITY.md](SECURITY.md).

## Try it in the browser

1. FileMaker Pro, advanced tools on: **Tools → Database Design Report → XML**, all files.
2. Open [fmsonar.com](https://fmsonar.com) and drop the DDR folder on the page.

Every name and line of code is searchable, references run both ways, scripts are
readable in full, call chains are drawn, and any script copies back to FileMaker as a
pasteable snippet. Self-host by serving `fm_ddr/web/index.html` as a static file.

## Install the CLI

```bash
brew install pipx          # macOS; see pipx.pypa.io elsewhere
pipx install fmsonar
fmsonar build /path/to/DDR/Summary.xml -o solution.db
fmsonar where solution.db "CUS__Customer::Name_Last"
```

```
# CUS__Customer::Name_Last (field) — 10 reference(s)
context        source_kind    source_parent_name  source_name
-------------  -------------  ------------------  -----------
calc           field          CUS__Customer       Name_Full_c
calc           layout_object  Lab Layout          labWeb
calc           script_step    Lab Mailer          Send Mail
layout_object  layout_object  Customer Detail
step_target    layout_object  labBar
validation     field          CUS__Customer       Name_Last
```

| Command | Answers |
|---|---|
| `where DB "name"` | every use of a field, script, layout, table occurrence or custom function |
| `search DB "text"` | full-text search across calculations, script steps and names |
| `investigate DB "Script"` | callers, callees, layout launch points, globals, full body |
| `report DB -o out.html` | a self-contained interactive viewer |
| `stats DB`, `list [folder]` | counts and resolution health; the indexes in a folder |
| `sql DB "SELECT ..."` | anything else; recipes in [QUERIES.md](QUERIES.md) |

Pure standard library, no dependencies. Building the same source again is a no-op
(`Unchanged:`); `--rebuild` forces it. Update with `pipx upgrade fmsonar`, then
`fmsonar install-skill` if you use the skill below.

## Two inputs, one index

Use the DDR when you want the browser, or a multi-file index with every cross-file
reference resolved. Use Save a Copy as XML when nobody should have to open FileMaker
Pro: a nightly job, CI, or an agent that exports and indexes on its own.

| | Database Design Report | Save a Copy as XML |
|---|---|---|
| Made with | FileMaker Pro: Tools → Database Design Report → XML | FileMaker 26 (format 2.3.0.0 or 2.3.0.1, "Include details for analysis tools" on): Tools → Save a Copy as XML, the script step of that name, or FileMaker Server's FMDeveloperTool on a closed file |
| Read by | the browser and the CLI | the CLI |
| Cross-file references | resolved | resolved when exported from Pro with every file open; empty from FMDeveloperTool |

```bash
# Unattended, on a copy of the closed file; use a dedicated export account,
# the password is visible in the process list while it runs
FMDeveloperTool --saveAsXML Solution.fmp12 <account> <password> -t Solution_saxml.xml -include_details
fmsonar build Solution_saxml.xml -o solution_saxml.db
```

Both inputs give the same database: the same tables, views and reference contexts, so
every query and the skill work unchanged. `ddr_run.source_format` records which export
fed it. The content differs only where the exports differ; the measured list is in
[COVERAGE.md](COVERAGE.md) ("SaXML next to the DDR"). A Save a Copy as XML index also
stores each object's last change, including the developer's account; the HTML report
leaves that out.

## Your AI assistant

```bash
fmsonar install-skill
```

Claude Code then knows the schema, the query recipes and the investigation protocol:
survey before deep-reading, climb to callers, sweep the globals a script writes, report
real FileMaker ids. Ask it *"which scripts write to CTC::email?"* with a DDR or an
export at hand; the index lands next to the export. Other assistants read
[AGENTS.md](AGENTS.md).

## What is in the index

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/schema-dark.svg">
    <img src="https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/main/docs/schema-light.svg" alt="fmsonar database: ddr_run, files, entities, refs, text_index" width="840">
  </picture>
</p>

`entities` has one row per named thing: a table, a field, a table occurrence, a
script, a script step, a layout, a layout object, a custom function. `refs` is the
join table "source uses target", one row per use, both sides foreign keys to
`entities`, with `context` saying what kind of use it is. The script step
`Set Field [CUS__Customer::Name_Last; ...]` is one row in `refs`: source the step,
target the field, context `step_target`. `where` selects on the target;
`v_unused_fields` lists the fields that are nobody's target. `text_index` is the
full-text copy of names and calculation text, for what no edge captures. The kinds,
the contexts and the gaps are listed in [COVERAGE.md](COVERAGE.md).

## Accuracy and cost

About 98% of references resolve as typed edges on the production solutions we measure
against; the rest is reachable through full-text search. Catalog counts match the
solution's own documentation, and a test suite checks the Python parser against the
browser's JavaScript twin edge by edge on every push. The Save a Copy as XML parse is
measured against the DDR parse of the same file; every difference falls in a class
listed in COVERAGE.md. The tool is A/B tested against raw text search on real tickets:
[The tests that shaped fmsonar](https://oogi.io/blog/the-tests-that-shaped-fmsonar/).

Measured: a 510 MB nine-file DDR builds in about 26 s; its 416 MB main file parses in
the browser in about 7 s using 80 MB; a 448 MB FileMaker 26 export of one file builds in
about 28 s using 230 MB.

## Not yet

- The browser reads DDRs only.
- Edit in the viewer and emit a patch for FileMaker's upgrade tool: an idea, parked.

> Sibling tool: [fmstyle](https://fmstyle.dev) formats FileMaker calculations without
> changing what they compute. Same shape: browser, `pipx install fmstyle`, Claude Code skill.

---

[Thomas De Smet](https://oogi.io) · [tdesmet@oogi.io](mailto:tdesmet@oogi.io) · MIT ·
FileMaker and Claris are trademarks of Claris International Inc.; fmsonar is independent.
