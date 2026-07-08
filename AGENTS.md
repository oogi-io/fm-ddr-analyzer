# Working with FMSonar databases (for AI agents)

You are working in the FMSonar / fm-ddr-analyzer repo, or next to a SQLite
database it produced. These databases are cross-reference indexes of FileMaker
solutions, designed to be queried by AI. This file tells you how to do that
well.

## Getting a database

```bash
# from a FileMaker DDR export (Tools > Database Design Report > XML):
python3 -m fm_ddr.cli build path/to/Solution_fmp12.xml -o solution.db
# multi-file solutions: point at the manifest - ALL files land in one DB
python3 -m fm_ddr.cli build path/to/Summary.xml -o solution.db
# then query:
python3 -m fm_ddr.cli sql solution.db "SELECT ..."
```

`build` warns if reference resolution lands below 95% — usually an incomplete
multi-file build (missing sibling XMLs), not a broken solution.

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
  (script/layout group), `ext_file` (TO whose base table lives in another file).
- **`refs`** — every "source USES target" edge:
  `source_entity_id, context, target_kind, target_name, target_to_name,
  target_raw, target_file, target_entity_id` (NULL = unresolved),
  `ambiguous` (1 = several same-named candidates; pick deterministic but verify).
  Contexts: `calc, step_target` (field a step acts on — Set Field target etc.),
  `join_predicate, perform_script, go_to_layout, trigger, layout_object,
  field_reference, value_list_source, value_list_field, sort, to_reference,
  function_ref`.
- **`files`** — one row per FileMaker file in a multi-file solution.
- **`text_index`** — FTS5 over every name, calculation, and step text.

## Views are the API — prefer them over raw joins

- **`v_usage`** — the main surface: one row per reference with readable
  `source_kind/source_name/source_parent_name`, `target_kind/target_name`,
  `context`, `resolved`, `ambiguous`, `target_file`.
- **`v_unused_fields`**, **`v_orphan_scripts`** — candidates, NOT verdicts (see guardrails).
- **`v_unresolved`** — unresolved refs grouped; **`v_ambiguous`** — flagged picks.

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

## Honesty guardrails (non-negotiable)

- **Never report "unused" or "safe to delete" from `v_unused_fields` /
  `v_orphan_scripts` alone.** Usage inside ExecuteSQL strings, calculated
  names, custom menus, and anything outside the DDR (other systems, users
  running scripts from the menu) is structurally invisible. Run the FTS check
  and still present results as "candidates to review". Full matrix: COVERAGE.md.
- **Unresolved refs are usually legitimate**, not broken: scripts in files not
  present in the DB (`target_file` set), calculated layout destinations,
  unbound globals. Check `v_unresolved` shape before calling anything broken.
- **`ambiguous=1` means the resolver guessed** among same-named candidates —
  say so when those rows matter to the answer.
- Built-in FileMaker functions are deliberately NOT edges; find them via FTS.
- **The index is a model, not the whole DDR.** Auto-enter/validation details,
  layout geometry, custom-menu internals, and import/export orders are not
  extracted. For questions outside the model, say so and read the original
  DDR XML directly (stream it - it is large UTF-16-LE) rather than guessing.

## Practical notes

- Read-only. Never UPDATE/DELETE these databases; rebuild from the DDR instead.
- `calc_text` is capped (4k; 64k for custom functions). Full step text:
  `json_extract(extra_json,'$.step_text')` on script_step rows.
- Fields are duplicated by name across tables — always qualify or group by
  `base_table`; resolution goes through the table occurrence.
- Don't dump whole tables into context: query for the answer's shape first
  (GROUP BY / COUNT), then fetch only the rows you'll cite.
- The CLI has shortcuts worth using: `where` (resolved where-used),
  `search` (FTS), `stats` (health), `report` (interactive HTML),
  `snippet` (script → FileMaker-pasteable clipboard XML, macOS `--clip`).
