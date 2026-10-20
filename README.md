<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/oogi-io/fmsonar/main/docs/banner-dark.png">
    <img src="https://raw.githubusercontent.com/oogi-io/fmsonar/main/docs/banner-light.png" alt="fmsonar" width="380">
  </picture>
</p>

fmsonar answers "where is this used?" for a FileMaker solution. It reads the design
export once into one file you then query in the browser, in the Terminal, or through
your AI assistant.

*[fmsonar.com](https://fmsonar.com) · [![PyPI](https://img.shields.io/pypi/v/fmsonar)](https://pypi.org/project/fmsonar/) `pipx install fmsonar`*

## Try it in the browser

1. In FileMaker Pro, with every file of the solution open and **Use advanced tools**
   on (Settings → General, then restart): Tools → Database Design Report (DDR), format
   XML, all files. FileMaker writes a folder with `Summary.xml` and one XML file per
   database.
2. Open [fmsonar.com](https://fmsonar.com) and drop that folder on the page.

Parsing runs in your browser; nothing is uploaded
([SECURITY.md](https://github.com/oogi-io/fmsonar/blob/main/SECURITY.md)).
Search names and code, follow references both ways, read scripts, see call chains.
The browser reads DDRs; a Save a Copy as XML file is refused with a message. Self-host
by serving `fm_ddr/web/index.html` as a static file.

## Install the CLI

Needs Python 3.10 or later and [pipx](https://pipx.pypa.io) (`brew install pipx` on
macOS; pipx installs on Windows too). In Terminal (macOS) or PowerShell (Windows):

```bash
pipx install fmsonar
pipx ensurepath          # once; then open a new window
fmsonar build /path/to/DDR/Summary.xml -o solution.db
fmsonar where solution.db "CUS__Customer::Name_First"
```

Output, here from the lab file `tests/fixtures/saxml_lab3_2.3.0.0.xml` (both inputs
print the same shape):

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

Each row is one use: `source` is what uses the field, `context` is how; the parent is
the script or layout object it sits in. A layout object with no name in FileMaker has
none here either; `ambiguous` is 1 when several things share the name.

`solution.db` is one file on disk (without `-o` it is written next to the export). It
holds the whole schema, so treat it like the export itself. fmsonar reads it, and so
does any SQLite tool such as DB Browser for SQLite.

| Command (`DB` is your `solution.db`) | Answers |
|---|---|
| `where DB "name"` | the uses of a field, script, layout, table occurrence or custom function |
| `search DB "text"` | full-text search across calculations, script steps and names |
| `investigate DB "Script"` | callers, callees, layout launch points, globals, full body |
| `report DB -o out.html` | a self-contained interactive viewer |
| `sql DB "SELECT * FROM v_unused_fields"` | anything else; recipes in [QUERIES.md](https://github.com/oogi-io/fmsonar/blob/main/QUERIES.md) |

`fmsonar --help` lists the rest. Building the same source again is skipped and says
`Unchanged:`. Update with `pipx upgrade fmsonar`.

## Two inputs, one index

Use the DDR for the browser. Use Save a Copy as XML when nobody should have to open
FileMaker Pro, such as a nightly job or an agent that exports and indexes on its own;
that headless export leaves references into other files empty.

| | Database Design Report | Save a Copy as XML |
|---|---|---|
| Made with | FileMaker Pro: Tools → Database Design Report → XML | FileMaker 26: Tools → Save a Copy as XML with "Include details for analysis tools" on; also the script step of that name, or FileMaker Server's FMDeveloperTool |
| Read by | the browser and the CLI | the CLI |
| References into other files | resolved, from a DDR of every file | resolved when exported from Pro with every file open; empty from FMDeveloperTool |

Both give the same tables and the same commands. Where their content differs, and
the FMDeveloperTool route, are in
[COVERAGE.md](https://github.com/oogi-io/fmsonar/blob/main/COVERAGE.md),
section "SaXML next to the DDR".

## Your AI assistant

```bash
fmsonar install-skill
```

Claude Code then knows the schema, the query recipes and how to investigate. Point it
at a DDR or an export; the index lands next to it. `fmsonar install-skill --check`
tells you when the installed skill differs from the tool's. Other assistants read
[AGENTS.md](https://github.com/oogi-io/fmsonar/blob/main/AGENTS.md).

## What is in the index

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/oogi-io/fmsonar/main/docs/schema-dark.svg">
    <img src="https://raw.githubusercontent.com/oogi-io/fmsonar/main/docs/schema-light.svg" alt="entities and refs, with the Set Field row from the example above" width="840">
  </picture>
</p>

`entities` has one row per thing in the solution: a table, a field, a layout, a
script, a script step, a layout object, a custom function. `refs` has one row per use
and joins entities to entities: the thing that uses, the thing used, and how. `where`
reads those rows. What is not a row, such as a field name inside an `ExecuteSQL`
string, is found by `search` instead. The kinds, the contexts and the gaps are in
[COVERAGE.md](https://github.com/oogi-io/fmsonar/blob/main/COVERAGE.md).

## Cost

A 510 MB nine-file DDR builds in about 26 s. Its 416 MB main file parses in the browser
in about 7 s using 80 MB. A 448 MB Save a Copy as XML export of one file builds in
about 28 s using 230 MB.

---

[Thomas De Smet](https://oogi.io) · [tdesmet@oogi.io](mailto:tdesmet@oogi.io) · MIT ·
FileMaker and Claris are trademarks of Claris International Inc.; fmsonar is independent.
