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
| `layout_object` | Field placed on a layout | `FieldObj > Name`, `FieldReference` under `Layout` |
| `step_target` | The field a step acts on: **Set Field's write target**, Go to Field, Insert ... — combine with the step's `step_type` to tell writes from navigation | direct `<Field>` child of `<Step>` |
| `field_reference` | Field referenced via a `FieldReference` element outside layouts | `FieldReference` |
| `perform_script` | Script called by a script step | `Script` reference elements |
| `trigger` | Script attached to a script trigger; `refs.trigger_event` records the firing event (OnRecordCommit, OnObjectSave, ...) (v1.9.0). **Trigger refs are sourced from layouts/layout objects, not scripts** — query them via `v_triggers`, never via a script-parent join. v1.9.1 adds **file-level triggers** (`WindowTriggers`: OnFirstWindowOpen, OnLastWindowClose, OnWindowClose — the startup/shutdown scripts); they appear in `v_triggers` with `layout_name IS NULL` | `Script` inside `ScriptTriggers` (event from the enclosing `Trigger`) or inside `WindowTriggers` (event = wrapper tag) |
| `go_to_layout` | Layout targeted by a Go to Layout step/button | `Layout` reference elements |
| `to_reference` | Table occurrence referenced directly | `TableOccurrenceReference` |
| `layout_context` (v1.12.0) | A layout's own context table occurrence ("Show records from"). Source = the layout, target_kind = `table_occurrence`. A TO used only as a layout context is no longer "unused". Resolved by (file, FileMaker id), then name. An **empty** `name` means the TO no longer exists: the ref keeps `target_fm_id` and stays unresolved, a real defect worth reporting (QUERIES.md: "layouts whose context TO is missing") | The `<Table id name/>` that is the direct child of a layout definition |

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
kept). The `<Table>` inside Go to Related Record or New Window steps is not a
layout context and is not captured by this rule.

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
| **Import/export field orders** | Not yet mapped | `search` |

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
layout's context `<Table>` with a non-zero id and no name reads `<unknown>`.
Ids never decide: an id that happens to exist is never resolved behind a
marker.

**`v_dangling`** lists every marked defect, one row each, with `context`,
`marker` (the raw text), `marker_class` (`<unknown>`, `<Table Missing>`,
`<Field Missing>`, `<Function Missing>`), the source and its parent, the
step type, and `disabled` (included with the flag, never filtered). Three
branches: unresolved refs whose `target_raw` carries a marker or whose
`target_file` is `<unknown>`; **broken calculations**, one row per entity and
marker found in `calc_text`, the auto-enter calc, the validation calc, a hide
calc or a tooltip calc (`context = 'broken_calc'`, `ref_id` NULL); table
occurrences on a missing data source (`context = 'missing_data_source'`).

Blind spots: a marker the DDR writes only in a step's TEXT, with no reference
element, has no ref and no row: `Add Account [ ...; Privilege Set: <unknown> ]`,
Go to Related Record `From table: <unknown>` (until a `step_table` context
exists), an `Open File` whose file marker is empty. Find those with
`json_extract(extra_json,'$.step_text') LIKE '%<unknown>%'` on `script_step`
rows (QUERIES.md). A real layout whose context `<Table>` has id 0 and no name
is dropped like a separator (none seen in a real DDR).

An unresolved ref is therefore one of three things: a marked defect
(`v_dangling`), an external target whose file is not in the DB
(`target_file` set to a real name), or a built-in or calculated name.

## What a rename breaks

FileMaker stores structural references by id and re-points them when you
rename a field, script or layout: a Set Field target, a Perform Script call, a
relationship predicate, a layout object binding all survive a rename. What
breaks is TEXT: `ExecuteSQL` strings, `GetField` / `Evaluate` arguments,
script and layout names built at runtime (`Perform Script [ By name ]`,
`Go to Layout [ By name ]`), and callers outside the file (other FileMaker
files, OData and Data API clients). fmsonar's `used` answers "is it
referenced", not "will a rename break it": for the breakage list, the text
gap is the FTS `search` today, and a `text_mention` context is planned for
1.15.0.

## Consumer contract

`ddr_run.parser_version` records the fmsonar version that built an index. A
consumer that needs a view or column introduced in version N compares that
value and asks for a rebuild when it is older; the `fmsonar` CLI prints the
rebuild hint itself when a view is missing (`cascades` exits on it, `stats`
prints one line and continues). Views added per version: `v_cascades`
(1.10.0), `v_triggers` (1.9.0), `layout_context` refs (1.12.0),
`v_data_sources` and `v_dangling` (1.13.0). For 1.15.0: a `text_mention`
ref counts as a use only when `target_entity_id IS NOT NULL`; an unresolved
mention never does.

## Practical rule

Before acting on "unused" or "no references":

1. `python3 -m fm_ddr.cli search db.db "name"` — catches every textual mention.
2. Remember what no tool can see: external systems calling the FM file (OData,
   Data API, other files), and users running scripts from the Scripts menu.
