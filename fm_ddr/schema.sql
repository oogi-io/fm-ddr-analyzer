-- FM DDR Analyzer — SQLite schema
-- One database can hold multiple files (a multi-file solution) and multiple DDR snapshots.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- A parsed DDR run. Lets one DB hold several snapshots for diffing later.
CREATE TABLE IF NOT EXISTS ddr_run (
    run_id        INTEGER PRIMARY KEY,
    source_path   TEXT,
    ddr_version   TEXT,
    creation_time TEXT,
    parsed_at     TEXT,
    label         TEXT          -- optional human label e.g. "mysolution 2026-07-07"
);

-- One FileMaker file within a run (a solution can span several files).
CREATE TABLE IF NOT EXISTS files (
    file_id     INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES ddr_run(run_id),
    name        TEXT,
    path        TEXT
);

-- Unified entity table. Every named thing in the DDR is a row here.
-- kind: base_table | field | table_occurrence | relationship | layout | layout_group
--       | script | script_group | script_step | custom_function | value_list
--       | privilege_set | account | extended_privilege | custom_menu | custom_menu_set
--       | external_data_source | theme | file_reference
CREATE TABLE IF NOT EXISTS entities (
    entity_id        INTEGER PRIMARY KEY,
    file_id          INTEGER NOT NULL REFERENCES files(file_id),
    kind             TEXT NOT NULL,
    fm_id            TEXT,          -- FileMaker's own id attr (unique per kind per file, mostly)
    name             TEXT,
    parent_entity_id INTEGER REFERENCES entities(entity_id),  -- e.g. field -> base_table, step -> script
    -- promoted common columns (nullable, kind-specific)
    base_table       TEXT,          -- for field & table_occurrence: owning base table name
    data_type        TEXT,          -- field: Text/Number/...
    field_type       TEXT,          -- field: Normal/Calculated/Summary/...
    calc_text        TEXT,          -- field calc, CF body, step calc, relationship n/a
    step_type        TEXT,          -- script_step: "Set Field", "Perform Script", ...
    seq              INTEGER,       -- ordering within parent (step order, etc.)
    grp              TEXT,          -- script/layout group path
    records          INTEGER,       -- base_table record count
    ext_file         TEXT,          -- table_occurrence: external file its base table lives in
    extra_json       TEXT           -- kind-specific leftovers
);

-- Generic reference edge: source entity USES target thing.
-- target may be unresolved at parse time; resolve.sql fills target_entity_id.
-- context: calc | join_predicate | perform_script | go_to_layout | trigger
--          | layout_object | value_list_source | field_reference | to_reference | function_ref
CREATE TABLE IF NOT EXISTS refs (
    ref_id           INTEGER PRIMARY KEY,
    file_id          INTEGER NOT NULL REFERENCES files(file_id),
    source_entity_id INTEGER REFERENCES entities(entity_id),
    source_kind      TEXT,
    context          TEXT NOT NULL,
    target_kind      TEXT NOT NULL,     -- field | table_occurrence | script | layout | custom_function | value_list
    target_fm_id     TEXT,
    target_name      TEXT,              -- field name (leaf) or object name
    target_to_name   TEXT,              -- for fields referenced as TO::Field, the TO part
    target_raw       TEXT,              -- original raw string as seen (e.g. "TO::Field")
    target_file      TEXT,              -- external target: FileMaker file named by a FileReference marker
    target_entity_id INTEGER REFERENCES entities(entity_id),  -- filled by resolve step
    ambiguous        INTEGER DEFAULT 0  -- 1 = several candidates matched; pick is deterministic but uncertain
);

CREATE INDEX IF NOT EXISTS ix_entities_lookup ON entities(file_id, kind, name);
-- field lookup by owning base table (drives external-TO field resolution)
CREATE INDEX IF NOT EXISTS ix_entities_bt     ON entities(kind, base_table, name, file_id);
CREATE INDEX IF NOT EXISTS ix_entities_fmid   ON entities(file_id, kind, fm_id);
CREATE INDEX IF NOT EXISTS ix_entities_parent ON entities(parent_entity_id);
CREATE INDEX IF NOT EXISTS ix_refs_source     ON refs(source_entity_id);
CREATE INDEX IF NOT EXISTS ix_refs_target     ON refs(target_entity_id);
CREATE INDEX IF NOT EXISTS ix_refs_tname       ON refs(file_id, target_kind, target_name);

-- Full-text catch-all: every calculation / step text / name is searchable here,
-- so "where is X mentioned?" works even where structured extraction is incomplete.
CREATE VIRTUAL TABLE IF NOT EXISTS text_index USING fts5(
    entity_id UNINDEXED,
    file_id   UNINDEXED,
    kind      UNINDEXED,
    name,
    body,
    tokenize = 'unicode61'
);
