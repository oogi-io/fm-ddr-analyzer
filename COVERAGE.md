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
| `sort` | Field used in a sort order (relationship sort lists and similar) | `PrimaryField`/`SecondaryField` outside value lists |
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

Every edge stores the raw text (`target_raw`) plus the resolved entity where
possible. Unresolvable targets stay `NULL` — usually **legitimately**:
scripts in other files, calculated layout destinations, unbound globals.
Ambiguous picks (several same-named candidates, no qualifier) are flagged
`ambiguous = 1` (see `v_ambiguous`); the pick is deterministic but uncertain.

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
| **Layout TEXT objects — merge fields `<<Field>>` and merge formulas `<<ƒ:...>>`** | Live in `TextObj` character-run `Data` — not parsed AND not in the FTS body, so even `search` misses layout text today | inspect in FileMaker |
| **Record-level security calcs** | Privilege-set custom record-access calcs not extracted | `search` |
| Detailed validation rules (unique, existing, range, strict type, not-empty) | Only validation *calcs* (and value-list bindings) are captured | inspect in FileMaker |
| **Relationship options** (`cascadeCreate` / `cascadeDelete` on the relationship ends, sort, non-equijoin operator) | Join predicates captured; the options are not — "what breaks if I delete X" can't see cascades | inspect in FileMaker |
| **Value-list DEFINITIONS** (source type, custom values list, show-related TO) | The value-list entity + its display-field refs are captured; the definition metadata is not | `search` on the list name |

**Captured but easy to miss** (verified against a real solution, v1.9.1): conditional-formatting
condition calcs, placeholder-text calcs, and tooltip/hide calcs all emit `calc`-context refs
sourced from the enclosing **layout object** — so where-used DOES include them (they are not
distinguishable from each other by context, but no edge is lost). Custom-menu items that run
scripts emit `perform_script` refs sourced from the **custom_menu** entity — menu-run scripts do
NOT appear in `v_orphan_scripts` claims wrongly. Validation by value list emits a
`value_list_source` ref from the field.
| Fields/scripts named in **any calculated string** (`GetField`, `Evaluate`, script names built at runtime) | Same — text, not structure | `search` |
| **Built-in function** usage as edges | Deliberately excluded (tens of thousands of `Get()` edges would drown the graph) | `search db "ExecuteSQL"` |
| **Import/export field orders** | Not yet mapped | `search` |

## Multi-file solutions

Build ALL files of a solution into one DB (`build Summary.xml` or list every
`*_fmp12.xml`). Cross-file references are resolved **only via explicit
FileReference markers** (external Perform Script / Go to Layout, and field
refs through external table occurrences) — never by name guessing. An external
ref whose target file is not in the DB stays unresolved rather than being
mis-linked to a same-named/same-id local object.

## Practical rule

Before acting on "unused" or "no references":

1. `python3 -m fm_ddr.cli search db.db "name"` — catches every textual mention.
2. Remember what no tool can see: external systems calling the FM file (OData,
   Data API, other files), and users running scripts from the Scripts menu.
