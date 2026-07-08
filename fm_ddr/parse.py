"""Stream a FileMaker DDR (FMPReport XML) into a normalized SQLite database.

DDRs are large (up to ~400MB) and UTF-16-LE. We use a SAX parser with a
context stack, so file size and line structure don't matter. Every named thing
becomes a row in `entities`; every "X uses Y" becomes a row in `refs`; and all
calculation / step text is mirrored into an FTS5 index for text-fallback search.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import xml.sax
from datetime import datetime, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Entity kinds that a reference edge can be attributed to (the "source").
# Ordered by preference: the nearest one on the stack wins.
SOURCE_KINDS = {
    "field", "custom_function", "script_step", "relationship",
    "layout", "layout_object", "value_list", "privilege_set", "custom_menu",
}

# Leaf text elements whose character data we accumulate.
CAPTURE_TAGS = {"Calculation", "StepText", "Chunk", "Name"}

def _parser_version():
    from fm_ddr import __version__
    return __version__


BATCH = 4000


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

    def new_entity(self, kind, attrs, **extra):
        self.eid += 1
        parent = self.entity_stack[-1]["entity_id"] if self.entity_stack else None
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
                 to_name=None, raw=None):
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

        if tag == "Group" and self.has_ancestor("ScriptCatalog"):
            self.new_entity("script_group", a)
            return
        if tag == "Group" and self.has_ancestor("LayoutCatalog"):
            self.new_entity("layout_group", a)
            return

        # A script definition sits directly under ScriptCatalog or a script Group
        # (mirrors the Layout rule above). A <Script> anywhere else — inside a
        # Step, Trigger or button — is a reference, handled below. Scripts kept
        # outside any folder (parent == ScriptCatalog) were previously missed.
        if tag == "Script" and parent in ("Group", "ScriptCatalog") and self.has_ancestor("ScriptCatalog"):
            # A script named "-" is a FileMaker script-menu separator (empty
            # StepList), not a script — skip it (never fall through to the ref).
            if (a.get("name") or "").strip() != "-":
                self.new_entity("script", a)
            return

        if tag == "Layout":
            # A definition sits directly under LayoutCatalog or a layout Group.
            # A <Layout id name/> anywhere else (incl. a Go-to-Layout button that
            # happens to live on a layout) is a reference.
            if parent in ("LayoutCatalog", "Group") and self.has_ancestor("LayoutCatalog"):
                if (a.get("name") or "").strip() != "-":   # "-" = layout-menu separator
                    self.new_entity("layout", a)
            else:
                self.emit_ref("go_to_layout", "layout", fm_id=a.get("id"),
                              name=a.get("name"), raw=a.get("name"))
            return

        if tag == "Step" and parent == "StepList":
            owner = next((e for e in reversed(self.entity_stack)
                          if e["kind"] == "script"), None)
            ordinal = None
            if owner is not None:
                owner["_nsteps"] = owner.get("_nsteps", 0) + 1
                ordinal = owner["_nsteps"]
            self.new_entity("script_step", a, step_type=a.get("name"),
                            name=a.get("name"), seq=ordinal)
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

        # ---- reference edges ----
        # External-target marker: a FileReference inside a script step means the
        # step's Script/Layout ref points at another FILE (its ids are numbered
        # in that file, so local resolution would silently link the wrong script).
        if tag == "FileReference" and parent == "Step":
            src = self.current_source()
            if src is not None and a.get("name"):
                src["_ext_file"] = a["name"]
            return

        # A FileReference inside a Table occurrence marks its base table as
        # living in another file (external data source) — field refs through
        # this TO must resolve against that file.
        if tag == "FileReference" and parent == "Table":
            if (self.entity_stack and a.get("name")
                    and self.entity_stack[-1]["kind"] == "table_occurrence"):
                self.entity_stack[-1]["ext_file"] = a["name"]
            return

        if tag == "FieldReference":
            to, leaf = _split_qualified(a.get("name"))
            ctx = "layout_object" if self.has_ancestor("Layout") else "field_reference"
            self.emit_ref(ctx, "field", fm_id=a.get("id"), name=leaf,
                          to_name=to, raw=a.get("name"))
            return

        if tag == "TableOccurrenceReference":
            self.emit_ref("to_reference", "table_occurrence",
                          fm_id=a.get("id"), name=a.get("name"), raw=a.get("name"))
            return

        # A <Field table id name/> reference element appears in several places:
        #   - relationship join predicates (LeftField/RightField)
        #   - inside a <Chunk type="FieldRef"> within a calculation
        #   - value-list field sources and sort orders (PrimaryField/SecondaryField)
        if tag == "Field" and parent in ("LeftField", "RightField", "Chunk",
                                         "PrimaryField", "SecondaryField", "Step"):
            if parent == "Chunk":
                ctx = "calc"
            elif parent == "Step":
                # the field a step acts on: Set Field's write target, Go to
                # Field, Insert ... - combine with the step_type to tell
                # writes from navigation
                ctx = "step_target"
            elif parent in ("PrimaryField", "SecondaryField"):
                ctx = "value_list_field" if self.has_ancestor("ValueList") else "sort"
            else:
                ctx = "join_predicate"
            self.emit_ref(ctx, "field", fm_id=a.get("id"),
                          name=a.get("name"), to_name=a.get("table"),
                          raw=(a.get("table", "") + "::" + a.get("name", "")))
            return
        if tag in ("LeftTable", "RightTable") and self.has_ancestor("Relationship"):
            self.emit_ref("join_predicate", "table_occurrence", name=a.get("name"),
                          raw=a.get("name"))
            # remember on the relationship for a synthetic name
            rel = next((e for e in reversed(self.entity_stack)
                        if e["kind"] == "relationship"), None)
            if rel is not None:
                rel.setdefault("_ends", []).append(a.get("name"))
            return

        # script reference (Perform Script, triggers) — NOT a definition
        if tag == "Script" and parent not in ("Group", "ScriptCatalog"):
            fmid, nm = a.get("id"), a.get("name")
            if fmid not in (None, "0") or (nm or ""):
                ctx = "trigger" if self.has_ancestor("ScriptTriggers") or parent == "Trigger" else "perform_script"
                if nm or (fmid and fmid != "0"):
                    self.emit_ref(ctx, "script", fm_id=fmid, name=nm, raw=nm)
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
            self._consume_text(tag, self._cap_attrs, text)
            self._cap_tag = None
            self._buf = []

        # pop the entity only if THIS element is the one that opened it
        if self.pushed and self.pushed.pop():
            self._finalize_entity(self.entity_stack.pop())

        # the external-file marker is scoped to its step (matters for button
        # steps, whose source is the enclosing layout, not a script_step)
        if tag == "Step":
            src = self.current_source()
            if src is not None:
                src.pop("_ext_file", None)

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
                else:
                    src.setdefault("_calc_parts", []).append(text)
        elif tag == "StepText":
            if src is not None:
                src["_step_text"] = text
        elif tag == "Name" and self.parent_tag() == "FieldObj":
            to, leaf = _split_qualified(text)
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
                    self.emit_ref("calc", "field", name=leaf, to_name=to, raw=text)
            # plain built-in FunctionRef / NoRef chunks are left to FTS search.

    def _finalize_entity(self, ent):
        import json as _j
        calc = "\n".join(ent.get("_calc_parts") or []) or None
        step_text = ent.get("_step_text")
        if ent["kind"] == "relationship" and ent.get("_ends"):
            ent["name"] = " :: ".join(ent["_ends"])
        extra = ent.get("extra_json")
        hide, tip, bounds = (ent.get("_hide_calc"), ent.get("_tooltip_calc"),
                             ent.get("_bounds"))
        if ent["kind"] == "layout_object" and (hide or tip or bounds):
            d = _j.loads(extra) if extra else {}
            if hide:
                d["hide_calc"] = hide
            if tip:
                d["tooltip_calc"] = tip
            if bounds:
                d["bounds"] = bounds
            extra = _json(d)
        # for steps, the display text is the searchable body; keep calc separately.
        # layout objects add their type, hide/tooltip calcs, and children so both
        # the object row and the aggregated layout body are searchable.
        body = " ".join(filter(None,
            [ent.get("name"), ent.get("_otype"), calc, step_text, hide, tip]
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
                "ext_file,extra_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self.ent_rows)
            self.ent_rows.clear()
        if self.ref_rows:
            self.conn.executemany(
                "INSERT INTO refs(ref_id,file_id,source_entity_id,source_kind,context,"
                "target_kind,target_fm_id,target_name,target_to_name,target_raw,"
                "target_file,target_entity_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self.ref_rows)
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


def _reject_dtd(path: str) -> None:
    """Refuse a DDR that declares a DTD internal subset. Real FileMaker DDRs
    have none; an internal subset with <!ENTITY declarations is the entity-
    expansion ("billion laughs") attack, which older expat (<2.6) won't stop.
    The subset always sits in the head, before the root element."""
    import re
    with open(path, "rb") as f:
        head = f.read(8192)
    enc = "utf-16-le" if head[:2] == b"\xff\xfe" else "utf-8"
    text = head.decode(enc, "replace")
    if re.search(r"<!DOCTYPE[^>]*\[", text) or "<!ENTITY" in text:
        raise ValueError(
            f"{path} declares an XML DTD/entity subset; refusing to parse it "
            "(a FileMaker DDR never contains one).")


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
          force: bool = False) -> dict:
    """Parse one or more DDR files (or a Summary.xml manifest) into a fresh
    SQLite DB. Returns a summary dict.

    The DB is built into a temp file alongside the target and moved into place
    only on success, so a malformed DDR (or a typo'd -o path) never destroys an
    existing file, and two concurrent builds can't corrupt each other's output.
    An existing file at db_path that is not itself an fmsonar DB is left intact
    unless force=True.
    """
    if isinstance(ddr_paths, str):
        ddr_paths = [ddr_paths]
    if len(ddr_paths) == 1:
        linked = expand_summary(ddr_paths[0])
        if linked:
            ddr_paths = linked

    if os.path.exists(db_path) and not force and not _is_fmsonar_db(db_path):
        raise ValueError(
            f"{db_path} already exists and is not an fmsonar database — "
            "refusing to overwrite it. Choose another -o path or pass --force.")

    target_dir = os.path.dirname(os.path.abspath(db_path)) or "."
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

        ver, ctime = _peek_report_meta(ddr_paths[0])
        cur = conn.execute(
            "INSERT INTO ddr_run(source_path,ddr_version,creation_time,parsed_at,parser_version,label)"
            " VALUES (?,?,?,?,?,?)",
            (";".join(os.path.abspath(p) for p in ddr_paths), ver, ctime,
             datetime.now(timezone.utc).isoformat(), _parser_version(),
             label or os.path.basename(ddr_paths[0])))
        run_id = cur.lastrowid

        # One handler across all files: entity/ref ids stay a single space, and
        # each <File> element opens a new row in `files`.
        handler = DDRHandler(conn, run_id)
        for p in ddr_paths:
            _reject_dtd(p)
            parser = xml.sax.make_parser()
            parser.setContentHandler(handler)
            # expat honors the BOM / XML declaration, so binary mode handles UTF-16-LE.
            with open(p, "rb") as f:
                parser.parse(f)
        handler.flush()
        conn.commit()

        if handler.file_id is None:
            raise ValueError(
                f"{ddr_paths[0]} is not a FileMaker DDR (no FMPReport/File found). "
                "Generate one via Tools > Database Design Report > XML.")

        # resolve edges + build views
        with open(os.path.join(os.path.dirname(__file__), "resolve.sql")) as f:
            conn.executescript(f.read())
        conn.commit()
        summary = _summarize(conn)
    except BaseException:
        conn.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    conn.close()
    os.replace(tmp_path, db_path)  # atomic; only reached on success
    return summary


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
