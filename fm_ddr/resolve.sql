-- Resolve refs.target_entity_id (raw name/id -> concrete entity) and build
-- convenience views for cross-reference ("where used") queries.
-- All picks are deterministic (ORDER BY entity_id); where several candidates
-- matched, refs.ambiguous is set so the uncertainty stays visible.

-- External data sources (v1.13.0): which file does a data source NAME point
-- at? One row per (file, distinct LOWER(name)) a file uses: the catalog's file
-- sources (extra_json {file: true, link}), the ext_file of its TOs and the
-- target_file of its refs ('<unknown>', a source that no longer exists, never
-- gets a row). target_file_id is the first of: (a) the catalog link minus a
-- trailing _fmp12.xml (the DDR writes X_fmp12.xml for X.fmp12), (b) the name
-- itself; both compared case-insensitively to files.name minus .fmp12, within
-- the same run. A source may name its own file. No match: NULL target.
DROP VIEW IF EXISTS v_data_sources;
CREATE VIEW v_data_sources AS
WITH names(file_id, ds_name, link, pri) AS (
    SELECT file_id, name, json_extract(extra_json, '$.link'), 0
    FROM entities
    WHERE kind = 'external_data_source' AND json_extract(extra_json, '$.file') = 1
      AND name IS NOT NULL AND name <> ''
    UNION ALL
    SELECT file_id, ext_file, NULL, 1 FROM entities
    WHERE kind = 'table_occurrence' AND ext_file IS NOT NULL AND ext_file <> '<unknown>'
    UNION ALL
    SELECT file_id, target_file, NULL, 1 FROM refs
    WHERE target_file IS NOT NULL AND target_file <> '<unknown>'
),
keyed AS (
    SELECT file_id, LOWER(ds_name) AS key,
           (SELECT n2.ds_name FROM names n2
            WHERE n2.file_id = n.file_id AND LOWER(n2.ds_name) = LOWER(n.ds_name)
            ORDER BY n2.pri, n2.ds_name LIMIT 1) AS ds_name,
           MAX(link) AS link
    FROM names n GROUP BY file_id, LOWER(ds_name)
),
resolved AS (
    SELECT k.file_id, k.ds_name, k.link,
           COALESCE(
             (SELECT fl.file_id FROM files fl JOIN files me ON me.run_id = fl.run_id
              WHERE me.file_id = k.file_id AND k.link IS NOT NULL
                AND LOWER(REPLACE(fl.name, '.fmp12', ''))
                    = LOWER(REPLACE(k.link, '_fmp12.xml', ''))
              ORDER BY fl.file_id LIMIT 1),
             (SELECT fl.file_id FROM files fl JOIN files me ON me.run_id = fl.run_id
              WHERE me.file_id = k.file_id
                AND LOWER(REPLACE(fl.name, '.fmp12', '')) = k.key
              ORDER BY fl.file_id LIMIT 1)
           ) AS target_file_id
    FROM keyed k
)
SELECT r.file_id, r.ds_name, r.link, r.target_file_id,
       (SELECT name FROM files WHERE file_id = r.target_file_id) AS target_file_name
FROM resolved r;

-- The resolver's copy, computed once from the same definition (a TEMP table
-- lives on the build connection only; parse.py is the one place this runs).
DROP TABLE IF EXISTS temp.ds_file;
CREATE TEMP TABLE ds_file AS SELECT * FROM v_data_sources;

-- Fields: match by leaf name, disambiguated by the base table behind the TO.
-- A TO on an external data source (ext_file set) never resolves locally, even
-- when a local base table carries the same name (v1.13.0).
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
              AND t.name = refs.target_to_name AND t.ext_file IS NULL LIMIT 1)
      )
    ORDER BY f.entity_id LIMIT 1)
WHERE target_kind = 'field' AND target_entity_id IS NULL;

-- Fields reached through an EXTERNAL table occurrence resolve against the
-- file its data source points at (ds_file: catalog link, then name).
UPDATE refs
SET target_entity_id = (
    SELECT fld.entity_id FROM entities t
    JOIN ds_file d ON d.file_id = t.file_id AND LOWER(d.ds_name) = LOWER(t.ext_file)
         AND d.target_file_id IS NOT NULL
    JOIN entities fld ON fld.file_id = d.target_file_id AND fld.kind = 'field'
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

-- Layout context TOs (v1.12.0): by FileMaker id within the SAME file first.
-- fm_id collides across files of a multi-file solution, so file_id is part of
-- the key. A layout's TO always lives in the layout's own file's graph.
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'table_occurrence'
      AND e.fm_id = refs.target_fm_id ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'table_occurrence' AND context = 'layout_context'
  AND target_entity_id IS NULL AND target_fm_id IS NOT NULL;

-- Table occurrences: by name (unique per file in FileMaker).
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'table_occurrence'
      AND e.name = refs.target_name ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'table_occurrence' AND target_entity_id IS NULL;

-- Scripts: prefer FileMaker id, then fall back to name (external-file calls stay NULL).
-- A '<unknown>' raw is a deleted script (v1.13.0): the id alone never resolves it.
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'script'
      AND e.fm_id = refs.target_fm_id ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'script' AND target_entity_id IS NULL AND target_fm_id IS NOT NULL
  AND target_file IS NULL AND target_raw IS NOT '<unknown>';
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

-- Layouts: id then name (same ambiguity rule as scripts; same '<unknown>' guard).
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    WHERE e.file_id = refs.file_id AND e.kind = 'layout'
      AND e.fm_id = refs.target_fm_id ORDER BY e.entity_id LIMIT 1)
WHERE target_kind = 'layout' AND target_entity_id IS NULL AND target_fm_id IS NOT NULL
  AND target_file IS NULL AND target_raw IS NOT '<unknown>';
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
-- file the data source points at (ds_file: catalog link, then name). If that
-- file is not in this DB, or the marker is '<unknown>' (no ds_file row), the
-- ref stays NULL: never mis-linked locally.
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    JOIN ds_file d ON d.file_id = refs.file_id
         AND LOWER(d.ds_name) = LOWER(refs.target_file)
         AND d.target_file_id = e.file_id
    WHERE e.kind = refs.target_kind
      AND e.fm_id = refs.target_fm_id
    ORDER BY e.entity_id LIMIT 1)
WHERE target_file IS NOT NULL AND target_entity_id IS NULL
  AND target_fm_id IS NOT NULL;
UPDATE refs
SET target_entity_id = (
    SELECT e.entity_id FROM entities e
    JOIN ds_file d ON d.file_id = refs.file_id
         AND LOWER(d.ds_name) = LOWER(refs.target_file)
         AND d.target_file_id = e.file_id
    WHERE e.kind = refs.target_kind
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
    r.trigger_event,
    r.file_id
FROM refs r
LEFT JOIN entities se ON se.entity_id = r.source_entity_id
LEFT JOIN entities sp ON sp.entity_id = se.parent_entity_id
LEFT JOIN entities te ON te.entity_id = r.target_entity_id
WHERE r.disabled = 0;

-- References emitted from DISABLED (commented-out) script steps: dead code.
-- Excluded from v_usage so counts and where-used reflect live behavior;
-- kept queryable here because "referenced only by disabled code" is a finding.
DROP VIEW IF EXISTS v_usage_disabled;
CREATE VIEW v_usage_disabled AS
SELECT
    r.ref_id, r.context,
    se.kind AS source_kind, se.name AS source_name, se.entity_id AS source_id,
    sp.kind AS source_parent_kind, sp.name AS source_parent_name,
    r.target_kind, COALESCE(te.name, r.target_raw) AS target_name,
    r.target_raw, te.entity_id AS target_id,
    (r.target_entity_id IS NOT NULL) AS resolved, r.ambiguous, r.target_file,
    r.trigger_event, r.file_id
FROM refs r
LEFT JOIN entities se ON se.entity_id = r.source_entity_id
LEFT JOIN entities sp ON sp.entity_id = se.parent_entity_id
LEFT JOIN entities te ON te.entity_id = r.target_entity_id
WHERE r.disabled = 1;

-- Fields never referenced anywhere (candidate dead fields — see COVERAGE.md
-- for the blind spots: ExecuteSQL strings and other text-only usage are NOT
-- captured, so treat this as a review list, never a delete list).
DROP VIEW IF EXISTS v_unused_fields;
CREATE VIEW v_unused_fields AS
SELECT f.entity_id, f.file_id, f.base_table, f.name, f.field_type,
       EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = f.entity_id
               AND r.disabled = 1) AS only_disabled_refs
FROM entities f
WHERE f.kind = 'field'
  AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = f.entity_id
                  AND r.disabled = 0);

-- Scripts never called by another script or trigger (candidate orphans;
-- may still be run by user/menu/external, so treat as a hint).
DROP VIEW IF EXISTS v_orphan_scripts;
CREATE VIEW v_orphan_scripts AS
SELECT s.entity_id, s.file_id, s.name, s.grp,
       EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = s.entity_id
               AND r.disabled = 1) AS only_disabled_refs
FROM entities s
WHERE s.kind = 'script'
  AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = s.entity_id
                  AND r.disabled = 0);

-- Script triggers with their firing EVENT and owning layout. Trigger refs are
-- sourced from layouts (layout-level triggers) or layout OBJECTS (object-level,
-- possibly nested in panels/portals) — a plain caller query joining through
-- script parents silently drops every one of them; this view is the safe path.
DROP VIEW IF EXISTS v_triggers;
CREATE VIEW v_triggers AS
WITH RECURSIVE anc(ref_id, eid) AS (
    SELECT r.ref_id, r.source_entity_id FROM refs r WHERE r.context = 'trigger'
    UNION ALL
    SELECT a.ref_id, e.parent_entity_id FROM anc a
    JOIN entities e ON e.entity_id = a.eid AND e.kind <> 'layout'
    WHERE e.parent_entity_id IS NOT NULL
)
SELECT
    r.ref_id,
    r.trigger_event,
    se.kind   AS source_kind,        -- 'layout' or 'layout_object'
    se.name   AS object_name,
    lay.name  AS layout_name,
    lay.fm_id AS layout_fm_id,
    COALESCE(te.name, r.target_raw) AS script_name,
    te.fm_id  AS script_fm_id,
    (r.target_entity_id IS NOT NULL) AS resolved,
    r.file_id
FROM refs r
LEFT JOIN entities se  ON se.entity_id = r.source_entity_id
LEFT JOIN entities lay ON lay.entity_id = (
    SELECT a.eid FROM anc a JOIN entities x ON x.entity_id = a.eid
    WHERE a.ref_id = r.ref_id AND x.kind = 'layout' LIMIT 1)
LEFT JOIN entities te  ON te.entity_id = r.target_entity_id
WHERE r.context = 'trigger' AND r.disabled = 0;

-- Cascade deletes (v1.10.0): one row per relationship side with cascadeDelete
-- set — deleting a record on deleting_table cascades into victim_table.
-- The DDR fact "what deletes into this table by cascade" that step censuses
-- structurally cannot see.
DROP VIEW IF EXISTS v_cascades;
CREATE VIEW v_cascades AS
SELECT rel.entity_id AS relationship_id,
       rel.name      AS relationship,
       json_extract(o.value,'$.to') AS deleting_to,
       oto.base_table               AS deleting_table,
       json_extract(s.value,'$.to') AS victim_to,
       vto.base_table               AS victim_table,
       rel.file_id
FROM entities rel
JOIN json_each(json_extract(rel.extra_json,'$.sides')) s
JOIN json_each(json_extract(rel.extra_json,'$.sides')) o
     ON json_extract(s.value,'$.side') <> json_extract(o.value,'$.side')
LEFT JOIN entities vto ON vto.file_id = rel.file_id
     AND vto.kind='table_occurrence' AND vto.name = json_extract(s.value,'$.to')
LEFT JOIN entities oto ON oto.file_id = rel.file_id
     AND oto.kind='table_occurrence' AND oto.name = json_extract(o.value,'$.to')
WHERE rel.kind='relationship'
  AND json_extract(s.value,'$.cascade_delete') = 1;

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

-- Dangling references (v1.13.0): one row per defect the DDR itself marks.
-- Three branches: (1) unresolved refs whose target_raw carries a marker
-- (fmsonar's '<unknown>' or the DDR's <Table Missing> / <Field Missing>) or
-- whose target_file is '<unknown>'; (2) broken calculations, one row per
-- (entity, marker) for <Table Missing>, <Field Missing> and <Function Missing>
-- in calc_text, the auto-enter calc, the validation calc, a hide calc or a
-- tooltip calc; (3) table occurrences on a data source that no longer exists.
-- v1.15.0: a <Field Missing> row of an entity marked extra_json.xfile_marker
-- (SaXML only: every such marker follows an empty external TO, a field in
-- another file the export cannot see) is context 'unverifiable_xfile', not
-- 'broken_calc'. A DDR never sets the flag, so a DDR index is unchanged.
-- SaXML only: a field object on a layout or a script step's target field
-- through such a TO (extra_json.external, an external TO with no data source)
-- has no field name in the export, live or deleted alike: one
-- 'unverifiable_xfile' row each, marker TO::<unknown>. A DDR TO never carries
-- the flag.
-- Disabled rows are included with the flag, never filtered. step_type is NULL
-- for a button step (layout objects are the source, their steps are not
-- entities). source_parent_name is the owning script for a step and the
-- enclosing object or layout for a layout object.
DROP VIEW IF EXISTS v_dangling;
CREATE VIEW v_dangling AS
SELECT r.ref_id, r.file_id, r.context, r.source_kind,
       se.name      AS source_name,
       sp.name      AS source_parent_name,
       se.step_type AS step_type,
       r.target_kind,
       r.target_raw AS marker,
       CASE WHEN r.target_raw LIKE '%<Field Missing>%' THEN '<Field Missing>'
            WHEN r.target_raw LIKE '%<Table Missing>%' THEN '<Table Missing>'
            ELSE '<unknown>' END AS marker_class,
       r.target_fm_id, r.disabled
FROM refs r
LEFT JOIN entities se ON se.entity_id = r.source_entity_id
LEFT JOIN entities sp ON sp.entity_id = se.parent_entity_id
WHERE r.target_entity_id IS NULL
  AND (r.target_raw LIKE '%<unknown>%'
       OR r.target_raw LIKE '%<Table Missing>%'
       OR r.target_raw LIKE '%<Field Missing>%'
       OR r.target_file = '<unknown>')
UNION ALL
SELECT r.ref_id, r.file_id, 'unverifiable_xfile', r.source_kind,
       se.name, sp.name, se.step_type, r.target_kind,
       r.target_to_name || '::<unknown>', '<unknown>', r.target_fm_id, r.disabled
FROM refs r
JOIN entities t ON t.file_id = r.file_id AND t.kind = 'table_occurrence'
     AND t.name = r.target_to_name AND json_extract(t.extra_json, '$.external') = 1
LEFT JOIN entities se ON se.entity_id = r.source_entity_id
LEFT JOIN entities sp ON sp.entity_id = se.parent_entity_id
WHERE r.context IN ('layout_object', 'step_target') AND r.target_kind = 'field'
  AND r.target_entity_id IS NULL AND r.target_raw IS NULL
UNION ALL
SELECT NULL, e.file_id,
       CASE WHEN m.marker = '<Field Missing>'
                 AND json_extract(e.extra_json, '$.xfile_marker') = 1
            THEN 'unverifiable_xfile' ELSE 'broken_calc' END,
       e.kind, e.name, sp.name, e.step_type, NULL,
       m.marker, m.marker, NULL,
       COALESCE(json_extract(e.extra_json, '$.disabled'), 0)
FROM entities e
JOIN (SELECT '<Table Missing>' AS marker
      UNION ALL SELECT '<Field Missing>'
      UNION ALL SELECT '<Function Missing>') m
LEFT JOIN entities sp ON sp.entity_id = e.parent_entity_id
WHERE e.calc_text LIKE '%' || m.marker || '%'
   OR json_extract(e.auto_enter, '$.calc') LIKE '%' || m.marker || '%'
   OR json_extract(e.extra_json, '$.validation_calc') LIKE '%' || m.marker || '%'
   OR json_extract(e.extra_json, '$.hide_calc') LIKE '%' || m.marker || '%'
   OR json_extract(e.extra_json, '$.tooltip_calc') LIKE '%' || m.marker || '%'
UNION ALL
SELECT NULL, t.file_id, 'missing_data_source', 'table_occurrence', t.name, NULL, NULL,
       NULL, '<unknown>', '<unknown>', NULL, 0
FROM entities t
WHERE t.kind = 'table_occurrence' AND t.ext_file = '<unknown>';
