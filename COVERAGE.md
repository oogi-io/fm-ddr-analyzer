# Reference coverage — what is and is not captured

The cross-reference graph is only as honest as its coverage. This is the
explicit list. **Anything in "NOT captured" means `v_unused_fields` and
"Referenced by: none" can be wrong for that usage pattern** — treat those
outputs as review lists, never as delete lists.

## Captured (structured edges in `refs`)

| Context | What it means | Source |
|---------|---------------|--------|
| `calc` | Field used in a calculation (field calc, script step calc, layout object calc) | `Chunk type="FieldRef"` (both element and text forms) |
| `function_ref` | **Custom** function called in a calculation | `Chunk type="CustomFunctionRef"` |
| `join_predicate` | Field or TO used in a relationship | `LeftField`/`RightField`/`LeftTable`/`RightTable` |
| `sort` | Field used in a sort order (relationship sort lists and similar) | `PrimaryField`/`SecondaryField` outside value lists |
| `value_list_field` | Field feeding a field-based value list | `PrimaryField`/`SecondaryField` inside `ValueList` |
| `value_list_source` | Value list bound to a layout object | `ValueList` bindings outside the catalog |
| `layout_object` | Field placed on a layout | `FieldObj > Name`, `FieldReference` under `Layout` |
| `step_target` | The field a step acts on: **Set Field's write target**, Go to Field, Insert ... — combine with the step's `step_type` to tell writes from navigation | direct `<Field>` child of `<Step>` |
| `field_reference` | Field referenced via a `FieldReference` element outside layouts | `FieldReference` |
| `perform_script` | Script called by a script step | `Script` reference elements |
| `trigger` | Script attached to a script trigger | `Script` inside `ScriptTriggers` |
| `go_to_layout` | Layout targeted by a Go to Layout step/button | `Layout` reference elements |
| `to_reference` | Table occurrence referenced directly | `TableOccurrenceReference` |

Every edge stores the raw text (`target_raw`) plus the resolved entity where
possible. Unresolvable targets stay `NULL` — usually **legitimately**:
scripts in other files, calculated layout destinations, unbound globals.
Ambiguous picks (several same-named candidates, no qualifier) are flagged
`ambiguous = 1` (see `v_ambiguous`); the pick is deterministic but uncertain.

## NOT captured (blind spots — use FTS `search` as the fallback)

| Usage pattern | Why | Fallback |
|---------------|-----|----------|
| **Fields named inside `ExecuteSQL` strings** | The DDR itself doesn't resolve SQL text; it's just a string | `search db "fieldname"` |
| Fields/scripts named in **any calculated string** (`GetField`, `Evaluate`, script names built at runtime) | Same — text, not structure | `search` |
| **Built-in function** usage as edges | Deliberately excluded (tens of thousands of `Get()` edges would drown the graph) | `search db "ExecuteSQL"` |
| Script invocation from **custom menus** | Menu internals not yet mapped | `search` on the script name |
| **Import/export field orders** | Not yet mapped | `search` |
| Merge fields / merge variables in layout text | Text objects are not parsed for `<<field>>` markers yet | `search` |

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
