"""Stream a FileMaker DDR (FMPReport XML) into a normalized SQLite database.

DDRs are large (up to ~400MB) and UTF-16-LE. We use a SAX parser with a
context stack, so file size and line structure don't matter. Every named thing
becomes a row in `entities`; every "X uses Y" becomes a row in `refs`; and all
calculation / step text is mirrored into an FTS5 index for text-fallback search.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import xml.sax
from datetime import datetime, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# A field object's repetition suffix in the DDR's FieldObj/Name ("Phone[3]")
_REP_SUFFIX = re.compile(r"\[\d+\]$")

# Entity kinds that a reference edge can be attributed to (the "source").
# Ordered by preference: the nearest one on the stack wins.
SOURCE_KINDS = {
    "field", "custom_function", "script_step", "relationship",
    "layout", "layout_object", "value_list", "privilege_set", "custom_menu",
}

# Leaf text elements whose character data we accumulate.
# Text = value-list custom values; Data = layout text-object character runs
# (both consumed only under their specific ancestors). TableAliasKey (v1.14.0)
# = the TO a portal shows; it and an empty FieldObj Name are decided at the
# END of the element (endElement), because _consume_text returns on empty text
# and the empty key is the case that matters.
CAPTURE_TAGS = {"Calculation", "StepText", "Chunk", "Name", "Text", "Data",
                "TableAliasKey"}

def _parser_version():
    from fm_ddr import __version__
    return __version__


BATCH = 4000

# v1.13.0: markers. The DDR's own are <Table Missing>, <Field Missing> and
# <Function Missing> in calculation and step text (and name="<unknown>" on a
# script); fmsonar's own is '<unknown>', written into target_raw, ext_file or
# target_file wherever the DDR left a REQUIRED name empty or missing. The two
# are told apart by target_name: the DDR's arrives as target_name='<unknown>',
# fmsonar's rewrite leaves target_name NULL.
UNKNOWN = "<unknown>"
TABLE_MISSING = "<Table Missing>"
FIELD_MISSING = "<Field Missing>"
# Layout destinations that are decided at runtime: an empty <Layout> under one
# of these is not a reference to anything (the by-calc <Calculation> child still
# reaches the step's calc_text through the normal capture).
RUNTIME_LAYOUT_DESTS = ("OriginalLayout", "CurrentLayout",
                        "LayoutNameByCalc", "LayoutNumberByCalc")


class DDRHandler(xml.sax.ContentHandler):
    def __init__(self, conn: sqlite3.Connection, run_id: int):
        super().__init__()
        self.conn = conn
        self.run_id = run_id
        self.file_id = None

        self.tagstack: list[str] = []          # element name ancestry
        self.pushed: list[bool] = []           # per-element: did it open an entity?
        self.entity_stack: list[dict] = []     # open entities (dicts)
        self.eid = 0                           # entity_id counter
        self.rid = 0                           # ref_id counter

        self._cap_tag = None                   # currently captured leaf tag
        self._cap_attrs = None
        self._buf: list[str] = []

        self._trig_events: list = []           # <Trigger event=...> ancestry
        self._in_dead_ae = False               # inside an AutoEnter whose calc option is OFF
        # v1.13.0: per-step state for the dangling-reference rules. Reset when
        # ANY <Step> opens (script steps, button steps, custom-menu steps) and
        # cleared when it closes; steps never nest, so one pair serves all.
        self._step_text_cur = None             # the current step's StepText
        self._layout_dest_cur = None           # <LayoutDestination value>
        self._step_table_cur = None            # the step's <Table name> (Import Records)
        self._step_restore_cur = None          # the step's <Restore state> (stored requests)
        self._label_fref = None                # an open label FieldReference (v1.16.0)

        self.ent_rows: list[tuple] = []
        self.ref_rows: list[tuple] = []
        self.fts_rows: list[tuple] = []

    # ---- helpers -------------------------------------------------------
    def parent_tag(self) -> str:
        return self.tagstack[-2] if len(self.tagstack) >= 2 else ""

    def has_ancestor(self, tag: str) -> bool:
        return tag in self.tagstack[:-1]

    def current_source(self):
        for e in reversed(self.entity_stack):
            if e["kind"] in SOURCE_KINDS:
                return e
        return self.entity_stack[-1] if self.entity_stack else None

    def current_group(self):
        for e in reversed(self.entity_stack):
            if e["kind"] in ("script_group", "layout_group"):
                return e["name"]
        return None

    def new_entity(self, kind, attrs, parent_id=None, **extra):
        self.eid += 1
        parent = self.entity_stack[-1]["entity_id"] if self.entity_stack else None
        if parent_id is not None:   # SaXML: catalog membership is by id, not nesting
            parent = parent_id
        ent = {
            "entity_id": self.eid,
            "file_id": self.file_id,
            "kind": kind,
            "fm_id": attrs.get("id"),
            "name": extra.get("name", attrs.get("name")),
            "parent_entity_id": parent,
            "base_table": extra.get("base_table"),
            "data_type": attrs.get("dataType"),
            "field_type": attrs.get("fieldType"),
            "step_type": extra.get("step_type"),
            "seq": extra.get("seq"),
            "grp": self.current_group(),
            "records": extra.get("records"),
            "ext_file": None,
            "extra_json": extra.get("extra_json"),
            "_calc_parts": [],           # transient
        }
        self.entity_stack.append(ent)
        self.pushed[-1] = True   # mark the current element as entity-opening
        return ent

    def emit_ref(self, context, target_kind, *, fm_id=None, name=None,
                 to_name=None, raw=None, trigger_event=None, force_disabled=None):
        src = self.current_source()
        # a FileReference inside the current step marks script/layout targets
        # as living in another FILE — they must never resolve locally
        target_file = None
        if target_kind in ("script", "layout") and src is not None:
            target_file = src.get("_ext_file")
        self.rid += 1
        self.ref_rows.append((
            self.rid, self.file_id,
            src["entity_id"] if src else None,
            src["kind"] if src else None,
            context, target_kind, fm_id, name, to_name, raw, target_file, None,
            # dead code: refs from disabled script steps, from auto-enter calcs
            # whose "Calculated value" option is unchecked (residue), or an
            # explicit per-ref verdict (lookups: calculation="False" is their
            # NORMAL state, so the calc-dead flag must not apply to them)
            1 if ((src and src.get("_disabled"))
                  or (force_disabled if force_disabled is not None
                      else self._in_dead_ae)) else 0,
            trigger_event,
        ))
        if len(self.ref_rows) >= BATCH:
            self.flush()

    # ---- SAX callbacks -------------------------------------------------
    def startElement(self, tag, attrs):
        a = {k: attrs.getValue(k) for k in attrs.getNames()}
        self.tagstack.append(tag)
        self.pushed.append(False)
        parent = self.parent_tag()

        # ---- files ----
        if tag == "File" and parent == "FMPReport":
            self.file_id = self._insert_file(a.get("name"), a.get("path"))
            return

        # ---- entity definitions ----
        if tag == "BaseTable" and parent == "BaseTableCatalog":
            recs = a.get("records")
            self.new_entity("base_table", a,
                            records=int(recs) if recs and recs.isdigit() else None)
            return

        if tag == "Field" and parent == "FieldCatalog":
            bt = self.entity_stack[-1]["name"] if self.entity_stack else None
            # walk up for the base_table (Field is under BaseTable>FieldCatalog)
            bt = next((e["name"] for e in reversed(self.entity_stack)
                       if e["kind"] == "base_table"), None)
            self.new_entity("field", a, base_table=bt)
            return

        if tag == "Table" and self.has_ancestor("RelationshipGraph"):
            self.new_entity("table_occurrence", a, base_table=a.get("baseTable"))
            return

        if tag == "Relationship":
            self.new_entity("relationship", a, name=None)
            return

        # v1.14.0: script and layout folders carry the raw "Include in menu"
        # flag ({"in_menu": true} when set; no key when not).
        if tag == "Group" and self.has_ancestor("ScriptCatalog"):
            self.new_entity("script_group", a, extra_json=_menu_flags(a))
            return
        if tag == "Group" and self.has_ancestor("LayoutCatalog"):
            self.new_entity("layout_group", a, extra_json=_menu_flags(a))
            return

        # A script definition sits directly under ScriptCatalog or a script Group
        # (mirrors the Layout rule above). A <Script> anywhere else — inside a
        # Step, Trigger or button — is a reference, handled below. Scripts kept
        # outside any folder (parent == ScriptCatalog) were previously missed.
        if tag == "Script" and parent in ("Group", "ScriptCatalog") and self.has_ancestor("ScriptCatalog"):
            # A script named "-" is a FileMaker script-menu separator (empty
            # StepList), not a script — skip it (never fall through to the ref).
            # v1.14.0: the raw flags {"in_menu", "run_full_access"}, keys
            # only when true; the separator is skipped before they are read.
            if (a.get("name") or "").strip() != "-":
                self.new_entity("script", a, extra_json=_menu_flags(a, script=True))
            return

        if tag == "Layout":
            # A definition sits directly under LayoutCatalog or a layout Group.
            # A <Layout id name/> anywhere else (incl. a Go-to-Layout button that
            # happens to live on a layout) is a reference.
            if parent in ("LayoutCatalog", "Group") and self.has_ancestor("LayoutCatalog"):
                if (a.get("name") or "").strip() != "-":   # "-" = layout-menu separator
                    self.new_entity("layout", a, extra_json=_menu_flags(a))
            else:
                nm = a.get("name")
                if nm:
                    self.emit_ref("go_to_layout", "layout", fm_id=a.get("id"),
                                  name=nm, raw=nm)
                # v1.13.0: an empty name is either a runtime destination (by
                # calc, original or current layout: nothing to reference) or
                # a deleted layout (<unknown> in the step text): a defect.
                elif self._layout_dest_cur not in RUNTIME_LAYOUT_DESTS:
                    self.emit_ref("go_to_layout", "layout", fm_id=a.get("id"),
                                  name=None, raw=UNKNOWN)
            return

        # v1.12.0: a layout's own context TO is its first child <Table id name/>.
        # Without this edge a TO used only as a layout context looked unused.
        # An empty name (seen in real DDRs) means the TO no longer exists: the
        # ref keeps the fm_id and stays unresolved, so it is findable as a defect
        # (v1.14.0: whatever the id; the old id-0 exception never fired, a
        # separator opens no entity). The top-of-stack guard skips "-"
        # separators (no entity opened); the <Table> inside a step (Go to
        # Related Record, Import Records) is the step_table rule below.
        if (tag == "Table" and parent == "Layout" and self.entity_stack
                and self.entity_stack[-1]["kind"] == "layout"):
            nm = a.get("name") or None
            self.emit_ref("layout_context", "table_occurrence", fm_id=a.get("id"),
                          name=nm, raw=nm or UNKNOWN)
            return

        # v1.14.0: the TO a step acts on: Go to Related Record ("From table")
        # and Import Records (the target table); no other step carries the
        # element. Source = the step, or the layout object / custom menu of a
        # button or menu step; a disabled step's refs inherit disabled=1. An
        # empty name is a deleted TO (the step text reads "From table:
        # <unknown>"): '<unknown>', id kept, unresolved, in v_dangling.
        if tag == "Table" and parent == "Step":
            nm = a.get("name") or None
            self._step_table_cur = nm
            self.emit_ref("step_table", "table_occurrence", fm_id=a.get("id"),
                          name=nm, raw=nm or UNKNOWN)
            return

        # v1.15.1: a step's stored request. Import Records' field mapping
        # (<TargetFields>, the TO is the step's <Table>), Export Records'
        # field order and group-by fields, a stored find request's fields
        # (Query/RequestRow/Criteria). It counts only when the step restores
        # it (<Restore state="True">, always written before the request): a
        # Perform Script button keeps a stale <Query> with no <Restore>, a
        # Perform Find with Restore off keeps its last request. A DoNotImport
        # map is no usage; an Import map with id 0 and no name is an arrow
        # whose target field is gone, an optional slot that emits nothing.
        # Any other empty name takes the step field marker rule.
        if tag == "Field" and parent in ("TargetFields", "ExportEntry",
                                         "SummaryFields", "Criteria"):
            if self._step_restore_cur != "True":
                return
            if parent == "TargetFields":
                if a.get("map") not in ("Import", "Match"):
                    return
                if not a.get("name") and a.get("id") == "0":
                    return
                ctx, tbl = "import_field", self._step_table_cur
            else:
                ctx = "find_request" if parent == "Criteria" else "export_field"
                tbl = a.get("table")
            nm = a.get("name")
            if nm:
                self.emit_ref(ctx, "field", fm_id=a.get("id"), name=nm,
                              to_name=tbl, raw=(tbl or "") + "::" + nm)
            else:
                self._marker_field_ref(ctx, a.get("id"), tbl)
            return

        # v1.14.0: the "starting from" TO of a related-values value list
        # (<ShowRelated value="True"><Table id name/>). An empty name is a TO
        # the file does not have (a cloned list): '<unknown>', in v_dangling.
        if (tag == "Table" and parent == "ShowRelated" and self.entity_stack
                and self.entity_stack[-1]["kind"] == "value_list"):
            nm = a.get("name") or None
            self.emit_ref("value_list_context", "table_occurrence", fm_id=a.get("id"),
                          name=nm, raw=nm or UNKNOWN)
            return

        # v1.13.0: per-step state. Every <Step> (StepList, ButtonObj,
        # GroupButtonObj, MenuItem) resets it; the rules below read it.
        if tag == "Step":
            self._step_text_cur = None
            self._layout_dest_cur = None
            self._step_table_cur = None
            self._step_restore_cur = None
        if tag == "Restore" and parent == "Step":
            self._step_restore_cur = a.get("state")
        if tag == "LayoutDestination":
            self._layout_dest_cur = a.get("value")
            return

        if tag == "Step" and parent == "StepList":
            owner = next((e for e in reversed(self.entity_stack)
                          if e["kind"] == "script"), None)
            ordinal = None
            if owner is not None:
                owner["_nsteps"] = owner.get("_nsteps", 0) + 1
                ordinal = owner["_nsteps"]
            ent = self.new_entity("script_step", a, step_type=a.get("name"),
                                  name=a.get("name"), seq=ordinal)
            if a.get("enable") == "False":
                ent["_disabled"] = True   # dead code: refs from it get flagged
            return

        # A layout object: button, field, tab/slide panel, portal, group, ...
        # Objects nest (panels/portals/groups hold child Objects); the parent
        # chain mirrors that. Hide conditions, tooltips, and button launch
        # params attach to the OBJECT so the UI control plane is queryable.
        if tag == "Object" and self.has_ancestor("Layout"):
            ent = self.new_entity("layout_object", a,
                                  name=(a.get("name") or None),
                                  extra_json=_json({"object_type": a.get("type"),
                                                    "key": a.get("key")}))
            ent["_otype"] = a.get("type")
            return

        if (tag == "Bounds" and parent == "Object" and self.entity_stack
                and self.entity_stack[-1]["kind"] == "layout_object"):
            try:
                self.entity_stack[-1]["_bounds"] = ",".join(
                    str(int(float(a.get(k, "0"))))
                    for k in ("top", "left", "bottom", "right"))
            except ValueError:
                pass
            return

        # Storage flags / auto-enter options attach to the OPEN field definition.
        # (Field REFERENCE elements are empty/self-closing, so they can never be
        # the parent of these tags — the entity-stack guard keeps this exact.)
        if tag in ("Storage", "AutoEnter") and parent == "Field" and (
                self.entity_stack and self.entity_stack[-1]["kind"] == "field"):
            if tag == "Storage":
                self.entity_stack[-1]["_storage"] = a
            else:
                self.entity_stack[-1]["_ae_attrs"] = a
                # calc text present while the option is OFF = dead residue;
                # refs emitted from inside get disabled=1 (like disabled steps)
                self._in_dead_ae = a.get("calculation") == "False"
            return

        # Serial-number auto-enter: config lives in a <Serial> child (v1.9.1)
        if tag == "Serial" and parent == "AutoEnter" and (
                self.entity_stack and self.entity_stack[-1]["kind"] == "field"):
            self.entity_stack[-1]["_ae_serial"] = a
            return

        # Script-trigger ancestry: remember the event so the Script ref below
        # can record WHICH trigger fires it (OnRecordCommit, OnObjectSave, ...)
        if tag == "Trigger":
            self._trig_events.append(a.get("event"))
            return

        if tag == "CustomFunction" and self.has_ancestor("CustomFunctionCatalog"):
            self.new_entity("custom_function", a,
                            extra_json=_json({"parameters": a.get("parameters"),
                                              "arity": a.get("functionArity")}))
            return

        if tag == "ValueList":
            if parent == "ValueListCatalog":
                self.new_entity("value_list", a)
            elif a.get("name"):
                # a layout object / field binding to a value list -> usage edge
                self.emit_ref("value_list_source", "value_list",
                              fm_id=a.get("id"), name=a.get("name"), raw=a.get("name"))
            return

        if tag == "PrivilegeSet" and parent == "PrivilegesCatalog":
            self.new_entity("privilege_set", a)
            return
        if tag == "Account" and parent == "AccountCatalog":
            self.new_entity("account", a)
            return
        if tag == "ExtendedPrivilege" and parent == "ExtendedPrivilegeCatalog":
            self.new_entity("extended_privilege", a)
            return
        if tag == "CustomMenu" and parent == "CustomMenuCatalog":
            self.new_entity("custom_menu", a)
            return
        if tag == "CustomMenuSet" and parent == "CustomMenuSetCatalog":
            self.new_entity("custom_menu_set", a)
            return
        if tag == "Theme" and parent == "ThemeCatalog":
            self.new_entity("theme", a)
            return
        if tag == "OdbcDataSource":
            self.new_entity("external_data_source", a)
            return
        # v1.13.0: the file data sources. `file` tells them from ODBC sources;
        # `link` is the sibling DDR file name the resolver matches first
        # (the DDR writes X_fmp12.xml for X.fmp12). The catalog sits at the
        # END of the file, so resolution happens in SQL, never at parse time.
        if tag == "FileReference" and parent == "ExternalDataSourcesCatalog":
            self.new_entity("external_data_source", a,
                            extra_json=_json({"file": True, "link": a.get("link")}))
            return

        # ---- reference edges ----
        # External-target marker: a FileReference inside a script step means the
        # step's Script/Layout ref points at another FILE (its ids are numbered
        # in that file, so local resolution would silently link the wrong script).
        # v1.13.0: an empty name is a data source that no longer exists; the
        # marker becomes '<unknown>' so the step's script/layout ref never
        # resolves locally by id (Perform Script [ <unknown> from file: "" ]).
        if tag == "FileReference" and parent == "Step":
            src = self.current_source()
            if src is not None:
                src["_ext_file"] = a.get("name") or UNKNOWN
            return

        # A FileReference inside a Table occurrence marks its base table as
        # living in another file (external data source) — field refs through
        # this TO must resolve against that file.
        if tag == "FileReference" and parent == "Table":
            if self.entity_stack and self.entity_stack[-1]["kind"] == "table_occurrence":
                self.entity_stack[-1]["ext_file"] = a.get("name") or UNKNOWN
            return

        if tag == "FieldReference":
            # v1.16.0: a FieldList under a layout. A portal's own list is a
            # summary of its child objects (their fields are read there):
            # nothing. A button or popover button label's merge field is held
            # to its end tag, where its TO child and repetition are known.
            if parent == "FieldList" and self.has_ancestor("Layout"):
                gp = self.tagstack[-3] if len(self.tagstack) >= 3 else None
                if gp == "PortalObj":
                    return
                if gp == "TextObj":
                    self._label_fref = {"id": a.get("id"), "name": a.get("name") or None,
                                        "rep": a.get("repetition"), "to": None,
                                        "marker": False}
                    return
            to, leaf = _split_qualified(a.get("name"))
            ctx = "layout_object" if self.has_ancestor("Layout") else "field_reference"
            self.emit_ref(ctx, "field", fm_id=a.get("id"), name=leaf,
                          to_name=to, raw=a.get("name"))
            return

        if tag == "TableOccurrenceReference":
            fr = self._label_fref
            if fr is not None and parent == "FieldReference":
                # A deleted TO is a marker element (id -1, <Table Missing>)
                if a.get("id") == "-1" or (a.get("name") or "").startswith("<"):
                    fr["marker"] = True
                else:
                    fr["to"] = a.get("name") or None
            self.emit_ref("to_reference", "table_occurrence",
                          fm_id=a.get("id"), name=a.get("name"), raw=a.get("name"))
            return

        # v1.16.0: a merge field <<TO::Field>> of a text object, listed in
        # its TextObj/FieldList. Source = the innermost object; one ref per
        # (field id, TO) per object. A FieldList elsewhere (a privilege
        # set's FieldAccess, a portal's own list) is not a merge field.
        if (tag == "Field" and parent == "FieldList"
                and len(self.tagstack) >= 3 and self.tagstack[-3] == "TextObj"):
            self._merge_field(a.get("id"), a.get("name"), a.get("table") or None,
                              a.get("repetition"))
            return

        # A <Field table id name/> reference element appears in several places:
        #   - relationship join predicates (LeftField/RightField)
        #   - inside a <Chunk type="FieldRef"> within a calculation
        #   - value-list field sources and sort orders (PrimaryField/SecondaryField)
        # Lookup source field (v1.9.1): <Lookup><Field table id name/> names the
        # field a looked-up value is copied FROM — a real dependency that was
        # invisible to where-used. A configured lookup with the option OFF is
        # residue: ref flagged disabled, like dead auto-enter calcs.
        if tag == "Field" and parent == "Lookup" and self.has_ancestor("AutoEnter"):
            if a.get("name"):   # unset lookups ship empty <Field table="" name=""/>
                fld = (self.entity_stack[-1]
                       if self.entity_stack and self.entity_stack[-1]["kind"] == "field"
                       else None)
                ae = (fld or {}).get("_ae_attrs") or {}
                dead = ae.get("lookup") != "True"
                if fld is not None:
                    fld["_lookup_src"] = (a.get("table", "") + "::" + a["name"])
                self.emit_ref("lookup", "field", fm_id=a.get("id"),
                              name=a["name"], to_name=a.get("table"),
                              raw=(a.get("table", "") + "::" + a["name"]),
                              force_disabled=dead)
            return

        if tag == "Field" and parent in ("LeftField", "RightField", "Chunk",
                                         "PrimaryField", "SecondaryField", "Step",
                                         "SummaryField", "AdditionalField"):
            # v1.16.0: SummaryField is two slots, told apart by the
            # grandparent: a summary field's source (SummaryInfo) or a sort
            # order's "reorder by summary field"; any other is not read
            gp = self.tagstack[-3] if len(self.tagstack) >= 3 else None
            if (parent in ("SummaryField", "AdditionalField") and gp != "SummaryInfo"
                    and not (parent == "SummaryField" and gp == "Sort")):
                return
            if parent in ("SummaryField", "AdditionalField") and gp == "SummaryInfo":
                # the summarized field and the restart ("summary by") field,
                # both in the summary field's own table: no TO, resolved
                # there (resolve.sql), never a file-wide guess
                nm = a.get("name")
                if nm:
                    self.emit_ref("summary_source", "field", fm_id=a.get("id"), name=nm,
                                  to_name=None, raw="::" + nm)
                else:
                    self._marker_field_ref("summary_source", a.get("id"), None)
                return
            if parent == "Chunk":
                # a field's own formula, an auto-enter calc, or a validation
                # calc — distinct mechanisms, distinct contexts
                if self.has_ancestor("AutoEnter"):
                    ctx = "auto_enter"
                elif self.has_ancestor("Validation"):
                    ctx = "validation"
                else:
                    ctx = "calc"
            elif parent == "Step":
                # the field a step acts on: Set Field's write target, Go to
                # Field, Insert ... - combine with the step_type to tell
                # writes from navigation
                ctx = "step_target"
            elif parent in ("PrimaryField", "SecondaryField"):
                ctx = "value_list_field" if self.has_ancestor("ValueList") else "sort"
            elif parent == "SummaryField":
                # v1.16.0: a sort order's "reorder based on summary field"
                ctx = "sort"
            else:
                ctx = "join_predicate"
            nm, tbl = a.get("name"), a.get("table")
            if nm:
                self.emit_ref(ctx, "field", fm_id=a.get("id"), name=nm,
                              to_name=tbl, raw=(tbl or "") + "::" + nm)
            # v1.13.0: an empty name. Three slots are optional and emit nothing:
            # a value list's SecondaryField (unset second display field), any
            # slot of a value list whose Source is not "Field" (a Custom or
            # "all values" list keeps stale field ids that mean nothing) and a
            # step <Field> with NO id attribute (a target never set: Insert
            # Text, Insert from URL, ...). Everything else is a deleted field:
            # the ref carries the DDR's own marker from the step text when it
            # is there, else '<unknown>', and stays unresolved (name NULL).
            elif ctx == "value_list_field" and (
                    parent == "SecondaryField"
                    or (self.entity_stack
                        and self.entity_stack[-1]["kind"] == "value_list"
                        and self.entity_stack[-1].get("_vl_source") != "Field")):
                pass
            elif tbl or not (parent == "Step" and "id" not in a):
                self._marker_field_ref(ctx, a.get("id"), tbl)
            qname = (tbl or "") + "::" + (nm or "")
            if ctx == "join_predicate":
                # v1.10.0: record the predicate field pair on the relationship
                rel = next((e for e in reversed(self.entity_stack)
                            if e["kind"] == "relationship"), None)
                if rel is not None:
                    rel.setdefault("_pred_fields", []).append((parent, qname))
            elif ctx == "value_list_field" and (
                    self.entity_stack and self.entity_stack[-1]["kind"] == "value_list"):
                # v1.10.0: definition metadata — which field each slot displays
                self.entity_stack[-1].setdefault("_vl_fields", {})[parent] = qname
            return
        if tag in ("LeftTable", "RightTable") and self.has_ancestor("Relationship"):
            self.emit_ref("join_predicate", "table_occurrence", name=a.get("name"),
                          raw=a.get("name"))
            # remember on the relationship for a synthetic name
            rel = next((e for e in reversed(self.entity_stack)
                        if e["kind"] == "relationship"), None)
            if rel is not None:
                rel.setdefault("_ends", []).append(a.get("name"))
                # v1.10.0: cascade options ride on the relationship ends —
                # cascadeDelete on a side means deleting a record on the OTHER
                # side deletes related records on THIS side
                rel.setdefault("_sides", []).append({
                    "side": "left" if tag == "LeftTable" else "right",
                    "to": a.get("name"),
                    "cascade_create": a.get("cascadeCreate") == "True",
                    "cascade_delete": a.get("cascadeDelete") == "True",
                })
            return

        # v1.10.0: join operator (Equal, NotEqual, Cartesian, ranges)
        if tag == "JoinPredicate" and self.has_ancestor("Relationship"):
            rel = next((e for e in reversed(self.entity_stack)
                        if e["kind"] == "relationship"), None)
            if rel is not None:
                rel.setdefault("_ops", []).append(a.get("type") or "Equal")
            return

        # v1.10.0: value-list DEFINITION metadata attaches to the open catalog
        # entry (Source, ShowRelated, display-field options)
        if tag in ("Source", "ShowRelated", "PrimaryField", "SecondaryField") and (
                self.entity_stack and self.entity_stack[-1]["kind"] == "value_list"):
            vl = self.entity_stack[-1]
            if tag == "Source":
                vl["_vl_source"] = a.get("value")
            elif tag == "ShowRelated":
                vl["_vl_showrel"] = a.get("value")
            else:
                vl.setdefault("_vl_fieldopts", {})[tag] = {
                    "show": a.get("show") == "True", "sort": a.get("sort") == "True"}
            return

        # script reference (Perform Script, triggers) — NOT a definition
        if tag == "Script" and parent not in ("Group", "ScriptCatalog"):
            fmid, nm = a.get("id"), a.get("name")
            # <Script id="0" name=""/> is the DDR's unset trigger slot: nothing.
            # v1.13.0: any other id with an empty or missing name is a deleted
            # script (<Script id="N"/> behind an empty FileReference): the ref
            # carries '<unknown>' and stays unresolved. name="<unknown>" is the
            # DDR's own marker and arrives as target_name = '<unknown>'.
            if nm or fmid not in (None, "0"):
                # File-level triggers (<WindowTriggers>) carry the event as the
                # WRAPPER TAG (<OnFirstWindowOpen><Script/>), not an attribute.
                in_window = self.has_ancestor("WindowTriggers")
                ctx = ("trigger" if in_window or self.has_ancestor("ScriptTriggers")
                       or parent == "Trigger" else "perform_script")
                if ctx != "trigger":
                    ev = None
                elif in_window:
                    ev = parent   # OnFirstWindowOpen, OnLastWindowClose, ...
                else:
                    ev = self._trig_events[-1] if self._trig_events else None
                self.emit_ref(ctx, "script", fm_id=fmid, name=nm or None,
                              raw=nm or UNKNOWN, trigger_event=ev)
            return

        # ---- text capture ----
        if tag in CAPTURE_TAGS:
            self._cap_tag = tag
            self._cap_attrs = a
            self._buf = []

    def characters(self, content):
        if self._cap_tag is not None:
            self._buf.append(content)

    def endElement(self, tag):
        # finish text capture
        if self._cap_tag == tag:
            text = "".join(self._buf).strip()
            # v1.14.0: two slots decided at the END of the element, because
            # _consume_text returns on empty text and the empty text is the
            # case that matters. (1) The TO a portal shows: an empty key is a
            # portal whose TO was deleted. (2) A field object whose <Name> is
            # empty: its field or TO is gone (1.13.0 dropped these silently).
            # Both read '<unknown>', unresolved, in v_dangling.
            if tag == "TableAliasKey":
                src = self.current_source()
                if (self.parent_tag() == "PortalObj" and src is not None
                        and src["kind"] == "layout_object"):
                    self.emit_ref("portal", "table_occurrence",
                                  name=text or None, raw=text or UNKNOWN)
            elif tag == "Name" and not text and self.parent_tag() == "FieldObj":
                self.emit_ref("layout_object", "field", name=None, to_name=None,
                              raw=UNKNOWN)
            else:
                self._consume_text(tag, self._cap_attrs, text)
            self._cap_tag = None
            self._buf = []

        # v1.16.0: a button label's merge field, its TO child now read
        if tag == "FieldReference" and self._label_fref is not None:
            fr, self._label_fref = self._label_fref, None
            if fr["marker"]:
                self._merge_field(fr["id"], None, None, None)
            else:
                self._merge_field(fr["id"], fr["name"], fr["to"], fr["rep"])

        # pop the entity only if THIS element is the one that opened it
        if self.pushed and self.pushed.pop():
            self._finalize_entity(self.entity_stack.pop())

        # the external-file marker is scoped to its step (matters for button
        # steps, whose source is the enclosing layout, not a script_step)
        if tag == "Step":
            src = self.current_source()
            if src is not None:
                src.pop("_ext_file", None)
            self._step_text_cur = None
            self._layout_dest_cur = None

        if tag == "AutoEnter":
            self._in_dead_ae = False
        elif tag == "Trigger" and self._trig_events:
            self._trig_events.pop()

        if self.tagstack:
            self.tagstack.pop()

    # ---- text + finalize ----------------------------------------------
    def _consume_text(self, tag, attrs, text):
        if not text:
            return
        src = self.current_source()
        if tag == "Calculation":
            if src is not None:
                if src["kind"] == "layout_object" and self.has_ancestor("HideCondition"):
                    src["_hide_calc"] = text
                elif src["kind"] == "layout_object" and self.has_ancestor("ToolTip"):
                    src["_tooltip_calc"] = text
                elif src["kind"] == "field" and self.has_ancestor("AutoEnter"):
                    # auto-enter calc — NOT the field's own formula; keeping
                    # them apart stops Normal fields posing as calc fields
                    src.setdefault("_ae_calc_parts", []).append(text)
                elif src["kind"] == "field" and self.has_ancestor("Validation"):
                    src.setdefault("_val_calc_parts", []).append(text)
                else:
                    src.setdefault("_calc_parts", []).append(text)
        elif tag == "StepText":
            self._step_text_cur = text
            if src is not None:
                src["_step_text"] = text
        elif tag == "Text":
            # value-list custom values (one Text block, newline-separated)
            if (self.parent_tag() == "CustomValues" and self.entity_stack
                    and self.entity_stack[-1]["kind"] == "value_list"):
                self.entity_stack[-1].setdefault("_vl_custom", []).append(text)
        elif tag == "Data":
            # layout TEXT-object character runs — carry merge fields <<F>> and
            # merge formulas <<ƒ:...>>; without this, layout text is invisible
            # even to FTS (the blind-spot fallback)
            if (src is not None and src["kind"] == "layout_object"
                    and self.has_ancestor("TextObj")):
                src.setdefault("_text_runs", []).append(text)
        elif tag == "Name" and self.parent_tag() == "FieldObj":
            to, leaf = _split_qualified(text)
            # A repetition other than 1 is written as Field[n]: the name
            # resolves without it, target_raw keeps it
            leaf = _REP_SUFFIX.sub("", leaf) if leaf else leaf
            if leaf:
                self.emit_ref("layout_object", "field", name=leaf,
                              to_name=to, raw=text)
        elif tag == "Chunk":
            ctype = (attrs or {}).get("type")
            if ctype == "CustomFunctionRef":
                self.emit_ref("function_ref", "custom_function", name=text, raw=text)
            elif ctype == "FieldRef":
                to, leaf = _split_qualified(text)
                if leaf:
                    ctx = ("auto_enter" if self.has_ancestor("AutoEnter")
                           else "validation" if self.has_ancestor("Validation")
                           else "calc")
                    self.emit_ref(ctx, "field", name=leaf, to_name=to, raw=text)
            # plain built-in FunctionRef / NoRef chunks are left to FTS search.

    def _merge_field(self, fm_id, nm, tbl, rep):
        """A merge field of a text or button label: layout_object, TO::Field
        ([n] in target_raw for a repetition other than 1, the name bare);
        an empty name is the field marker rule. Once per (field id, TO)."""
        src = self.current_source()
        if src is not None:
            seen = src.setdefault("_merge_seen", set())
            if (fm_id, tbl) in seen:
                return
            seen.add((fm_id, tbl))
        if nm:
            suffix = f"[{rep}]" if rep and rep != "1" else ""
            self.emit_ref("layout_object", "field", fm_id=fm_id, name=nm,
                          to_name=tbl,
                          raw=f"{tbl}::{nm}{suffix}" if tbl else nm + suffix)
        else:
            self._marker_field_ref("layout_object", fm_id, tbl)

    def _marker_field_ref(self, ctx, fm_id, tbl):
        """A field slot with no name: a deleted field. With a TO it reads
        TO::<Field Missing> when the step text says so, else TO::<unknown>;
        with no TO <Table Missing> or <unknown>. Unresolved, in v_dangling."""
        text = self._step_text_cur
        if tbl:
            marker = (FIELD_MISSING if text and (tbl + "::" + FIELD_MISSING) in text
                      else UNKNOWN)
            self.emit_ref(ctx, "field", fm_id=fm_id, name=None, to_name=tbl,
                          raw=tbl + "::" + marker)
        else:
            marker = TABLE_MISSING if text and TABLE_MISSING in text else UNKNOWN
            self.emit_ref(ctx, "field", fm_id=fm_id, name=None, to_name=None, raw=marker)

    def _finalize_entity(self, ent):
        import json as _j
        calc = "\n".join(ent.get("_calc_parts") or []) or None
        step_text = ent.get("_step_text")
        if ent["kind"] == "relationship" and ent.get("_ends"):
            ent["name"] = " :: ".join(ent["_ends"])
        extra = ent.get("extra_json")
        # v1.10.0: relationship attributes — cascade options + join predicates
        if ent["kind"] == "relationship" and (ent.get("_sides") or ent.get("_ops")):
            d0 = _j.loads(extra) if extra else {}
            if ent.get("_sides"):
                d0["sides"] = ent["_sides"]
            ops = ent.get("_ops") or []
            if ops:
                pf = ent.get("_pred_fields") or []
                lefts = [q for p, q in pf if p == "LeftField"]
                rights = [q for p, q in pf if p == "RightField"]
                d0["predicates"] = [
                    {"op": ops[i],
                     "left": lefts[i] if i < len(lefts) else None,
                     "right": rights[i] if i < len(rights) else None}
                    for i in range(len(ops))]
            extra = _json(d0)
        # v1.10.0: value-list definition metadata
        if ent["kind"] == "value_list" and (
                ent.get("_vl_source") or ent.get("_vl_custom") or ent.get("_vl_fields")):
            d0 = _j.loads(extra) if extra else {}
            if ent.get("_vl_source"):
                d0["source"] = ent["_vl_source"]
            if ent.get("_vl_showrel") is not None:
                d0["show_related"] = ent["_vl_showrel"] == "True"
            for slot, qname in (ent.get("_vl_fields") or {}).items():
                key = "primary" if slot == "PrimaryField" else "secondary"
                d0[key] = {"field": qname,
                           **(ent.get("_vl_fieldopts", {}).get(slot) or {})}
            if ent.get("_vl_custom"):
                vals = []
                for t in ent["_vl_custom"]:
                    vals += [ln for ln in t.split("\n") if ln != ""]
                d0["custom_values"] = vals
            extra = _json(d0)
        if ent.get("_disabled"):
            d0 = _j.loads(extra) if extra else {}
            d0["disabled"] = True
            extra = _json(d0)
        # field storage + auto-enter capture (v1.9.0)
        stored = indexed = is_global = auto_enter = None
        ae_calc = val_calc = None
        if ent["kind"] == "field":
            st = ent.get("_storage")
            if st is not None:
                stored = 0 if st.get("storeCalculationResults") == "False" else 1
                indexed = st.get("index")
                g = st.get("global")
                is_global = 1 if g == "True" else (0 if g == "False" else None)
            ae = ent.get("_ae_attrs")
            ae_calc = "\n".join(ent.get("_ae_calc_parts") or []) or None
            val_calc = "\n".join(ent.get("_val_calc_parts") or []) or None
            if ae is not None:
                d = {}
                if ae_calc is not None:
                    d["calc"] = ae_calc
                    # option unchecked but calc retained = dead residue
                    d["calc_active"] = ae.get("calculation") == "True"
                if ae.get("value"):
                    d["type"] = ae["value"]      # CreationTimeStamp, ConstantData, ...
                serial = ent.get("_ae_serial")
                if serial is not None:           # v1.9.1: serial-number config
                    d["serial"] = {k: serial[k] for k in
                                   ("increment", "nextValue", "generate") if k in serial}
                if ent.get("_lookup_src"):       # v1.9.1: lookup source + residue flag
                    d["lookup_source"] = ent["_lookup_src"]
                    d["lookup_active"] = ae.get("lookup") == "True"
                if ae.get("lookup") == "True":
                    d["lookup"] = True
                if "alwaysEvaluate" in ae:
                    d["always_evaluate"] = ae["alwaysEvaluate"] == "True"
                if "overwriteExistingValue" in ae:
                    d["overwrite_existing"] = ae["overwriteExistingValue"] == "True"
                if ae.get("allowEditing") == "False":
                    d["prohibit_editing"] = True
                if ae.get("constant") == "True":
                    d["constant"] = True
                auto_enter = _json(d) if d else None
            if val_calc:
                d0 = _j.loads(extra) if extra else {}
                d0["validation_calc"] = val_calc
                extra = _json(d0)
        hide, tip, bounds = (ent.get("_hide_calc"), ent.get("_tooltip_calc"),
                             ent.get("_bounds"))
        # v1.10.0: text-object character runs (merge fields / merge formulas)
        obj_text = " ".join(ent.get("_text_runs") or []) or None
        if ent["kind"] == "layout_object" and (hide or tip or bounds or obj_text):
            d = _j.loads(extra) if extra else {}
            if hide:
                d["hide_calc"] = hide
            if tip:
                d["tooltip_calc"] = tip
            if bounds:
                d["bounds"] = bounds
            if obj_text:
                d["text"] = obj_text[:2000]
            extra = _json(d)
        # for steps, the display text is the searchable body; keep calc separately.
        # layout objects add their type, hide/tooltip calcs, and children so both
        # the object row and the aggregated layout body are searchable.
        # auto-enter / validation calcs stay in the FTS body: the text-fallback
        # blind-spot check must keep finding them now that they left calc_text
        body = " ".join(filter(None,
            [ent.get("name"), ent.get("_otype"), calc, step_text, hide, tip,
             ae_calc, val_calc, obj_text,
             # value-list custom values are searchable ("which list holds X?")
             " ".join(v for t in (ent.get("_vl_custom") or [])
                      for v in t.split("\n") if v) or None]
            + (ent.get("_child_texts") or [])))
        # bubble object text up: portals/panels aggregate their children, the
        # layout aggregates everything — keeps the "read a full layout body"
        # recipe working (and now includes object names).
        if ent["kind"] == "layout_object" and self.entity_stack:
            parent_ent = self.entity_stack[-1]
            if parent_ent["kind"] in ("layout", "layout_object") and body.strip():
                parent_ent.setdefault("_child_texts", []).append(body)
        self.ent_rows.append((
            ent["entity_id"], ent["file_id"], ent["kind"], ent["fm_id"], ent["name"],
            ent["parent_entity_id"], ent["base_table"], ent["data_type"], ent["field_type"],
            calc, ent["step_type"], ent["seq"], ent["grp"], ent["records"],
            ent["ext_file"], _merge_extra(extra, step_text),
            stored, indexed, is_global, auto_enter,
        ))
        if body.strip():
            self.fts_rows.append((ent["entity_id"], ent["file_id"], ent["kind"],
                                  ent["name"] or "", body))
        if len(self.ent_rows) >= BATCH:
            self.flush()

    # ---- persistence ---------------------------------------------------
    def _insert_file(self, name, path):
        cur = self.conn.execute(
            "INSERT INTO files(run_id, name, path) VALUES (?,?,?)",
            (self.run_id, name, path))
        return cur.lastrowid

    def flush(self):
        if self.ent_rows:
            self.conn.executemany(
                "INSERT INTO entities(entity_id,file_id,kind,fm_id,name,parent_entity_id,"
                "base_table,data_type,field_type,calc_text,step_type,seq,grp,records,"
                "ext_file,extra_json,stored,indexed,is_global,auto_enter)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self.ent_rows)
            self.ent_rows.clear()
        if self.ref_rows:
            self.conn.executemany(
                "INSERT INTO refs(ref_id,file_id,source_entity_id,source_kind,context,"
                "target_kind,target_fm_id,target_name,target_to_name,target_raw,"
                "target_file,target_entity_id,disabled,trigger_event)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self.ref_rows)
            self.ref_rows.clear()
        if self.fts_rows:
            self.conn.executemany(
                "INSERT INTO text_index(entity_id,file_id,kind,name,body) VALUES (?,?,?,?,?)",
                self.fts_rows)
            self.fts_rows.clear()


# ---- small utils ------------------------------------------------------
def _split_qualified(qname):
    """'TO::Field' -> ('TO', 'Field'); 'Field' -> (None, 'Field')."""
    if qname is None:
        return None, None
    if "::" in qname:
        to, _, leaf = qname.partition("::")
        return to, leaf
    return None, qname


def _json(d):
    import json
    d = {k: v for k, v in d.items() if v is not None}
    return json.dumps(d) if d else None


def _menu_flags(a, script=False):
    """v1.14.0: the raw menu flags of a script, script folder, layout or
    layout folder as extra_json: {"in_menu": true} when includeInMenu="True",
    plus {"run_full_access": true} on a script when runFullAccess="True".
    No key when a flag is off or missing; None when neither is set."""
    d = {}
    if a.get("includeInMenu") == "True":
        d["in_menu"] = True
    if script and a.get("runFullAccess") == "True":
        d["run_full_access"] = True
    return _json(d)


def _merge_extra(extra_json, step_text):
    import json
    if not step_text:
        return extra_json
    d = json.loads(extra_json) if extra_json else {}
    d["step_text"] = step_text
    return json.dumps(d)


# ---- public API -------------------------------------------------------
def expand_summary(path: str) -> list[str] | None:
    """If path is a DDR Summary.xml manifest, return the linked DDR file paths
    (resolved relative to the manifest); otherwise None."""
    import re
    with open(path, "rb") as f:
        head = f.read(2048)
    enc = "utf-16-le" if head[:2] == b"\xff\xfe" else "utf-8"
    text = head.decode(enc, "replace")
    if 'type="Summary"' not in text:
        return None
    with open(path, encoding=enc, errors="replace") as f:
        full = f.read()
    base = os.path.dirname(os.path.abspath(path))
    links = re.findall(r'<File\s+link="([^"]+)"', full)
    out = []
    for l in links:
        # Resolve relative to the manifest and confine to its directory tree.
        # A real Summary.xml only ever links sibling DDR files; anything that
        # escapes (absolute paths, embedded ../) is a crafted manifest and is
        # skipped so it can't pull arbitrary files into the shared database.
        resolved = os.path.normpath(os.path.join(base, l))
        if os.path.commonpath([base, resolved]) != base:
            continue
        out.append(resolved)
    return out


def expand_paths(paths) -> list[str]:
    """The source list a build reads: a single Summary.xml manifest becomes
    the DDR files it links; anything else is returned as given."""
    if isinstance(paths, str):
        paths = [paths]
    if len(paths) == 1:
        linked = expand_summary(paths[0])
        if linked:
            return linked
    return list(paths)


def build_key(paths) -> str:
    """v1.16.0: the key of a build, SHA-256 over the fmsonar version and the
    SHA-256 of each source's bytes, in order. No path, label or mtime: a
    copied index or a relabelled build of the same bytes is the same build."""
    import hashlib
    key = hashlib.sha256(f"fmsonar {_parser_version()}\n".encode())
    for p in paths:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        key.update(h.hexdigest().encode() + b"\n")
    return key.hexdigest()


def unchanged_build(db_path: str, key: str) -> dict | None:
    """If db_path is an fmsonar index whose build_key equals key, refresh its
    recorded build time (so `list`, which compares source mtimes with it,
    stops flagging it; after a skip parsed_at is the time the build was last
    confirmed, not when it was parsed) and return {parsed_at, label, _refs,
    _refs_resolved}; else None. An index built before the column never
    matches. A read-only index still skips, without the refresh."""
    if not key or not os.path.exists(db_path) or not _is_fmsonar_db(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        try:
            row = conn.execute("SELECT build_key, parsed_at, label FROM ddr_run").fetchall()
        except sqlite3.OperationalError:
            return None
        if len(row) != 1 or row[0][0] != key:
            return None
        out = {"parsed_at": row[0][1], "label": row[0][2]}
        out.update({k: v for k, v in _summarize(conn).items() if k.startswith("_refs")})
        try:
            conn.execute("UPDATE ddr_run SET parsed_at = ?",
                         (datetime.now(timezone.utc).isoformat(),))
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Read-only file: the skip stands, the build time stays
        return out
    finally:
        conn.close()


def _is_fmsonar_db(path: str) -> bool:
    """True if `path` looks like a database this tool produced — a SQLite file
    with a ddr_run table. Used to avoid clobbering an unrelated file at -o."""
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ddr_run'"
            ).fetchone() is not None
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError):
        return False


def build(ddr_paths, db_path: str, label: str | None = None,
          force: bool = False, key: str | None = None) -> dict:
    """Parse one or more DDR files (or a Summary.xml manifest) into a fresh
    SQLite DB. Returns a summary dict: entity counts per kind, _refs,
    _refs_resolved, and _warnings (a list of warning texts, maybe empty).

    The DB is built into a temp file alongside the target and moved into place
    only on success, so a malformed DDR (or a typo'd -o path) never destroys an
    existing file, and two concurrent builds can't corrupt each other's output.
    An existing file at db_path that is not itself an fmsonar DB is left intact
    unless force=True. key (build_key of the same paths) is stored in
    ddr_run.build_key, in the same transaction as the rest.
    """
    ddr_paths = expand_paths(ddr_paths)

    from fm_ddr import saxml   # here, not at the top: saxml imports this module

    if os.path.exists(db_path) and not force and not _is_fmsonar_db(db_path):
        raise ValueError(
            f"{db_path} already exists and is not an fmsonar database — "
            "refusing to overwrite it. Choose another -o path or pass --force.")

    target_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    # The temp DB is written next to the target so the final rename is atomic,
    # which means the target's directory has to exist: -o into a folder that
    # does not exist yet (`-o new/folder/x.db`) is an ordinary first build.
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix=".fmsonar-build-",
                                    dir=target_dir)
    os.close(fd)
    conn = sqlite3.connect(tmp_path)
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        # Entities and refs are streamed interleaved and share a single id space
        # assigned in-process, so referential integrity holds by construction.
        # Disable FK enforcement during load (a ref may be flushed before its
        # still-open source entity is inserted). Resolution happens after, in SQL.
        conn.execute("PRAGMA foreign_keys=OFF")

        # Sniff every path before parsing any: one source format per index.
        # The sniff reads the prolog to the real root and refuses a DTD
        # internal subset (the entity-expansion attack older expat won't
        # stop) and a root that is neither format.
        infos = []
        for p in ddr_paths:
            info = saxml.sniff(p)
            saxml.refuse_unknown_root(info, p)
            infos.append(info)
        sax = [p for p, i in zip(ddr_paths, infos) if i["fmt"] == "saxml"]
        other = [p for p, i in zip(ddr_paths, infos) if i["fmt"] != "saxml"]
        if sax and other:
            raise ValueError(
                f"build takes one source format per index: {other[0]} is a "
                f"DDR, {sax[0]} is a Save a Copy as XML export.")
        if sax:
            for p, i in zip(ddr_paths, infos):
                saxml.check(i, p)
            saxml.refuse_duplicates(zip(ddr_paths, infos))
            (ver, src_ver), ctime, fmt = saxml.run_versions(infos), None, "saxml"
        else:
            ver, ctime = _peek_report_meta(ddr_paths[0])
            fmt, src_ver = "ddr", None
        cur = conn.execute(
            "INSERT INTO ddr_run(source_path,ddr_version,creation_time,parsed_at,parser_version,"
            "source_format,source_version,label,build_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (";".join(os.path.abspath(p) for p in ddr_paths), ver, ctime,
             datetime.now(timezone.utc).isoformat(), _parser_version(), fmt, src_ver,
             label or os.path.basename(ddr_paths[0]), key))
        run_id = cur.lastrowid

        # One handler across all files: entity/ref ids stay a single space, and
        # each <File> element (SaXML: each export's root) opens a row in `files`.
        if sax:
            handler = saxml.SaXMLHandler(conn, run_id)
            for p, i in zip(ddr_paths, infos):
                handler.parse(p, i["version"])   # pass 1 (DDR_INFO), then pass 2
        else:
            handler = DDRHandler(conn, run_id)
            for p in ddr_paths:
                parser = xml.sax.make_parser()
                parser.setContentHandler(handler)
                # expat honors the BOM / XML declaration, so binary mode handles UTF-16-LE.
                with open(p, "rb") as f:
                    parser.parse(f)
        handler.flush()
        conn.commit()

        if handler.file_id is None and sax:
            raise ValueError(
                f"{ddr_paths[0]} holds no FMSaveAsXML root element: not a "
                "readable Save a Copy as XML export.")
        if handler.file_id is None:
            raise ValueError(
                f"{ddr_paths[0]} is not a FileMaker DDR (no FMPReport/File found). "
                "Generate one via Tools > Database Design Report > XML.")

        # resolve edges + build views
        with open(os.path.join(os.path.dirname(__file__), "resolve.sql")) as f:
            conn.executescript(f.read())
        conn.commit()
        summary = _summarize(conn)
        warnings = _build_warnings(conn, summary, ver, saxml if sax else None,
                                   list(zip(ddr_paths, infos)))
    except BaseException:
        conn.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    conn.close()
    os.replace(tmp_path, db_path)  # atomic; only reached on success
    # Every warning of the build, in one list: the CLI prints them as one
    # block before "Done:"
    summary["_warnings"] = warnings
    return summary


def _build_warnings(conn, summary, ver, saxml_mod, infos) -> list:
    """The build's warnings, from the sniffed roots and the finished index.
    A warning never stops the build."""
    if saxml_mod is not None:
        return saxml_mod.build_warnings(conn, infos)
    out = []
    total, resolved = summary.get("_refs", 0), summary.get("_refs_resolved", 0)
    if total and resolved / total < 0.95:
        out.append(
            f"WARNING: only {100 * resolved // total}% of references resolved "
            f"(source version {ver}). On a healthy single-file solution expect ~98%.\n"
            "  Low resolution usually means an unmapped FM-version construct or a "
            "multi-file solution: check `stats` / v_unresolved.")
    return out


def _peek_report_meta(path):
    import re
    with open(path, "rb") as f:
        head = f.read(4096)
    enc = "utf-16-le" if head[:2] == b"\xff\xfe" else "utf-8"
    text = head.decode(enc, "replace")
    ver = re.search(r'<FMPReport[^>]*\bversion="([^"]*)"', text)
    ct = re.search(r'creationTime="([^"]*)"', text)
    return (ver.group(1) if ver else None, ct.group(1) if ct else None)


def _summarize(conn):
    rows = conn.execute(
        "SELECT kind, COUNT(*) FROM entities GROUP BY kind ORDER BY 2 DESC").fetchall()
    counts = {k: c for k, c in rows}
    counts["_refs"] = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    counts["_refs_resolved"] = conn.execute(
        "SELECT COUNT(*) FROM refs WHERE target_entity_id IS NOT NULL").fetchone()[0]
    return counts
