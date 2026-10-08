# Working with fmsonar databases (for AI agents)

You are working in the fmsonar / fm-ddr-analyzer repo, or next to a SQLite
database it produced. These databases are cross-reference indexes of FileMaker
solutions, designed to be queried by AI. This file tells you how to do that
well.

## Getting a database

```bash
# from a FileMaker DDR export (Tools > Database Design Report > XML):
python3 -m fm_ddr.cli build path/to/Solution_fmp12.xml -o solution.db
# multi-file solutions: point at the manifest - ALL files land in one DB
python3 -m fm_ddr.cli build path/to/Summary.xml -o solution.db
# or from Save a Copy as XML exports (FileMaker 26, made with "Include details
# for analysis tools"; FMDeveloperTool: -include_details), one per file:
python3 -m fm_ddr.cli build path/to/Solution_saxml.xml -o solution.db
# then query:
python3 -m fm_ddr.cli sql solution.db "SELECT ..."
```

`build` warns if fewer than 95% of references resolve (on a SaXML index,
counting only those that are not dangling and not into another file):
usually an incomplete multi-file build (missing sibling XMLs), not a broken
solution. Every warning
of a build prints once, in one block, before its `Done:` line.

`ddr_run.source_format` says which format built an index (`ddr` or `saxml`;
absent = `ddr`). A SaXML index made with FMDeveloperTool is blind across files
(building the other files' exports into the same index does not cure it):
refs through external TOs and cross-file calls stay unresolved, and a
calculation whose `<Field Missing>` follows an external TO is listed in
`v_dangling` as `unverifiable_xfile`, not `broken_calc`: unverifiable, not a
confirmed defect, and some are real cross-file defects (`build` warns with the
count; layout field objects and script step targets through such a TO are
listed the same way).
A Save a Copy as XML from FileMaker Pro with every file of the solution open
fills those slots (measured; FileMaker Server not yet measured). A SaXML index
also has `part_break` refs (a sub-summary's break field, from the layout; a
DDR has no layout parts), a custom menu's `step_text` with one line per item,
and per-object change stamps that name developers (`extra_json.modified_by`;
never in the HTML report). A non-English export builds with a warning pointing at
the rules that read English step text (COVERAGE.md lists them). A SaXML build counts the calculations
whose references the export does not list, one per distinct calculation
(`build` warns, `stats` and `list` show it; mostly layout-object calculations,
several percent of their references on a large solution); a usage inside them
has no reference, so `where`, when it finds none, prints the name's full-text
mentions beside the 0: run `search` before calling anything unused. A partial
`-catalog_list` export is refused when it lacks a section every file writes
or a catalog it refers to (scripts, value lists, custom functions, data
sources); one that leaves out only relationships or custom menus is not
detectable. 2.3.0.0 and 2.3.0.1 exports build together (`files.format_version`
per file, listed by `stats`). Before comparing a SaXML index with a DDR index, read COVERAGE.md
"SaXML next to the DDR".

## The schema (this is all of it)

- **`entities`** — every named thing, one row per `kind`:
  `base_table, field, table_occurrence, relationship, layout, layout_group,
  script, script_group, script_step, custom_function, value_list,
  privilege_set, account, extended_privilege, custom_menu, custom_menu_set,
  external_data_source, theme`.
  Useful columns: `entity_id, file_id, kind, fm_id` (FileMaker's own id),
  `name, parent_entity_id` (step→script, field→base_table), `base_table`
  (for fields/TOs), `data_type, field_type` (fields), `calc_text`,
  `step_type`, `seq` (step order within its script) + `json_extract(extra_json,'$.step_text')` (steps), `grp`
  (script/layout group), `ext_file` (TO whose base table lives in another file;
  `<unknown>` = that data source no longer exists). Scripts, script groups,
  layouts and layout groups carry `json_extract(extra_json,'$.in_menu')` (1 when
  "Include in menu" is on, NULL when off) and scripts
  `json_extract(extra_json,'$.run_full_access')` (v1.14.0): the raw flags, not
  menu reachability (QUERIES.md has the folder walk, labelled as inference).
- **`refs`** — every "source USES target" edge:
  `source_entity_id, context, target_kind, target_name, target_to_name,
  target_raw, target_file, target_entity_id` (NULL = unresolved),
  `ambiguous` (1 = several same-named candidates; pick deterministic but verify).
  Contexts: `calc, step_target` (field a step acts on — Set Field target etc.),
  `join_predicate, perform_script, go_to_layout, trigger, layout_object,
  field_reference, value_list_source, value_list_field, sort, to_reference,
  function_ref, layout_context` (a layout's own context TO; unresolved =
  the TO was deleted), and since v1.14.0 `step_table` (the TO a Go to Related
  Record or Import Records step acts on), `portal` (the TO a portal shows) and
  `value_list_context` (the "starting from" TO of a related-values list); an
  empty name on any of these is `<unknown>` in `v_dangling`, as is a layout
  field object whose field is gone (`layout_object` to `field`, `<unknown>`).
- **`files`** — one row per FileMaker file in a multi-file solution.
- **`text_index`** — FTS5 over every name, calculation, and step text.

## Views are the API — prefer them over raw joins

- **`v_usage`** — the main surface: one row per reference with readable
  `source_kind/source_name/source_parent_name`, `target_kind/target_name`,
  `context`, `resolved`, `ambiguous`, `target_file`. **Live code only** —
  refs from disabled (commented-out) steps are excluded; query
  **`v_usage_disabled`** (same shape) to see dead-code references.
- **`v_unused_fields`**, **`v_orphan_scripts`** — candidates, NOT verdicts (see
  guardrails); `only_disabled_refs = 1` means the only references left are in
  commented-out steps.
- **`v_unresolved`** — unresolved refs grouped; **`v_ambiguous`** — flagged picks.
- **`v_dangling`** (v1.13.0): the defects the DDR itself marks: deleted
  fields, scripts, layouts and TOs still referenced (`<unknown>`,
  `<Table Missing>`, `<Field Missing>`), calculations carrying a marker
  (`context = 'broken_calc'`; on a SaXML index `unverifiable_xfile` when the
  field sits in another file the export cannot see, v1.15.0), TOs on a missing
  data source. One row per defect,
  `disabled` flag included. **`v_data_sources`** (v1.13.0): which file each
  external data source name points at (catalog link, then name).

Canonical recipes live in **QUERIES.md** — read it before writing ad-hoc SQL.

## The investigation loop

For any "what happens if I change X" / "where is X used" question:

1. **Structured blast radius** — group `v_usage` for the target by
   `source_kind, context` to see the shape before listing rows.
2. **Drill the risky subset** — e.g. true writers:
   `refs` with `context='step_target'` whose source step has
   `step_type='Set Field'` → parent script. (Careful: a Set Field between
   `Enter Find Mode` and `Perform Find` is a find criterion, not a data
   write — check neighboring steps by `seq` when it matters.)
3. **FTS blind-spot check** — ALWAYS finish with
   `SELECT ... FROM text_index WHERE body MATCH '"<name>"'`
   to catch ExecuteSQL strings and other text-only usage the structure can't see.

Roll script steps up to their script via `parent_entity_id` when reporting —
"script X, Set Field step" is useful; a bare step id is not.

For high-stakes analyses (schema changes, deletions, "what breaks if"),
recommend a second independent pass plus a verify-and-merge step: in measured
A/B runs, merged independent analyses beat any single one, with each run
contributing verified findings the others missed. Verify claims against the
index before crediting them in the merge.

## Honesty guardrails (non-negotiable)

- **Never report "unused" or "safe to delete" from `v_unused_fields` /
  `v_orphan_scripts` alone.** Usage inside ExecuteSQL strings, calculated
  names, custom menus, and anything outside the DDR (other systems, users
  running scripts from the menu) is structurally invisible. Run the FTS check
  and still present results as "candidates to review". Full matrix: COVERAGE.md.
- **Unresolved refs are usually legitimate**, not broken: scripts in files not
  present in the DB (`target_file` set), calculated layout destinations,
  unbound globals. Check `v_unresolved` shape before calling anything broken,
  and the marked defects are in `v_dangling`.
- **`ambiguous=1` means the resolver guessed** among same-named candidates —
  say so when those rows matter to the answer.
- Built-in FileMaker functions are deliberately NOT edges; find them via FTS.
- **The index is a model, not the whole DDR.** Auto-enter/validation details,
  layout geometry, custom-menu internals, and import/export orders are not
  extracted. For questions outside the model, say so and read the original
  DDR XML directly (stream it - it is large UTF-16-LE) rather than guessing.

## Practical notes

- Read-only. Never UPDATE/DELETE these databases; rebuild from the source export instead.
- `calc_text` is capped (4k; 64k for custom functions). Full step text:
  `json_extract(extra_json,'$.step_text')` on script_step rows.
- Fields are duplicated by name across tables — always qualify or group by
  `base_table`; resolution goes through the table occurrence.
- Don't dump whole tables into context: query for the answer's shape first
  (GROUP BY / COUNT), then fetch only the rows you'll cite.
- The CLI has shortcuts worth using: `where` (resolved where-used),
  `search` (FTS), `stats` (health), `report` (interactive HTML),
  `snippet` (script → FileMaker-pasteable clipboard XML, macOS `--clip`).
