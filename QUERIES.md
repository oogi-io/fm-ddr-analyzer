# Cross-reference query recipes

The database is designed to be queried directly. Run any of these with:

```bash
python3 -m fm_ddr.cli sql solution.db "<SQL>"
# or: sqlite3 solution.db "<SQL>"
```

## Investigation protocol (read this before debugging with the index)

A precise index invites tunnel vision: name-lookup jumps you straight to the one artefact
someone mentioned, and the surrounding mechanism never enters view. In an A/B test on a real
production bug, an analyst using raw DDR text-searching out-performed one using this index —
not because the queries were worse, but because the slow tool forced a survey pass that
surfaced the controller script, while the index answered the narrow question and the
investigation stopped there.

When the task is *analyze / debug / find the mechanism* (not a one-fact lookup), do these in
order — each is one query:

1. **Survey before you dive.** List the script family (`name LIKE '%<domain>%'`) so callers,
   siblings, and dispatch scripts are on the table before you deep-read anything.
2. **Never conclude from a single script — climb to its callers** (`v_usage`, recipe below).
   The mechanism you're hunting usually lives in the controller, one level up.
3. **Sweep the `$$globals` the script writes** ("Variable hygiene" recipe below). Write-only
   globals are latent bugs and cost one round-trip to detect.
4. **Report FileMaker ids (`fm_id`), never the index's internal `entity_id`** — findings must
   be checkable inside FileMaker.
5. **The DDR is schema, not data.** Flag values, sort orders, and config rows are record
   data — verify data-dependent hypotheses against the live system, and say which findings
   are DDR-derived vs. live-verified.

The main surface is the **`v_usage`** view (one row per reference edge, with
readable source/target names) and the **`text_index`** FTS5 table (catch-all
search). `refs.target_entity_id` is NULL for unresolved (external/calculated)
targets.

## "Where is X used?"

Where is a **field** used (calcs, scripts, layouts, relationships)?

```sql
SELECT context, source_kind, source_parent_name, source_name
FROM v_usage
WHERE target_kind = 'field' AND target_raw LIKE 'CONTACT::email';
-- or match the leaf name only: WHERE target_kind='field' AND target_name='email'
-- CAVEAT: the leaf-name form lumps together same-named fields from DIFFERENT
-- tables (every table's `id`, `email`, ...). Qualify with the table occurrence
-- (target_raw 'TO::field') or GROUP BY base_table unless you truly mean all of
-- them. `Set Field` writes specifically: add AND context='step_target'.
```

Who **calls a script** (and via triggers)?

```sql
SELECT context, source_kind, source_parent_name, source_name
FROM v_usage
WHERE target_kind = 'script' AND target_name = 'Navigate to Dashboard';
```

Where is a **table occurrence** referenced (relationships, layouts, TO refs)?

```sql
SELECT context, source_kind, source_parent_name, source_name
FROM v_usage WHERE target_kind = 'table_occurrence' AND target_name = 'contact_INT';
```

Where is a **custom function** called?

```sql
SELECT DISTINCT source_kind, source_parent_name, source_name
FROM v_usage WHERE target_kind = 'custom_function' AND target_name = 'CustomList';
```

Which **layouts** does a script navigate to?

```sql
SELECT source_parent_name AS script, target_name AS layout
FROM v_usage WHERE context = 'go_to_layout';
```

## "What does X depend on?" (outbound)

Everything a given **script** touches (roll steps up to their script):

```sql
SELECT u.context, u.target_kind, u.target_name
FROM refs r
JOIN entities step ON step.entity_id = r.source_entity_id
JOIN entities scr  ON scr.entity_id = step.parent_entity_id AND scr.kind='script'
JOIN v_usage u ON u.ref_id = r.ref_id
WHERE scr.name = 'Navigate to Dashboard';
```

All fields referenced by a **calculation field**:

```sql
SELECT target_raw FROM v_usage
WHERE source_kind='field' AND source_name='FullName_c' AND target_kind='field';
```

## "Read a full script body" (in order)

Every script step is stored as a `script_step` entity under its parent script,
with a `seq` ordinal and the FileMaker-readable StepText in
`extra_json.step_text`. So you can dump a complete script in execution order —
no need to re-stream the raw DDR XML just to read a script:

```sql
SELECT s.seq, s.step_type, json_extract(s.extra_json,'$.step_text') AS step_text
FROM entities s
JOIN entities scr ON scr.entity_id = s.parent_entity_id AND scr.kind='script'
WHERE scr.name = 'Navigate to Dashboard' AND s.kind='script_step'
ORDER BY s.seq;
-- LIKE 'Navigate%' if you don't want to type the full signature;
-- add AND json_extract(s.extra_json,'$.step_text') LIKE '%SomeField%'
-- to jump straight to the steps that mention a variable/field.
```

`extra_json.step_text` is the full, untruncated StepText (the HTML report
truncates for display, but the DB does not). This is the recipe to reach for
when you need to reason about a script's actual logic — reserve raw XML
streaming for step attributes that fmsonar doesn't capture.

## Layout objects (v1.3.0+): buttons, hide conditions, launch params — structured

Every layout object is an entity (`kind='layout_object'`), nested under its layout
(panels/portals/groups parent their children). Buttons carry their launch step and params in
`extra_json.step_text`; hide conditions in `extra_json.hide_calc`; tooltips in
`extra_json.tooltip_calc`; plus `object_type`, `key`, `bounds`.

Objects on a layout, with what each button launches and when it hides:

```sql
SELECT o.name, json_extract(o.extra_json,'$.object_type') AS type,
       json_extract(o.extra_json,'$.step_text')  AS launches,
       json_extract(o.extra_json,'$.hide_calc')  AS hidden_when
FROM entities o JOIN entities l ON l.entity_id = o.parent_entity_id
WHERE l.kind='layout' AND l.name = 'Admin - Task Options' AND o.kind='layout_object';
-- nested objects hang under their panel/portal, not the layout: recurse on
-- parent_entity_id (or search the layout body blob) when hunting deep objects
```

References from inside an object (button script params, conditional formatting, tooltips)
attribute to the OBJECT as source, so `v_usage` shows which button — not just which layout —
touches a field or calls a script.

One-shot script neighborhood (callers + layout launch params + callees + $$global hygiene + body):

```bash
python3 -m fm_ddr.cli investigate solution.db "Task Option Picker Continue"
```

## "Read a full layout body" (buttons, hide conditions, launch params)

Layouts have readable bodies too — the FTS table stores each layout's searchable text
(object calcs, hide conditions, tooltips, button script-parameters) as one blob:

```sql
SELECT body FROM text_index WHERE kind='layout' AND name = 'Admin - Task Options';
```

Read this like you'd read a script body whenever the mechanism involves UI behavior —
what a button passes to its script, when something is hidden, how a picker is launched.
Launch parameters and `Session.GetValue(...)` / `$$flag` gates live here, NOT in
`script_step` rows, so querying step_text for them returns nothing. When the body shows a
session/global flag gating visibility, immediately FTS that flag name solution-wide — a
flag that is read on a layout but set by no launch anywhere is the layout-side twin of the
write-only global bug. Since v1.3.0 the blob also includes object names, and objects are queryable individually
(see "Layout objects" above); use `extra_json.bounds` for coarse geometry.

## Variable hygiene ($$globals)

Variables live inside step text, not in `refs`, so they need text queries.
A `$$global` that a script **writes but nothing ever reads** is a classic
latent bug (wrong-global typo, dead handoff). Two-step recipe:

**1) Census** — every `$$global` a script sets, with solution-wide step counts
(write-only candidates sort to the top):

```sql
WITH t AS (
  SELECT json_extract(s.extra_json,'$.step_text') AS txt
  FROM entities s
  JOIN entities scr ON scr.entity_id = s.parent_entity_id AND scr.kind='script'
  WHERE scr.name LIKE 'My Script%' AND s.kind='script_step'
    AND json_extract(s.extra_json,'$.step_text') LIKE 'Set Variable [ $$%'
), script_writes AS (
  SELECT DISTINCT substr(txt, instr(txt,'$$'),
    CASE WHEN instr(substr(txt,instr(txt,'$$')),';') > 0
         THEN instr(substr(txt,instr(txt,'$$')),';') - 1 ELSE 40 END) AS var
  FROM t
)
SELECT w.var,
  (SELECT COUNT(*) FROM entities st WHERE st.kind='script_step'
     AND json_extract(st.extra_json,'$.step_text') LIKE 'Set Variable [ '||w.var||';%') AS writes,
  (SELECT COUNT(*) FROM entities st WHERE st.kind='script_step'
     AND json_extract(st.extra_json,'$.step_text') LIKE '%'||w.var||'%'
     AND json_extract(st.extra_json,'$.step_text') NOT LIKE 'Set Variable [ '||w.var||';%') AS other_mentions
FROM script_writes w
ORDER BY other_mentions;
```

**2) Confirm across ALL entity kinds** before calling anything write-only —
globals are also read in layout-object calcs, tooltips, and field calcs:

```sql
SELECT kind, name, snippet(text_index, 4, '[', ']', '...', 8) AS match
FROM text_index WHERE text_index MATCH '"MY_GLOBAL_NAME"' LIMIT 20;
```

The FTS tokenizer drops the `$$` prefix, so this over-matches (recall-oriented,
by design): a var whose only hits are its own `Set Variable` writes is
**definitively write-only**; anything else needs a human glance. Verified on a
real solution: this pair of queries surfaces a write-only global bug in one
round-trip that a per-question grep hunt took an entire session to find.

## Record operations (v1.5.0+): what creates or deletes records

Record-op steps (New Record, Delete Record, Delete All, Delete Portal Row, Duplicate,
Import, Truncate) carry NO table in the DDR — they act on the runtime layout context.
Field-writer sweeps are structurally blind to them (deleting a record writes no field).
fmsonar therefore SURFACES them with a context clue and a confidence tier, never a
resolved claim:

```bash
fm-ddr mutations solution.db                 # complete inventory, tiered
fm-ddr mutations solution.db --like adder    # loose filter: script name OR context clue
```

Tiers: `confident` (Truncate with a named table — the DDR states it) · `likely` (clean
in-script Go to Layout context, browse mode) · `check` (context set by caller /
conditional / portal row / import mapping — the note says what to verify). Find-mode
record ops are find REQUESTS, not mutations: shown and tagged, never dropped; same for
disabled (`//`) steps. `investigate <script>` includes the same analysis per script — and
(v1.6.0+) ALWAYS prints a compact recursive **chain rollup** (N scripts, deleters listed with
tiers, creator counts) when the script has callees; `investigate ... --chain` expands it to
the full per-site census. "What does this nightly job touch?" is one command.

Layout context and browse/find mode are runtime state a script can inherit from its
caller — treat `likely` as a strong hint, not a guarantee.

## Health / tech-debt hints

Candidate **dead fields** (never referenced anywhere — read COVERAGE.md first;
ExecuteSQL and other text-only usage is NOT captured):

```sql
SELECT base_table, name, field_type FROM v_unused_fields ORDER BY base_table, name;
```

Candidate **orphan scripts** (never called by another script/trigger — may still
be run from a menu/button/external, so verify):

```sql
SELECT grp, name FROM v_orphan_scripts ORDER BY grp, name;
```

**Table occurrences** never used on a layout or in a relationship:

```sql
SELECT t.name FROM entities t
WHERE t.kind='table_occurrence'
  AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = t.entity_id);
```

**Most-referenced fields** (schema hotspots):

```sql
SELECT te.base_table||'::'||te.name AS field, COUNT(*) uses
FROM refs r JOIN entities te ON te.entity_id = r.target_entity_id
WHERE r.target_kind='field'
GROUP BY te.entity_id ORDER BY uses DESC LIMIT 25;
```

Unresolved references (external files, calculated destinations — a health signal):

```sql
SELECT * FROM v_unresolved;
```

Ambiguous picks (several same-named candidates matched; deterministic but verify):

```sql
SELECT * FROM v_ambiguous;
```

## Full-text search (catch-all)

Find every calc / step / name mentioning a token, even where structured
extraction is incomplete (e.g. built-in functions, hard-coded strings):

```sql
SELECT kind, name, snippet(text_index, 4, '[', ']', '...', 12) AS match
FROM text_index WHERE text_index MATCH 'ExecuteSQL' ORDER BY rank LIMIT 30;
```

FTS5 supports phrases (`"go to related"`), prefixes (`Get*`), and boolean
(`email AND NOT trigger`).

## Inventory

Entity counts (the auto-generated equivalent of a DDR summary):

```sql
SELECT kind, COUNT(*) n FROM entities GROUP BY kind ORDER BY n DESC;
```

Base tables with field + record counts:

```sql
SELECT bt.name, bt.records,
       (SELECT COUNT(*) FROM entities f WHERE f.parent_entity_id=bt.entity_id AND f.kind='field') AS fields
FROM entities bt WHERE bt.kind='base_table' ORDER BY bt.records DESC;
```

Scripts by group, with step counts:

```sql
SELECT s.grp, s.name,
       (SELECT COUNT(*) FROM entities st WHERE st.parent_entity_id=s.entity_id) AS steps
FROM entities s WHERE s.kind='script' ORDER BY s.grp, s.name;
```
