---
name: ddr
description: Analyze a FileMaker DDR (Database Design Report) — build a queryable SQLite index and answer cross-reference questions ("where is field/script/TO/custom function X used?", "what does this script touch?", "unused fields", "orphan scripts"). Use when the user hands over a DDR file or *_fmp12.xml, names a FileMaker solution to analyze, asks where something is used in a FileMaker solution, or wants an interactive HTML map of a solution. Powered by projects/fm-ddr-analyzer.
---

You are answering FileMaker schema / cross-reference questions using the **fm-ddr-analyzer** tool (``). Output English. The tool parses a DDR XML into SQLite; you query that DB. Do not hand-parse the DDR XML yourself — build the DB and query it.

## 0. Parse the argument

The arg is one of:
- **A DDR path** (`.../Something_fmp12.xml`) → build from it.
- **A solution name** → find its DDR (see §1).
- **A cross-reference question** ("where is CONTACT::email used?", "who calls script X?") → ensure a DB exists, then query it (§3).
- **Nothing** → ask which solution/DDR, or offer to analyze one you can find.

## 1. Locate the DDR and the DB

DDR files are large UTF-16-LE `*_fmp12.xml`. Locate them with `find . -iname '*_fmp12.xml' -o -iname 'Summary.xml'`.

**Build DBs into** `dbs/<solution>.db` (that dir is gitignored via `*.db`). Reuse an existing DB if it is newer than its source DDR; otherwise rebuild. **Multi-file solutions: ALWAYS build all files into one DB** — point `build` at the `Summary.xml` manifest (it expands to every linked `*_fmp12.xml`) or list the XMLs. Cross-file refs (external Perform Script, fields through external TOs) only resolve when all files are present; a partial build leaves them unresolved (never mis-linked).

## 2. Build

Run from the tool dir. No dependencies (stdlib only).

```bash
python3 -m fm_ddr.cli build "/abs/path/Solution_fmp12.xml" -o dbs/solution.db --label "solution <date>"
# multi-file solution: use the manifest
python3 -m fm_ddr.cli build "/abs/path/Summary.xml" -o dbs/solution.db --label "solution <date>"
```

`build` prints entity + reference counts and WARNS if resolution <95% (usually an incomplete multi-file build or an unmapped FM-version construct). Sanity-check counts. Measured: 34MB in ~1s, a 510MB 9-file solution in ~26s.

## 3. Query

Prefer the built-ins for common asks, drop to `sql` for anything else. Full recipe list: `QUERIES.md` — read it before writing ad-hoc SQL.

```bash
python3 -m fm_ddr.cli where  dbs/solution.db "CONTACT::email"   # where used (field/script/layout/TO/CF)
python3 -m fm_ddr.cli search dbs/solution.db "ExecuteSQL"        # FTS across calcs/steps/names
python3 -m fm_ddr.cli stats  dbs/solution.db                     # counts + resolution health
python3 -m fm_ddr.cli sql    dbs/solution.db "SELECT * FROM v_unused_fields LIMIT 30"
```

The main SQL surface is the **`v_usage`** view (readable source/target per edge). Roll script steps up to their script via `parent_entity_id` (a ref's source is often a `script_step`; its parent is the `script`).

## 4. Interpret honestly — do NOT cry wolf on unresolved refs

~98% of references resolve on a healthy solution. The unresolved ones are **usually legitimate, not broken**:
- `perform_script` unresolved → script lives in **another file** (external), or the DDR is single-file. Not a bug.
- `go_to_layout` unresolved → **calculated** layout destination. Not a bug.
- `layout_object` field unresolved → **global / unbound** field. Usually fine.
- Built-in FileMaker functions are **not** stored as edges by design — use `search` (FTS) to find built-in usage.

Only flag a *genuinely* missing internal target (a script/field that should exist in this file but doesn't) as a real broken reference. Check `v_unresolved` for the shape, then verify before calling anything broken. Also check `v_ambiguous` (same-named candidates; pick deterministic but uncertain) and **read `COVERAGE.md`** before ANY "unused"/"unreferenced" claim — ExecuteSQL strings, calculated names, and custom-menu invocations are structurally invisible; `search` (FTS) is the fallback.

`v_unused_fields` / `v_orphan_scripts` are **hints, not verdicts**: a script with no caller may still run from a button, menu, or external file; a field with no ref may be display-only on a layout via a path not captured. Present them as "candidates to review," never "safe to delete."

## 5. Interactive map (when exploration is broad)

For "give me a map of X" or open-ended exploration, generate the HTML viewer and send it:

```bash
python3 -m fm_ddr.cli report dbs/solution.db -o dbs/solution.html
```

Then `SendUserFile` it (display: render). It is self-contained (data embedded), so it opens anywhere.

## 6. FileMaker version note

The parser is tolerant — it ignores unknown tags and won't crash on an unfamiliar FM version. But a much older/newer DDR may resolve at a lower rate because a new reference type isn't mapped yet. If `stats` shows resolution well below ~95%, note the `ddr_version` (stored in `ddr_run`) and treat it as a parser-coverage gap for that version, not a bad solution. Recon a new version's vocabulary by dumping its tags/attrs (see the tool's git history for the recon one-liner).

## Guardrails

- **Read-only.** This tool never touches the live FileMaker file. It reads a DDR export. Never edit FM artefacts as part of DDR analysis.
- DDR files and `.db`/`.html` outputs are gitignored — they hold client schema; keep them out of git.
- State findings directly and honestly, with the COVERAGE.md caveats where relevant.
