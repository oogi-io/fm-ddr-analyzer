---
name: fmsonar
description: Analyze FileMaker solutions via their DDR (Database Design Report). Build a queryable SQLite index and answer cross-reference questions - "where is this field/script/table occurrence/custom function used?", "which scripts WRITE to this field?", "what breaks if I rename X?", unused-field and orphan-script candidates, call chains. Use when the user mentions a FileMaker DDR, a *_fmp12.xml file, or asks structural questions about a FileMaker solution. Works from any directory; databases are cached centrally.
---

You analyze FileMaker solutions using the `fm-ddr` CLI (fmsonar's engine).
Never hand-parse DDR XML - build the SQLite index and query it.

## 1. Locate or build the database

Databases live in the central cache `~/.fmsonar/dbs/<solution>.db` - create the
directory if needed. `fm-ddr list` shows what is cached (label, build time,
parser version, staleness) - run it first when unsure what exists. Reuse a
cached DB if it is newer than its source DDR and not flagged stale.

To build, you need the user's DDR export (FileMaker: Tools > Database Design
Report > XML). If you don't know where it is, ask - don't scan the whole disk.

```bash
mkdir -p ~/.fmsonar/dbs
# multi-file solutions: ALWAYS build from Summary.xml so cross-file references resolve
fm-ddr build "/path/to/DDR/Summary.xml" -o ~/.fmsonar/dbs/<solution>.db --label "<solution> <date>"
# single-file solutions: point at the *_fmp12.xml directly
```

`build` warns below 95% resolution - usually a missing sibling XML of a
multi-file solution, not a broken solution. If `fm-ddr` is not on PATH:
`pipx install fmsonar` (PyPI), or fall back to `python3 -m fm_ddr.cli` from a
checkout of github.com/oogi-io/fm-ddr-analyzer.

## 2. Query

```bash
fm-ddr where  <db> "TO::Field"      # resolved where-used (field/script/layout/TO/CF)
fm-ddr cascades <db> [table]        # cascade deletes INTO a table (v1.10)
fm-ddr valuelist <db> "<name>"      # value-list definition + bindings (v1.10)
fm-ddr search <db> "text"           # FTS across every calc / step / name
fm-ddr stats  <db>                  # counts + resolution health
fm-ddr sql    <db> "SELECT ..."     # anything else
```

The SQL surface: `entities` (every named thing; steps carry `step_type`,
`seq` = order within script, full text via
`json_extract(extra_json,'$.step_text')`), `refs` (source USES target;
`context` says how), and the views - **`v_usage`** (readable edges),
`v_unused_fields`, `v_orphan_scripts`, `v_unresolved`, `v_ambiguous`.

Key contexts: `step_target` = the field a step acts on (Set Field's write
target - combine with `step_type='Set Field'` for true writers, but note a
Set Field between Enter Find Mode and Perform Find is a find criterion, not a
write). `calc` = used in a calculation. `perform_script`/`trigger` = script
calls. Full recipes: QUERIES.md, installed next to this file (also in the
repo, with COVERAGE.md stating exactly what the index does not capture).

## 3. The investigation loop

1. **Blast radius first**: group `v_usage` for the target by
   `source_kind, context` before listing rows.
2. **Drill the subset that matters** (e.g. writers via `step_target` +
   `step_type='Set Field'`, rolled up to the parent script).
3. **ALWAYS finish with the FTS blind-spot check**:
   `fm-ddr search <db> '"<name>"'` - ExecuteSQL strings and calculated names
   are structurally invisible.

## 3b. Debugging protocol - the index must not narrow your vision

A precise index invites tunnel vision: name-lookup jumps straight to the one
artefact someone mentioned, and the surrounding mechanism never enters view.
When the task is *analyze / debug / find the mechanism* (not a one-fact
lookup), these are mandatory, one query each:

1. **Survey before you dive** - list the script family
   (`name LIKE '%<domain>%'`) so callers, siblings, and dispatch scripts are
   on the table before deep-reading anything.
2. **Never conclude from a single script - climb to its callers** (`v_usage`
   with `target_kind='script'`) and read the controller's body too. The
   mechanism usually lives one level up.
3. **Sweep the `$$globals` the script writes** (QUERIES.md "Variable
   hygiene"): a global that is written but never read anywhere is a classic
   latent bug and costs one round-trip to detect.
4. **Report FileMaker `fm_id`, never the internal `entity_id`** - findings
   must be checkable inside FileMaker.
5. **If the mechanism involves UI behavior, query the layout objects**
   (v1.3.0+): every object is an entity - buttons carry
   `extra_json.step_text` (launch params), `hide_calc`, `tooltip_calc`.
   The layout's aggregated body is still one query:
   `SELECT body FROM text_index WHERE kind='layout' AND name=...`.
   A flag read on a layout but set by no launch anywhere is a bug of the
   same family as the write-only global.
5b. **Data flows through FOUR write mechanisms, not one** (v1.9.1+) - a
   field is populated by Set Field steps (`step_target`), by an **auto-enter
   calc** (`context='auto_enter'`; `auto_enter.calc_active=false` = dead
   residue, refs `disabled=1`), by a **lookup** (`context='lookup'`,
   `auto_enter.lookup_source`; dead lookups follow the same residue rule),
   or by a **script trigger** firing on a layout OR FILE event. Sweep all
   four: a writer sweep that stops at Set Field misses auto-enter-heavy
   tables entirely. Triggers ONLY via `v_triggers` (sourced from
   layouts/objects - a script-parent join returns zero rows and looks
   complete; `layout_name IS NULL` rows are the file's startup/shutdown
   scripts). For any perf or caching question, check `stored`/`is_global`
   per cited field first: a stored calc cannot reference an
   unstored/global/related field, and that constraint usually IS the
   mechanism.

6. **The DDR is schema, not data** - verify flag values / sort orders /
   config rows against the live system, and label findings DDR-derived vs
   live-verified.
7. **For high-stakes work (schema changes, deletions, "what breaks if"),
   recommend a second independent analysis plus a verify-and-merge pass.**
   In measured A/B runs, merged independent analyses beat any single one:
   each run contributed verified findings the others missed. Verify each
   claimed finding against the index before crediting it in the merge. Also
   use the most capable model available for mechanism-hunting: in the same
   runs the model mattered more than the tooling, and no index closes a
   comprehension gap.

Shortcut: `fm-ddr investigate <db> "<script>"` runs steps 2-4 (plus layout
launch sites with their params, record operations, and the full body) in ONE
command - reach for it first when a script is the entry point. It also always
prints a recursive CHAIN rollup (every script reachable from this one, with
its deleters tiered and creators counted); add `--chain` for the full
per-site census. For "what does this scheduled job / controller touch",
that rollup IS the data census - don't hand-roll step-type counts.

6b. **Field-writer sweeps miss record operations** (creating/deleting a
record writes no field). `fm-ddr mutations <db> [--like text]` inventories
every record-op step with a context clue and confidence tier (confident /
likely / check). The clue is the nearest Go to Layout - runtime state a
script can inherit from its caller - so treat 'likely' as a strong hint and
'check' rows as the to-verify list. Find-mode record ops are find requests,
not mutations (tagged, never dropped).

## 4. Honesty guardrails (non-negotiable)

- Never report "unused"/"safe to delete" from the views alone: ExecuteSQL
  strings, calculated names, custom menus, and everything outside the DDR
  (Data API/OData clients, users running scripts from the menu) are invisible.
  Present candidates, run the FTS check, state the caveats.
- Unresolved refs are usually legitimate (other files, calculated
  destinations, unbound globals) - check `v_unresolved` before calling
  anything broken. `ambiguous=1` = the resolver guessed among same-named
  candidates; say so when it matters.
- Read-only: never modify a DB; rebuild from the DDR instead.

## 5. Extras

- Interactive HTML for the user: `fm-ddr report <db> -o out.html` (self-contained; send it to them).
- Copy a script as a FileMaker-pasteable snippet: `fm-ddr snippet <ddr.xml> "Script Name" --clip` (macOS).
- Skill freshness: `fm-ddr install-skill --check` compares this installed skill
  against the one shipped with your fm_ddr install (`--remote` = against GitHub
  main). If it reports drift, tell the user and offer to update.
- The web app for humans is https://fmsonar.com (same engine, in-browser).
