# Cross-reference query recipes

The database is designed to be queried directly. Run any of these with:

```bash
python3 -m fm_ddr.cli sql solution.db "<SQL>"
# or: sqlite3 solution.db "<SQL>"
```

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
