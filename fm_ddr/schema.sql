-- FM DDR Analyzer — SQLite schema
-- One database can hold multiple files (a multi-file solution) and multiple DDR snapshots.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- A parsed DDR run. Lets one DB hold several snapshots for diffing later.
CREATE TABLE IF NOT EXISTS ddr_run (
    run_id        INTEGER PRIMARY KEY,
    source_path   TEXT,
    ddr_version   TEXT,         -- root element version: FMPReport@version (DDR) or
                                -- FMSaveAsXML@version (SaXML, e.g. 2.3.0.0); a SaXML
                                -- build of several versions lists them comma-separated
                                -- (2.3.0.0, 2.3.0.1): each file's is files.format_version
    creation_time TEXT,
    parsed_at     TEXT,
    parser_version TEXT,      -- fm_ddr version that built this index (stale-check)
    source_format TEXT,         -- 'ddr' | 'saxml' (Save a Copy as XML); NULL = an index
                                -- built before this column, read as 'ddr'
    source_version TEXT,        -- SaXML: FMSaveAsXML@Source, the FileMaker version that
                                -- wrote it (e.g. 26.0.1; comma-separated as ddr_version);
                                -- NULL for a DDR
    label         TEXT,         -- optional human label e.g. "mysolution 2026-07-07"
    ddrinfo_refs  INTEGER,      -- SaXML only (NULL for a DDR, and on an index built
                                -- before these columns): DDRREF elements read
    ddrinfo_missing_steps INTEGER,  -- of those, steps whose DDR_INFO entry is
                                    -- absent: no step text
    ddrinfo_missing_calcs INTEGER,  -- calculations whose chunk list is absent: no
                                    -- field or custom function references; one per
                                    -- distinct (key, hash) pair, not per DDRREF; a key
                                    -- whose list under another hash names nothing
                                    -- is not counted (nothing lost)
    build_key     TEXT          -- v1.16.0: SHA-256 over the fmsonar version and the
                                -- SHA-256 of each source's bytes, in build order; `build`
                                -- skips when -o holds the same key. NULL on an older
                                -- index (never skipped)
);

-- One FileMaker file within a run (a solution can span several files).
CREATE TABLE IF NOT EXISTS files (
    file_id     INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES ddr_run(run_id),
    name        TEXT,
    path        TEXT,
    format_version TEXT,    -- SaXML: this export's FMSaveAsXML@version (2.3.0.0 and
                            -- 2.3.0.1 build together); NULL for a DDR, older index
    source_version TEXT     -- SaXML: its Source (the FileMaker version); NULL for a DDR
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
    ext_file         TEXT,          -- table_occurrence: external data source NAME its base table
                                    -- lives in (v_data_sources maps it to a file); '<unknown>' =
                                    -- the data source no longer exists (v1.13.0)
    extra_json       TEXT,          -- kind-specific leftovers; external_data_source (file kind):
                                    -- {file: true, link: "X_fmp12.xml"} (v1.13.0); script,
                                    -- script_group, layout, layout_group: {in_menu: true} and
                                    -- script {run_full_access: true}, keys only when the DDR
                                    -- flag is True (v1.14.0). SaXML builds add: external_data_source
                                    -- {file_name} (the path without its scheme); table_occurrence
                                    -- {external: true} = an external TO with no data source (its
                                    -- field refs stay unresolved); entities with a UUID element
                                    -- {uuid, modified_at, modified_by, modified_account, modified_n};
                                    -- any entity {xfile_marker: true} = its every <Field Missing>
                                    -- follows such a TO (v_dangling: unverifiable_xfile, v1.15.0)
    -- field storage & auto-enter (v1.9.0; NULL for non-fields and pre-1.9 builds)
    stored           INTEGER,       -- 0 = unstored calc (storeCalculationResults="False"), 1 = stored
    indexed          TEXT,          -- index setting: None | Minimal | All
    is_global        INTEGER,       -- 1 = global storage field
    auto_enter       TEXT           -- JSON: {calc, calc_active, type, lookup, always_evaluate,
                                    --        overwrite_existing, prohibit_editing, constant}
                                    -- calc_active=false marks DEAD residue: a calc left behind after
                                    -- the "Calculated value" option was unchecked (refs disabled=1)
);

-- Generic reference edge: source entity USES target thing.
-- target may be unresolved at parse time; resolve.sql fills target_entity_id.
-- context: calc | auto_enter | validation | lookup | join_predicate
--          | perform_script | go_to_layout | trigger | layout_object
--          | value_list_source | field_reference | to_reference | function_ref
--          | layout_context (a layout's own context TO; v1.12.0)
--          | step_table (the TO a Go to Related Record / Import Records step
--            acts on) | portal (the TO a portal shows) | value_list_context
--            (the "starting from" TO of a related-values list) (v1.14.0; all
--            three resolve by name within the file, an empty name is '<unknown>')
--          | part_break (a sub-summary part's break field, source the layout;
--            Save a Copy as XML input only, the DDR has no Part element; v1.15.0)
--          | import_field (an Import Records target field mapped Import or
--            Match) | export_field (an Export Records field or group-by field)
--            | find_request (a stored find request's field)
--            (v1.15.1; source the step, or a button / menu item)
-- (auto_enter/validation = field refs inside an AutoEnter / Validation calc;
--  lookup = the source field a looked-up value copies FROM; a field's full
--  dependency set is context IN ('calc','auto_enter','validation','lookup'))
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
    target_raw       TEXT,              -- original raw string as seen (e.g. "TO::Field"); may carry a
                                        -- marker (v1.13.0): '<unknown>', '<Table Missing>',
                                        -- 'TO::<Field Missing>' = a deleted target, listed by v_dangling
    target_file      TEXT,              -- external target: data source NAME from a FileReference marker
                                        -- (v_data_sources maps it to a file); '<unknown>' = that data
                                        -- source no longer exists (v1.13.0)
    target_entity_id INTEGER REFERENCES entities(entity_id),  -- filled by resolve step
    ambiguous        INTEGER DEFAULT 0, -- 1 = several candidates matched; pick is deterministic but uncertain
    disabled         INTEGER DEFAULT 0, -- 1 = emitted from a disabled (enable="False") script step OR from a
                                        --     dead auto-enter calc (option unchecked, calc residue): dead code
    trigger_event    TEXT               -- context='trigger' only: OnRecordCommit, OnObjectSave, ...
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
