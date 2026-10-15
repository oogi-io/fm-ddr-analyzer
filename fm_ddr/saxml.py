"""FileMaker "Save a Copy as XML" (root FMSaveAsXML) input: format sniff,
supported versions, refusals, the DDR_INFO pre-pass and the SaXMLHandler.

The DDR (root FMPReport) stays the parser's first format; build() sniffs each
path and dispatches. Only versions in SUPPORTED are read, and only exports made
with "Include details for analysis tools" (Has_DDR_INFO): step text and
calculation references exist only in that section.
"""

from __future__ import annotations

import json
import os
import re
import xml.sax

from fm_ddr import parse as _parse

# FMSaveAsXML@version -> fixture (the synthetic lab file; the 2.3.0.1 export
# is a later state, its OnFirstWindowOpen trigger removed)
SUPPORTED = {
    "2.3.0.0": "saxml_2.3.0.0.xml",   # FileMaker 26.0.1, FMDeveloperTool 26.0.1.68 (measured)
    "2.3.0.1": "saxml_2.3.0.1.xml",   # FileMaker 26.1.1, Pro 26.1.1 (measured; Server not yet)
}

# FMSaveAsXML@version -> the FileMaker release that writes it, for messages
FILEMAKER = {"2.3.0.0": "26.0", "2.3.0.1": "26.1"}


def supported_text() -> str:
    """'version 2.3.0.0 (FileMaker 26.0)' / 'versions A and B (FileMaker
    x, y)', built from SUPPORTED."""
    vers = sorted(SUPPORTED)
    listing = vers[0] if len(vers) == 1 else ", ".join(vers[:-1]) + " and " + vers[-1]
    fms = [FILEMAKER[v] for v in vers if v in FILEMAKER]
    tail = f" (FileMaker {', '.join(fms)})" if fms else ""
    return f"version{'s' if len(vers) > 1 else ''} {listing}{tail}"

# Subtrees the handler never enters: FileMaker's credential elements, and bulk
# no DDR rule reads. Bounds is read (the DDR reads it), so it is not here.
SKIP = frozenset({
    # Credentials
    "PasswordEncrypted", "INSECURE_PASSWORD", "INSECURE_TEXT", "Login",
    # Bulk
    "CSS", "Image", "namedstyles", "LibraryCatalog", "LocalCSS",
    "ExtendedAttributes", "TagList", "CoordRect", "Color",
})

# Skipped only under the named parent: Account/Authentication/AccountName is
# read, and the file-level Metadata section carries the file triggers.
SKIP_UNDER = {
    "Authentication": "Authorization",
    "Metadata": "Theme",
}


# Read only these children under the named parent: a layout's TableView
# keeps its column list, a column its field reference. Anything else there
# (none seen in the exports read so far) is skipped.
READ_ONLY_UNDER = {
    "TableView": frozenset({"ObjectList"}),
    "TableViewLayoutObject": frozenset({"FieldReference"}),
}


def is_skipped(name: str, parent: str | None) -> bool:
    """True if the element `name` under `parent` opens a skipped subtree."""
    keep = READ_ONLY_UNDER.get(parent)
    return (name in SKIP or SKIP_UNDER.get(name, object()) == parent
            or (keep is not None and name not in keep))


def _dtd_refusal(path):
    return ValueError(
        f"{path} declares an XML DTD/entity subset; refusing to parse it "
        "(a FileMaker DDR or Save a Copy as XML export never contains one).")


class _Root(Exception):
    """Raised at the first start element: the root is read."""


def read_root(path: str):
    """Parse the file with expat up to the root element's start tag and
    return (root name, {attribute: value}). Expat reads the prolog
    (declaration, comments, processing instructions, a DOCTYPE, any encoding
    it knows) and decodes the attribute values. Raises ValueError for an
    empty file, for XML expat cannot read up to the root (its reason, line
    and column), and for a DOCTYPE with an internal subset or an ENTITY
    declaration, before any of it is used: the subset is the
    entity-expansion attack older expat does not stop. Nothing is read past
    the chunk that holds the root's start tag."""
    import xml.parsers.expat as expat
    root = []

    def start(name, attrs):
        root.append((name, attrs))
        raise _Root

    def doctype(name, system_id, public_id, has_internal_subset):
        if has_internal_subset:
            raise _dtd_refusal(path)

    def entity(*args):
        raise _dtd_refusal(path)

    p = expat.ParserCreate()
    p.StartElementHandler = start
    p.StartDoctypeDeclHandler = doctype
    p.EntityDeclHandler = entity
    size = 0
    with open(path, "rb") as f:
        try:
            while True:
                chunk = f.read(65536)
                size += len(chunk)
                p.Parse(chunk, not chunk)
                if not chunk:   # expat raises before this: no root element
                    break
        except _Root:
            return root[0]
        except expat.ExpatError as e:
            if not size:
                raise ValueError(
                    f"{path} is empty: fmsonar reads a FileMaker DDR (root "
                    "FMPReport) or a Save a Copy as XML export (root "
                    "FMSaveAsXML).") from None
            raise ValueError(
                f"{path} is not well-formed XML: {expat.ErrorString(e.code)} at "
                f"line {e.lineno}, column {e.offset}. fmsonar reads a FileMaker "
                "DDR (root FMPReport) or a Save a Copy as XML export (root "
                "FMSaveAsXML); re-export the file, or check it is the one you "
                "meant.") from None
    raise ValueError(f"{path} holds no XML root element.")


def sniff(path: str) -> dict:
    """Report the format from the root element (read_root): fmt is "saxml"
    on an FMSaveAsXML root, "ddr" on FMPReport, None otherwise ("root" names
    it). The other keys describe the root's attributes. Raises ValueError on
    a DTD internal subset or an ENTITY declaration."""
    root, a = read_root(path)
    info = {"fmt": None, "root": root, "type": a.get("type"),
            "version": None, "source": None, "has_details": None,
            "split_catalogs": None, "binary_under_lo": None, "file": None,
            "locale": None}
    if root == "FMSaveAsXML":
        info.update(
            fmt="saxml",
            version=a.get("version"),
            source=a.get("Source"),
            has_details=a.get("Has_DDR_INFO") == "True",
            split_catalogs=a.get("split_catalogs") == "True",
            binary_under_lo=a.get("binary_under_lo") == "True",
            file=a.get("File"),
            locale=a.get("locale"))
    elif root == "FMPReport":
        info["fmt"] = "ddr"
    return info


def refuse_unknown_root(info: dict, path: str) -> None:
    """Refuse a file whose root is neither FMPReport nor FMSaveAsXML, and
    the summary Pro writes next to a multi-file Save a Copy as XML."""
    if info["fmt"] == "ddr" and info["type"] == "XMLv2_Summary":
        raise ValueError(
            f"{path} is the summary FileMaker Pro writes next to a Save a Copy "
            "as XML of several open files (FMPReport type XMLv2_Summary): it "
            "lists the exports and holds no design itself. fmsonar does not read "
            "it; build from each file's export instead (one per build, or "
            "several in one build).")
    if info["fmt"] is not None:
        return
    raise ValueError(
        f"{path} is neither a FileMaker DDR (root FMPReport) nor a Save a Copy "
        f"as XML export (root FMSaveAsXML): its root element is <{info['root']}>.")


def check(info: dict, path: str) -> None:
    """Refuse a SaXML export fmsonar does not read. Raises ValueError."""
    from fm_ddr import __version__
    if info["version"] not in SUPPORTED:
        ver = "unknown" if info["version"] is None else '"' + info["version"] + '"'
        src = f", FileMaker {info['source']}" if info["source"] else ""
        raise ValueError(
            f"{path} is a Save a Copy as XML export (FMSaveAsXML version "
            f"{ver}{src}); fmsonar {__version__} reads {supported_text()}. "
            "Export the file with one of those FileMaker versions or build "
            "from a DDR.")
    if not (info["file"] or "").strip():
        raise ValueError(
            f"{path} has an empty File attribute on its FMSaveAsXML root: "
            "fmsonar names the file in the index from it. Re-export the file.")
    if not info["has_details"]:
        raise ValueError(
            f'{path} was exported without "Include details for analysis '
            'tools" (no Has_DDR_INFO on the root): step text and calculation '
            "references exist only in that section. Re-export with the option "
            "on (FMDeveloperTool: -include_details).")
    if info["split_catalogs"] or info["binary_under_lo"]:
        raise ValueError(
            f"{path} was exported with split_catalogs or binary_under_lo; "
            "fmsonar reads the default export only. Re-export with the "
            "defaults.")


def build_warnings(conn, infos=()) -> list:
    """The SaXML-only build warnings: the exports' locale (from the sniffed
    roots, `infos` as (path, info) pairs), then the built index's."""
    return [w for w in (locale_warning(infos), ddrinfo_warning(conn),
                        external_to_warning(conn), resolution_warning(conn)) if w]


def resolution_warning(conn):
    """The DDR's low-resolution warning, on the references nothing else
    explains: an unresolved reference v_dangling lists (a deleted target)
    or one into another file (a target_file, a TO of another file) has its
    own row or warning. On a small file a few planted defects would
    otherwise pass the 5 percent line (the lab: 4 of 78)."""
    total, res = conn.execute(
        "SELECT count(*), count(target_entity_id) FROM refs").fetchone()
    explained = conn.execute(
        "SELECT count(*) FROM refs r WHERE r.target_entity_id IS NULL AND ("
        "r.target_file IS NOT NULL "
        "OR r.ref_id IN (SELECT ref_id FROM v_dangling WHERE ref_id IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM entities t WHERE t.kind = 'table_occurrence' "
        "AND t.file_id = r.file_id AND t.name = r.target_to_name "
        "AND (t.ext_file IS NOT NULL "
        "OR json_extract(t.extra_json, '$.external') = 1)))").fetchone()[0]
    rest = total - explained
    if not rest or res / rest >= 0.95:
        return None
    return (
        f"WARNING: only {100 * res // rest}% of references resolved, leaving out "
        f"the {explained} that v_dangling lists or that point into another file.\n"
        "  On a whole export expect ~98%: check `stats` / v_unresolved for a "
        "construct fmsonar does not read.")


def ddrinfo_misses(conn):
    """(DDRREFs read, steps without step text, calculations without
    reference list) from ddr_run; None on a DDR index or one built before
    the columns."""
    try:
        row = conn.execute("SELECT ddrinfo_refs, ddrinfo_missing_steps, "
                           "ddrinfo_missing_calcs FROM ddr_run LIMIT 1").fetchone()
    except Exception:   # sqlite3.OperationalError: an older index
        return None
    if not row or row[0] is None:
        return None
    return row[0], row[1] or 0, row[2] or 0


def unused_caveat(conn):
    """One line for `stats` and the report's health page: on a SaXML index
    with calculations without reference list or unverifiable_xfile rows,
    the unused candidates may include objects used only there."""
    m = ddrinfo_misses(conn)
    if m is None or not (m[2] or conn.execute(
            "SELECT 1 FROM v_dangling WHERE context = 'unverifiable_xfile' "
            "LIMIT 1").fetchone()):
        return None
    return ("Unused candidates may include objects used only in calculations "
            "without reference list or through table occurrences of another "
            "file: check each with `search`.")


def ddrinfo_warning(conn):
    """The DDRREFs whose DDR_INFO entry is absent, from ddr_run."""
    m = ddrinfo_misses(conn)
    if not m or not (m[1] or m[2]):
        return None
    refs, steps, calcs = m
    return (
        f"WARNING: {calcs} calculation(s) without reference list and {steps} "
        f"step(s) without step text in this export's DDR_INFO ({refs} "
        "references read).\n"
        "  A field or custom function used only inside such a calculation has no "
        "reference from it: it can\n"
        "  read as unused in v_unused_fields; `where` prints its full-text mentions "
        "when it finds no reference,\n"
        "  and `search` finds the text. A calculation whose list under another hash "
        "names no field or custom function\n"
        "  is not counted: nothing is lost there.")


def locale_warning(infos):
    """Every export measured so far is English (root locale="English"); a
    few rules read English step text. Another locale, or none, builds with
    this warning until such an export is measured. The rules are listed in
    one place, COVERAGE.md's "Locale" paragraph."""
    odd = [(os.path.basename(p), i.get("locale")) for p, i in infos
           if i.get("locale") != "English"]
    if not odd:
        return None
    which = ", ".join(f'{n}: locale "{loc}"' if loc else f"{n}: no locale"
                      for n, loc in odd)
    return (
        f"WARNING: {which}. fmsonar's SaXML rules are measured on English "
        "exports only.\n"
        "  The few rules that read English step text (COVERAGE.md (\"Locale\") "
        "lists them) may miss on this export:\n"
        "  a miss drops a deleted-layout reference from v_dangling or lists a "
        "cross-file call as a deleted script.\n"
        "  Everything read by id (catalogs, field, script, layout and TO "
        "references, calculation chunks) does not depend on the language.")


def external_to_warning(conn):
    """The one warning for external TOs with no data source (the
    FMDeveloperTool route), from the built index; None when there are none."""
    tos = conn.execute(
        "SELECT count(*) FROM entities WHERE kind = 'table_occurrence' "
        "AND json_extract(extra_json, '$.external') = 1").fetchone()[0]
    if not tos:
        return None
    # Entities whose every <Field Missing> follows such a TO (_mark_xfile)
    # and field objects and step targets through one: v_dangling's
    # unverifiable_xfile rows
    calcs = conn.execute(
        "SELECT count(*) FROM entities "
        "WHERE json_extract(extra_json, '$.xfile_marker') = 1").fetchone()[0]
    objs = conn.execute(
        "SELECT count(*) FROM v_dangling WHERE context = 'unverifiable_xfile' "
        "AND ref_id IS NOT NULL").fetchone()[0]
    return (
        f"WARNING: {tos} external table occurrence(s) in this Save a Copy as XML "
        f"export carry no data source and no base table.\n"
        f"  Field references through them stay unresolved, and calculations that use "
        f"a field in another file read\n"
        f"  TO::<Field Missing>: {calcs} object(s) here show in v_dangling as "
        f"unverifiable_xfile, not broken_calc:\n"
        f"  the field may exist in the other file or may be gone; this export "
        f"cannot tell.\n"
        + (f"  {objs} field reference(s) from layout objects or script steps through "
           f"them name no field and show in v_dangling as unverifiable_xfile too.\n"
           if objs else "")
        + "  The export left these table occurrences empty (FMDeveloperTool does "
        "this; measured: FileMaker Pro with the files open fills them;\n"
        "  Server not yet measured).")


def refuse_duplicates(pairs) -> None:
    """Refuse two exports of the same file in one build (the same path
    twice, or a stale copy next to the current one): both would open a
    files row of the same name and double every count."""
    seen = {}
    for path, info in pairs:
        key = (info["file"] or "").casefold()
        if key in seen:
            raise ValueError(
                f"{seen[key]} and {path} are both exports of {info['file']}: "
                "pass each file of the solution once.")
        seen[key] = path


# The sections every whole export writes, as element name -> the name
# FMDeveloperTool's -catalog_list takes. A file always has a table, an
# occurrence, fields, a layout, the built-in privilege sets and extended
# privileges, an account, file access and theme catalogs, and Metadata (file
# options and triggers); -catalog_list writes each only when it is named
# (measured, FMDeveloperTool 26.0.1).
REQUIRED_SECTIONS = {
    "FileAccessCatalog": "FileAccessCatalog",
    "BaseTableCatalog": "BaseTableCatalog",
    "TableOccurrenceCatalog": "TableOccurrenceCatalog",
    "FieldsForTables": "FieldCatalog",
    "ThemeCatalog": "ThemeCatalog",
    "LayoutCatalog": "LayoutCatalog",
    "PrivilegeSetsCatalog": "PrivilegeSetsCatalog",
    "ExtendedPrivilegesCatalog": "ExtendedPrivilegesCatalog",
    "AccountsCatalog": "AccountsCatalog",
    "Metadata": "Metadata",
}

# Catalogs a whole export leaves out when the file has none of their kind,
# as the kind pass 1 records -> (section, -catalog_list name, the words for
# the message). The export still refers to that kind (a button's script, a
# field's value list, a calculation's custom function, an external table
# occurrence) only when the catalog was left out. Relationships, custom
# menus and menu sets have no such tell and are not checked.
REFERRED_SECTIONS = {
    "script": ("ScriptCatalog", "ScriptCatalog", "scripts"),
    "value_list": ("ValueListCatalog", "ValueListCatalog", "value lists"),
    "custom_function": ("CustomFunctionsCatalog", "CustomFunctionsCatalog",
                        "custom functions"),
    "data_source": ("ExternalDataSourceCatalog", "ExternalDataSourceCatalog",
                    "external data sources"),
}


def refuse_partial(sections, kinds, path) -> None:
    """Refuse an export that lacks a section every file writes, or lacks a
    catalog while it refers to that catalog's kind (`kinds`, from pass 1): a
    -catalog_list export indexes as a whole file with part of it missing."""
    missing = [cl for el, cl in REQUIRED_SECTIONS.items() if el not in sections]
    referred = [(cl, words) for kind, (el, cl, words) in REFERRED_SECTIONS.items()
                if kind in kinds and el not in sections]
    if not missing and not referred:
        return
    why = []
    if missing:
        why.append(f"it lacks {', '.join(missing)}, which every FileMaker file "
                   "writes")
    if referred:
        why.append("it lacks " + ", ".join(cl for cl, _ in referred)
                   + " while it refers to "
                   + ", ".join(words for _, words in referred))
    raise ValueError(
        f"{path} is a partial Save a Copy as XML export: {'; '.join(why)}. "
        "FMDeveloperTool makes such a file with -catalog_list; an index of "
        "part of a file lists used fields as unused and called scripts as "
        "orphaned. Re-export without -catalog_list.")


def run_versions(infos):
    """(versions, sources) for ddr_run: the build's distinct FMSaveAsXML
    versions and FileMaker versions, ", "-joined, so a build of 2.3.0.0 and
    2.3.0.1 exports names both (files.format_version holds each file's).
    No rule reads the version: every SUPPORTED version builds with any
    other."""
    vers = sorted({i["version"] for i in infos if i["version"]})
    srcs = sorted({i["source"] for i in infos if i["source"]})
    return ", ".join(vers) or None, ", ".join(srcs) or None


# A step text DDR_INFO does not hold is damage: every whole export measured
# has none missing. Past this share of a file's StepText DDRREFs the export
# is refused; calculations without reference list are a normal class and
# never refuse.
STEPTEXT_MISS_LIMIT = 0.10


class _InfoPass(xml.sax.ContentHandler):
    """Pass 1: read DDR_INFO (the last section) into a dict keyed by
    (entry key, hash), the pair a DDRREF carries (text, @hash). Keys repeat
    with different hashes on a real file, so the key alone is not unique.

    Payloads: StepText -> the element text, stripped as the DDR capture
    strips. ChunkList -> a list of tuples, ("CustomFunctionRef", name) and
    ("FieldRef", field_id, field_name, repetition, to_id, to_name), attribute
    strings as written; to_id and to_name are None when the FieldReference
    has no TableOccurrenceReference (<Table Missing>). Other chunk types are
    dropped, as the DDR path ignores them. The first copy of a pair wins.
    """

    def __init__(self):
        super().__init__()
        self.info = {}
        self.inside = False
        self._stack = []        # element names below DDR_INFO
        self._entry = None      # (key, hash, datatype) of the open entry
        self._chunks = None
        self._chunk_type = None
        self._field = None      # [id, name, repetition, to_id, to_name]
        self._buf = None
        self.root = None        # the root element's name
        self.sections = set()   # Metadata, Structure, ... and the catalogs
        self.kinds = set()      # kinds the export refers to (REFERRED_SECTIONS)
        self._head = []         # the open element path
        self._ds_child = []     # per open element: a DataSourceReference child seen

    def parse(self, path: str) -> dict:
        p = xml.sax.make_parser()
        p.setContentHandler(self)
        with open(path, "rb") as f:
            p.parse(f)
        return self.info

    def startElement(self, name, attrs):
        head = self._head
        head.append(name)
        if len(head) == 1:
            self.root = name
        elif len(head) == 2:
            self.sections.add(name)
        elif len(head) == 4 and head[1:3] == ["Structure", "AddAction"]:
            self.sections.add(name)
        if not self.inside:
            self._refer(name, attrs)
            if name == "DDR_INFO":
                self.inside = True
            return
        st = self._stack
        if (len(st) == 2 and st[1] == "ObjectList" and name.startswith("_")):
            dt = attrs.get("datatype")
            self._entry = (name, attrs.get("hash"), dt)
            if dt == "StepText":
                self._buf = []
            elif dt == "ChunkList":
                self._chunks = []
        elif self._chunks is not None:
            if name == "Chunk":
                self._chunk_type = attrs.get("type")
                if self._chunk_type == "CustomFunctionRef":
                    self._buf = []
                    self.kinds.add("custom_function")
            elif name == "FieldReference" and self._chunk_type == "FieldRef":
                self._field = [attrs.get("id"), attrs.get("name"),
                               attrs.get("repetition"), None, None]
            elif name == "TableOccurrenceReference" and self._field is not None:
                self._field[3] = attrs.get("id")
                self._field[4] = attrs.get("name")
        st.append(name)

    def _refer(self, name, attrs):
        """Record the kinds outside DDR_INFO that need their catalog. A
        script reference after a DataSourceReference sibling (a cross-file
        Perform Script) names another file's script; an unset slot (id 0,
        no name) names none."""
        ds = self._ds_child
        ds.append(False)
        if name == "DataSourceReference":
            if len(ds) >= 2:
                ds[-2] = True
            self.kinds.add("data_source")
        elif name == "ScriptReference":
            if not (len(ds) >= 2 and ds[-2]) and (
                    attrs.get("name") or attrs.get("id") not in (None, "0")):
                self.kinds.add("script")
        elif name == "ValueListReference":
            self.kinds.add("value_list")
        elif name == "TableOccurrence" and attrs.get("type") == "External":
            self.kinds.add("data_source")

    def characters(self, content):
        if self._buf is not None:
            self._buf.append(content)

    def endElement(self, name):
        self._head.pop()
        if not self.inside:
            self._ds_child.pop()
            return
        if name == "DDR_INFO" and not self._stack:
            self.inside = False
            self._ds_child.pop()
            return
        self._stack.pop()
        ent = self._entry
        if ent is None:
            return
        if len(self._stack) == 2:            # the entry itself closes
            key, hsh, dt = ent
            if dt == "StepText":
                payload = "".join(self._buf).strip()
            elif dt == "ChunkList":
                payload = self._chunks
            else:
                payload = None
            if payload is not None:
                self.info.setdefault((key, hsh), payload)
            self._entry = self._chunks = self._buf = None
        elif name == "Chunk" and self._chunks is not None:
            if self._chunk_type == "CustomFunctionRef":
                self._chunks.append(("CustomFunctionRef",
                                     "".join(self._buf).strip()))
                self._buf = None
            elif self._chunk_type == "FieldRef" and self._field is not None:
                self._chunks.append(("FieldRef", *self._field))
            self._chunk_type = None
            self._field = None


# ---- pass 2 -------------------------------------------------------------

# AutoEnter@type -> the DDR's AutoEnter@value. Only these eleven write a
# value (the DDR index's distinct auto_enter.type values); Calculated,
# SerialNumber, Looked_up and "" write none, as the DDR writes none.
AE_VALUE = {
    "CreationTimestamp": "CreationTimeStamp",
    "ModificationTimestamp": "ModificationTimeStamp",
    "CreationDate": "CreationDate", "ModificationDate": "ModificationDate",
    "CreationTime": "CreationTime", "ModificationTime": "ModificationTime",
    "CreationAccountName": "CreationAccountName",
    "ModificationAccountName": "ModificationAccountName",
    "CreationName": "CreationName", "ModificationName": "ModificationName",
    "ConstantData": "ConstantData",
}

# JoinPredicate@type -> the DDR's spelling; Equal, NotEqual and
# CartesianProduct are spelled alike, anything else passes through.
JOIN_OPS = {
    "Greater": "GreaterThan", "GreaterOrEqual": "GreaterThanOrEqualTo",
    "Less": "LessThan", "LessOrEqual": "LessThanOrEqualTo",
}


# Field@datatype -> the DDR's dataType; every other value (Text, Number,
# Date, Time, Binary) is spelled alike in both formats
DATA_TYPES = {"Timestamp": "TimeStamp"}

# Step@name -> the DDR's step name; every other step name read so far is
# spelled alike in both formats
STEP_NAMES = {"Set Field By Name": "Set Field by Name"}


def _theme_id(sax_id):
    """Theme@id is decimal; the DDR writes the same number as two-digit
    upper-case hex ("15" -> "0F"). Anything else passes through."""
    try:
        return format(int(sax_id), "02X")
    except (TypeError, ValueError):
        return sax_id


# Steps whose layout reference the end-of-step rule checks
LAYOUT_STEPS = ("Go to Layout", "Go to Related Record", "New Window")

# The step text's own marker for a deleted layout: "Using layout: <unknown>"
# (Go to Related Record, New Window), "Go to Layout [ <unknown>". Another
# <unknown> in the text (GTRR's "From table: <unknown>") is not the layout.
LAYOUT_UNKNOWN = re.compile(r"(?:layout: |Go to Layout \[ )<unknown>")

# A cross-file call in the step text: Perform Script [ “Ping” from file: “Sat” ]
FROM_FILE = re.compile(r'from file: ["“]([^"”]+)["”]')


def _marker_to(a) -> bool:
    """A TableOccurrenceReference that stands for a deleted TO (the lab:
    id="-1" name="<Table Missing>")."""
    return a.get("id") == "-1" or (a.get("name") or "").startswith("<")


def _step_name_as_ddr(text: str, step: dict) -> str:
    """The step text opens with the step's name ("//  " first when the step
    is disabled) in the export's spelling; write the DDR's (STEP_NAMES)."""
    raw, name = step.get("raw_name"), step["name"]
    if not raw or raw == name:
        return text
    for lead in ("", "//  "):
        if text.startswith(lead + raw):
            return lead + name + text[len(lead + raw):]
    return text


def _norm_cr(text: str) -> str:
    """SaXML text (CDATA) ends lines with CR; the DDR side is LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class SaXMLHandler(_parse.DDRHandler):
    """Pass 2: the SaXML twin of DDRHandler's rules. It reuses DDRHandler's
    helpers unchanged: new_entity, emit_ref, _finalize_entity, flush,
    _insert_file. Rows are written in the DDR's shape; what the SaXML cannot
    supply stays NULL.

    Element rules live in _start_<Tag> / _end_<Tag> methods, looked up by
    tag. Every rule checks the open entity it writes to, so an element of
    the same name elsewhere (a layout's Field, a step's FieldReference) does
    nothing until its own rule exists.
    """

    def __init__(self, conn, run_id, info=None, version=None, path=None):
        super().__init__(conn, run_id)
        self.info = info or {}
        self.version = version
        self.path = path
        self._skip = None            # depth at which a skipped subtree opened
        self._cap = None             # captured leaf tag
        self._cap_buf = []
        self._cur_bt = None          # (entity_id, name) of the open FieldCatalog
        self._bt_by_id = {}          # base table fm_id -> (entity_id, name)
        self._script_by_id = {}      # script fm_id -> entity_id (phase 5)
        self._folders = []           # open folder entities (phases 5, 6)
        self._step_seen = set()      # reference kinds seen in the step (phase 5)
        self._step = None            # the open Step (script step or action)
        self._step_owner = None      # (entity_id, grp) of StepsForScripts/Script
        self._ddrref = None          # (kind, hash) of the open DDRREF
        self._ddrref_seen = 0
        self._steptext_seen = 0      # StepText DDRREFs read
        self._steptext_missing = 0   # of those, with no entry
        self._chunkless = set()      # (key, hash) of calculations with no list
        self._refless = set()        # keys whose every chunk list names nothing
        self._fref = None            # the open FieldReference
        self._map_kind = None        # the open import Map's kind ("1" = not imported)
        self._side = None            # the open LeftTable / RightTable tag
        self._ext_empty = 0          # external TOs with no data source
        self._ext_empty_tos = set()  # their names
        self._action = None          # the open button action (phase 6)
        self._trigger = None         # the open ScriptTrigger (phase 6)
        self._tvcol = None           # the open TableViewLayoutObject attributes
        self._start_rules = {n[7:]: getattr(self, n) for n in dir(self)
                             if n.startswith("_start_")}
        self._end_rules = {n[5:]: getattr(self, n) for n in dir(self)
                           if n.startswith("_end_")}

    def parse(self, path, version):
        """Pass 1 (DDR_INFO) then pass 2 over one export. Several exports
        share one handler: one id space, one files row each. Pass 1 reads
        the whole file, so a cut-off or damaged one is refused before any
        row is written, with expat's reason and place."""
        info_pass = _InfoPass()
        try:
            self.info = info_pass.parse(path)
            lists = [(k, p) for (k, _h), p in self.info.items() if isinstance(p, list)]
            self._refless = {k for k, _ in lists} - {k for k, p in lists if p}
        except xml.sax.SAXParseException as e:
            raise ValueError(
                f"{path} is cut off or damaged: {e.getMessage()} at line "
                f"{e.getLineNumber()}, column {e.getColumnNumber()}. Re-export "
                "it from FileMaker.") from None
        if info_pass.root == "FMSaveAsXML":
            refuse_partial(info_pass.sections, info_pass.kinds, path)
        self.version, self.path = version, path
        self._bt_by_id, self._script_by_id = {}, {}
        self._folders, self._cur_bt, self._ext_empty = [], None, 0
        self._ext_empty_tos = set()
        self._step, self._step_owner = None, None
        self._ddrref_seen = 0
        self._steptext_seen = self._steptext_missing = 0
        self._chunkless = set()
        p = xml.sax.make_parser()
        p.setContentHandler(self)
        with open(path, "rb") as f:
            p.parse(f)
        self.flush()
        self._mark_xfile()
        # A reference DDR_INFO cannot answer is a step text or a calculation's
        # references lost. A valid export has such references (calculations
        # with no chunk list), so a count is no verdict: only a DDR_INFO with
        # no entry at all (cut off, emptied) is refused; expat refuses a file
        # truncated anywhere else.
        if self._ddrref_seen and not self.info:
            raise ValueError(
                f"{path}: DDR_INFO holds no entry for any of its "
                f"{self._ddrref_seen} references: a partial or truncated export")
        seen, miss = self._steptext_seen, self._steptext_missing
        if miss and miss > STEPTEXT_MISS_LIMIT * seen:
            raise ValueError(
                f"{path}: {miss} of {seen} step text(s) are missing from its "
                f"DDR_INFO, more than {STEPTEXT_MISS_LIMIT:.0%} (a whole export "
                "misses none): the file is damaged or was edited. Re-export it "
                "from FileMaker.")
        # Every miss is recorded (summed over the exports of the build):
        # build warns, stats and list show it, where points at search. A
        # calculation counts once: the export writes one (key, hash) pair in
        # several places
        self.conn.execute(
            "UPDATE ddr_run SET ddrinfo_refs = COALESCE(ddrinfo_refs, 0) + ?, "
            "ddrinfo_missing_steps = COALESCE(ddrinfo_missing_steps, 0) + ?, "
            "ddrinfo_missing_calcs = COALESCE(ddrinfo_missing_calcs, 0) + ? "
            "WHERE run_id = ?",
            (self._ddrref_seen, self._steptext_missing,
             len(self._chunkless), self.run_id))

    def _mark_xfile(self):
        """An entity whose every <Field Missing> marker follows an empty
        external TO (`ext_TO::<Field Missing>`, the FMDeveloperTool route)
        names a field in another file this export cannot see: it may exist
        or may be gone. Mark it `extra_json.xfile_marker`; v_dangling then
        lists its <Field Missing> row as `unverifiable_xfile`, not
        `broken_calc`. One bare or local-TO marker keeps the entity a
        defect. The TO before the marker is the longest TO name of the file
        the text ends with, so `a_ext_X::` is never read as `ext_X::`."""
        if not self._ext_empty_tos:
            return
        conn, fid = self.conn, self.file_id
        names = sorted((r[0] for r in conn.execute(
            "SELECT name FROM entities WHERE kind = 'table_occurrence' "
            "AND file_id = ? AND name IS NOT NULL", (fid,))), key=len, reverse=True)
        marker = _parse.FIELD_MISSING
        rows = conn.execute(
            "SELECT entity_id, calc_text, json_extract(auto_enter, '$.calc'), "
            "json_extract(extra_json, '$.validation_calc'), "
            "json_extract(extra_json, '$.hide_calc'), "
            "json_extract(extra_json, '$.tooltip_calc') "
            "FROM entities WHERE file_id = ? AND (calc_text LIKE '%<Field Missing>%' "
            "OR auto_enter LIKE '%<Field Missing>%' OR extra_json LIKE '%<Field Missing>%')",
            (fid,)).fetchall()
        marked = []
        for eid, *texts in rows:
            texts = [t for t in texts if t and marker in t]
            if texts and all(self._xfile_only(t, names) for t in texts):
                marked.append((eid,))
        conn.executemany(
            "UPDATE entities SET extra_json = json_set(COALESCE(extra_json, '{}'), "
            "'$.xfile_marker', json('true')) WHERE entity_id = ?", marked)

    def _xfile_only(self, text, names):
        """True when every <Field Missing> in text follows TO:: of an empty
        external TO."""
        marker = _parse.FIELD_MISSING
        pos = text.find(marker)
        while pos >= 0:
            if text[pos - 2:pos] != "::":
                return False
            head = text[:pos - 2]
            to = next((n for n in names if head.endswith(n)), None)
            if to not in self._ext_empty_tos:
                return False
            pos = text.find(marker, pos + len(marker))
        return True

    def new_entity(self, kind, attrs, **kw):
        ent = super().new_entity(kind, attrs, **kw)
        ent["_depth"] = len(self.tagstack)   # the UUID rule reads it
        return ent

    def _anc(self, n):
        """The tag n levels up the stack (1 = the current element), or None
        when the stack is shallower: a crafted nesting never indexes past it."""
        st = self.tagstack
        return st[-n] if len(st) >= n else None

    def _top(self, kind):
        """The stack-top entity if it is of `kind`, else None."""
        st = self.entity_stack
        return st[-1] if st and st[-1]["kind"] == kind else None

    # ---- SAX callbacks -------------------------------------------------
    def startElement(self, tag, attrs):
        self.tagstack.append(tag)
        self.pushed.append(False)
        if self._skip is not None:
            return
        if is_skipped(tag, self.parent_tag()):
            self._skip = len(self.tagstack)
            return
        rule = self._start_rules.get(tag)
        if rule is not None:
            rule({k: attrs.getValue(k) for k in attrs.getNames()})

    def characters(self, content):
        if self._cap is not None and self._skip is None:
            self._cap_buf.append(content)

    def endElement(self, tag):
        if self._skip is not None:
            if len(self.tagstack) == self._skip:
                self._skip = None
            self.tagstack.pop()
            self.pushed.pop()
            return
        # (1) text capture
        if self._cap == tag:
            self._cap = None
            self._text(tag, "".join(self._cap_buf))
            self._cap_buf = []
        rule = self._end_rules.get(tag)
        if rule is not None:
            rule()
        # (3) pop and finalize the entity this element opened
        if self.pushed.pop():
            ent = self.entity_stack.pop()
            self._close(ent)
            self._finalize_entity(ent)
        self.tagstack.pop()

    def _capture(self, tag):
        self._cap = tag
        self._cap_buf = []

    def _close(self, ent):
        """Kind-specific finishing before _finalize_entity writes the row,
        then the UUID metadata merged into extra_json."""
        fin = getattr(self, "_close_" + ent["kind"], None)
        if fin is not None:
            fin(ent)
        meta = ent.get("_uuid")
        if meta:
            d = json.loads(ent["extra_json"]) if ent["extra_json"] else {}
            d.update(meta)
            ent["extra_json"] = json.dumps(d)

    # ---- UUID metadata ----------------------------------------------------
    # The UUID element directly under the element that opened the entity.
    # Catalog entries carry the last change on its attributes; steps and
    # layout objects carry a bare UUID. Empty attributes write no key.
    def _start_UUID(self, a):
        # pushed[-2]: the parent element opened the entity (a closed folder
        # stays on the stack; its marker's UUID is not the folder's)
        st = self.entity_stack
        if (st and st[-1].get("_depth") == len(self.tagstack) - 1
                and self.pushed[-2]):
            n = a.get("modifications")
            st[-1]["_uuid"] = {k: v for k, v in (
                ("modified_at", a.get("timestamp") or None),
                ("modified_by", a.get("userName") or None),
                ("modified_account", a.get("accountName") or None),
                ("modified_n", int(n) if n and n.isdigit() else None),
            ) if v is not None}
            self._capture("UUID")

    # ---- DDRREF replay ----------------------------------------------------
    # A DDRREF's text and hash key the DDR_INFO entry pass 1 read. StepText
    # under a Step is the step text (the DDR's StepText); ChunkList under any
    # Calculation replays the DDR's Chunk rules, owner = current_source().
    def _start_DDRREF(self, a):
        self._ddrref = (a.get("kind"), a.get("hash"))
        self._capture("DDRREF")

    def _ddrref_text(self, raw):
        kind, hsh = self._ddrref
        self._ddrref = None
        self._ddrref_seen += 1
        payload = self.info.get((raw.strip(), hsh))
        if kind == "StepText":
            self._steptext_seen += 1
        if payload is None:
            if kind == "StepText":
                self._steptext_missing += 1
            elif kind == "ChunkList" and raw.strip() not in self._refless:
                # Lost here unless the key's list under another hash names no
                # field or custom function (then there is nothing to lose)
                self._chunkless.add((raw.strip(), hsh))
            return
        parent = self.parent_tag()
        if kind == "StepText" and parent == "Step" and self._step is not None:
            text = _step_name_as_ddr(_norm_cr(payload), self._step)
            self._step_text_cur = text
            src = self.current_source()
            if src is not None and text:
                self._put_step_text(src, text)
        elif kind == "ChunkList" and (parent == "Calculation"
                                      or self._popover_title()):
            self._replay_chunks(payload)

    def _popover_title(self):
        """LayoutObject/Title (a popover's title calculation) holds its DDRREF
        and Text with no Calculation wrapper; the DDR's TitleCalc is a
        calculation of the popover object. Read at a Title child."""
        st = self.tagstack
        return (len(st) >= 3 and st[-2] == "Title" and st[-3] == "LayoutObject"
                and self._top("layout_object") is not None)

    def _replay_chunks(self, chunks):
        """parse.py's Chunk rules: CustomFunctionRef -> function_ref; FieldRef
        -> calc / auto_enter / validation by ancestor, from the structured
        reference (field id kept as fm_id)."""
        if self.current_source() is None:
            return
        ctx = ("auto_enter" if self.has_ancestor("AutoEnter")
               else "validation" if self.has_ancestor("Validation") else "calc")
        for c in chunks:
            if c[0] == "CustomFunctionRef":
                if c[1]:
                    self.emit_ref("function_ref", "custom_function", name=c[1],
                                  raw=c[1])
            elif c[0] == "FieldRef":
                _, fid, fname, _rep, _to_id, to_name = c
                if fname:
                    self.emit_ref(ctx, "field", fm_id=fid, name=fname,
                                  to_name=to_name,
                                  raw=f"{to_name}::{fname}" if to_name else fname)

    # ---- text ------------------------------------------------------------
    def _text(self, tag, raw):
        if tag == "DDRREF":
            self._ddrref_text(raw)
            return
        if tag == "UUID":
            ent = self.entity_stack[-1] if self.entity_stack else None
            if ent is not None and raw.strip():
                ent["_uuid"]["uuid"] = raw.strip()
            return
        if tag in self.ACCOUNT_NAME_TAGS:
            acc = self._top("account")
            if acc is not None:
                acc["name"] = raw.strip() or None
            return
        if tag == "Data":
            # Layout text: a text object's runs and a button's label
            obj = self._top("layout_object")
            text = raw.strip()
            if obj is not None and text:
                obj.setdefault("_text_runs", []).append(text)
            return
        if tag == "UniversalPathList":
            ds = self._top("external_data_source")
            first = _norm_cr(raw).strip().split("\n")[0]
            if ds is not None and first:
                ds["_file_name"] = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*:", "", first)
            return
        if tag == "Text":
            text = _norm_cr(raw).strip()
            if not text:
                return
            parent = self.parent_tag()
            if (parent == "Calculation" and self._anc(4) == "repetition"
                    and self._anc(5) in ("FieldReference", "Variable")):
                # A field or variable target's repetition (Set Field, Go to
                # Field, Insert Text into $v): the DDR keeps it out of the
                # step's calculation text. Set Variable's own repetition
                # (Parameter/repetition) stays, as on the DDR.
                return
            if parent == "Calculation" or self._popover_title():
                if self._action is not None and self._anc(3) == "action":
                    # A button's script parameter, unstripped as the DDR's
                    # step text quotes it
                    self._action["param"] = _norm_cr(raw)
                self._calc_text(text)
            elif parent == "CustomValues":
                vl = self._top("value_list")
                if vl is not None:
                    vl.setdefault("_vl_custom", []).append(text)

    def _calc_text(self, text):
        """Calculation/Text routed as the DDR routes <Calculation> text."""
        src = self.current_source()
        if src is None:
            return
        if src["kind"] == "layout_object" and self.has_ancestor("Hide"):
            src["_hide_calc"] = text
        elif src["kind"] == "layout_object" and self.has_ancestor("Tooltip"):
            src["_tooltip_calc"] = text
        elif src["kind"] == "field" and self.has_ancestor("AutoEnter"):
            src.setdefault("_ae_calc_parts", []).append(text)
        elif src["kind"] == "field" and self.has_ancestor("Validation"):
            src.setdefault("_val_calc_parts", []).append(text)
        else:
            src.setdefault("_calc_parts", []).append(text)

    def _start_AccountName(self, a):
        self._account_text("AccountName")

    def _account_text(self, tag):
        st = self.tagstack
        if (tag in self.ACCOUNT_NAME_TAGS and "Authentication" in st[-3:-1]
                and self._top("account") is not None):
            self._capture(tag)

    def _start_Text(self, a):
        if self.parent_tag() in ("Calculation", "CustomValues") or self._popover_title():
            self._capture("Text")
        elif (self.parent_tag() == "Parameter" and self._step is not None
              and "value" in a):
            self._step["literal"] = _norm_cr(a["value"])   # Insert Text

    # ---- file --------------------------------------------------------------
    def _start_FMSaveAsXML(self, a):
        self.file_id = self._insert_file(a.get("File"), self.path)
        self.conn.execute(
            "UPDATE files SET format_version = ?, source_version = ? WHERE file_id = ?",
            (a.get("version"), a.get("Source"), self.file_id))

    # ---- pass 2 never re-reads DDR_INFO (pass 1 did) ----------------------
    def _start_DDR_INFO(self, a):
        self._skip = len(self.tagstack)

    # ---- scripts and script folders ----------------------------------------
    # Folder membership is positional: isFolder="True" opens a folder that
    # stays on the entity stack after its element closes, isFolder="Marker"
    # closes the top one; entries in between are its members.
    def _start_Script(self, a):
        if self.parent_tag() != "ScriptCatalog" or a.get("isSeparatorItem") == "True":
            return
        f = a.get("isFolder")
        if f == "True":
            self._open_folder("script_group", a)
        elif f == "Marker":
            self._close_folder("script_group")
        else:
            parent = self._folders[-1]["entity_id"] if self._folders else None
            ent = self.new_entity("script", a, parent_id=parent)
            self._script_by_id[a.get("id")] = (ent["entity_id"], ent["grp"])

    def _end_Script(self):
        parent = self.parent_tag()
        if parent == "ScriptCatalog":
            top = self.entity_stack[-1] if self.entity_stack else None
            if self.pushed[-1] and top is not None and top.get("_folder"):
                self.pushed[-1] = False   # open until its marker
        elif parent == "StepsForScripts":
            self._step_owner = None

    def _end_ScriptCatalog(self):
        while self._folders:              # unbalanced markers: close the rest
            self._close_folder(self._folders[-1]["kind"])

    def _open_folder(self, kind, a):
        ent = self.new_entity(kind, {"id": a.get("id"), "name": a.get("name")})
        ent["_folder"] = True
        self._folders.append(ent)

    def _close_folder(self, kind):
        st = self.entity_stack
        if not self._folders or not st or st[-1] is not self._folders[-1]:
            return                        # a marker with no open folder
        ent = self._folders.pop()
        st.pop()
        self._close(ent)
        self._finalize_entity(ent)

    def _start_Options(self, a):
        # Script, layout and folder flags; hidden="False" is "Show in menu"
        # ("Include in layout menus") on
        parent = self.parent_tag()
        if parent == "action" and self._action is not None:
            self._action["options"] = True
            return
        if parent not in ("Script", "Layout") or not self.pushed[-2]:
            return
        ent = self.entity_stack[-1]
        flags = {"includeInMenu": "True" if a.get("hidden") == "False" else "False"}
        if ent["kind"] == "script":
            flags["runFullAccess"] = a.get("runwithfullaccess")
            ent["extra_json"] = _parse._menu_flags(flags, script=True)
        elif ent["kind"] in ("script_group", "layout", "layout_group"):
            ent["extra_json"] = _parse._menu_flags(flags)

    # ---- steps -----------------------------------------------------------
    # A script step is an entity under its script (StepsForScripts/Script/
    # ScriptReference names the owner); a Step under `action` (button,
    # button bar segment, custom menu item) opens none and its source is the
    # enclosing object or menu, as the DDR's ButtonObj and MenuItem steps.
    def _start_ScriptReference(self, a):
        st = self.tagstack
        parent = self.parent_tag()
        if parent == "Script" and len(st) >= 3 and st[-3] == "StepsForScripts":
            self._step_owner = self._script_by_id.get(a.get("id"))
            if self._step_owner is None:
                self._order_error()
        elif parent == "ScriptTrigger" and self._trigger is not None:
            # Layout, object and file triggers; a file trigger has no source
            self._trigger["script"] = True
            fmid, nm = a.get("id"), a.get("name")
            if nm or fmid not in (None, "0"):
                self.emit_ref("trigger", "script", fm_id=fmid, name=nm or None,
                              raw=nm or _parse.UNKNOWN,
                              trigger_event=self._trigger["event"])
        elif parent == "action" and self._action is not None:
            # A button's Perform Script, source the button object
            self._action["script"] = True
            self._action["name"] = a.get("name") or None
            self._script_ref(a)
        elif self._step is not None:
            # Perform Script "From list", Install OnTimer Script
            self._step_seen.add("script")
            self._script_ref(a)

    def _start_Step(self, a):
        st = self.tagstack
        if self._action is not None and self.parent_tag() == "action":
            self._action["step"] = True
        if (self.parent_tag() == "ObjectList" and len(st) >= 4
                and st[-3] == "Script" and st[-4] == "StepsForScripts"):
            if self._step_owner is None:
                self._order_error()
            owner, grp = self._step_owner
            idx = a.get("index")
            nm = STEP_NAMES.get(a.get("name"), a.get("name"))
            ent = self.new_entity(
                "script_step", {"id": a.get("id")}, step_type=nm,
                name=nm, parent_id=owner,
                seq=int(idx) + 1 if idx and idx.isdigit() else None)
            ent["grp"] = grp              # the DDR's step sits in the folder
            if a.get("enable") == "False":
                ent["_disabled"] = True
        elif self.parent_tag() != "action":
            return
        self._step = {"name": STEP_NAMES.get(a.get("name"), a.get("name")),
                      "raw_name": a.get("name")}
        src = self.current_source()
        if (self.parent_tag() == "action" and src is not None
                and src["kind"] == "custom_menu"):
            # One slot per menu item step, filled when its text arrives
            texts = src.setdefault("_menu_texts", [])
            self._step["menu_slot"] = len(texts)
            texts.append(None)
        self._step_seen = set()
        self._step_text_cur = None

    def _end_Step(self):
        if self._step is None:
            return
        self._insert_text_literal()
        self._end_of_step_rules()
        src = self.current_source()
        if src is not None:
            src.pop("_ext_file", None)
        self._step = None
        self._step_seen = set()
        self._step_text_cur = None

    def _insert_text_literal(self):
        """Insert Text's literal is Parameter/Text@value. The analysis step
        text quotes it after a field target and leaves it out after a
        variable; the DDR quotes it in both: Insert Text [ $v; “literal” ],
        before the first line's closing bracket, as the DDR's text."""
        lit = self._step.get("literal")
        text = self._step_text_cur
        if self._step["name"] != "Insert Text" or not lit or not text:
            return
        quoted = "“" + lit + "”"
        line, nl, rest = text.partition("\n")
        if quoted in text or not line.endswith(" ]"):
            return
        text = line[:-2] + "; " + quoted + " ]" + nl + rest
        self._step_text_cur = text
        src = self.current_source()
        if src is not None:
            self._put_step_text(src, text)

    def _put_step_text(self, src, text):
        """The open step's text onto its source. A custom menu keeps every
        item's step, one line each in item order (SaXML only: the DDR path
        keeps only the last); any other source has one step."""
        slot = self._step.get("menu_slot") if self._step is not None else None
        if slot is None:
            src["_step_text"] = text
            return
        texts = src["_menu_texts"]
        texts[slot] = text
        src["_step_text"] = "\n".join(t for t in texts if t)

    # ---- step parameters ---------------------------------------------------
    # Each reference element records its kind in _step_seen; the end-of-step
    # rules read it, because a deleted target has no element at all.
    def _step_ref(self, context, target_kind, **kw):
        if self.current_source() is not None:
            self.emit_ref(context, target_kind, **kw)

    def _start_List(self, a):
        if (self._step is not None and self.parent_tag() == "Parameter"
                and a.get("name") == "From list"):
            self._step_seen.add("from_list")

    def _script_ref(self, a):
        # parse.py's Script rule: id 0 with no name is an unset slot
        fmid, nm = a.get("id"), a.get("name")
        if nm or fmid not in (None, "0"):
            self._step_ref("perform_script", "script", fm_id=fmid,
                           name=nm or None, raw=nm or _parse.UNKNOWN)

    def _start_CallbackScriptReference(self, a):
        if self._step is not None:
            self._step_seen.add("callback")
            self._script_ref(a)

    def _start_LayoutReference(self, a):
        if self._step is None or self.parent_tag() != "LayoutReferenceContainer":
            return
        # GTRR with a new window repeats its layout inside WindowReference;
        # New Window has only that one
        if self._anc(3) == "WindowReference" and "layout" in self._step_seen:
            return
        self._step_seen.add("layout")
        nm = a.get("name") or None
        self._step_ref("go_to_layout", "layout", fm_id=a.get("id"), name=nm,
                       raw=nm or _parse.UNKNOWN)

    def _step_table(self, a):
        """GTRR's "From table" and Import's target TO. A marker TO (deleted)
        counts as absent: GTRR's end-of-step rule writes it, Import's here."""
        if _marker_to(a):
            if self.parent_tag() == "Target":
                self._step_seen.add("table")
                self._step_ref("step_table", "table_occurrence", name=None,
                               raw=_parse.UNKNOWN)
            return
        self._step_seen.add("table")
        nm = a.get("name") or None
        self._step_ref("step_table", "table_occurrence", fm_id=a.get("id"),
                       name=nm, raw=nm or _parse.UNKNOWN)

    def _step_field(self, ctx, fr):
        """A field a step names (Set Field's target, a sort field): the DDR's
        step <Field> rule, its marker read from the step text."""
        nm, to = fr["name"], fr["to"]
        text = self._step_text_cur or ""
        if nm:
            self._step_ref(ctx, "field", fm_id=fr["id"], name=nm, to_name=to,
                           raw=(to or "") + "::" + nm)
        elif to in self._ext_empty_tos:
            # A field in another file through a TO with no data source: the
            # export leaves its name empty, live or deleted alike: unresolved;
            # v_dangling lists a step target as unverifiable_xfile
            self._step_ref(ctx, "field", fm_id=fr["id"], name=None, to_name=to,
                           raw=None)
        elif to:
            marker = (_parse.FIELD_MISSING
                      if (to + "::" + _parse.FIELD_MISSING) in text else _parse.UNKNOWN)
            self._step_ref(ctx, "field", fm_id=fr["id"], name=None, to_name=to,
                           raw=to + "::" + marker)
        else:
            marker = (_parse.TABLE_MISSING if _parse.TABLE_MISSING in text
                      else _parse.UNKNOWN)
            self._step_ref(ctx, "field", fm_id=fr["id"], name=None, to_name=None,
                           raw=marker)

    def _end_of_step_rules(self):
        """Run at </Step> while the step (or the button's object, the menu)
        is still the source: what a deleted target leaves behind is only the
        step text."""
        if self.current_source() is None:
            return
        seen, text = self._step_seen, self._step_text_cur or ""
        name = self._step["name"]
        if "from_list" in seen and "script" not in seen:
            m = FROM_FILE.search(text)
            if m and "ds" not in seen:
                # Cross-file call with an empty reference: unresolved, but
                # target_file keeps it out of v_dangling
                self.current_source()["_ext_file"] = m.group(1)
                self._step_ref("perform_script", "script", name=None, raw=None)
            else:
                self._step_ref("perform_script", "script", name=None,
                               raw=_parse.UNKNOWN)
        if (name in LAYOUT_STEPS and "layout" not in seen
                and LAYOUT_UNKNOWN.search(text)):
            self._step_ref("go_to_layout", "layout", name=None, raw=_parse.UNKNOWN)
        if (name == "Go to Related Record" and "table" not in seen
                and _parse.UNKNOWN in text):
            self._step_ref("step_table", "table_occurrence", name=None,
                           raw=_parse.UNKNOWN)

    # ---- layouts and layout folders ----------------------------------------
    # Folders as script folders (positional); a separator opens nothing.
    def _start_Layout(self, a):
        if self.parent_tag() != "LayoutCatalog" or a.get("isSeparatorItem") == "True":
            return
        f = a.get("isFolder")
        if f == "True":
            self._open_folder("layout_group", a)
        elif f == "Marker":
            self._close_folder("layout_group")
        elif (a.get("name") or "").strip() != "-":
            parent = self._folders[-1]["entity_id"] if self._folders else None
            self.new_entity("layout", a, parent_id=parent)

    def _end_Layout(self):
        if self.parent_tag() == "LayoutCatalog":
            top = self.entity_stack[-1] if self.entity_stack else None
            if self.pushed[-1] and top is not None and top.get("_folder"):
                self.pushed[-1] = False   # open until its marker

    _end_LayoutCatalog = _end_ScriptCatalog

    # ---- layout objects ------------------------------------------------------
    # Every LayoutObject under a layout, at any depth (portals, panels,
    # groups, popovers nest ObjectList/LayoutObject); parent = the enclosing
    # object or the layout, as the DDR's nested Object.
    def _start_LayoutObject(self, a):
        if not self.has_ancestor("Layout") or not self.entity_stack:
            return
        if self.entity_stack[-1]["kind"] not in ("layout", "layout_object"):
            return
        ent = self.new_entity(
            "layout_object", a, name=(a.get("name") or None),
            extra_json=_parse._json({"object_type": a.get("type"), "key": a.get("id")}))
        ent["_otype"] = a.get("type")

    def _layout(self):
        """The open layout entity, below any open objects."""
        for ent in reversed(self.entity_stack):
            if ent["kind"] == "layout":
                return ent
        return None

    # ---- table view columns ----------------------------------------------
    # The DDR lists a table-view-only column (a field shown in Table View and
    # not on the form) as a field object of the layout; the SaXML has it only
    # in Layout/TableView. A column is read as a field object unless its
    # field (field id, TO id) is a field object of the layout (the DDR does
    # not list those twice). Column ids are their own id space: an id equal
    # to an object of the layout is common and says nothing. TableView comes
    # after PartsList, so every form object is known by then.
    def _start_TableViewLayoutObject(self, a):
        if (self.parent_tag() == "ObjectList" and self._anc(3) == "TableView"
                and self._top("layout") is not None):
            self._tvcol = a

    def _end_TableViewLayoutObject(self):
        self._tvcol = None

    def _table_view_column(self, fr):
        lay, col = self._top("layout"), self._tvcol
        if (fr["id"], fr.get("to_id")) in lay.get("_form_fields", ()):
            return
        # Opened on the FieldReference element: closed at its end
        ent = self.new_entity(
            "layout_object", col, name=None,
            extra_json=_parse._json({"object_type": "Edit Box", "key": col.get("id"),
                                     "table_view_only": True}))
        ent["_otype"] = "Edit Box"
        self._field_object(fr)

    def _start_Bounds(self, a):
        obj = self._top("layout_object")
        if obj is not None and self.parent_tag() == "LayoutObject":
            try:
                obj["_bounds"] = ",".join(
                    str(int(float(a.get(k, "0"))))
                    for k in ("top", "left", "bottom", "right"))
            except ValueError:
                pass

    def _start_Data(self, a):
        st = self.tagstack
        if (self.parent_tag() == "StyledText" and len(st) >= 3 and st[-3] == "Text"
                and self._top("layout_object") is not None):
            self._capture("Data")

    def _field_object(self, fr):
        """A field object's field: TO::Field, [n] appended to target_raw when
        the repetition is not 1 (the DDR's FieldObj/Name text); the name
        stays bare so it resolves, as on the DDR. No TO (or a deleted-TO marker): '<unknown>'. An empty
        name through an external TO with no data source: a field in another
        file, unresolved; v_dangling lists it as unverifiable_xfile."""
        nm, to, rep = fr["name"], fr["to"], fr.get("rep")
        suffix = f"[{rep}]" if rep and rep != "1" else ""
        if to is None:
            self.emit_ref("layout_object", "field", fm_id=fr["id"], name=None,
                          to_name=None, raw=_parse.UNKNOWN)
        elif nm:
            self.emit_ref("layout_object", "field", fm_id=fr["id"], name=nm,
                          to_name=to, raw=f"{to}::{nm}{suffix}")
        elif to in self._ext_empty_tos:
            self.emit_ref("layout_object", "field", fm_id=fr["id"], name=None,
                          to_name=to, raw=None)
        else:
            # The DDR's empty FieldObj/Name: '<unknown>'
            self.emit_ref("layout_object", "field", fm_id=fr["id"], name=None,
                          to_name=to, raw=_parse.UNKNOWN)

    def _start_Portal(self, a):
        obj = self._top("layout_object")
        if obj is not None and self.parent_tag() == "LayoutObject":
            obj["_portal_to"] = False

    def _end_Portal(self):
        obj = self._top("layout_object")
        if (obj is not None and self.parent_tag() == "LayoutObject"
                and obj.get("_portal_to") is False):
            # A portal whose TO is gone carries no reference element
            self.emit_ref("portal", "table_occurrence", name=None, raw=_parse.UNKNOWN)

    # ---- buttons and triggers -------------------------------------------------
    # A button's action: a ScriptReference (Perform Script), a Step (any
    # other step, through the step code with the object as source) or, for
    # a Perform Script whose script is gone, its Options and no reference.
    # A Perform Script action has no DDRREF StepText: its step text is
    # written here as the DDR writes the button's StepText,
    # Perform Script [ “Name”; Parameter: <calc> ] (“<unknown>” for a
    # deleted script).
    def _start_action(self, a):
        if (self.parent_tag() in ("Button", "GroupedButton")
                and self._top("layout_object") is not None):
            self._action = {"options": False, "script": False, "step": False,
                            "name": None, "param": None}

    def _end_action(self):
        act, self._action = self._action, None
        if act is None or act["step"] or not (act["script"] or act["options"]):
            return
        if not act["script"]:
            self.emit_ref("perform_script", "script", name=None, raw=_parse.UNKNOWN)
        src = self.current_source()
        if src is not None:
            text = f"Perform Script [ “{act['name'] or _parse.UNKNOWN}”"
            if act["param"]:
                text += "; Parameter: " + act["param"]
            src["_step_text"] = text + " ]"

    def _start_ScriptTrigger(self, a):
        if self.parent_tag() == "ScriptTriggers":
            self._trigger = {"event": a.get("action"), "script": False}

    def _end_ScriptTrigger(self):
        trig, self._trigger = self._trigger, None
        if trig is not None and not trig["script"]:
            # A trigger whose script is gone carries no reference element
            self.emit_ref("trigger", "script", name=None, raw=_parse.UNKNOWN,
                          trigger_event=trig["event"])

    # ---- base tables and fields -------------------------------------------
    def _start_BaseTable(self, a):
        if self.parent_tag() == "BaseTableCatalog":
            ent = self.new_entity("base_table", a, records=None)
            self._bt_by_id[a.get("id")] = (ent["entity_id"], a.get("name"))

    def _order_error(self):
        raise ValueError(
            f"{self.path}: <{self.tagstack[-1]}> names an entry this export has "
            f"not written yet (at {'/'.join(self.tagstack)}). FileMaker writes "
            "each catalog before the catalog that refers to it, so the file was "
            "edited or is damaged. Re-export it from FileMaker.")

    def _start_BaseTableReference(self, a):
        parent = self.parent_tag()
        if parent == "FieldCatalog":
            self._cur_bt = self._bt_by_id.get(a.get("id"))
            if self._cur_bt is None:
                self._order_error()
        elif parent == "BaseTableSourceReference":
            to = self._top("table_occurrence")
            if to is not None:
                to["base_table"] = a.get("name")

    # ---- table occurrences and relationships --------------------------
    def _start_TableOccurrence(self, a):
        if self.parent_tag() == "TableOccurrenceCatalog":
            ent = self.new_entity("table_occurrence", a, base_table=None)
            ent["_external"] = a.get("type") == "External"

    def _start_DataSourceReference(self, a):
        # Filled external TO: the data source and the base table in it
        to = self._top("table_occurrence")
        if to is not None and self.parent_tag() == "BaseTableSourceReference":
            to["ext_file"] = a.get("name") or _parse.UNKNOWN
            to["_ds_seen"] = True
        elif self._step is not None and self.parent_tag() == "List":
            # Filled cross-file Perform Script: the parse.py FileReference
            # rule; the script reference that follows gets target_file
            self._step_seen.add("ds")
            src = self.current_source()
            if src is not None:
                src["_ext_file"] = a.get("name") or _parse.UNKNOWN

    def _close_table_occurrence(self, ent):
        # Empty external TO (the FMDeveloperTool route): no source, no table;
        # its field references stay unresolved, never mis-linked
        if ent.get("_external") and not ent.get("_ds_seen"):
            ent["ext_file"] = None
            ent["base_table"] = None
            ent["extra_json"] = _parse._json({"external": True})
            self._ext_empty += 1
            self._ext_empty_tos.add(ent["name"])

    def _start_Relationship(self, a):
        if self.parent_tag() == "RelationshipCatalog":
            self.new_entity("relationship", a, name=None)

    def _start_LeftTable(self, a):
        if self._top("relationship") is not None:
            self._side = (self.tagstack[-1], a)

    _start_RightTable = _start_LeftTable

    def _end_LeftTable(self):
        self._side = None

    _end_RightTable = _end_LeftTable

    def _start_JoinPredicate(self, a):
        rel = self._top("relationship")
        if rel is not None:
            op = a.get("type") or "Equal"
            rel.setdefault("_ops", []).append(JOIN_OPS.get(op, op))

    # ---- custom functions and value lists ------------------------------
    def _start_CustomFunction(self, a):
        # CF folders are flattened: the DDR has no CF folder kind
        if (self.has_ancestor("CustomFunctionsCatalog")
                and a.get("isFolder") not in ("True", "Marker")):
            ent = self.new_entity("custom_function", a)
            ent["_params"] = []

    def _start_Parameter(self, a):
        if (self._step is not None and self._step["name"] == "Show Custom Dialog"
                and self.parent_tag() == "ParameterValues"
                and (a.get("type") or "").startswith("Button") and a.get("value")):
            # A button label is an attribute here; the DDR writes it as a
            # quoted calculation after the title and the message
            src = self.current_source()
            if src is not None:
                label = a["value"].replace("\\", "\\\\").replace('"', '\\"')
                src.setdefault("_calc_parts", []).append(f'"{label}"')
            return
        cf = self._top("custom_function")
        if (cf is not None and self.parent_tag() == "ObjectList"
                and self._anc(3) == "CustomFunction"):
            cf["_params"].append(a.get("name") or "")

    def _close_custom_function(self, ent):
        # The DDR's string form: parameters="a;b" (empty for none),
        # functionArity="2", as the DDR writes them
        ps = ent.get("_params") or []
        ent["extra_json"] = _parse._json({"parameters": ";".join(ps),
                                   "arity": str(len(ps))})

    def _start_ValueList(self, a):
        if self.parent_tag() == "ValueListCatalog":
            self.new_entity("value_list", a)

    def _start_Source(self, a):
        vl = self._top("value_list")
        if vl is not None and self.parent_tag() == "ValueList":
            v = a.get("value")
            # The 1.13.0 optional-slot rule reads _vl_source != "Field"
            vl["_vl_source"] = "Field" if v == "FromField" else v

    def _start_ShowRelated(self, a):
        vl = self._top("value_list")
        if vl is not None:
            vl["_vl_showrel"] = a.get("value")

    def _start_PrimaryField(self, a):
        vl = self._top("value_list")
        if vl is not None and self.parent_tag() == "Field":
            vl.setdefault("_vl_fieldopts", {})[self.tagstack[-1]] = {
                "show": a.get("show") == "True", "sort": a.get("sort") == "True"}

    _start_SecondaryField = _start_PrimaryField

    # ---- security, menus, themes, data sources -------------------------
    def _in_catalog(self, catalog):
        """Catalog/ObjectList/<this element>."""
        st = self.tagstack
        return len(st) >= 3 and st[-2] == "ObjectList" and st[-3] == catalog

    def _start_PrivilegeSet(self, a):
        if self._in_catalog("PrivilegeSetsCatalog"):
            self.new_entity("privilege_set", a)

    def _start_ExtendedPrivilege(self, a):
        if self._in_catalog("ExtendedPrivilegesCatalog"):
            self.new_entity("extended_privilege", a)

    def _start_Account(self, a):
        # The name is Authentication/AccountName text; nothing else under
        # Authentication is read (PasswordEncrypted is on SKIP)
        if self._in_catalog("AccountsCatalog"):
            self.new_entity("account", {"id": a.get("id")})

    ACCOUNT_NAME_TAGS = ("AccountName",)   # the credentials test widens it

    def _start_CustomMenu(self, a):
        if self.parent_tag() == "CustomMenuCatalog":
            self.new_entity("custom_menu", a)

    def _start_CustomMenuSet(self, a):
        if self._in_catalog("CustomMenuSetCatalog"):
            self.new_entity("custom_menu_set", a)

    def _start_Theme(self, a):
        # The DDR names a theme by its display name (Theme@Display), not the
        # internal com.filemaker.theme.* name, and spells the id in hex
        if self.parent_tag() == "ThemeCatalog":
            self.new_entity("theme", {"id": _theme_id(a.get("id")),
                                      "name": a.get("Display") or a.get("name")})

    def _start_ExternalDataSource(self, a):
        if self.parent_tag() == "ExternalDataSourceCatalog":
            ent = self.new_entity("external_data_source", a)
            ent["_ds_type"] = a.get("type")

    def _start_UniversalPathList(self, a):
        st = self.tagstack
        if (len(st) >= 3 and st[-2] == "File" and st[-3] == "ExternalDataSource"
                and self._top("external_data_source") is not None):
            self._capture("UniversalPathList")

    def _close_external_data_source(self, ent):
        # FileMaker sources: {file, link, file_name}; link is the DDR's
        # sibling file name, which a SaXML does not carry. Other types
        # (ODBC) carry no file key, as the DDR's OdbcDataSource.
        if ent.get("_ds_type") == "FileMaker":
            ent["extra_json"] = _parse._json({"file": True, "link": None,
                                       "file_name": ent.get("_file_name")})

    def _end_FieldCatalog(self):
        self._cur_bt = None

    def _start_Field(self, a):
        st = self.tagstack
        if len(st) >= 3 and st[-2] == "ObjectList" and st[-3] == "FieldCatalog":
            if self._cur_bt is None:
                self._order_error()
            bt_eid, bt_name = self._cur_bt
            self.new_entity(
                "field",
                {"id": a.get("id"), "name": a.get("name"),
                 "dataType": DATA_TYPES.get(a.get("datatype"), a.get("datatype")),
                 "fieldType": a.get("fieldtype")},
                base_table=bt_name, parent_id=bt_eid)

    def _start_Storage(self, a):
        fld = self._top("field")
        if fld is not None and self.parent_tag() == "Field":
            fld["_storage"] = a

    def _start_AutoEnter(self, a):
        fld = self._top("field")
        if fld is None or self.parent_tag() != "Field":
            return
        t = a.get("type")
        ae = {}
        if "prohibitModification" in a:
            ae["allowEditing"] = ("False" if a["prohibitModification"] == "True"
                                  else "True")
        # The DDR writes the two flags for an auto-enter calculation, a
        # calculation field (no type) or when one is on; Pro 26.1 writes
        # them on every field
        if t in (None, "Calculated") or "True" in (a.get("overwriteExisting"),
                                                  a.get("alwaysEvaluate")):
            if "overwriteExisting" in a:
                ae["overwriteExistingValue"] = a["overwriteExisting"]
            if "alwaysEvaluate" in a:
                ae["alwaysEvaluate"] = a["alwaysEvaluate"]
        if t == "ConstantData":
            ae["constant"] = "True"
        if t in AE_VALUE:
            ae["value"] = AE_VALUE[t]
        fld["_ae_attrs"] = ae

    def _end_AutoEnter(self):
        self._in_dead_ae = False

    def _start_Calculated(self, a):
        fld = self._top("field")
        if fld is not None and self.parent_tag() == "AutoEnter":
            fld.setdefault("_ae_attrs", {})["calculation"] = a.get("enable")
            # calc text present while the option is off = dead residue
            self._in_dead_ae = a.get("enable") == "False"

    def _start_Looked_up(self, a):
        fld = self._top("field")
        if fld is not None and self.parent_tag() == "AutoEnter":
            fld.setdefault("_ae_attrs", {})["lookup"] = a.get("enable")

    def _start_SerialNumber(self, a):
        fld = self._top("field")
        if fld is not None and self.parent_tag() == "AutoEnter":
            fld["_ae_serial"] = {k: a[s] for k, s in
                                 (("increment", "increment"),
                                  ("nextValue", "nextvalue"),
                                  ("generate", "generate")) if s in a}

    def _start_ValueListReference(self, a):
        # A field's validation "member of value list"
        if (self.parent_tag() == "Validation" and self._top("field") is not None
                and a.get("name")):
            self.emit_ref("value_list_source", "value_list", fm_id=a.get("id"),
                          name=a["name"], raw=a["name"])
        elif (self._step is not None and self.parent_tag() == "Sort"
              and a.get("name")):
            # Sort Records by a value list's order
            self._step_ref("value_list_source", "value_list", fm_id=a.get("id"),
                           name=a["name"], raw=a["name"])
        elif (self.parent_tag() in ("Display", "Sort")
              and self._top("layout_object") is not None and a.get("name")):
            # A field object's value list; a portal sorted by a value list
            self.emit_ref("value_list_source", "value_list", fm_id=a.get("id"),
                          name=a["name"], raw=a["name"])

    # ---- FieldReference: the TO arrives as a child, so decide at the end --
    def _start_FieldReference(self, a):
        self._fref = {"id": a.get("id"), "name": a.get("name") or None,
                      "to": None, "slot": self.parent_tag(),
                      "rep": a.get("repetition")}

    def _start_Map(self, a):
        self._map_kind = a.get("kind")

    def _end_FieldReference(self):
        fr, self._fref = self._fref, None
        if fr is None:
            return
        slot = fr["slot"]
        st = self.tagstack
        anc3 = self._anc(3)
        if (slot == "Field" and anc3 == "LayoutObject"
                and self._top("layout_object") is not None):
            lay = self._layout()
            if lay is not None:
                lay.setdefault("_form_fields", set()).add((fr["id"], fr.get("to_id")))
            self._field_object(fr)
        elif (slot == "TableViewLayoutObject" and self._tvcol is not None
              and self._top("layout") is not None):
            self._table_view_column(fr)
        elif (self._step is None and slot == "PrimaryField" and anc3 == "Sort"
              and self.has_ancestor("Portal")
              and self._top("layout_object") is not None):
            # Portal sort: the portal object is the source
            if not fr["name"] and fr["to"] in self._ext_empty_tos:
                self.emit_ref("sort", "field", fm_id=fr["id"], name=None,
                              to_name=fr["to"], raw=None)
            else:
                self._field_ref("sort", slot, fr, fr["to"])
        elif self._step is not None and slot == "Parameter":
            self._step_field("step_target", fr)      # Set Field, Go to Field, ...
        elif self._step is not None and (
                slot == "find" or (slot == "Map" and self._map_kind != "1")
                or (slot == "Field" and anc3 == "Order")):
            # v1.15.1: a stored find request's field (FindRequestSet), an
            # Import Records mapped field (kind 1 = "not import to": no
            # usage) and Export Records' field order, as the DDR's Criteria,
            # TargetFields and ExportEntries. An Import map with id 0 and no
            # name emits nothing (the DDR's optional slot); everything else
            # is the step field rule, markers and external files included.
            # The export writes no Restore for these steps, so a stale
            # request counts here (COVERAGE.md); no repetition in raw, as
            # the DDR writes none in a criterion
            if not (slot == "Map" and not fr["name"] and fr["id"] == "0"):
                ctx = ("find_request" if slot == "find"
                       else "import_field" if slot == "Map" else "export_field")
                self._step_field(ctx, fr)
        elif (self._step is not None and slot == "PrimaryField"
              and anc3 == "Sort"):
            self._step_field("sort", fr)             # Sort Records
        elif (slot == "Definition" and anc3 == "Part"
              and self._top("layout") is not None):
            # A sub-summary part's break field, source the layout: a
            # SaXML-only reference (the DDR writes no Part element)
            if not fr["name"] and fr["to"] in self._ext_empty_tos:
                self.emit_ref("part_break", "field", fm_id=fr["id"], name=None,
                              to_name=fr["to"], raw=None)
            else:
                self._field_ref("part_break", slot, fr, fr["to"])
        elif slot == "Looked_up":
            self._lookup(fr)
        elif slot in ("LeftField", "RightField"):
            rel = self._top("relationship")
            if rel is not None:
                # A predicate field belongs to its side's TO
                side = "LeftTable" if slot == "LeftField" else "RightTable"
                tbl = fr["to"] or rel.get("_side_to", {}).get(side)
                if not fr["name"] and tbl in self._ext_empty_tos:
                    # A field in another file through an empty external TO:
                    # unresolved, outside v_dangling;
                    # an empty name with no TO keeps the <unknown> rule
                    self.emit_ref("join_predicate", "field", fm_id=fr["id"],
                                  name=None, to_name=tbl, raw=None)
                    q = tbl + "::"
                else:
                    q = self._field_ref("join_predicate", slot, fr, tbl)
                rel.setdefault("_pred_fields", []).append((slot, q))
        elif (slot in ("PrimaryField", "SecondaryField") and anc3 == "Field"
              and self._top("value_list") is not None):
            vl = self._top("value_list")
            q = self._field_ref("value_list_field", slot, fr, fr["to"])
            # which field each slot displays
            vl.setdefault("_vl_fields", {})[slot] = q
        elif (slot == "PrimaryField" and len(st) >= 4 and anc3 == "Sort"
              and self._side is not None):
            rel = self._top("relationship")
            if rel is not None:
                # Relationship sort: the field of that side's TO
                tbl = fr["to"] or rel.get("_side_to", {}).get(self._side[0])
                self._field_ref("sort", slot, fr, tbl)

    def _field_ref(self, ctx, slot, fr, tbl):
        """Emit a field reference as the DDR's <Field> rule does
        (parse.py, Field under LeftField ... SecondaryField), including the
        v1.13.0 empty-name rules; returns the qualified name it recorded."""
        nm = fr["name"]
        if nm:
            self.emit_ref(ctx, "field", fm_id=fr["id"], name=nm, to_name=tbl,
                          raw=(tbl or "") + "::" + nm)
        elif ctx == "value_list_field" and (
                slot == "SecondaryField"
                or (self._top("value_list") or {}).get("_vl_source") != "Field"):
            pass   # optional slots: an unset second field, a non-field list
        elif tbl:
            self.emit_ref(ctx, "field", fm_id=fr["id"], name=None, to_name=tbl,
                          raw=tbl + "::" + _parse.UNKNOWN)
        else:
            self.emit_ref(ctx, "field", fm_id=fr["id"], name=None, to_name=None,
                          raw=_parse.UNKNOWN)
        return (tbl or "") + "::" + (nm or "")

    def _lookup(self, fr):
        fld = self._top("field")
        if fld is None or not fr["name"]:
            return
        to, nm = fr["to"] or "", fr["name"]
        dead = (fld.get("_ae_attrs") or {}).get("lookup") != "True"
        fld["_lookup_src"] = to + "::" + nm
        self.emit_ref("lookup", "field", fm_id=fr["id"], name=nm,
                      to_name=fr["to"], raw=to + "::" + nm, force_disabled=dead)

    def _start_TableOccurrenceReference(self, a):
        parent = self.parent_tag()
        if parent == "FieldReference":
            if self._fref is not None:
                # In a step or a field object a deleted TO is a marker
                # element: no TO
                marker = ((self._step is not None
                           or self._fref["slot"] in ("Field", "TableViewLayoutObject"))
                          and _marker_to(a))
                self._fref["to"] = None if marker else a.get("name") or None
                self._fref["to_id"] = a.get("id")
        elif parent == "Layout" and self._top("layout") is not None:
            # The layout's context TO; a deleted one is a marker element
            if _marker_to(a):
                self.emit_ref("layout_context", "table_occurrence", name=None,
                              raw=_parse.UNKNOWN)
            else:
                nm = a.get("name") or None
                self.emit_ref("layout_context", "table_occurrence",
                              fm_id=a.get("id"), name=nm, raw=nm or _parse.UNKNOWN)
        elif parent == "Portal" and self._top("layout_object") is not None:
            obj = self._top("layout_object")
            obj["_portal_to"] = True
            nm = None if _marker_to(a) else a.get("name") or None
            self.emit_ref("portal", "table_occurrence",
                          fm_id=a.get("id") if nm else None, name=nm,
                          raw=nm or _parse.UNKNOWN)
        elif self._step is not None and (
                parent == "Parameter"
                or (parent == "Target" and self._anc(3) == "ImportField")):
            self._step_table(a)
        elif parent == "ShowRelated":
            # The "starting from" TO of a related-values list
            if self._top("value_list") is not None:
                nm = a.get("name") or None
                self.emit_ref("value_list_context", "table_occurrence",
                              fm_id=a.get("id"), name=nm, raw=nm or _parse.UNKNOWN)
        elif parent in ("LeftTable", "RightTable"):
            rel = self._top("relationship")
            if rel is not None and self._side is not None:
                nm = a.get("name")
                side = self._side[1]
                self.emit_ref("join_predicate", "table_occurrence", name=nm, raw=nm)
                rel.setdefault("_ends", []).append(nm)
                rel.setdefault("_side_to", {})[parent] = nm
                # cascade options ride on the side wrapper
                rel.setdefault("_sides", []).append({
                    "side": "left" if parent == "LeftTable" else "right",
                    "to": nm,
                    "cascade_create": side.get("cascadeCreate") == "True",
                    "cascade_delete": side.get("cascadeDelete") == "True",
                })
