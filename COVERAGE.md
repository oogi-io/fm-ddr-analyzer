# Reference coverage — what is and is not captured

The cross-reference graph is only as honest as its coverage. This is the
explicit list. **Anything in "NOT captured" means `v_unused_fields` and
"Referenced by: none" can be wrong for that usage pattern** — treat those
outputs as review lists, never as delete lists.

## Captured (structured edges in `refs`)

| Context | What it means | Source |
|---------|---------------|--------|
| `calc` | Field used in a calculation (field's own calc, script step calc, layout object calc) | `Chunk type="FieldRef"` (both element and text forms) |
| `auto_enter` (v1.9.0) | Field used in an **auto-enter** calculation — distinct from `calc` so a Normal field with auto-enter can't pose as a calc field. A field's full dependency set is `context IN ('calc','auto_enter','validation')` | `Chunk type="FieldRef"` inside `AutoEnter` |
| `validation` (v1.9.0) | Field used in a validation calculation | `Chunk type="FieldRef"` inside `Validation` |
| `lookup` (v1.9.1) | The source field a looked-up value copies FROM. A configured lookup whose option is unchecked is residue: ref flagged `disabled=1` | `Field` inside `AutoEnter > Lookup` |
| `function_ref` | **Custom** function called in a calculation | `Chunk type="CustomFunctionRef"` |
| `join_predicate` | Field or TO used in a relationship | `LeftField`/`RightField`/`LeftTable`/`RightTable` |
| `sort` | Field used in a sort order: relationship sort lists, Sort Records steps, portal sort lists | `PrimaryField`/`SecondaryField` outside value lists |
| `value_list_field` | Field feeding a field-based value list | `PrimaryField`/`SecondaryField` inside `ValueList` |
| `value_list_source` | Value list bound to a layout object | `ValueList` bindings outside the catalog |
| `layout_object` entities (v1.3.0) | Every layout object as an entity: name, type, key, bounds, button launch step+params (`extra_json.step_text`), hide condition (`extra_json.hide_calc`), tooltip calc | `Object` elements inside `Layout`, nested |
| `layout_object` | Field placed on a layout. A field object showing a repetition other than 1 keeps `[n]` in `target_raw` (`TO::Field[3]`, as the DDR writes it) and resolves by the bare name (v1.15.1; before, it stayed unresolved) | `FieldObj > Name`, `FieldReference` under `Layout` |
| `step_target` | The field a step acts on: **Set Field's write target**, Go to Field, Insert ... — combine with the step's `step_type` to tell writes from navigation | direct `<Field>` child of `<Step>` |
| `field_reference` | Field referenced via a `FieldReference` element outside layouts | `FieldReference` |
| `perform_script` | Script called by a script step | `Script` reference elements |
| `trigger` | Script attached to a script trigger; `refs.trigger_event` records the firing event (OnRecordCommit, OnObjectSave, ...) (v1.9.0). **Trigger refs are sourced from layouts/layout objects, not scripts** — query them via `v_triggers`, never via a script-parent join. v1.9.1 adds **file-level triggers** (`WindowTriggers`: OnFirstWindowOpen, OnLastWindowClose, OnWindowClose — the startup/shutdown scripts); they appear in `v_triggers` with `layout_name IS NULL` | `Script` inside `ScriptTriggers` (event from the enclosing `Trigger`) or inside `WindowTriggers` (event = wrapper tag) |
| `go_to_layout` | Layout targeted by a Go to Layout step/button | `Layout` reference elements |
| `to_reference` | Table occurrence referenced directly | `TableOccurrenceReference` |
| `layout_context` (v1.12.0) | A layout's own context table occurrence ("Show records from"). Source = the layout, target_kind = `table_occurrence`. A TO used only as a layout context is no longer "unused". Resolved by (file, FileMaker id), then name. An **empty** `name` means the TO no longer exists: the ref keeps `target_fm_id` and stays unresolved, a real defect worth reporting (QUERIES.md: "layouts whose context TO is missing") | The `<Table id name/>` that is the direct child of a layout definition |
| `step_table` (v1.14.0) | The table occurrence a step acts on: Go to Related Record ("From table") and Import Records (the target table); no other step carries the element. Source = the script step, or the layout object / custom menu of a button or menu step; a disabled step's ref is `disabled = 1`. Resolved by name within the file (`target_fm_id` kept). An empty name is `<unknown>`, see Dangling references | The `<Table id name/>` that is a direct child of `<Step>` |
| `portal` (v1.14.0) | The table occurrence a portal shows. Source = the portal's layout object. Resolved by name within the file (the element carries no id). An empty key is a portal whose TO was deleted: `<unknown>`, see Dangling references | `TableAliasKey` under `PortalObj` |
| `value_list_context` (v1.14.0) | The "starting from" table occurrence of a related-values value list. Source = the value list (`extra_json.show_related` stays as it was). Resolved by name within the file (`target_fm_id` kept). An empty name is `<unknown>`, see Dangling references | The `<Table id name/>` under `<ShowRelated value="True">` in the catalog |
| `part_break` (v1.15.0, SaXML only) | A sub-summary part's break field ("when sorted by"). Source = the layout. A field used only to break a sub-summary is not "unused" on a SaXML index. A break field through a deleted TO arrives as `<Table Missing>::<unknown>` and is listed in `v_dangling`; through an external TO with no data source it stays unresolved with `target_raw` NULL. The DDR has no element for layout parts, so a DDR index never has this context | SaXML: the `FieldReference` under `Part/Definition` |
| `import_field` (v1.15.1) | An Import Records target field mapped Import or Match. Source = the step (or the button / menu item). A DoNotImport map (SaXML `Map kind="1"`, "not import to") is no usage and emits nothing, nor does a map with no field (`id="0"`, empty name). The DDR names the target TO once, on the step's `<Table>`. A SaXML Match map is not yet measured; any kind other than 1 counts as a usage | DDR: `TargetFields/Field[@map]`; SaXML: `ImportField/Field/Map/FieldReference` |
| `export_field` (v1.15.1) | An Export Records field (the field order) or group-by field. Source = the step. The SaXML group-by shape (`Order type="Group"`) is not yet measured with a field in it | DDR: `ExportEntries/ExportEntry/Field`, `SummaryFields/Field`; SaXML: `Export/Order/Field/FieldReference` |

Every edge stores the raw text (`target_raw`) plus the resolved entity where
possible. Unresolvable targets stay `NULL` — usually **legitimately**:
scripts in other files, calculated layout destinations, unbound globals.
Ambiguous picks (several same-named candidates, no qualifier) are flagged
`ambiguous = 1` (see `v_ambiguous`); the pick is deterministic but uncertain.

**Record-level security calcs:** a privilege set's custom record-access
calculations ("Limited" view/edit/delete) are captured: the text sits in the
set's `calc_text`, and the fields it names are `calc`-context refs sourced from
the `privilege_set` entity.

**Layout context TO (v1.12.0):** every layout definition emits exactly one
`layout_context` ref to its context TO (menu separators named "-" emit none).
FileMaker ids collide across the files of a multi-file solution, so the
resolver keys on the layout's own file plus the TO's FileMaker id, never the id
alone. A DDR writes `<Table id="..." name=""/>` when the layout's TO was
deleted; such refs stay unresolved (`target_entity_id IS NULL`, `target_fm_id`
kept). The `<Table>` inside a Go to Related Record or Import Records step is
not a layout context: it is the `step_table` context (v1.14.0). New Window
carries no `<Table>`; its layout is a `go_to_layout` ref.

**Menu flags (v1.14.0):** scripts, script folders, layouts and layout folders
carry `extra_json.in_menu = true` when their "Include in menu" checkbox is on
(no key when it is off); scripts also carry `extra_json.run_full_access = true`
when "Run script with full access privileges" is on. These are the DDR's raw
attributes (`includeInMenu`, `runFullAccess`), nothing more. Whether a flagged
script inside a cleared folder is reachable from the Scripts menu is the
help's inference (a folder "appears as a submenu"; clearing its checkbox
removes it), unverified in FileMaker Pro: QUERIES.md carries the folder walk
as a recipe labelled as such, and `v_orphan_scripts` has no menu column. The
web viewer's orphan panel does not show the flag; the SQL does.

**Disabled steps (v1.6.1):** the DDR includes commented-out (`enable="False"`,
`//`-prefixed) script steps, and their references used to count as live usage.
Edges from disabled steps are now flagged `refs.disabled = 1` and excluded
from `v_usage` and the health views; `v_usage_disabled` exposes them, and
`v_unused_fields` / `v_orphan_scripts` mark entities whose only references are
dead code with `only_disabled_refs = 1`. Steps themselves carry
`extra_json.disabled`.

**Field storage & auto-enter (v1.9.0):** fields carry `stored` (0 = unstored
calc — the classic FM performance hazard), `indexed` (None/Minimal/All),
`is_global`, and `auto_enter` (JSON: calc, calc_active, type, lookup,
always_evaluate, overwrite_existing). The DDR retains auto-enter calc text
even after the "Calculated value" option is **unchecked** — such **dead
residue** is marked `auto_enter.calc_active = false` and its refs get
`disabled = 1`, so leftover calcs no longer produce false where-used
positives (they remain visible in `v_usage_disabled` and in FTS). Auto-enter
and validation calc text lives in `auto_enter`/`extra_json.validation_calc`,
NOT in `calc_text` — `calc_text` is now exclusively the field's own formula.

**Relationship attributes, value-list definitions & layout text (v1.10.0):**
relationships carry `extra_json.sides` (`cascade_create`/`cascade_delete` per
end — `v_cascades` view + `cascades` command answer "what deletes into this
table by cascade") and `extra_json.predicates` (operator + field pair per join
predicate). Value lists carry their DEFINITION (`source`, `custom_values`,
`primary`/`secondary` display fields, `show_related`) — `valuelist` command;
custom values are FTS-searchable. Layout TEXT objects' character runs (incl.
merge fields `<<F>>` and merge formulas `<<ƒ:...>>`) land in
`extra_json.text` and the FTS body — layout text is finally searchable.
Merge-formula field refs are TEXT, not structured edges (stage b, future).

**Serial numbers & lookups (v1.9.1):** serial auto-enters carry
`auto_enter.serial` (`{increment, nextValue, generate}`); lookups carry
`auto_enter.lookup_source` (`TO::Field`) + `lookup_active`, and emit a
`lookup`-context ref so the source field's where-used includes its copies.
Dead lookups (configured, option off) follow the same residue rule as dead
auto-enter calcs: `lookup_active=false`, ref `disabled=1`.

## NOT captured (blind spots — use FTS `search` as the fallback)

| Usage pattern | Why | Fallback |
|---------------|-----|----------|
| **Fields named inside `ExecuteSQL` strings** | The DDR itself doesn't resolve SQL text; it's just a string | `search db "fieldname"` |
| Detailed validation rules (unique, existing, range, strict type, not-empty) | Only validation *calcs* (and value-list bindings) are captured | inspect in FileMaker |
| Fields/scripts named in **any calculated string** (`GetField`, `Evaluate`, script names built at runtime) | Same: text, not structure | `search` |
| **Built-in function** usage as edges | Deliberately excluded (tens of thousands of `Get()` edges would drown the graph) | `search db "ExecuteSQL"` |
| The **table occurrence of a lookup** (`<Table>` under `<Lookup>`) as a TO reference | Only the source field is captured (`lookup` context, whose `target_to_name` keeps the TO off the unused list); `where <TO>` does not show the lookup | `search` |

**Captured but easy to miss** (verified against a real solution, v1.9.1): conditional-formatting
condition calcs, placeholder-text calcs, and tooltip/hide calcs all emit `calc`-context refs
sourced from the enclosing **layout object** — so where-used DOES include them (they are not
distinguishable from each other by context, but no edge is lost). Custom-menu items that run
scripts emit `perform_script` refs sourced from the **custom_menu** entity — menu-run scripts do
NOT appear in `v_orphan_scripts` claims wrongly. Validation by value list emits a
`value_list_source` ref from the field.

## Multi-file solutions

Build ALL files of a solution into one DB (`build Summary.xml` or list every
`*_fmp12.xml`). Cross-file references are resolved **only via explicit
FileReference markers** (external Perform Script / Go to Layout, and field
refs through external table occurrences) — never by name guessing. An external
ref whose target file is not in the DB stays unresolved rather than being
mis-linked to a same-named/same-id local object.

**External data sources (v1.13.0):** a FileReference marker carries the data
source's display NAME (`Attachments`, `Archive`), which need not equal the
file name. The `<ExternalDataSourcesCatalog>` maps each name to a sibling DDR
file through its `link` attribute (`X_fmp12.xml` for `X.fmp12`); the catalog
sits at the END of the DDR, so the mapping is applied at resolve time, in SQL,
never while parsing. `v_data_sources(file_id, ds_name, link, target_file_id,
target_file_name)` holds one row per name a file uses: the file is found by
the catalog link first, then by the name itself (the pre-1.13.0 rule); a name
with neither match has a NULL target. Field refs through an external table
occurrence, and script or layout refs behind a FileReference, resolve only in
the file the data source points at; a TO on an external data source never
resolves to a local field with the same base table name (the local rule skips
every TO with an `ext_file`). A data source may name its own file
and then resolves to it like any other. A TO or step whose data source no
longer exists (`<FileReference id="N" name=""/>`) carries `ext_file =
'<unknown>'` or `target_file = '<unknown>'` and never resolves; the TOs are
listed by `v_dangling` as `missing_data_source`.

## Dangling references (v1.13.0)

Two kinds of marker exist. The DDR's own: `<Table Missing>`, `<Field Missing>`
and `<Function Missing>` inside calculation and step text, and
`name="<unknown>"` on a deleted script. fmsonar's own: the string `<unknown>`,
written into `target_raw`, `ext_file` or `target_file` wherever the DDR left a
REQUIRED name empty or missing. A ref's `target_name` tells them apart: the
DDR's marker arrives as `target_name = '<unknown>'`, fmsonar's rewrite leaves
`target_name` NULL.

One rule for every reference slot, in both parsers: **an empty or missing name
on a required slot is `<unknown>`; an empty name on an optional slot emits
nothing.** The optional slots, each identified by the DDR's own convention, are
exactly these five:

1. a value list's `SecondaryField` (an unset second display field; the id is
   non-zero and plays no part);
2. any field slot of a value list whose `Source` is not `Field` (a Custom or
   "all values" list keeps stale field ids that mean nothing);
3. a step `<Field>` with **no `id` attribute** (a target that was never set:
   Insert Text, Insert from URL, Execute FileMaker Data API, Open Data File,
   Insert Calculated Result, Get File Exists, Read from Data File, Get File
   Size, Write to Data File);
4. `<Script id="0" name=""/>` under a trigger (an unset trigger script);
5. a `<Layout>` whose step's `LayoutDestination` is `OriginalLayout`,
   `CurrentLayout`, `LayoutNameByCalc` or `LayoutNumberByCalc` (a runtime
   destination; the by-calc `<Calculation>` still lands in the step's
   `calc_text`).

Everything else with an empty name is a deleted target and emits an
unresolved ref: a step field with an id and no name reads `<Table Missing>`
when the step text carries that literal, else `<unknown>`; a field with a
table and no name reads `TO::<Field Missing>` when the step text carries that
literal, else `TO::<unknown>` (sort criteria in steps, portals and
relationships, join predicates); a `<Layout>` with any other destination reads
`<unknown>`; a `<Script>` with a non-zero id and no name reads `<unknown>`; a
table occurrence slot with no name reads `<unknown>` **whatever the id**: a
layout's context `<Table>`, a step's `<Table>` (the step text says `From
table: <unknown>`, with id 0 or not), a portal's `TableAliasKey` (no id at
all) and a related-values list's `<Table>` (v1.14.0; the 1.12.0 exception
that dropped a layout context with id 0 is gone, it never fired on a real
DDR). A layout field object whose `<Name>` is empty is a required slot too:
its field or table occurrence is gone, the DDR writes the name empty with no
other marker, and the ref reads `<unknown>` (`layout_object` context, name
and TO NULL); 1.13.0 dropped these silently. Ids never decide: an id that
happens to exist is never resolved behind a marker.

A value list cloned into a file that lacks its table occurrence (the usual
satellite-file case) is one dead list with several slots: its
`value_list_field` rows and its `value_list_context` row each read
`<unknown>`, one row per slot, not one finding. Import Records into a "New
Table" was not seen in any DDR at hand: if FileMaker writes it as an empty
`<Table>`, that step would read `<unknown>` too (unverified).

**`v_dangling`** lists every marked defect, one row each, with `context`,
`marker` (the raw text), `marker_class` (`<unknown>`, `<Table Missing>`,
`<Field Missing>`, `<Function Missing>`), the source and its parent, the
step type, and `disabled` (included with the flag, never filtered). Three
branches: unresolved refs whose `target_raw` carries a marker or whose
`target_file` is `<unknown>`; **broken calculations**, one row per entity and
marker found in `calc_text`, the auto-enter calc, the validation calc, a hide
calc or a tooltip calc (`context = 'broken_calc'`, `ref_id` NULL); table
occurrences on a missing data source (`context = 'missing_data_source'`).
On a SaXML index (v1.15.0), a `<Field Missing>` row of an entity whose every
`<Field Missing>` follows an external TO with no data source is
`context = 'unverifiable_xfile'` instead of `broken_calc` (see "SaXML next to
the DDR"); a DDR index never has that context.

Blind spots: a marker the DDR writes only in a step's TEXT, with no reference
element, has no ref and no row: `Add Account [ ...; Privilege Set: <unknown> ]`,
an `Open File` whose file marker is empty. Find those with
`json_extract(extra_json,'$.step_text') LIKE '%<unknown>%'` on `script_step`
rows (QUERIES.md). Go to Related Record `From table: <unknown>` has a row
since 1.14.0 (`step_table`); its "Using layout: <unknown>" twin is the
`go_to_layout` row of the same step.

An unresolved ref is therefore one of three things: a marked defect
(`v_dangling`), an external target whose file is not in the DB
(`target_file` set to a real name), or a built-in or calculated name.

## SaXML next to the DDR (v1.15.0)

`build` also reads FileMaker's **Save a Copy as XML** export (root element
`FMSaveAsXML`, "SaXML" below) into the same index, with the same kinds,
contexts, views and markers. The DDR stays the reference format: every rule in
this file was written for the DDR first, and the SaXML handler mirrors it rule
by rule. The command-line tool only: the browser viewer refuses a SaXML file with a
message that names the format.

**Which format built an index.** `ddr_run.source_format` is `ddr` or `saxml`;
`ddr_run.source_version` is the FileMaker version that wrote a SaXML export
(its `Source` attribute) and NULL for a DDR; `ddr_run.ddr_version` is the root
element's own version for both. An index built before these columns existed
has neither and is a DDR index. `stats` prints the source on its first line,
`list` has a format column, and `tools/compare_resolution.py` prints a
cross-source banner when the two indexes differ in format.

### Supported versions and the refusal rule

| `FMSaveAsXML@version` | Written by (measured) | Test fixture |
|---|---|---|
| `2.3.0.0` | FileMaker 26.0.1 (FMDeveloperTool 26.0.1) | `tests/fixtures/saxml_2.3.0.0.xml`; `tests/fixtures/saxml_lab3_2.3.0.0.xml` (lab v3: button bar, grouped button, popover with a title calculation, tab and slide panels, web viewer, custom menu items with steps, a sub-summary part, a validation calculation, a placeholder, layout and object triggers, Send Mail calculations, Go to Related Record into a new window); `tests/fixtures/saxml_xfile_fmdt_2.3.0.0.xml` and `saxml_xfile_sat_fmdt_2.3.0.0.xml` (the two-file lab exported by FMDeveloperTool: the empty cross-file shapes and `unverifiable_xfile`); `tests/fixtures/saxml_partial_2.3.0.0.xml` (a `-catalog_list` export, refused) |
| `2.3.0.1` | FileMaker 26.1.1 (Pro 26.1.1; a Server 26.1 export is not yet measured) | `tests/fixtures/saxml_2.3.0.1.xml` (the lab file in a later state than `saxml_2.3.0.0.xml`: its `OnFirstWindowOpen` script trigger removed, which `TestSaXMLVersions` names); `tests/fixtures/saxml_xfile_2.3.0.1.xml` (a two-file lab exported by Pro with both files open: the filled cross-file shapes) |

Both versions share one rule set (the 26.1 export only adds elements fmsonar
does not read), so exports of the two build together: `files.format_version`
and `files.source_version` record each export's, `stats` lists them per file,
and on such a build `ddr_run.ddr_version` and `source_version` name both
(`2.3.0.0, 2.3.0.1`). A patch release that keeps the version string is
accepted; the FileMaker build is recorded in `source_version`. `build` refuses, with a
message that names the file and what to do:

- a file whose root element is neither `FMPReport` (a DDR) nor `FMSaveAsXML`.
  The format is read from the real root element by the XML parser itself,
  after any XML declaration, comments and DOCTYPE however long; text before
  the root (a comment that names the other format) never decides it. A
  DOCTYPE with an internal subset, or an `ENTITY` declaration, is refused
  before any parse, in both formats;
- any other version string (FileMaker 19 to 22 write `2.2.x`): the message
  lists the supported versions from the version table; export with FileMaker
  26, or build from a DDR;
- an export made **without** "Include details for analysis tools"
  (`Has_DDR_INFO` absent; FMDeveloperTool: `-include_details`): step text and
  calculation references exist only in that section;
- an export with `split_catalogs` or `binary_under_lo` (FMDeveloperTool's
  `-split_catalogs` writes one file per catalog with both attributes on);
- a **partial export**, which FMDeveloperTool's `-catalog_list` writes: it
  writes a section only when the list names it, `Metadata` included (measured,
  FMDeveloperTool 26.0.1). Indexed, such a file would list used fields as
  unused and called scripts as orphaned. Refused, naming the sections:
  - an export that lacks a section every whole export writes:
    `FileAccessCatalog`, `BaseTableCatalog`, `TableOccurrenceCatalog`,
    `FieldCatalog`, `ThemeCatalog`, `LayoutCatalog`, `PrivilegeSetsCatalog`,
    `ExtendedPrivilegesCatalog`, `AccountsCatalog`, `Metadata`;
  - an export that lacks a catalog a whole export leaves out only when the
    file has none of its kind, while it still refers to that kind:
    `ScriptCatalog` with a script reference (a button, a script trigger, a
    custom menu item; a cross-file Perform Script names another file's script
    and does not count), `ValueListCatalog` with a value list reference,
    `CustomFunctionsCatalog` with a custom function in a calculation,
    `ExternalDataSourceCatalog` with an external table occurrence or a data
    source reference.

  Not detectable: a `-catalog_list` export that names every section above but
  leaves out `RelationshipCatalog`, `CustomMenuCatalog` or
  `CustomMenuSetCatalog`. Nothing else in the export refers to a relationship,
  and a whole FMDeveloperTool export already leaves out the standard menus, so
  their absence is no tell. Such an index lacks the join predicates (a field
  used only in a relationship reads as unused) or the menu items (a script
  called only from a menu reads as orphaned);
- an export whose root `File` attribute is empty (the index names the file
  from it);
- two exports of the same file in one `build` (the same path twice, or a stale
  copy next to the current one: same `File` attribute);
- a DDR and a SaXML export in one `build` (one source format per index);
- an export whose analysis section (`DDR_INFO`) holds no entry while the
  export refers to it: a cut-off or emptied export;
- an export where more than 10 percent of the step texts it refers to are
  missing from `DDR_INFO`: every whole export measured misses none, so the
  file is damaged or was edited. Calculations with no chunk list of their own
  are a normal class (below) and never refuse. Every miss is recorded:
  `ddr_run` holds `ddrinfo_refs` (`DDRREF` elements read),
  `ddrinfo_missing_steps` and `ddrinfo_missing_calcs` (calculations whose
  references are lost, one per distinct key and hash: the export writes one
  calculation in several places; below); NULL on a DDR index and on an index
  built before the columns.
  `build` prints the counts in its `WARNING:` block, `stats` and `list` show
  "N calculation(s) without reference list", and `where`, when it finds no
  reference to an object on such an index, prints the full-text mentions of
  its name beside the 0.

A missing file, a folder or an unreadable path gives one `error:` line naming
the path. **Locale.** Every export measured so far says `locale="English"` on
its root. A few rules read the step text the export writes, which is English
there: a deleted layout in Go to Layout, Go to Related Record and New Window
(`layout: <unknown>`), a cross-file Perform Script (`from file:`), the
written-out text of a button's Perform Script, Insert Text's literal and the
step name at the start of the step text. An export with another locale, or
none, **builds with a warning** that points at this list, rather than being
refused: everything read by id (catalogs, field, script, layout and TO
references, calculation chunks) does not depend on the language, and a
refusal would withhold that whole index for a handful of rules. Until such an
export is measured, on it a deleted-layout row can be missing from
`v_dangling` and a cross-file call can show as a deleted script.

Several SaXML exports in one `build` make one index with one `files` row per
export, named by the export's `File` attribute. The script step and
FMDeveloperTool write no summary. The menu command File > Save a Copy as XML in
FileMaker Pro writes one export per open file plus a summary
(`FMPReport type="XMLv2_Summary"`) that lists them; `build` refuses that
summary by name, so pass the per-file exports.

Cross-file references between two SaXML exports built together, measured on
the two-file lab:

- **Both files exported by FMDeveloperTool 26.0.1**
  (`tests/fixtures/saxml_xfile_fmdt_2.3.0.0.xml` with
  `saxml_xfile_sat_fmdt_2.3.0.0.xml`): one index, one `files` row each, and
  `v_data_sources` maps the data source to the satellite's row; still nothing
  resolves across the files, because the main file's export leaves every
  reference into the other file empty (the external TO, the cross-file Perform
  Script, the fields through the TO).
- **The main file exported by FileMaker Pro 26.1.1 with the satellite open**
  (`tests/fixtures/saxml_xfile_2.3.0.1.xml`, built alone): the references
  into the satellite are filled and name it (`ext_file`, `target_file`). With
  an FMDeveloperTool 26.0.1 export of the satellite in the same build
  (`saxml_xfile_sat_fmdt_2.3.0.0.xml`), every one of them resolves into the
  satellite (`calc`, `join_predicate`, `step_target`, `perform_script`) and
  `v_dangling` is empty. A satellite export by Pro 26.1 is not measured.

### The FMDeveloperTool route and other files

A SaXML export can carry the full cross-file reference: with the sibling file
open, FileMaker Pro 26.1 writes an external table occurrence with its data
source and base table, and a cross-file Perform Script with its data source
and script (measured on a two-file lab). **FMDeveloperTool leaves those slots
empty**, with or without the sibling files next to the exported file
(measured). On that route:

- **External table occurrences** carry no data source: `ext_file` and
  `base_table` are NULL and `extra_json.external` is true. Field references
  through them stay unresolved (`target_name` NULL, `target_raw` NULL,
  `target_to_name` the TO), never mis-linked and outside `v_dangling` for
  sort fields and join predicates. A layout field object, a table view column
  or a script step's target field through such a TO is one
  `unverifiable_xfile` row in `v_dangling` (below). `build` prints one `WARNING:` block, before
  its `Done:` line, that gives the count, says what it does to field
  references, how many calculations and field references land in
  `unverifiable_xfile`, and names the route measured to fill them (FileMaker
  Pro with every file open; FileMaker Server is not yet measured); `stats`
  prints the same count. A field reference the DDR marks `<Field Missing>`
  through such a TO (a real defect across files) is therefore not confirmed
  on this route: a step target is an `unverifiable_xfile` row, a sort field or
  join predicate has no row.
- **Cross-file calls** (Perform Script from another file) carry no script:
  the row has `target_file` = the file name taken from the step text, name and
  `target_raw` NULL, unresolved and outside `v_dangling`.
- **Calculations through an external TO** carry no structured reference: the
  export's analysis section names no field through an external TO, so those
  `calc` refs are absent (the DDR has them, unresolved, in a single-file
  build). The calculation text is still indexed for FTS.
- **unverifiable_xfile (v1.15.0).** On an FMDeveloperTool export, a
  calculation that uses a field in another file reads `TO::<Field Missing>`
  in its text (fields, steps, layout objects alike). The export writes that
  both for a field that exists in the other file and for one that was deleted
  there, so it cannot tell the two apart. An entity whose every
  `<Field Missing>` follows an external TO with no data source carries
  `extra_json.xfile_marker` and its `v_dangling` row has
  `context = 'unverifiable_xfile'`, not `broken_calc`: unverifiable in this
  export, not a confirmed defect. Most of these name a field that exists in
  the other file, but some are real cross-file defects (the field was deleted
  there), and this route lists those as unverifiable too: only the DDR, or a
  Pro export with every file open, tells them apart. A layout field object
  through such a TO has no field name in the export, for a live field and a
  deleted one alike, so it gets one `unverifiable_xfile` row too (marker
  `TO::<unknown>`, `ref_id` set; a table view column and a script step's
  target field alike). One bare
  `<Field Missing>` or one through a local TO keeps the entity in
  `broken_calc`, and `<Table Missing>` and `<Function Missing>` always stay
  there. The DDR route tells the two apart and keeps them as defects. FileMaker Pro 26.1 with every
  file of the solution open fills it (measured on a two-file lab): the
  calculation text names the field (`ext_TO::Name`), its chunk list carries
  the field reference through the external TO, the external TO carries its
  data source and base table, and the step text names the field. The same
  lab exported by FMDeveloperTool writes `ext_TO::<Field Missing>` and leaves
  every one of those empty.

The DDR, built from `Summary.xml` with every file, stays the route for a fully
resolved multi-file index.

### Expected differences between a DDR index and a SaXML index

Measured on a real multi-file solution (a DDR and a SaXML export of the same
file a day apart) and on the lab file. `compare_resolution.py` exits 1 on any
lost or changed key, so on such a pair it exits 1 whenever one of these
classes applies (the lab pair: 2 lost keys, exit 1); every lost, changed and
gained key falls in one of these classes.

Not emitted or not filled from SaXML:

- `to_reference` and `field_reference` refs are not emitted: in SaXML those
  elements are the universal pointer, and every usage they stood for has its
  own context.
- `base_table.records` is NULL (the export has no record counts; nothing in
  fmsonar reads the column).
- **Chart series** calculations have no reference owner in the export: their
  field and function references are lost.
- **Calculation variants without a chunk list.** The export's analysis section
  keys a calculation by text and hash; some variants of a calculation have no
  chunk list of their own, and their field and function references are lost
  (button actions, Set Field repetitions, button step parameters, conditional
  formatting and Send Mail button parameters are the usual owners).
  Calculations on layout objects are the most affected class: on a large real
  solution several percent of the calculation references on layout objects
  were lost, script steps well under one percent, field calculations none.
  In the exports measured every such reference carries an empty hash and the
  export holds a list for the same key under another hash. Where that list
  names no field or custom function, nothing is lost and the calculation is
  not counted (lab v3's Go to Related Record layout calculation is one: lab v3
  counts 0); where it names some, those references land only on the other
  copy's object, not on this one, and the calculation is counted. On the real
  solution about half of the keys fall in each class, and the references a
  replay of the counted ones would add are of the order of the measured loss. The share
  differs per solution: `build` warns with the count, `stats` and `list` show
  it ("N calculation(s) without reference list", one per distinct
  calculation), and `where` prints the full-text mentions of a name it finds
  no reference to. A used custom function can then have no reference at all,
  and only the text shows its use. FTS keeps the text. On an index with such
  calculations or `unverifiable_xfile` rows, `stats` and the report's health
  page say that unused candidates may include objects used only there.
- **Chunk lists that omit a name.** For a few calculations the export's own
  chunk list (an exact hit) carries no reference for a field or function the
  calculation text names; those references are lost. FTS keeps the text.
- The file's **open-layout default** (`Metadata/AddAction/Defaults`) is not
  read: a parser gap, named, not yet fixed. (A popover's title calculation,
  `LayoutObject/Title` with no `Calculation` wrapper, is read as the
  popover object's calculation, as the DDR's `TitleCalc`.)
- Table occurrences behind an empty external TO carry `base_table` NULL; a
  compare reports the refs to them as changed, though the TO and its `fm_id`
  are the same. A cascade delete into such a TO has `v_cascades.victim_table`
  NULL: `cascades <db> <table>` lists those after the matched rows as
  candidates ("base table unknown on this export"), and an unfiltered run
  counts them; the index cannot know the table in the other file.
- **`missing_data_source` never fires on the FMDeveloperTool route.** Every
  external TO there is empty, so a TO whose data source was deleted cannot be
  told from one on a live file: the DDR lists it in `v_dangling` as
  `missing_data_source`, the SaXML index of the same file does not.
- **A table-view-only column's tooltip.** The DDR writes such a column as a
  hidden field object and keeps its tooltip calculation; the export writes the
  column with its field only, so the tooltip and its `calc` refs are absent.
- **A Save a Copy as XML step's option calculation.** FMDeveloperTool 26.0.1
  writes none for the step (its `calc_text` is NULL); FileMaker Pro 26.1.1
  and the DDR write it.
- **A value list binding on an edit-box field object** (a SaXML gap). The DDR
  writes one (`<ValueList>` on a `FieldObj` of display type 0); the export
  writes the object as an edit box with no value list. The file holds the
  binding: read with the FileMaker CLI on the lab file, the edit box carries
  its value list (control style edit box, auto-complete off; an edit box
  uses the list only with auto-complete on). On a SaXML index `valuelist` and
  `where` miss such a binding: a value list bound only to edit boxes answers
  0 references there (the lab: 1 on the DDR, 0 on SaXML).

Different form, same usage:

- A deleted script marked by the DDR arrives with `target_name =
  '<unknown>'`; the SaXML has no element for it, so its row carries
  `target_name` NULL and the same `target_raw = '<unknown>'`. Both are in
  `v_dangling`.
- **Names with a character outside the Basic Multilingual Plane** (an emoji in
  a script name): the DDR writes it as `??`, the SaXML keeps it. Every
  reference that carries such a name compares as lost plus gained.
- The source key of a layout object reference can differ (another parent or
  object name for the same target on the same layout).
- `extra_json.key` of a layout object is the SaXML object id, not the DDR key;
  `extra_json.object_type` is stored as the export writes it (`Edit Box`,
  `Grouped Button`, `Panel`, `Web Viewer` where the DDR writes `Field`,
  `GroupButton`, `SlidePanel` or `TabPanel`, `ExternalObject`).
- Step text and calculation text are stored with CR normalised to LF (the
  DDR ends the lines of a button's step text with CR).
- **A button's Perform Script.** The export writes no step text for it; the
  SaXML route writes it as the DDR does, `Perform Script [ “Name”; Parameter:
  <calc> ]` (`“<unknown>”` for a deleted script), so `investigate` lists
  layout launch sites with their parameters on both routes.
- **Insert Text's literal** is in the export's parameter, not in its step
  text after a variable target; the SaXML route puts it into the step text as
  the DDR writes it (`Insert Text [ $v; “literal” ]`), so FTS finds SQL and
  JSON templates kept there on both routes.
- **Names in step text can differ in letter case.** The step text quotes
  calculations as the export writes them, and the export spells some Get
  functions in another case than the DDR (`CurrentTimeStamp` against
  `CurrentTimestamp`). FileMaker function names are case-insensitive; the
  index keeps the export's text. A case-sensitive `LIKE` or `instr()` on step
  text can therefore match one format and not the other; FTS is
  case-insensitive.
- **A custom menu's step text.** On SaXML a custom menu's
  `extra_json.step_text` holds the step text of every item that runs a step,
  one line per item in item order; the DDR route keeps only the last item's
  (a menu with several such items shows one of them). Its references
  (`perform_script`, `go_to_layout`, ...) are complete on both routes.
- **Values the export writes in another form, kept in the DDR's.** A field or
  variable target's repetition (Set Field, Go to Field, Insert Text into a
  variable, ...) is a calculation in the export and stays out of the step's
  `calc_text`, as on the DDR (Set Variable's own repetition stays in, as on
  the DDR). Show Custom Dialog's button labels are attributes in the export;
  they are written into `calc_text` as the DDR's quoted calculations
  (`"OK"`), after the title and the message. A field's `auto_enter` JSON
  carries `always_evaluate` and `overwrite_existing` only for an auto-enter
  calculation, a calculation field, or when one of them is on (FileMaker Pro
  26.1 writes both attributes on every field; the DDR does not).
- Spellings translated to the DDR's: data type `Timestamp` to `TimeStamp`, step
  `Set Field By Name` to `Set Field by Name`, relationship operators
  (`Greater` to `GreaterThan` and so on), theme names (the display name, `fm_id`
  as two-digit hex). These and the Show Custom Dialog labels above are
  normalisation, kept on purpose: a theme's `fm_id` and a step's `calc_text`
  then compare equal across the two routes (the `fm_id` is the identity a
  comparison keys on), and `search` finds a dialog's button label on both.
- **Dangling portal sort fields** differ in marker and count: the DDR marks
  most of them `<unknown>`, the export `<Table Missing>`, and `v_dangling`
  lists a different number of such `sort` rows on the two routes (the export
  also keeps the sort fields of a portal with sorting off, below).
- **`v_ambiguous`.** The DDR writes some layout field objects without their
  table occurrence, and a field name several tables share then resolves as a
  guess (`v_ambiguous`); the export names the occurrence and the field id on
  every field object, so `v_ambiguous` stays empty there.
- **The full-text body of a layout text object.** The DDR's repeats the
  object's text (the lab: `Text [Name_First] [Name_First]`), the export's
  does not: `search` finds the object on both, its snippet differs.

Present in one format only:

- **Table view columns.** The SaXML lists a layout's table view columns. A
  column whose field is not a form field object of the layout is read as a
  `layout_object` (`extra_json.table_view_only` true, `object_type` `Edit Box`,
  no bounds). Column ids are their own id space: a column id equal to the id
  of another object on the layout says nothing. The DDR writes most of these
  as hidden field objects and omits some columns entirely; fmsonar reads every
  such column, so a SaXML index can list a field on a layout where the DDR
  index does not.
- **Stale residue the DDR keeps:** stale field slots on Custom value lists, stale names on field-based
  lists whose fields are gone (the SaXML gives one `<unknown>` there), and the
  custom values of a list switched to field values (`extra_json.custom_values`;
  the export writes none).
- **Dormant specifications:** the DDR keeps a Go to Related Record's
  new-window calculations with "Show in new window" off, and a Sort Records
  step's sort order with "Restore" off (the SaXML writes none); the SaXML keeps a
  portal's sort fields with sorting off (portal `Options` bit 8 clear, read
  from the measured split, not from Claris documentation). Neither is a live
  usage.
- **Usages the DDR route misses:** the callback script of Perform Script on
  Server with Callback (a `perform_script` ref only from SaXML); a sub-summary
  part's break field (a `part_break` ref from the layout, only from SaXML: the
  DDR writes no layout parts); steps whose
  text says `<unknown>` for a script or layout while the DDR writes no
  reference (the SaXML emits the `<unknown>` row).
- Catalog counts that can differ by file state: themes and custom menu sets
  (the SaXML catalog can list fewer).
- **Custom menus depend on the exporting tool:** FileMaker Pro writes the
  standard menus into the export, FMDeveloperTool writes a file's own custom
  menus only.

Validation calculations are measured on lab v3 (`validation` refs, as the
DDR). Unverified shapes: ODBC data sources, charts and conditional formatting
(neither can be authored in the lab without FileMaker Pro).

### SaXML has, fmsonar does not read

Summary field sources, calculation context TOs, layout parts other than a
sub-summary's break field, step `OwnerID`, `SourceUUID`, privilege detail
per category, `Metadata/Defaults`, and the `Options` bitfields beyond the menu
flags.

### Per-object UUID and last change

Every catalog entity (tables, fields, TOs, relationships, scripts and script
folders, layouts and layout folders, value lists, custom functions, privilege
sets, accounts, extended privileges, custom menus and menu sets, themes, data
sources) carries in `extra_json`: `uuid`, `modified_at` (the export's
`timestamp`), `modified_by` (`userName`), `modified_account` (`accountName`)
and `modified_n` (the modification count). Script steps and layout objects
carry `uuid` only. An empty attribute writes no key. A DDR index has none of
these keys (QUERIES.md: "Last change per object"). `modified_by` and
`modified_account` name developers: they stay in the index and the HTML
`report` never embeds them (SECURITY.md).

### Credentials

FileMaker's own credential elements are never indexed: the handler skips
`PasswordEncrypted`, `INSECURE_PASSWORD`, `INSECURE_TEXT`, the auto-login
`Login`, and the file access `Authentication`; under an account only its name
is read. The guard is that no rule of the handler reads those elements. A test
builds every SaXML fixture with a sentinel in every credential element and
asserts it appears nowhere in the index. The same test proves the check can
see a leak: a test-only handler that also reads an account's
`PasswordEncrypted` text as its name keeps the sentinel out while the skip
list holds, and writes it into the index once the skip list is emptied. It
cannot see a future rule that reads a credential element under a path the
fixtures do not carry. A password typed as a literal into a calculation (Send Mail, Add
Account, Re-Login) is the developer's text and is indexed by both formats, as
any calculation is. The FMDeveloperTool route takes the account password as a
command-line argument, visible in the process list while the export runs;
that is outside fmsonar.

## What a rename breaks

FileMaker stores structural references by id and re-points them when you
rename a field, script or layout: a Set Field target, a Perform Script call, a
relationship predicate, a layout object binding, a step's table, a portal's
table occurrence and a value list's context all survive a rename. What
breaks is TEXT: `ExecuteSQL` strings, `GetField` / `Evaluate` arguments,
script and layout names built at runtime (`Perform Script [ By name ]`,
`Go to Layout [ By name ]`), and callers outside the file (other FileMaker
files, OData and Data API clients). fmsonar's `used` answers "is it
referenced", not "will a rename break it": for the breakage list, the text
gap is the FTS `search` today, and a `text_mention` context is planned for
1.16.0.

## Consumer contract

`ddr_run.parser_version` records the fmsonar version that built an index. A
consumer that needs a view or column introduced in version N compares that
value and asks for a rebuild when it is older; the `fmsonar` CLI prints the
rebuild hint itself when a view is missing (`cascades` exits on it, `stats`
prints one line and continues). Views added per version: `v_cascades`
(1.10.0), `v_triggers` (1.9.0), `layout_context` refs (1.12.0),
`v_data_sources` and `v_dangling` (1.13.0), `step_table`, `portal` and
`value_list_context` refs, the `<unknown>` field objects and the menu flags
(1.14.0), SaXML input with `ddr_run.source_format` and `source_version`, the
per-object UUID keys in `extra_json`, the `unverifiable_xfile` context of
`v_dangling` and the SaXML-only `part_break` context (1.15.0). For 1.16.0: a `text_mention`
ref counts as a use only when `target_entity_id IS NOT NULL`; an unresolved
mention never does.

## Practical rule

Before acting on "unused" or "no references":

1. `python3 -m fm_ddr.cli search db.db "name"` — catches every textual mention.
2. Remember what no tool can see: external systems calling the FM file (OData,
   Data API, other files), and users running scripts from the Scripts menu.
