-- Resolve refs.target_entity_id (raw name/id -> concrete entity) and build
-- convenience views for cross-reference ("where used") queries.
-- All picks are deterministic (ORDER BY entity_id); where several candidates
-- matched, refs.ambiguous is set so the uncertainty stays visible.

-- Fields: match by leaf name, disambiguated by the base table behind the TO.
UPDATE refs
SET target_entity_id = (
    SELECT f.entity_id FROM entities f
    WHERE f.file_id = refs.file_id AND f.kind = 'field'
      AND f.name = refs.target_name
      AND (
        refs.target_to_name IS NULL
        OR f.base_table = (
            SELECT t.base_table FROM entities t
            WHERE t.file_id = refs.file_id AND t.kind = 'table_occurrence'
              AND t.name = refs.target_to_name LIMIT 1)
      )
    ORDER BY f.entity_id LIMIT 1)
WHERE target_kind = 'field' AND target_entity_id IS NULL;

-- Fields reached through an EXTERNAL table occurrence (its FileReference names
-- the file that owns the base table) resolve against that file's fields.
UPDATE refs
SET target_entity_id = (
    SELECT fld.entity_id FROM entities t
    JOIN files fl ON LOWER(REPLACE(fl.name, '.fmp12', '')) = LOWER(t.ext_file)
    JOIN entities fld ON fld.file_id = fl.file_id AND fld.kind = 'field'
         AND fld.base_table = t.base_table AND fld.name = refs.target_name
    WHERE t.file_id = refs.file_id AND t.kind = 'table_occurrence'
      AND t.name = refs.target_to_name AND t.ext_file IS NOT NULL
    ORDER BY fld.entity_id LIMIT 1)
WHERE target_kind = 'field' AND target_entity_id IS NULL
  AND target_to_name IS NOT NULL;

-- Unqualified field refs matching several same-named fields are guesses.
UPDATE refs
SET ambiguous = 1
WHERE target_kind = 'field' AND target_entity_id IS NOT NULL
  AND target_to_name IS NULL
  AND (SELECT COUNT(*) FROM entities f
       WHERE f.file_id = refs.file_id AND f.kind = 'field'
         AND f.name = refs.target_name) > 1;

-- Table occurrences: by name (unique per file in FileMaker).
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'table_occurrence'
      AND e.name = refs.target_name ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'table_occurrence' AND target_entity_id IS NULL;

-- Scripts: prefer FileMaker id, then fall back to name (external-file calls stay NULL).
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'script'
      AND e.fm_id = refs.target_fm_id ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'script' AND target_entity_id IS NULL AND target_fm_id IS NOT NULL
  AND target_file IS NULL;
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'script'
      AND e.name = refs.target_name ORDER BY e.entity_id LIMIT 1),
    ambiguous = CASE WHEN (
      SELECT COUNT(*) FROM entities e
      WHERE e.file_id = refs.file_id AND e.kind = 'script'
        AND e.name = refs.target_name) > 1 THEN 1 ELSE ambiguous END
WHERE target_kind = 'script' AND target_entity_id IS NULL AND target_name IS NOT NULL
  AND target_file IS NULL;

-- Layouts: id then name (same ambiguity rule as scripts).
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'layout'
      AND e.fm_id = refs.target_fm_id ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'layout' AND target_entity_id IS NULL AND target_fm_id IS NOT NULL
  AND target_file IS NULL;
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'layout'
      AND e.name = refs.target_name ORDER BY e.entity_id LIMIT 1),
    ambiguous = CASE WHEN (
      SELECT COUNT(*) FROM entities e
      WHERE e.file_id = refs.file_id AND e.kind = 'layout'
        AND e.name = refs.target_name) > 1 THEN 1 ELSE ambiguous END
WHERE target_kind = 'layout' AND target_entity_id IS NULL AND target_name IS NOT NULL
  AND target_file IS NULL;

-- Value lists: by name.
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'value_list'
      AND e.name = refs.target_name ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'value_list' AND target_entity_id IS NULL;

-- Custom functions: function_ref chunks that match a CF name (built-ins stay NULL).
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'custom_function'
      AND e.name = refs.target_name ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'custom_function' AND target_entity_id IS NULL;

-- Cross-file: refs carrying a FileReference marker resolve ONLY against the
-- named file (matched on files.name minus .fmp12, case-insensitive). If that
-- file is not in this DB, the ref stays NULL — never mis-linked locally.
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    JOIN files fl ON fl.file_id = e.file_id
    WHERE e.kind = refs.target_kind
      AND LOWER(REPLACE(fl.name, '.fmp12', '')) = LOWER(refs.target_file)
      AND e.fm_id = refs.target_fm_id
    ORDER BY e.entity_id LIMIT 1)
WHERE target_file IS NOT NULL AND target_entity_id IS NULL
  AND target_fm_id IS NOT NULL;
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    JOIN files fl ON fl.file_id = e.file_id
    WHERE e.kind = refs.target_kind
      AND LOWER(REPLACE(fl.name, '.fmp12', '')) = LOWER(refs.target_file)
      AND e.name = refs.target_name
    ORDER BY e.entity_id LIMIT 1)
WHERE target_file IS NOT NULL AND target_entity_id IS NULL
  AND target_name IS NOT NULL;

-- ---- Views -----------------------------------------------------------

-- The main cross-reference surface. One row per edge, with readable names and
-- the source's parent (e.g. a script_step's owning script, a field's base table).
DROP VIEW IF EXISTS v_usage;
CREATE VIEW v_usage AS
SELECT
    r.ref_id,
    r.context,
    se.kind                         AS source_kind,
    se.name                         AS source_name,
    se.entity_id                    AS source_id,
    sp.kind                         AS source_parent_kind,
    sp.name                         AS source_parent_name,
    r.target_kind,
    COALESCE(te.name, r.target_raw) AS target_name,
    r.target_raw,
    te.entity_id                    AS target_id,
    (r.target_entity_id IS NOT NULL) AS resolved,
    r.ambiguous,
    r.target_file,
    r.file_id
FROM refs r
LEFT JOIN entities se ON se.entity_id = r.source_entity_id
LEFT JOIN entities sp ON sp.entity_id = se.parent_entity_id
LEFT JOIN entities te ON te.entity_id = r.target_entity_id;

-- Fields never referenced anywhere (candidate dead fields — see COVERAGE.md
-- for the blind spots: ExecuteSQL strings and other text-only usage are NOT
-- captured, so treat this as a review list, never a delete list).
DROP VIEW IF EXISTS v_unused_fields;
CREATE VIEW v_unused_fields AS
SELECT f.entity_id, f.file_id, f.base_table, f.name, f.field_type
FROM entities f
WHERE f.kind = 'field'
  AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = f.entity_id);

-- Scripts never called by another script or trigger (candidate orphans;
-- may still be run by user/menu/external, so treat as a hint).
DROP VIEW IF EXISTS v_orphan_scripts;
CREATE VIEW v_orphan_scripts AS
SELECT s.entity_id, s.file_id, s.name, s.grp
FROM entities s
WHERE s.kind = 'script'
  AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = s.entity_id);

-- Unresolved references (broken/external/built-in). Useful health signal.
DROP VIEW IF EXISTS v_unresolved;
CREATE VIEW v_unresolved AS
SELECT target_kind, context, COUNT(*) AS n
FROM refs WHERE target_entity_id IS NULL
GROUP BY target_kind, context ORDER BY n DESC;

-- Ambiguous picks (several candidates matched; verify before trusting).
DROP VIEW IF EXISTS v_ambiguous;
CREATE VIEW v_ambiguous AS
SELECT * FROM v_usage WHERE ambiguous = 1;
