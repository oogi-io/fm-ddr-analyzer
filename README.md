# fmsonar

**Ping your FileMaker solution — every reference echoes back.**
One engine, two interfaces: **an explorer for you**, **a queryable index for your AI**.

*Live at **[fmsonar.com](https://fmsonar.com)** · [![PyPI](https://img.shields.io/pypi/v/fmsonar)](https://pypi.org/project/fmsonar/) `pipx install fmsonar` · repo/engine name: `fm-ddr-analyzer`*

## The problems this solves

1. **"Where is this actually used?"** A FileMaker solution's only complete map is
   its Database Design Report — a huge (often 400+ MB) UTF-16 XML export. Answering
   one cross-reference question by hand means text-searching that file and hoping
   you didn't miss a calculated name, a trigger, or a button parameter.
2. **Your AI assistant *can* work with the raw XML — expensively.** It works;
   we measured it. But every question costs a fresh pass and a hand-written
   parser, an analysis needs dozens of questions, and the hard-won structure is
   thrown away afterwards. An index turns that repeated cost into one parse and
   millisecond queries, at roughly a third of the time and tokens. It's a
   projection, not the whole DDR: ~98% of references resolve as typed edges,
   text-only usage (ExecuteSQL, calculated names) is caught by full-text search,
   and [COVERAGE.md](COVERAGE.md) states exactly what isn't captured — for those
   cases the raw XML is still there, and the AI skill says when to go back to it.
3. **A DDR is a client's entire schema — table names, business logic, everything.**
   Sending that to someone else's server is a confidentiality decision most client
   work doesn't allow. fmsonar.com is a web *page*, not a web service: parsing runs
   in your browser, and no DDR bytes are transmitted — verifiably (watch the network
   tab, or go offline and it still works), not as a promise. Self-hosting the
   single-file app removes even that dependency. See [SECURITY.md](SECURITY.md).

One engine answers all three. It parses the DDR **once** into a normalized index
(entities, reference edges, full-text) — then every question is instant.

## Which tool for which problem

| You want to… | Use | You need |
|---|---|---|
| Explore a solution visually — search, call chains, health report | **[fmsonar.com](https://fmsonar.com)** (browser) | nothing — no install, no upload |
| Query from the shell, script it, export reports | **`fmsonar` CLI** | Python 3.10+ and pipx |
| Let your AI assistant answer cross-reference questions | **CLI + the Claude Code skill** | the CLI (above) + Claude Code |

The three build on each other in that order — start in the browser, install the
CLI when you want it scriptable, add the skill when your AI should use it too.

## Path 1 — Explore in the browser (no install)

1. In FileMaker Pro (advanced tools on): **Tools → Database Design Report → XML**, all files.
2. Open **[fmsonar.com](https://fmsonar.com)**.
3. Drag the DDR folder onto the page.

Parsing runs entirely client-side in a Web Worker — **nothing is uploaded**.
Seconds later the whole solution is explorable: every name and line of code
searchable, references inbound and outbound, complete scripts readable, call
chains as diagrams, a health report, shareable single-entity HTML exports, and
copy-any-script-back-to-FileMaker as a pasteable snippet.

Prefer to self-host? The whole app is one file — serve `fm_ddr/web/index.html`
as a static page; it works identically.

## Path 2 — The CLI

Prerequisites: Python 3.10+ and [pipx](https://pipx.pypa.io). On macOS, pipx
comes from Homebrew:

```bash
brew install pipx
```

Install fmsonar (pure standard library, no dependencies):

```bash
pipx install fmsonar
```

Build the index once — point it at the `Summary.xml` manifest for a multi-file
solution, or a single `*_fmp12.xml`:

```bash
fmsonar build /path/to/DDR/Summary.xml -o solution.db
```

Then query it. Each command is copy-paste safe on its own:

| Command | Answers |
|---|---|
| `fmsonar where solution.db "CONTACT::email"` | where is this field / script / layout / TO / custom function used |
| `fmsonar search solution.db "GetContainerAttribute"` | full-text across every calc, script step, and name |
| `fmsonar investigate solution.db "Some Script"` | one-shot script report: callers, layout launch params, callees, $$global hygiene, full body |
| `fmsonar report solution.db -o solution.html` | self-contained interactive HTML viewer |
| `fmsonar stats solution.db` | entity counts + reference-resolution health |
| `fmsonar sql solution.db "SELECT * FROM v_unused_fields LIMIT 20"` | anything — see [QUERIES.md](QUERIES.md) for the recipe book |

Because the output is plain SQLite, anything can query it — `sqlite3`,
Datasette, DB Browser, or an AI.

From a clone, without installing: replace `fmsonar` with `python3 -m fm_ddr.cli`
in any command above.

## Path 3 — Your AI assistant

Prerequisite: Path 2 (the CLI on your PATH). Then give Claude Code the skill —
one command, works from any directory afterwards:

```bash
fmsonar install-skill
```

The skill teaches the assistant the schema, the query recipes, and — learned the
hard way, see the eval story — the **investigation protocol**: survey before
deep-reading, climb to callers, sweep the globals a script writes, read layout
bodies for UI mechanisms, report real FileMaker ids.

Then, wherever you're working: *"analyze the DDR in ~/Desktop/MyDDR — which
scripts write to CTC::email?"* Claude Code builds the index into a central cache
(`~/.fmsonar/dbs/`) and answers with SQL-backed evidence. Other AI tools
(Cursor, Copilot): they read [AGENTS.md](AGENTS.md) next to a built database.

## Updating

A pipx install doesn't auto-update. Versions are bumped on every release, so:

```bash
pipx upgrade fmsonar
fmsonar install-skill
```

The second command refreshes the Claude Code skill to match. Check for skill
drift anytime with `fmsonar install-skill --check` (offline, against your
installed version) or `--remote` (against GitHub main). Installed from the git
URL instead of PyPI? Use `pipx reinstall fmsonar`. A git clone updates with
`git pull`. Every index is stamped with the parser version that built it —
querying through a stale index warns loudly instead of silently missing newer
reference types.

## Performance and scope

- **Input:** DDR XML — one file, several, or a `Summary.xml` manifest. Both
  parsers (Python CLI and the browser's JS twin, parity-tested edge by edge)
  stream with SAX, so file size doesn't matter. Measured: a 510 MB 9-file
  production solution builds in ~26 s; the 416 MB main file parses in-browser
  in ~7 s using ~80 MB of memory.
- **Output:** one `.db` SQLite file — `entities`, a generic `refs` edge table
  (the heart of "where used"), and an FTS5 full-text index as a catch-all.
- **General-purpose:** no solution-specific assumptions; validated against
  multiple unrelated production solutions.

## Data model

One database holds a whole solution — all files of a multi-file solution share
one entity space, so cross-file references resolve. The schema is also
snapshot-aware (`ddr_run`) so a future diff feature can store several DDR
exports side by side; today `build` always writes a fresh single-snapshot DB
(diffing is on the roadmap).

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

**Reference contexts** (`refs.context`): `calc`, `step_target` (the field a step
writes to — e.g. Set Field), `join_predicate`, `perform_script`, `go_to_layout`,
`trigger`, `layout_object`, `field_reference`, `value_list_source`,
`value_list_field`, `sort`, `to_reference`, `function_ref`.

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
- [ ] **Signed helper installer.** Replace the unsigned zip / curl|bash
  install paths with a signed + notarized .pkg (Developer ID) so macOS
  installs the snippet watcher without any Gatekeeper friction. Parked
  until a dedicated signing session.
- [ ] **Phase 3 — DDR diff.** Two snapshots in one DB → what changed between
  deploys (added/removed/modified fields, scripts, layouts).
- [x] **Copy as FM snippet (web + CLI).** In the web app, every script has a
  "Copy FM snippet" button that re-streams the source file, extracts that
  script's raw steps, and copies fmxmlsnippet XML as text (byte-identical to
  FileMaker's own clipboard copy). Browsers cannot write FileMaker's private
  clipboard flavor, so paste needs a one-time bridge. Pick any: the bundled
  helpers in `helpers/` (macOS `.command`, Windows `.ps1` — also downloadable
  from the app after copying), `fm-ddr clip` (converts clipboard text in
  place), or FmClipTools if you already use it.
- [x] **Copy as FM snippet (CLI, macOS).** `fm-ddr snippet DDR.xml "Script
  Name" --clip` transforms a script's DDR steps into FileMaker's clipboard
  format and places it on the private XMSS pasteboard flavor — paste straight
  into Script Workspace. The transform reproduces FileMaker's own copied
  output byte-for-byte (268/268 steps on the reference script) and is
  paste-verified in Script Workspace; see SNIPPET_FORMAT.md. Browsers cannot set
  the XMSS flavor, so the web app cannot paste directly — CLI only.
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

---

[Thomas De Smet](https://oogi.io) · [tdesmet@oogi.io](mailto:tdesmet@oogi.io) · MIT

*FileMaker and Claris are trademarks of Claris International Inc. fmsonar is an
independent tool, not affiliated with or endorsed by Claris. See
[SECURITY.md](SECURITY.md) for the privacy and threat model.*
