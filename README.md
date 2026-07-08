# FM DDR Analyzer

Parse a FileMaker **Database Design Report** (DDR, the `*_fmp12.xml` files) into a
normalized **SQLite** database you can query for cross-references — *"where is this
field / script / table occurrence / custom function actually used?"* — and hand to
an AI to answer schema questions.

Spiritual successor to the FileMaker accessibility patcher: same idea (stream the
huge FileMaker XML with SAX), but instead of patching layouts it builds a queryable
index of the whole solution.

Two front-ends over the same logic:

- **`fm_ddr/web/index.html`** — a zero-install, client-side web app. Open it, drop in a
  DDR, and it parses **entirely in your browser** (nothing is uploaded — important,
  since a DDR contains a client's whole schema). Best for sharing / non-technical
  reach. The parser is a JS port of `parse.py`, validated to produce an identical
  graph.
- **`fm_ddr/` (Python CLI)** — the scriptable / CI version: build a SQLite DB and
  query it from the shell or hand it to an AI.

## Overview

- **Input:** DDR XML files (FileMaker: *Tools → Database Design Report → XML*) —
  a single file, several, or the `Summary.xml` manifest of a multi-file solution.
  Files are large (400+ MB) and UTF-16-LE; both parsers stream, so size doesn't
  matter (measured: a 510 MB 9-file solution builds in ~26 s; the 416 MB main
  file parses in-browser in ~7 s using ~80 MB of memory).
- **Output:** a single `.db` SQLite file — a unified `entities` table, a generic
  `refs` edge table (the heart of "where used"), and an FTS5 full-text index over
  every calculation and script step as a catch-all.
- **General-purpose:** no solution-specific assumptions. Validated against two
  unrelated production solutions and a 9-file, 510 MB multi-file solution.

## Web app (no install)

Open `fm_ddr/web/index.html` in a browser (or host it as a static page) and drop a DDR
onto it. Parsing, resolution, and the interactive viewer all run client-side —
no server, no upload. It also has a **Download report** button that exports a
self-contained HTML of the current solution to share.

## Install (Python CLI)

Pure standard library — no dependencies, Python 3.10+.

```bash
cd projects/fm-ddr-analyzer
python3 -m fm_ddr.cli build /path/to/Solution_fmp12.xml -o solution.db
```

## Usage

```bash
# Parse a DDR into SQLite (prints entity + reference counts)
python3 -m fm_ddr.cli build Solution_fmp12.xml -o solution.db

# Where is something used? (field / script / layout / TO / custom function)
python3 -m fm_ddr.cli where solution.db "CONTACT::email"
python3 -m fm_ddr.cli where solution.db "Navigate to Dashboard"

# Full-text search across every calc / script step / name
python3 -m fm_ddr.cli search solution.db "GetContainerAttribute"

# Interactive HTML viewer (self-contained, opens in any browser, no server)
python3 -m fm_ddr.cli report solution.db -o solution.html

# Counts + reference-resolution health
python3 -m fm_ddr.cli stats solution.db

# Any SQL (this is the real power — see QUERIES.md)
python3 -m fm_ddr.cli sql solution.db "SELECT * FROM v_unused_fields LIMIT 20"
```

Because the output is plain SQLite, an AI (or `sqlite3`, Datasette, DB Browser,
etc.) can query it directly. See **[QUERIES.md](QUERIES.md)** for canonical recipes.

## Data model

One database can hold several files (a multi-file solution) and several DDR
snapshots (for future diffing).

| Table | What it holds |
|-------|---------------|
| `ddr_run` | One parse run (source path, DDR version, timestamp, label) |
| `files` | Each FileMaker file in the run |
| `entities` | Every named thing — one row per `kind` (see below) |
| `refs` | Every "source **uses** target" edge; `target_entity_id` resolved after load |
| `text_index` | FTS5 mirror of names + calcs + step text (catch-all search) |
| `v_usage` | Friendly view over `refs` with readable source/target names |
| `v_unused_fields`, `v_orphan_scripts`, `v_unresolved` | Health hints |

**Entity kinds:** `base_table`, `field`, `table_occurrence`, `relationship`,
`layout`, `layout_group`, `script`, `script_group`, `script_step`,
`custom_function`, `value_list`, `privilege_set`, `account`, `extended_privilege`,
`custom_menu`, `custom_menu_set`, `external_data_source`, `theme`.

**Reference contexts** (`refs.context`): `calc`, `join_predicate`, `perform_script`,
`go_to_layout`, `trigger`, `layout_object`, `value_list_source`, `to_reference`,
`function_ref`.

### How references resolve

Every edge is captured raw during parse (target name + FileMaker id), then
`resolve.sql` fills `target_entity_id` by matching against `entities`. On real
solutions ~98% resolve; the rest are genuinely unresolvable and expected:

- `perform_script` to scripts in **other files** (external),
- `go_to_layout` with a **calculated** destination,
- `layout_object` fields that are **globals / unbound**.

Built-in FileMaker functions are intentionally *not* stored as edges (only
`CustomFunctionRef` chunks become `custom_function` edges); use FTS to find
built-in usage.

## Accuracy

Validated against a production solution's independently documented DDR summary —
every catalog count matches (base tables, table occurrences, relationships,
layouts, value lists, custom functions). A committed micro-fixture plus a full
test suite (structural counts, resolution semantics, UTF-16 round-trip,
edge-by-edge Python↔JS parity under torture chunking) runs in CI on every push.

## Roadmap

- [x] **Phase 1 — Cross-reference engine.** SAX parser → SQLite, generic edge
  table, FTS fallback, CLI (`build` / `where` / `search` / `sql` / `stats`).
- [x] **Phase 2 — Interactive HTML.** `report` command emits a self-contained
  page (data embedded, no server): searchable entity list with kind filters,
  click any field/script/TO to see inbound ("referenced by") and outbound
  ("references") edges grouped by the OTHER entity's kind (Scripts / Layouts /
  Fields / Custom functions / Relationships / ...), with the usage context
  folded into each group header, and click-through navigation.
- [x] **Correctness hardening.** `where` resolves through the TO before
  filtering (no more leaf-name over-matching), ambiguous picks are flagged
  (`refs.ambiguous`, `v_ambiguous`), VL field sources and sort fields are
  captured, non-DDR input errors clearly, and `build` warns on low resolution.
  See **COVERAGE.md** for the explicit captured / not-captured matrix.
- [x] **Multi-file solutions.** `build Summary.xml` (or list the XMLs) ingests
  all files into one DB; the web app accepts multi-drop. Cross-file references
  resolve via explicit FileReference markers only — external Perform Script,
  and field refs through external table occurrences (98.8% resolution measured
  on a 9-file production solution). External refs whose file is absent stay unresolved
  instead of silently mis-linking to same-id local objects.
- [x] **Explorer UX.** FMPerception-style flow in the browser: drop the whole
  DDR folder (every `*_fmp12.xml` loads, cross-file links resolve), filter by
  file, and click a script to read it as **full step text** — document order,
  block indentation (If/Loop/Else), comment steps dimmed, step/comment/call
  counts, copy per line / selection / whole script. Works in the web app, the
  exported report, and the CLI report alike.
- [x] **Call chain diagram.** Toggle any script's detail between Steps and a
  layered SVG call chain: callers flow in from the left (green), called
  scripts to the right (orange), the **full chain** in both directions
  (cycle-safe; each script appears once), externals dashed, per-level
  fan-out capped with "+N more", every node clickable to re-root.
- [x] **Navigate like an app.** Browser Back/Forward work while exploring;
  every entity has a deep link (`#e123`) that also works inside exported
  reports; `#health` opens the health report directly.
- [x] **Search in code.** Enter-search scans every calculation and script
  step; results show highlighted snippets and clicking a step match opens
  the script scrolled to that exact line.
- [x] **Sort the entity list.** A compact "Sort" popover on the list orders
  by Name or Referenced-by (and, when scripts are soloed, Steps / Calls /
  Complexity); sorting by a metric shows that number inline on each row.
  Lighter than a full table — the list stays the single browse surface.
- [x] **Solution health report.** Unused-field and orphan-script candidates,
  unresolved and ambiguous references, hotspots and biggest scripts — every
  list clickable, each downloadable as CSV, with the coverage caveats printed
  on the page. The call chain is downloadable as a standalone SVG.
- [x] **Call chain, expanded.** Edge semantics (solid Perform / long-dash
  PSoS / dotted trigger / dash-dot button, with tooltips), call-count weights
  (×N), entry-point badges, hover-highlight of connected nodes, in-chain
  search (matches surface out of "+N more"), click → steps preview below the
  chain with Re-root / Open fully, drag-pan + wheel-zoom (double-click
  resets), Copy as Mermaid, Download SVG, and a print stylesheet (Cmd+P →
  clean PDF of chain, script text, or health report).
- [x] **Share one insight.** Every entity has a Share button that downloads
  a small self-contained static HTML (no JavaScript inside): the script's
  steps, its call chain exactly as arranged on screen, and its references.
  Kilobytes — safe to mail or Slack without sharing the whole schema. For
  whole-solution sharing, drop an exported report on a shared drive and use
  deep links (`report.html#e123`).
- [ ] **Copy-link button.** One-click copy of an entity's deep link for the
  shared-drive workflow.
- [ ] **Union impact graph.** Select several scripts and see one merged
  call graph with shared dependencies emphasized — "the five scripts I'm
  about to change, and everything they touch". Its own session.
- [ ] **Annotations.** Mark entities (deprecated / refactor / reviewed) with
  notes; persisted per solution, exportable, embedded in shared reports.
  Viewer-wide, not chain-only; pairs with the health report.
- [ ] **Phase 3 — DDR diff.** Two snapshots in one DB → what changed between
  deploys (added/removed/modified fields, scripts, layouts).
- [ ] **Copy as FM snippet (fmxmlsnippet).** Copy a script (or selected
  steps) to the clipboard in FileMaker's snippet format so it pastes straight
  into Script Workspace. Prerequisite: retain the raw `<Step>` XML at parse
  time (the parser currently keeps only the rendered StepText) and convert
  DDR step XML to the clipboard format.
- [ ] **Edit → patch (idea).** Make selected changes in the viewer and emit
  them as input for the FileMaker upgrade tool to apply. Shares the raw-XML
  prerequisite with snippet copy; parked until the read-only explorer has
  proven itself.
- [ ] Health report: dead fields, orphan scripts, missing references, TO sprawl.

## Project structure

```
fm-ddr-analyzer/
├── README.md
├── QUERIES.md          # canonical cross-reference SQL recipes (for humans + AI)
└── fm_ddr/
    ├── web/index.html  # zero-install client-side web app (JS parser + viewer)
    ├── __init__.py
    ├── parse.py        # SAX streaming parser -> SQLite
    ├── schema.sql      # entities / refs / FTS schema
    ├── resolve.sql     # reference resolution + convenience views
    ├── report.py       # self-contained interactive HTML generator
    └── cli.py          # build / where / search / sql / stats / report
```

## Tech stack

| Concern | Choice | Why |
|---------|--------|-----|
| Parsing | `xml.sax` (expat) | Streams 400 MB UTF-16-LE files; ignores line structure |
| Storage | SQLite | Portable, queryable by AI/tools, no server |
| Search | FTS5 | Catch-all text search where structured extraction is incomplete |
| Language | Python 3.10+, stdlib only | No dependencies to install |
