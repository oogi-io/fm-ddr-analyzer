"""Test suite for fm-ddr-analyzer.

Covers:
- structural parse of the micro fixture (counts, groups, parents)
- resolution semantics (duplicate field names via TO, unresolved externals)
- health views (v_unused_fields, v_orphan_scripts)
- UTF-16-LE handling (real DDRs are UTF-16-LE; fixture is committed as UTF-8)
- report HTML embedding safety (calc text containing </script>)
- snapshot regression (full normalized entity+ref dump)
- Python <-> JS parser parity, edge by edge (requires node; skipped if absent)

Run from the repo root:  python3 -m unittest discover tests -v
Regenerate the snapshot:  UPDATE_SNAPSHOT=1 python3 -m unittest discover tests
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fm_ddr.parse import build  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "micro_ddr.xml")
FIXTURE_B = os.path.join(ROOT, "tests", "fixtures", "micro_ddr_b.xml")
FIXTURE_C = os.path.join(ROOT, "tests", "fixtures", "micro_ddr_c.xml")
FIXTURE_D = os.path.join(ROOT, "tests", "fixtures", "micro_ddr_d.xml")
SNAPSHOT = os.path.join(ROOT, "tests", "expected_snapshot.json")
JS_RUNNER = os.path.join(ROOT, "tests", "parity", "run_js.mjs")
WEB_APP = os.path.join(ROOT, "fm_ddr", "web", "index.html")


# ---------------------------------------------------------------------------
# Normalization: convert either parser's output into one comparable shape.
# Entities are keyed (kind, name, parent-key, ordinal) so steps with equal
# names stay distinct but ids (which differ between parsers) drop out.
# ---------------------------------------------------------------------------

def normalize(entities, refs):
    """entities: [{id,kind,name,parent_id,base_table,grp}], refs: [{source_id,
    context,target_kind,target_raw,target_id}] -> canonical dict."""
    by_id = {e["id"]: e for e in entities}

    keys = {}
    ordinal_counter = {}
    for e in sorted(entities, key=lambda x: x["id"]):  # id order == document order
        parent_key = keys.get(e["parent_id"]) if e["parent_id"] is not None else None
        base = (e["kind"], e["name"] or "", parent_key or "")
        ordinal = ordinal_counter.get(base, 0)
        ordinal_counter[base] = ordinal + 1
        keys[e["id"]] = f'{e["kind"]}|{e["name"] or ""}|{parent_key or ""}|{ordinal}'

    ent_rows = sorted(
        [keys[e["id"]], e["base_table"] or "", e["grp"] or "",
         # v1.9.0 columns — guard JS/Python parity on storage + auto-enter
         "" if e.get("stored") is None else str(e["stored"]),
         e.get("indexed") or "",
         "" if e.get("is_global") is None else str(e["is_global"]),
         _canon_ae(e.get("auto_enter")),
         _canon_v10(e.get("v10")),
         # v1.13.0: the external data source a TO points at ('<unknown>' when
         # the source is gone) must agree between the parsers
         e.get("ext_file") or ""]
        for e in entities
    )
    ref_rows = sorted(
        [
            keys.get(r["source_id"]) or "",
            r["context"],
            r["target_kind"],
            r["target_raw"] or "",
            keys.get(r["target_id"]) or "" if r["target_id"] is not None else "",
            r["ambiguous"],
            r.get("disabled", 0),
            r.get("trigger_event") or "",
            # v1.13.0: the FileReference marker ('<unknown>' when the file is
            # gone): a sentinel that changes no resolution still fails parity
            r.get("target_file") or "",
        ]
        for r in refs
    )
    return {"entities": ent_rows, "refs": ref_rows}


def _canon_ae(ae):
    """Auto-enter JSON is emitted key-order-independently by the two parsers;
    canonicalize to a sorted-key string so parity compares content, not order."""
    if not ae:
        return ""
    d = ae if isinstance(ae, dict) else json.loads(ae)
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


# v1.10.0 extra capture (relationship sides/predicates, value-list defs,
# layout-object text) — the keys both parsers must agree on.
V10_KEYS = ("sides", "predicates", "source", "show_related", "primary",
            "secondary", "custom_values", "text")
# v1.13.0: external data source entities carry {file, link}
# v1.14.0: scripts, layouts and their folders carry the raw menu flags
EXTRA_KEYS = V10_KEYS + ("file", "link", "in_menu", "run_full_access")


def _canon_v10(v10):
    if not v10:
        return ""
    d = {k: v for k, v in v10.items() if k in EXTRA_KEYS and v is not None}
    return json.dumps(d, sort_keys=True, ensure_ascii=False) if d else ""


def read_sqlite(db_path):
    conn = sqlite3.connect(db_path)
    entities = [
        {"id": i, "kind": k, "name": n, "parent_id": p, "base_table": bt, "grp": g,
         "stored": st, "indexed": ix, "is_global": ig, "auto_enter": ae,
         "ext_file": ef,
         # every kind: _canon_v10 keeps only the keys both parsers must agree on
         "v10": (json.loads(ex) if ex else None)}
        for i, k, n, p, bt, g, st, ix, ig, ae, ef, ex in conn.execute(
            "SELECT entity_id,kind,name,parent_entity_id,base_table,grp,"
            "stored,indexed,is_global,auto_enter,ext_file,extra_json FROM entities")
    ]
    refs = [
        {"source_id": s, "context": c, "target_kind": tk, "target_raw": tr,
         "target_id": t, "ambiguous": a or 0, "disabled": d or 0, "trigger_event": ev,
         "target_file": tf}
        for s, c, tk, tr, t, a, d, ev, tf in conn.execute(
            "SELECT source_entity_id,context,target_kind,target_raw,target_entity_id,"
            "ambiguous,disabled,trigger_event,target_file FROM refs")
    ]
    conn.close()
    return entities, refs


def read_js(js_json):
    entities = [
        {"id": e["id"], "kind": e["k"], "name": e["n"], "parent_id": e["p"],
         "base_table": e["bt"], "grp": e["g"],
         "stored": e.get("sto"), "indexed": e.get("idx"),
         "is_global": e.get("glb"), "auto_enter": e.get("ae"),
         "ext_file": e.get("ef"),
         "v10": (dict(e["ex"] or {}, **({"text": e["tx"]} if e.get("tx") else {}))
                 if (e.get("ex") or e.get("tx")) else None)}
        for e in js_json["entities"]
    ]
    refs = [
        {"source_id": r["s"], "context": r["c"], "target_kind": r["tk"],
         "target_raw": r["tr"], "target_id": r["t"], "ambiguous": r.get("a", 0),
         "disabled": r.get("dis", 0), "trigger_event": r.get("ev"),
         "target_file": r.get("tf")}
        for r in js_json["edges"]
    ]
    return entities, refs


def build_fixture_db(tmpdir, src=FIXTURE):
    db = os.path.join(tmpdir, "fixture.db")
    build(src, db, label="fixture")
    return db


def utf16_copy(tmpdir):
    """Real DDRs are UTF-16-LE with BOM; produce one from the UTF-8 fixture."""
    with open(FIXTURE, encoding="utf-8") as f:
        text = f.read()
    out = os.path.join(tmpdir, "fixture_utf16.xml")
    with open(out, "wb") as f:
        f.write(b"\xff\xfe")
        f.write(text.encode("utf-16-le"))
    return out


# ---------------------------------------------------------------------------

class TestParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_test_")
        cls.db = build_fixture_db(cls.tmp)
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def counts(self):
        return dict(self.conn.execute(
            "SELECT kind, COUNT(*) FROM entities GROUP BY kind"))

    def test_entity_counts(self):
        c = self.counts()
        self.assertEqual(c["base_table"], 2)
        self.assertEqual(c["field"], 13)
        self.assertEqual(c["table_occurrence"], 4)  # incl. external ext_LOG
        self.assertEqual(c["relationship"], 1)
        # 2 layout DEFINITIONS; the go-to-layout button on the layout is a ref
        self.assertEqual(c["layout"], 2)
        self.assertEqual(c["layout_group"], 1)
        self.assertEqual(c["script"], 6)  # 3 Main + 1 loose + Chain Root + Record Ops Torture
        self.assertEqual(c["script_group"], 1)
        self.assertEqual(c["script_step"], 24)  # 9 + 14 torture + 1 Chain Root
        self.assertEqual(c["custom_function"], 1)
        self.assertEqual(c["value_list"], 2)

    def test_menu_separators_are_not_entities(self):
        # Scripts and layouts named "-" are FileMaker menu-divider lines, not
        # real entities; they must not appear in the index or inflate counts.
        n = self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE kind IN ('script','layout') AND name='-'"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_top_level_script_captured_and_resolved(self):
        # A script kept OUTSIDE any folder (directly under ScriptCatalog) must be
        # captured as a definition, and an inbound Perform Script must resolve to
        # it — the regression for the "ungrouped scripts vanish" bug.
        row = self.conn.execute(
            "SELECT entity_id, grp FROM entities WHERE kind='script' AND name='Loose Script'"
        ).fetchone()
        self.assertIsNotNone(row, "top-level script not captured as a definition")
        loose_id, grp = row
        self.assertIn(grp, (None, ""), "loose script should have no group")
        resolved = self.conn.execute(
            "SELECT COUNT(*) FROM refs WHERE context='perform_script' "
            "AND target_entity_id=?", (loose_id,)).fetchone()[0]
        self.assertEqual(resolved, 1, "call to the loose script did not resolve")

    def test_duplicate_field_name_resolves_through_TO(self):
        # CTC::zkp and ctc_INV::zkp are different fields (bt CTC vs INV)
        rows = self.conn.execute("""
            SELECT r.target_raw, f.base_table FROM refs r
            JOIN entities f ON f.entity_id = r.target_entity_id
            WHERE r.context='join_predicate' AND r.target_kind='field'
        """).fetchall()
        by_raw = dict(rows)
        self.assertEqual(by_raw.get("CTC::zkp"), "CTC")
        self.assertEqual(by_raw.get("ctc_INV::zkp"), "INV")

    def test_external_script_unresolved_internal_resolved(self):
        rows = dict(self.conn.execute("""
            SELECT target_raw, target_entity_id FROM refs
            WHERE context IN ('perform_script','trigger') AND target_kind='script'
        """).fetchall())
        self.assertIsNone(rows["Ghost Script"])       # external -> stays NULL
        self.assertIsNotNone(rows["Helper Script"])   # internal -> resolves

    def test_external_call_never_misresolves_locally(self):
        # 'Remote Helper' carries FileReference "MicroB" and Script id=2 —
        # the SAME fm_id as the local Helper Script. Without the target_file
        # guard this would silently link to the wrong (local) script.
        row = self.conn.execute("""
            SELECT target_file, target_entity_id FROM refs
            WHERE target_raw='Remote Helper'""").fetchone()
        self.assertEqual(row[0], "MicroB")
        self.assertIsNone(row[1])  # MicroB not in this single-file build

    def test_go_to_layout_resolves_both_sources(self):
        # one from a script step, one from an on-layout button
        rows = self.conn.execute("""
            SELECT source_kind, resolved FROM v_usage
            WHERE context='go_to_layout' AND target_name='Invoices'
        """).fetchall()
        self.assertEqual(len(rows), 2)
        # the on-layout button ref attributes to the button OBJECT (v1.3.0)
        self.assertEqual({r[0] for r in rows}, {"script_step", "layout_object"})
        self.assertTrue(all(r[1] for r in rows))

    def test_layout_objects_captured(self):
        # the named button object exists, parented under its layout
        row = self.conn.execute("""
            SELECT o.entity_id, json_extract(o.extra_json,'$.object_type'),
                   json_extract(o.extra_json,'$.hide_calc'),
                   json_extract(o.extra_json,'$.step_text'), l.name
            FROM entities o JOIN entities l ON l.entity_id = o.parent_entity_id
            WHERE o.kind='layout_object' AND o.name='btnGo'""").fetchone()
        self.assertIsNotNone(row)
        _, otype, hide, step_text, layout_name = row
        self.assertEqual(otype, "Button")
        self.assertIn("HideMe_g", hide)
        self.assertIn("Go to Layout", step_text)
        self.assertEqual(layout_name, "Contacts")
        # object name + hide calc are searchable at BOTH levels: object row
        # and the aggregated layout body (the "read a full layout body" recipe)
        body = self.conn.execute("""
            SELECT body FROM text_index WHERE kind='layout' AND name='Contacts'
        """).fetchone()[0]
        self.assertIn("btnGo", body)
        self.assertIn("HideMe_g", body)

    def test_list_command(self):
        import subprocess, sys as _sys
        r = subprocess.run([_sys.executable, "-m", "fm_ddr.cli", "list",
                            os.path.dirname(self.db)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(os.path.basename(self.db), r.stdout)
        from fm_ddr import __version__
        self.assertIn(__version__, r.stdout)   # fresh build shows current parser
        self.assertIn("ok", r.stdout)

    def test_investigate_chain_rollup(self):
        import subprocess, sys as _sys
        r = subprocess.run([_sys.executable, "-m", "fm_ddr.cli", "investigate",
                            self.db, "Chain Root", "--no-body"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        # default-on: the compact rollup appears without any flag
        self.assertIn("## Chain (recursive callees): 2 scripts (1 direct", out)
        # torture script's ops aggregated: the named-table Truncate is confident
        self.assertRegex(out, r"confident\s+\[99\] Record Ops Torture — Truncate Table → CONTACT")
        # find-mode/disabled ops are tagged, not counted
        self.assertIn("tagged (find-mode requests / disabled)", out)
        # --chain expands to the full census
        r2 = subprocess.run([_sys.executable, "-m", "fm_ddr.cli", "investigate",
                             self.db, "Chain Root", "--no-body", "--chain"],
                            capture_output=True, text=True)
        self.assertIn("### CHAIN DELETERS", r2.stdout)
        self.assertIn("portal row: layout context is NOT the target table", r2.stdout)

    def test_disabled_steps_flagged(self):
        # the disabled Perform Script emits a ref flagged disabled=1
        row = self.conn.execute("""
            SELECT r.disabled FROM refs r
            JOIN entities st ON st.entity_id = r.source_entity_id
            JOIN entities scr ON scr.entity_id = st.parent_entity_id
            WHERE scr.name='Record Ops Torture' AND r.context='perform_script'
              AND r.target_name='Helper Script'""").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)
        # v_usage (live) excludes it; v_usage_disabled carries it
        live = self.conn.execute("""SELECT COUNT(*) FROM v_usage
            WHERE context='perform_script' AND target_name='Helper Script'
              AND source_parent_name='Record Ops Torture'""").fetchone()[0]
        dead = self.conn.execute("""SELECT COUNT(*) FROM v_usage_disabled
            WHERE context='perform_script' AND target_name='Helper Script'
              AND source_parent_name='Record Ops Torture'""").fetchone()[0]
        self.assertEqual((live, dead), (0, 1))
        # the step entity itself carries the flag
        d = self.conn.execute("""SELECT json_extract(s.extra_json,'$.disabled')
            FROM entities s JOIN entities scr ON scr.entity_id=s.parent_entity_id
            WHERE scr.name='Record Ops Torture' AND s.step_type='Perform Script'""").fetchone()[0]
        self.assertEqual(d, 1)

    def test_mutations_command(self):
        import subprocess, sys as _sys
        r = subprocess.run([_sys.executable, "-m", "fm_ddr.cli", "mutations",
                            self.db, "--like", "Torture"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        # 1) browse-mode New Record on clean context -> likely CONTACT
        self.assertRegex(out, r"likely\s+\[99\] Record Ops Torture\s+New Record/Request\s+CONTACT")
        # 2) find-mode New Record shown, tagged, not counted as creator
        self.assertIn("find request (not a mutation)", out)
        self.assertIn("## CREATORS (1)", out)
        # 3) disabled delete shown, tagged
        self.assertIn("disabled", out)
        # 4) conditional Go to Layout demotes to check
        self.assertIn("conditional context", out)
        # 5) portal row never gets a layout-based likely
        self.assertIn("portal row: layout context is NOT the target table", out)
        # 6) truncate with named table is the only confident
        self.assertRegex(out, r"confident\s+\[99\] Record Ops Torture\s+Truncate Table\s+CONTACT")
        # 7) truncate WITHOUT a table is not confident (1 confident ROW only)
        import re as _re
        self.assertEqual(len(_re.findall(r"^confident\b", out, _re.M)), 1)

    def test_step_target_captured(self):
        # Set Field's write target is a direct <Field> child of <Step> in real
        # DDRs (the agent-discovered gap: it was missed entirely before)
        rows = self.conn.execute('''
            SELECT st.step_type, te.base_table, te.name FROM refs r
            JOIN entities st ON st.entity_id=r.source_entity_id
            JOIN entities te ON te.entity_id=r.target_entity_id
            WHERE r.context='step_target' ''').fetchall()
        self.assertEqual(rows, [("Set Field", "CTC", "email")])

    def test_step_seq_is_ordinal(self):
        rows = self.conn.execute('''
            SELECT s.seq FROM entities s
            JOIN entities p ON p.entity_id=s.parent_entity_id
            WHERE s.kind='script_step' AND p.name='Main Script'
            ORDER BY s.entity_id''').fetchall()
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4, 5, 6])

    def test_calc_field_refs_both_chunk_forms(self):
        # element form (<Field> in Chunk) and text form ("CTC::email")
        raws = {r[0] for r in self.conn.execute(
            "SELECT target_raw FROM refs WHERE context='calc' AND target_kind='field'")}
        self.assertIn("CTC::zkp", raws)
        self.assertIn("CTC::email", raws)

    def test_custom_function_refs(self):
        n = self.conn.execute("""
            SELECT COUNT(*) FROM refs r JOIN entities e ON e.entity_id=r.target_entity_id
            WHERE r.target_kind='custom_function' AND e.name='MakeTitle'
        """).fetchone()[0]
        self.assertEqual(n, 2)  # field calc + Helper Script step

    def test_unused_fields_documents_executesql_blind_spot(self):
        unused = {r[0] for r in self.conn.execute("SELECT name FROM v_unused_fields")}
        # sql_only_field IS used (inside ExecuteSQL) but structurally invisible.
        # This assert DOCUMENTS the blind spot; see COVERAGE.md.
        self.assertIn("sql_only_field", unused)
        self.assertNotIn("email", unused)
        self.assertNotIn("zkp", unused)
        # amount is used via VL field source + relationship sort (Phase B capture)
        self.assertNotIn("amount", unused)

    def test_value_list_field_source_captured(self):
        rows = self.conn.execute("""
            SELECT source_name, target_raw, resolved FROM v_usage
            WHERE context='value_list_field'""").fetchall()
        self.assertEqual(rows, [("VL Fields", "ctc_INV::amount", 1)])

    def test_relationship_sort_field_captured(self):
        rows = self.conn.execute("""
            SELECT source_kind, target_raw, resolved FROM v_usage
            WHERE context='sort'""").fetchall()
        self.assertEqual(rows, [("relationship", "ctc_INV::amount", 1)])

    def test_unqualified_field_ref_flagged_ambiguous(self):
        # bare 'zkp' matches two fields; pick is deterministic (first by id = CTC)
        # but flagged so the uncertainty stays visible
        rows = self.conn.execute("""
            SELECT te.base_table, r.ambiguous FROM refs r
            JOIN entities te ON te.entity_id = r.target_entity_id
            WHERE r.target_kind='field' AND r.target_raw='zkp'""").fetchall()
        self.assertEqual(rows, [("CTC", 1)])
        # qualified refs are never flagged
        n = self.conn.execute("""
            SELECT COUNT(*) FROM refs
            WHERE ambiguous=1 AND target_to_name IS NOT NULL""").fetchone()[0]
        self.assertEqual(n, 0)

    def test_multifile_build_resolves_cross_file(self):
        db = os.path.join(self.tmp, "multi.db")
        build([FIXTURE, FIXTURE_B], db, label="multi")
        conn = sqlite3.connect(db)
        n_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        self.assertEqual(n_files, 2)
        # the external call now resolves — to MicroB's script, not the local one
        row = conn.execute("""
            SELECT te.name, fl.name FROM refs r
            JOIN entities te ON te.entity_id = r.target_entity_id
            JOIN files fl ON fl.file_id = te.file_id
            WHERE r.target_raw='Remote Helper'""").fetchone()
        self.assertEqual(row, ("Remote Helper", "MicroB.fmp12"))
        # Ghost Script (no FileReference marker, no local match) is still NULL
        self.assertIsNone(conn.execute(
            "SELECT target_entity_id FROM refs WHERE target_raw='Ghost Script'"
        ).fetchone()[0])
        # field ref through the EXTERNAL TO (ext_LOG -> MicroB's LOG table)
        row = conn.execute("""
            SELECT te.name, te.base_table, fl.name FROM refs r
            JOIN entities te ON te.entity_id = r.target_entity_id
            JOIN files fl ON fl.file_id = te.file_id
            WHERE r.target_raw='ext_LOG::message'""").fetchone()
        self.assertEqual(row, ("message", "LOG", "MicroB.fmp12"))
        conn.close()

    def test_external_to_field_unresolved_single_file(self):
        # without MicroB in the DB the external-TO field ref must stay NULL
        row = self.conn.execute("""
            SELECT target_entity_id FROM refs
            WHERE target_raw='ext_LOG::message'""").fetchone()
        self.assertIsNone(row[0])

    def test_build_rejects_non_ddr(self):
        bogus = os.path.join(self.tmp, "not_a_ddr.xml")
        with open(bogus, "w") as f:
            f.write('<?xml version="1.0"?><FMSaveAsXML><Structure/></FMSaveAsXML>')
        with self.assertRaises(ValueError):
            build(bogus, os.path.join(self.tmp, "bogus.db"))

    def test_build_preserves_target_on_bad_input(self):
        # A malformed DDR must not destroy whatever already sits at the -o path.
        target = os.path.join(self.tmp, "precious.db")
        with open(target, "wb") as f:
            f.write(b"IRREPLACEABLE")
        truncated = os.path.join(self.tmp, "trunc.xml")
        with open(truncated, "w") as f:
            f.write('<?xml version="1.0"?><FMPReport type="Report"><File name="X">')
        with self.assertRaises(Exception):
            build(truncated, target, force=True)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"IRREPLACEABLE")  # untouched
        # and no half-built temp files are left behind
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".fmsonar-build-")]
        self.assertEqual(leftovers, [])

    def test_build_creates_a_missing_output_directory(self):
        """-o into a directory that does not exist yet is the normal first run
        on a new machine (~/.fmsonar/dbs). mkstemp does not create it, so build
        has to: without this, the failure surfaces as Errno 2 naming the temp
        file, which reads as if the temp file were the problem."""
        target = os.path.join(self.tmp, "fresh", "dbs", "solution.db")
        self.assertFalse(os.path.isdir(os.path.dirname(target)))
        build(FIXTURE, target, label="fresh machine")
        from fm_ddr.parse import _is_fmsonar_db
        self.assertTrue(_is_fmsonar_db(target))

    def test_build_refuses_to_clobber_non_fmsonar_file(self):
        target = os.path.join(self.tmp, "notes.md")
        with open(target, "w") as f:
            f.write("# my notes\n")
        with self.assertRaises(ValueError):
            build(FIXTURE, target)                       # no --force
        self.assertTrue(os.path.exists(target))
        build(FIXTURE, target, force=True)               # --force overrides
        from fm_ddr.parse import _is_fmsonar_db
        self.assertTrue(_is_fmsonar_db(target))

    def test_build_rejects_dtd_entity_bomb(self):
        bomb = os.path.join(self.tmp, "bomb.xml")
        with open(bomb, "w") as f:
            f.write('<?xml version="1.0"?>\n<!DOCTYPE FMPReport [\n'
                    '<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;">\n]>\n'
                    '<FMPReport type="Report"><File name="X">&b;</File></FMPReport>')
        with self.assertRaises(ValueError):
            build(bomb, os.path.join(self.tmp, "bomb.db"))

    def test_summary_traversal_confined(self):
        from fm_ddr.parse import expand_summary
        d = os.path.join(self.tmp, "manifest")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "Summary.xml"), "w") as f:
            f.write('<?xml version="1.0"?><FMPReport type="Summary">'
                    '<File link="Good_fmp12.xml"/>'
                    '<File link="../../../../etc/passwd"/>'
                    '<File link="/etc/hosts"/></FMPReport>')
        linked = expand_summary(os.path.join(d, "Summary.xml"))
        self.assertEqual(linked, [os.path.join(d, "Good_fmp12.xml")])

    def test_orphan_scripts(self):
        orphans = {r[0] for r in self.conn.execute("SELECT name FROM v_orphan_scripts")}
        self.assertIn("Orphan Script", orphans)
        self.assertNotIn("Helper Script", orphans)

    def test_value_list_binding_captured(self):
        rows = self.conn.execute("""
            SELECT source_kind, resolved FROM v_usage
            WHERE context='value_list_source' AND target_name='VL Custom'
        """).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][1])

    def test_utf16_gives_identical_graph(self):
        u16 = utf16_copy(self.tmp)
        db2 = os.path.join(self.tmp, "fixture16.db")
        build(u16, db2, label="fixture-utf16")
        self.assertEqual(normalize(*read_sqlite(self.db)),
                         normalize(*read_sqlite(db2)))

    def test_report_embedding_is_script_safe(self):
        from fm_ddr.report import report
        out = report(self.db, os.path.join(self.tmp, "fixture.html"))
        with open(out) as f:
            html = f.read()
        # the fixture calc contains a literal </script>; it must arrive escaped
        self.assertNotIn("<html><script>alert(1)</script>", html)
        self.assertIn("\\u003c/script", html)

    def test_snapshot(self):
        got = normalize(*read_sqlite(self.db))
        if os.environ.get("UPDATE_SNAPSHOT"):
            with open(SNAPSHOT, "w") as f:
                json.dump(got, f, indent=1, sort_keys=True)
            self.skipTest("snapshot regenerated")
        self.assertTrue(os.path.exists(SNAPSHOT),
                        "no snapshot; run with UPDATE_SNAPSHOT=1")
        with open(SNAPSHOT) as f:
            want = json.load(f)
        self.assertEqual(got, want)


class TestSnippet(unittest.TestCase):
    def test_ddr_to_fmxmlsnippet(self):
        from fm_ddr.snippet import extract_script_xml, ddr_steps_to_snippet
        xml = extract_script_xml(FIXTURE, "Main Script")
        snip, n = ddr_steps_to_snippet(xml)
        self.assertEqual(n, 6)
        self.assertTrue(snip.startswith('<fmxmlsnippet type="FMObjectList">'))
        # DDR-only elements are stripped
        self.assertNotIn("StepText", snip)
        self.assertNotIn("DisplayCalculation", snip)
        # references and calcs pass through
        self.assertIn('<Script id="2" name="Helper Script">', snip)
        self.assertIn('<Layout id="2" name="Invoices">', snip)
        self.assertIn("<![CDATA[", snip)
        # self-closing expanded to FileMaker's form
        self.assertNotIn("/>", snip)

    def test_snippet_skips_earlier_references(self):
        # Helper Script is referenced by a layout trigger (open+close <Script>
        # pair) BEFORE its definition; extraction must skip that and find the
        # real definition with its StepList
        from fm_ddr.snippet import extract_script_xml, ddr_steps_to_snippet
        xml = extract_script_xml(FIXTURE, "Helper Script")
        self.assertIn("<StepList>", xml)
        snip, n = ddr_steps_to_snippet(xml)
        self.assertEqual(n, 1)
        self.assertIn("Set Variable", snip)

    def test_snippet_id_disambiguation(self):
        from fm_ddr.snippet import extract_script_xml
        # right id resolves, wrong id is not found (no silent fallback)
        xml = extract_script_xml(FIXTURE, "Main Script", script_id="1")
        self.assertIn("<StepList>", xml)
        with self.assertRaises(ValueError):
            extract_script_xml(FIXTURE, "Main Script", script_id="9999")

    def test_snippet_refuses_saxml_by_name(self):
        # A Save a Copy as XML export has another step shape; snippet reads
        # a DDR and says so, not "script not found"
        import contextlib
        import io
        from fm_ddr.cli import main
        from fm_ddr.snippet import extract_script_xml
        sax = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")
        with self.assertRaises(ValueError) as cm:
            extract_script_xml(sax, "Caller")
        msg = str(cm.exception)
        self.assertIn("Save a Copy as XML", msg)
        self.assertIn("DDR", msg)
        self.assertNotIn("not found", msg)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main(["snippet", sax, "Caller"])
        self.assertIn("Save a Copy as XML", err.getvalue())

    def test_snippet_clip_guarded_off_macos(self):
        import sys
        from fm_ddr.snippet import set_clipboard_xmss
        if sys.platform == "darwin":
            self.skipTest("guard only triggers off macOS")
        with self.assertRaises(RuntimeError):
            set_clipboard_xmss('<fmxmlsnippet type="FMObjectList"></fmxmlsnippet>')


class TestCLI(unittest.TestCase):
    def test_errors_print_friendly_not_traceback(self):
        import io, contextlib
        from fm_ddr.cli import main
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            main(["build", "/no/such/file_fmp12.xml", "-o",
                  os.path.join(tempfile.mkdtemp(), "x.db")])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("error:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_search_never_crashes_on_fts_syntax(self):
        import io, contextlib
        from fm_ddr.cli import main
        db = build_fixture_db(tempfile.mkdtemp())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["search", db, "email AND ("])   # malformed FTS - must not crash
        self.assertIn("Text search", out.getvalue())

    def test_shipped_skill_docs_match_repo_root(self):
        # QUERIES.md / COVERAGE.md ship inside the skill; repo root is canonical.
        # A drift here means install-skill would deliver stale docs.
        for name in ("QUERIES.md", "COVERAGE.md"):
            root_copy = open(os.path.join(ROOT, name), "rb").read()
            ship_copy = open(os.path.join(ROOT, "fm_ddr", "skill", name), "rb").read()
            self.assertEqual(root_copy, ship_copy,
                             f"{name} drifted: cp {name} fm_ddr/skill/{name}")

    def test_skill_protocol_has_merge_step(self):
        # The blog claims the shipped protocol recommends verify-and-merge;
        # this pins that claim to the artifact.
        s = open(os.path.join(ROOT, "fm_ddr", "skill", "SKILL.md")).read()
        self.assertIn("verify-and-merge", s)
        self.assertIn("most capable model", s)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TestJSParity(unittest.TestCase):
    """The web app's JS parser must produce the identical graph, edge by edge."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_parity_")
        cls.py_norm = normalize(*read_sqlite(build_fixture_db(cls.tmp)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_js(self, xml_paths, chunk_size):
        out = subprocess.run(
            ["node", JS_RUNNER, WEB_APP, str(chunk_size)] + list(xml_paths),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, f"js runner failed:\n{out.stderr}")
        return normalize(*read_js(json.loads(out.stdout)))

    def test_parity_utf8_large_chunks(self):
        self.assertEqual(self.py_norm, self.run_js([FIXTURE], 65536))

    def test_parity_utf8_torture_chunks(self):
        # 13-byte chunks split tags, CDATA markers, and entities mid-stream
        self.assertEqual(self.py_norm, self.run_js([FIXTURE], 13))

    def test_parity_utf16_torture_chunks(self):
        # odd chunk size also splits UTF-16 code units across reads
        u16 = utf16_copy(self.tmp)
        self.assertEqual(self.py_norm, self.run_js([u16], 13))

    def test_parity_multifile(self):
        db = os.path.join(self.tmp, "multi_parity.db")
        build([FIXTURE, FIXTURE_B], db, label="multi")
        py = normalize(*read_sqlite(db))
        self.assertEqual(py, self.run_js([FIXTURE, FIXTURE_B], 13))

    def test_parity_three_files_data_sources(self):
        # v1.13.0: external data sources resolved by catalog link, then name
        db = os.path.join(self.tmp, "three_parity.db")
        build([FIXTURE, FIXTURE_B, FIXTURE_C], db, label="three")
        py = normalize(*read_sqlite(db))
        self.assertEqual(py, self.run_js([FIXTURE, FIXTURE_B, FIXTURE_C], 13))

    def test_parity_dangling_fixture_alone(self):
        # v1.13.0: every emit-nothing and marker case, edge by edge
        db = os.path.join(self.tmp, "c_parity.db")
        build(FIXTURE_C, db, label="c")
        py = normalize(*read_sqlite(db))
        self.assertEqual(py, self.run_js([FIXTURE_C], 13))

    def test_parity_table_contexts_fixture_alone(self):
        # v1.14.0: step_table, portal, value_list_context, the empty field
        # objects and the menu flags, edge by edge
        db = os.path.join(self.tmp, "d_parity.db")
        build(FIXTURE_D, db, label="d")
        py = normalize(*read_sqlite(db))
        self.assertEqual(py, self.run_js([FIXTURE_D], 13))

    def test_parity_table_contexts_two_files(self):
        # v1.14.0: the portal on TO "CTC" must resolve inside MicroD, never
        # to MicroTest's TO of the same name
        db = os.path.join(self.tmp, "ad_parity.db")
        build([FIXTURE, FIXTURE_D], db, label="ad")
        py = normalize(*read_sqlite(db))
        self.assertEqual(py, self.run_js([FIXTURE, FIXTURE_D], 13))

    def test_parity_import_export_find(self):
        # v1.15.1: Import Records mappings, Export Records orders and stored
        # find requests, edge by edge
        fx = os.path.join(ROOT, "tests", "fixtures", "micro_ddr_e.xml")
        db = os.path.join(self.tmp, "e_parity.db")
        build(fx, db, label="e")
        py = normalize(*read_sqlite(db))
        self.assertEqual(py, self.run_js([fx], 13))

    def test_parity_lab_ddr(self):
        # The lab file's real DDR (UTF-16): a field object on repetition 3
        # (FieldObj/Name "...Phone_r[3]") resolves in both parsers
        lab = os.path.join(ROOT, "tests", "fixtures", "saxml_lab_ddr.xml")
        db = os.path.join(self.tmp, "lab_parity.db")
        build(lab, db, label="lab")
        py = normalize(*read_sqlite(db))
        self.assertEqual(py, self.run_js([lab], 13))


@unittest.skipUnless(shutil.which("node"), "node not available")
class TestViewerSaXMLRefusal(unittest.TestCase):
    """The browser viewer refuses every SaXML fixture with a message that
    names the format, read in the browser's 64 KB chunks; a DDR still
    loads."""

    def run_js(self, path):
        return subprocess.run(["node", JS_RUNNER, WEB_APP, "65536", path],
                              capture_output=True, text=True, timeout=120)

    def test_refused(self):
        import glob
        from fm_ddr.saxml import sniff
        paths = [p for p in sorted(glob.glob(os.path.join(
            ROOT, "tests", "fixtures", "saxml_*.xml"))) if sniff(p)["fmt"] == "saxml"]
        self.assertGreaterEqual(len(paths), 7)
        for path in paths:
            with self.subTest(fixture=os.path.basename(path)):
                out = self.run_js(path)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("This is a Save a Copy as XML export (FMSaveAsXML "
                              f'version {sniff(path)["version"]}); the browser '
                              "viewer reads the DDR only.", out.stderr)

    def test_ddr_loads(self):
        self.assertEqual(self.run_js(FIXTURE).returncode, 0)


class TestV19Capture(unittest.TestCase):
    """v1.9.0: field storage, auto-enter (active vs dead residue), validation
    contexts, and trigger events."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_v19_")
        cls.db = build_fixture_db(cls.tmp)
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def field(self, name, cols):
        row = self.conn.execute(
            f"SELECT {cols} FROM entities WHERE kind='field' AND name=?",
            (name,)).fetchone()
        self.assertIsNotNone(row, f"field {name} missing")
        return row

    def test_storage_columns(self):
        self.assertEqual(self.field("zkp", "stored, indexed, is_global"),
                         (1, "All", 0))
        self.assertEqual(self.field("Segment", "stored, indexed, is_global"),
                         (1, "None", 0))
        self.assertEqual(self.field("pref_g", "stored, indexed, is_global"),
                         (1, None, 1))
        # no <Storage> tag at all -> all NULL
        self.assertEqual(self.field("email", "stored, indexed, is_global"),
                         (None, None, None))

    def test_auto_enter_active(self):
        (ae,) = self.field("autofill_active", "auto_enter")
        d = json.loads(ae)
        self.assertTrue(d["calc_active"])
        self.assertIn("Upper", d["calc"])
        self.assertTrue(d["overwrite_existing"])
        # the auto-enter calc must NOT pose as the field's own formula
        (calc,) = self.field("autofill_active", "calc_text")
        self.assertIsNone(calc)
        # its refs are live, context=auto_enter
        rows = self.conn.execute(
            "SELECT context, disabled FROM refs r JOIN entities s "
            "ON s.entity_id=r.source_entity_id WHERE s.name='autofill_active' "
            "AND r.target_kind='field'").fetchall()
        self.assertEqual(rows, [("auto_enter", 0)])

    def test_auto_enter_dead_residue(self):
        (ae,) = self.field("autofill_dead", "auto_enter")
        d = json.loads(ae)
        self.assertFalse(d["calc_active"])
        # refs from the dead calc are flagged dead code...
        rows = self.conn.execute(
            "SELECT context, disabled FROM refs r JOIN entities s "
            "ON s.entity_id=r.source_entity_id WHERE s.name='autofill_dead' "
            "AND r.target_kind='field'").fetchall()
        self.assertEqual(rows, [("auto_enter", 1)])
        # ...excluded from v_usage, kept in v_usage_disabled
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM v_usage WHERE source_name='autofill_dead'"
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM v_usage_disabled WHERE source_name='autofill_dead'"
        ).fetchone()[0], 1)

    def test_dead_auto_enter_text_stays_searchable(self):
        # the FTS blind-spot check must keep finding auto-enter text
        n = self.conn.execute(
            "SELECT COUNT(*) FROM text_index WHERE body MATCH 'deadresidue'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_validation_context(self):
        rows = self.conn.execute(
            "SELECT context, disabled, target_name FROM refs r JOIN entities s "
            "ON s.entity_id=r.source_entity_id WHERE s.name='validated_qty' "
            "AND r.target_kind='field'").fetchall()
        self.assertEqual(rows, [("validation", 0, "zkp")])
        # validation calc lands in extra_json, not calc_text
        (calc, extra) = self.field("validated_qty", "calc_text, extra_json")
        self.assertIsNone(calc)
        self.assertIn("validation_calc", extra or "")

    def test_trigger_event_and_view(self):
        events = {r[0] for r in self.conn.execute(
            "SELECT trigger_event FROM refs WHERE context='trigger'")}
        self.assertEqual(events,
                         {"OnObjectEnter", "OnFirstWindowOpen", "OnLastWindowClose"})
        rows = self.conn.execute(
            "SELECT trigger_event, source_kind, layout_name, script_name, resolved "
            "FROM v_triggers WHERE layout_name IS NOT NULL").fetchall()
        self.assertEqual(rows, [("OnObjectEnter", "layout_object", "Contacts",
                                 "Helper Script", 1)])


class TestV191Capture(unittest.TestCase):
    """v1.9.1: serial-number config, lookup source refs (live vs dead residue),
    file-level WindowTriggers events."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_v191_")
        cls.db = build_fixture_db(cls.tmp)
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ae(self, field):
        (raw,) = self.conn.execute(
            "SELECT auto_enter FROM entities WHERE kind='field' AND name=?",
            (field,)).fetchone()
        return json.loads(raw) if raw else None

    def test_serial_config(self):
        d = self.ae("invoice_no")
        self.assertEqual(d["serial"], {"increment": "1", "nextValue": "42",
                                       "generate": "OnCreation"})

    def test_lookup_live(self):
        d = self.ae("looked_up_amount")
        self.assertEqual(d["lookup_source"], "ctc_INV::amount")
        self.assertTrue(d["lookup_active"])
        # dependency edge exists, live, resolved through the TO to INV::amount
        rows = self.conn.execute(
            "SELECT r.context, r.disabled, te.base_table, te.name FROM refs r "
            "JOIN entities s ON s.entity_id=r.source_entity_id "
            "LEFT JOIN entities te ON te.entity_id=r.target_entity_id "
            "WHERE s.name='looked_up_amount'").fetchall()
        self.assertEqual(rows, [("lookup", 0, "INV", "amount")])

    def test_lookup_dead_residue(self):
        d = self.ae("dead_lookup")
        self.assertFalse(d["lookup_active"])
        rows = self.conn.execute(
            "SELECT r.context, r.disabled FROM refs r JOIN entities s "
            "ON s.entity_id=r.source_entity_id WHERE s.name='dead_lookup'"
        ).fetchall()
        self.assertEqual(rows, [("lookup", 1)])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM v_usage WHERE source_name='dead_lookup'"
        ).fetchone()[0], 0)

    def test_file_level_triggers(self):
        rows = self.conn.execute(
            "SELECT trigger_event, script_name, resolved FROM v_triggers "
            "WHERE layout_name IS NULL ORDER BY trigger_event").fetchall()
        self.assertEqual(rows, [("OnFirstWindowOpen", "Helper Script", 1),
                                ("OnLastWindowClose", "Missing Closer", 0)])
        # file-level trigger must NOT be misfiled as perform_script
        n = self.conn.execute(
            "SELECT COUNT(*) FROM refs WHERE context='perform_script' "
            "AND target_name='Missing Closer'").fetchone()[0]
        self.assertEqual(n, 0)


class TestV110Capture(unittest.TestCase):
    """v1.10.0: relationship attributes (cascades, predicates), value-list
    definitions, layout text-object capture."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_v110_")
        cls.db = build_fixture_db(cls.tmp)
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def extra(self, kind, name):
        (raw,) = self.conn.execute(
            "SELECT extra_json FROM entities WHERE kind=? AND name=?",
            (kind, name)).fetchone()
        return json.loads(raw) if raw else {}

    def test_relationship_attributes(self):
        d = self.extra("relationship", "CTC :: ctc_INV")
        self.assertEqual(d["sides"], [
            {"side": "left", "to": "CTC",
             "cascade_create": False, "cascade_delete": False},
            {"side": "right", "to": "ctc_INV",
             "cascade_create": True, "cascade_delete": True}])
        self.assertEqual(d["predicates"],
                         [{"op": "Equal", "left": "CTC::zkp",
                           "right": "ctc_INV::zkp"}])

    def test_v_cascades(self):
        rows = self.conn.execute(
            "SELECT deleting_table, victim_table, victim_to FROM v_cascades"
        ).fetchall()
        # deleting a CTC record cascades into INV (via the ctc_INV occurrence)
        self.assertEqual(rows, [("CTC", "INV", "ctc_INV")])

    def test_value_list_definitions(self):
        d = self.extra("value_list", "VL Custom")
        self.assertEqual(d["source"], "Custom")
        self.assertEqual(d["custom_values"], ["alpha", "beta"])
        d = self.extra("value_list", "VL Fields")
        self.assertEqual(d["source"], "Field")
        self.assertEqual(d["primary"],
                         {"field": "ctc_INV::amount", "show": True, "sort": True})
        self.assertFalse(d["show_related"])

    def test_layout_text_capture(self):
        # the text object's runs land in extra_json.text (merge markers intact)
        row = self.conn.execute(
            "SELECT extra_json FROM entities WHERE kind='layout_object' "
            "AND extra_json LIKE '%mergemarker42%'").fetchone()
        self.assertIsNotNone(row)
        d = json.loads(row[0])
        self.assertIn("<<ctc_INV::amount>>", d["text"])
        self.assertIn("<<ƒ:Upper ( CTC::email )>>", d["text"])

    def test_layout_text_searchable(self):
        # FTS finds layout text now — on the object AND the bubbled layout body
        kinds = {k for (k,) in self.conn.execute(
            "SELECT kind FROM text_index WHERE body MATCH 'mergemarker42'")}
        self.assertIn("layout_object", kinds)
        self.assertIn("layout", kinds)

    def test_custom_values_searchable(self):
        kinds = {k for (k,) in self.conn.execute(
            "SELECT kind FROM text_index WHERE body MATCH 'beta'")}
        self.assertIn("value_list", kinds)


class TestLayoutContext(unittest.TestCase):
    """v1.12.0: each layout's own context TO is a layout_context ref, resolved
    by (file_id, fm_id); an empty <Table name=""/> is a missing TO."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_v112_")
        cls.db = build_fixture_db(cls.tmp)
        cls.conn = sqlite3.connect(cls.db)
        cls.multi = os.path.join(cls.tmp, "multi.db")
        build([FIXTURE, FIXTURE_B], cls.multi, label="multi")
        cls.mconn = sqlite3.connect(cls.multi)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.mconn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ctx_rows(self, conn):
        return {lay: (fmid, tname, te_name, te_file)
                for lay, fmid, tname, te_name, te_file in conn.execute("""
            SELECT lay.name, r.target_fm_id, r.target_name, te.name, fl.name
            FROM refs r
            JOIN entities lay ON lay.entity_id = r.source_entity_id
            LEFT JOIN entities te ON te.entity_id = r.target_entity_id
            LEFT JOIN files fl ON fl.file_id = te.file_id
            WHERE r.context = 'layout_context'""")}

    def test_named_context_to_resolves(self):
        rows = self.ctx_rows(self.conn)
        self.assertEqual(rows["Contacts"], ("1", "CTC", "CTC", "MicroTest.fmp12"))
        src_kind, tk = self.conn.execute(
            "SELECT source_kind, target_kind FROM refs "
            "WHERE context='layout_context' AND target_name='CTC'").fetchone()
        self.assertEqual((src_kind, tk), ("layout", "table_occurrence"))

    def test_empty_name_is_unresolved_and_findable(self):
        # <Table id="9" name=""/>: no TO with fm_id 9 exists -> missing TO
        rows = self.ctx_rows(self.conn)
        self.assertEqual(rows["Invoices"], ("9", None, None, None))
        missing = [r[0] for r in self.conn.execute("""
            SELECT lay.name FROM refs r
            JOIN entities lay ON lay.entity_id = r.source_entity_id
            WHERE r.context='layout_context' AND r.target_entity_id IS NULL""")]
        self.assertEqual(missing, ["Invoices"])

    def test_separator_layout_emits_nothing(self):
        # the "-" menu separator carries <Table id="0" name=""/>: no layout, no ref
        n = self.conn.execute(
            "SELECT COUNT(*) FROM refs WHERE context='layout_context'").fetchone()[0]
        self.assertEqual(n, 2)

    def test_multifile_fm_id_collision_keys_on_file(self):
        # MicroB's layout "Log" points at TO id 1 (LOG); MicroTest also has a
        # TO id 1 (CTC). It must resolve inside its own file.
        rows = self.ctx_rows(self.mconn)
        self.assertEqual(rows["Log"], ("1", "LOG", "LOG", "MicroB.fmp12"))
        self.assertEqual(rows["Contacts"], ("1", "CTC", "CTC", "MicroTest.fmp12"))
        # "Log Orphan" names TO id 3, which exists only in MicroTest (ctc_INV):
        # it must stay unresolved, never cross-link to the other file
        self.assertEqual(rows["Log Orphan"], ("3", None, None, None))

    def test_where_shows_layout_context(self):
        import io, contextlib
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["where", self.db, "CTC"])
        text = out.getvalue()
        self.assertIn("layout_context", text)
        self.assertIn("Contacts", text)


class TestExternalDataSources(unittest.TestCase):
    """v1.13.0: a reference through a TO on an external data source, or a
    step behind a FileReference marker, resolves to the file the data source
    points at: by the catalog's link, then by name (v_data_sources). An
    external TO never resolves to a local field with the same base table."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_ds_")
        cls.multi = os.path.join(cls.tmp, "three.db")
        build([FIXTURE, FIXTURE_B, FIXTURE_C], cls.multi, label="three")
        cls.conn = sqlite3.connect(cls.multi)
        cls.single = build_fixture_db(cls.tmp, src=FIXTURE_C)
        cls.sconn = sqlite3.connect(cls.single)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.sconn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def target(self, conn, raw):
        """(field name, base table, file) the calc ref with that raw resolved to."""
        return conn.execute("""
            SELECT te.name, te.base_table, fl.name FROM refs r
            LEFT JOIN entities te ON te.entity_id = r.target_entity_id
            LEFT JOIN files fl ON fl.file_id = te.file_id
            WHERE r.context = 'calc' AND r.target_raw = ?""", (raw,)).fetchone()

    def test_external_to_resolves_by_catalog_link(self):
        # the data source is named "Main" but links MicroTest_fmp12.xml: only
        # the link can find it. MicroC has its own CTC::email; it must lose.
        self.assertEqual(self.target(self.conn, "main_CTC::email"),
                         ("email", "CTC", "MicroTest.fmp12"))

    def test_external_to_resolves_by_name(self):
        self.assertEqual(self.target(self.conn, "b_LOG::message"),
                         ("message", "LOG", "MicroB.fmp12"))

    def test_self_referencing_source_resolves_to_own_file(self):
        self.assertEqual(self.target(self.conn, "self_CTC::email"),
                         ("email", "CTC", "MicroC.fmp12"))

    def test_source_without_a_file_stays_unresolved(self):
        self.assertEqual(self.target(self.conn, "gone_X::email"), (None, None, None))

    def test_external_script_call_resolves_in_the_linked_file(self):
        # Script id 1 exists in MicroC too ("C One"); the call must land on
        # MicroTest's script 1
        row = self.conn.execute("""
            SELECT te.name, fl.name FROM refs r
            JOIN entities te ON te.entity_id = r.target_entity_id
            JOIN files fl ON fl.file_id = te.file_id
            WHERE r.target_raw = 'Main Script' AND r.target_file = 'Main'""").fetchone()
        self.assertEqual(row, ("Main Script", "MicroTest.fmp12"))
        # the two <Script id="2"/> calls stay unresolved, the first with the
        # '<unknown>' file marker
        rows = self.conn.execute("""
            SELECT r.target_file, r.target_entity_id FROM refs r
            JOIN entities s ON s.entity_id = r.source_entity_id
            JOIN entities scr ON scr.entity_id = s.parent_entity_id
            WHERE scr.name = 'Calls' AND r.target_fm_id = '2' ORDER BY r.ref_id""").fetchall()
        self.assertEqual(rows, [("<unknown>", None), (None, None)])

    def test_v_data_sources(self):
        rows = self.conn.execute("""
            SELECT ds_name, link, target_file_name FROM v_data_sources d
            JOIN files f ON f.file_id = d.file_id
            WHERE f.name = 'MicroC.fmp12' ORDER BY ds_name""").fetchall()
        self.assertEqual(rows, [("Gone", None, None),
                                ("Main", "MicroTest_fmp12.xml", "MicroTest.fmp12"),
                                ("MicroB", None, "MicroB.fmp12"),
                                ("Self", "MicroC_fmp12.xml", "MicroC.fmp12")])
        # a file without a catalog still gets rows for the names it uses
        rows = self.conn.execute("""
            SELECT ds_name, target_file_name FROM v_data_sources d
            JOIN files f ON f.file_id = d.file_id
            WHERE f.name = 'MicroTest.fmp12'""").fetchall()
        self.assertEqual(rows, [("MicroB", "MicroB.fmp12")])

    def test_missing_source_marked_on_the_to(self):
        self.assertEqual(self.conn.execute(
            "SELECT ext_file FROM entities WHERE kind='table_occurrence' AND name='lost_Y'"
        ).fetchone()[0], "<unknown>")

    def test_single_file_build_never_links_externals_locally(self):
        # without MicroTest and MicroB in the DB the refs through main_CTC,
        # gone_X and b_LOG stay NULL (never the local CTC::email); only the
        # self-referencing source resolves, to this file's own field
        for raw in ("main_CTC::email", "gone_X::email", "b_LOG::message"):
            self.assertEqual(self.target(self.sconn, raw), (None, None, None), raw)
        self.assertEqual(self.target(self.sconn, "self_CTC::email"),
                         ("email", "CTC", "MicroC.fmp12"))


class TestDangling(unittest.TestCase):
    """v1.13.0: the DDR's own markers and the LayoutDestination decide what an
    empty reference element is. An unset optional slot emits nothing; a deleted
    target emits an unresolved ref carrying '<unknown>' or the DDR's marker,
    listed by v_dangling. Single-file build of micro_ddr_c.xml."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_v113_")
        cls.db = build_fixture_db(cls.tmp, src=FIXTURE_C)
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def step_refs(self, step_type, context=None):
        """(target_raw, target_fm_id, target_name, target_file, disabled) per
        ref sourced from a script step of that type."""
        where = "AND r.context = ?" if context else ""
        return self.conn.execute(f"""
            SELECT r.target_raw, r.target_fm_id, r.target_name, r.target_file,
                   r.disabled
            FROM refs r JOIN entities s ON s.entity_id = r.source_entity_id
            WHERE s.kind = 'script_step' AND s.step_type = ? {where}
            ORDER BY r.ref_id""", (step_type,) + ((context,) if context else ())
        ).fetchall()

    def test_total_ref_count(self):
        # 4 calc + 4 join predicate + 2 layout context + 2 on-layout + 1 value
        # list + 4 Calls + 9 Defects (8 + the Go to Related Record's step
        # table, v1.14.0) + 1 menu; every optional slot dropped
        n = self.conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        self.assertEqual(n, 27)

    def test_optional_slots_emit_nothing(self):
        # step <Field> with no id (Insert Text), OriginalLayout, the two by-calc
        # layouts (New Window has a <Layout> with neither id nor name), the
        # Add Account marker in text only, the empty Open File marker
        for st in ("Insert Text", "New Window", "Add Account", "Open File"):
            self.assertEqual(self.step_refs(st), [], st)
        self.assertEqual(len(self.step_refs("Go to Layout", "go_to_layout")), 2)
        # the unset trigger slot <Script id="0" name=""/>
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM refs WHERE context='trigger'").fetchone()[0], 0)
        # the value list's unset SecondaryField, and both stale slots of the
        # Custom-source list (its field ids mean nothing)
        self.assertEqual(self.conn.execute(
            "SELECT target_raw FROM refs WHERE context='value_list_field'").fetchall(),
            [("CTC::email",)])
        # the separator layout's <Table id="0" name=""/>
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM refs WHERE context='layout_context' "
            "AND target_fm_id='0'").fetchone()[0], 0)

    def test_by_calc_layout_keeps_its_calc(self):
        calcs = {r[0]: r[1] for r in self.conn.execute(
            "SELECT json_extract(extra_json,'$.step_text'), calc_text FROM entities "
            "WHERE kind='script_step' AND step_type IN ('Go to Layout','New Window')")}
        self.assertEqual(calcs['Go to Layout [ "C Main" ]'], '"C Main"')
        self.assertEqual(calcs["New Window [ Style: Card; Using layout: $lay ]"], "$lay")

    def test_deleted_layout_targets(self):
        rows = self.step_refs("Go to Layout", "go_to_layout")
        # SelectedLayout with id 0 and with id 1: both '<unknown>', name NULL;
        # id 1 exists as a layout but must never resolve
        self.assertEqual(rows, [("<unknown>", "0", None, None, 0),
                                ("<unknown>", "1", None, None, 0)])
        # the Go to Related Record behind an empty FileReference: its step
        # table (v1.14.0) resolves locally, the marker only guards the layout
        self.assertEqual(self.step_refs("Go to Related Record"),
                         [("self_CTC", "4", "self_CTC", None, 0),
                          ("<unknown>", "0", None, "<unknown>", 0)])
        # the button's Go to Layout [ <unknown> ] on the layout
        row = self.conn.execute("""
            SELECT source_kind, target_raw, target_fm_id, target_name
            FROM refs WHERE context='go_to_layout' AND source_kind='layout_object'
        """).fetchall()
        self.assertEqual(row, [("layout_object", "<unknown>", "324", None)])

    def test_deleted_field_targets_carry_the_ddr_marker(self):
        self.assertEqual(self.step_refs("Set Field"), [
            ("<Table Missing>", "0", None, None, 0),
            ("CTC::<Field Missing>", "13", None, None, 0),
            ("<Table Missing>", "0", None, None, 1),      # the disabled one
        ])
        # the empty sort criterion is '<unknown>' next to its real sibling
        self.assertEqual(self.step_refs("Sort Records"), [
            ("CTC::email", "1", "email", None, 0),
            ("<unknown>", "11", None, None, 0),
        ])
        # the relationship's RightField with a table and no name
        self.assertEqual(self.conn.execute(
            "SELECT target_raw, target_entity_id IS NULL FROM refs "
            "WHERE context='join_predicate' AND target_kind='field' "
            "ORDER BY ref_id").fetchall(), [("CTC::email", 0), ("CTC::<unknown>", 1)])

    def test_missing_layout_context_to(self):
        rows = self.conn.execute(
            "SELECT l.name, r.target_raw, r.target_fm_id, r.target_name FROM refs r "
            "JOIN entities l ON l.entity_id = r.source_entity_id "
            "WHERE r.context='layout_context' ORDER BY r.ref_id").fetchall()
        self.assertEqual(rows, [("C Main", "CTC", "1", "CTC"),
                                ("C Lost", "<unknown>", "7", None)])

    def test_deleted_script_targets(self):
        rows = self.step_refs("Perform Script")
        self.assertEqual(rows, [
            ("Main Script", "1", "Main Script", "Main", 0),   # other file
            ("<unknown>", "2", None, "<unknown>", 0),         # <Script id="2"/>, file gone
            ("<unknown>", "2", None, None, 0),                # <Script id="2"/>, no marker
            ("<unknown>", "0", "<unknown>", None, 0),         # the DDR's own marker
        ])
        # the custom menu's step performs the DDR's '<unknown>' script
        self.assertEqual(self.conn.execute(
            "SELECT source_kind, target_raw, target_name FROM refs "
            "WHERE source_kind='custom_menu'").fetchall(),
            [("custom_menu", "<unknown>", "<unknown>")])

    def test_markers_never_resolve(self):
        # the two "id exists but name is empty" cases: layout 1 ("C Main") and
        # script 2 ("C Two") exist, the refs must not land on them
        n = self.conn.execute("""
            SELECT COUNT(*) FROM refs r JOIN entities t ON t.entity_id = r.target_entity_id
            WHERE t.name IN ('C Main', 'C Two')""").fetchone()[0]
        self.assertEqual(n, 0)
        n = self.conn.execute("""
            SELECT COUNT(*) FROM refs WHERE target_entity_id IS NOT NULL
              AND (target_raw LIKE '%<unknown>%' OR target_raw LIKE '%Missing>%'
                   OR target_file = '<unknown>')""").fetchone()[0]
        self.assertEqual(n, 0)
        # the real sort criterion next to the empty one resolves
        self.assertEqual(self.conn.execute("""
            SELECT te.name FROM refs r JOIN entities te ON te.entity_id = r.target_entity_id
            WHERE r.context = 'sort'""").fetchall(), [("email",)])

    def test_v_dangling_refs(self):
        rows = self.conn.execute("""
            SELECT context, source_kind, source_parent_name, step_type, marker,
                   marker_class, disabled
            FROM v_dangling WHERE ref_id IS NOT NULL ORDER BY ref_id""").fetchall()
        self.assertEqual(rows, [
            ("join_predicate", "relationship", None, None, "CTC::<unknown>", "<unknown>", 0),
            ("go_to_layout", "layout_object", "C Main", None, "<unknown>", "<unknown>", 0),
            ("layout_context", "layout", None, None, "<unknown>", "<unknown>", 0),
            ("perform_script", "script_step", "Calls", "Perform Script", "<unknown>", "<unknown>", 0),
            ("perform_script", "script_step", "Calls", "Perform Script", "<unknown>", "<unknown>", 0),
            ("perform_script", "script_step", "Calls", "Perform Script", "<unknown>", "<unknown>", 0),
            ("go_to_layout", "script_step", "Defects", "Go to Layout", "<unknown>", "<unknown>", 0),
            ("go_to_layout", "script_step", "Defects", "Go to Layout", "<unknown>", "<unknown>", 0),
            ("step_target", "script_step", "Defects", "Set Field", "<Table Missing>", "<Table Missing>", 0),
            ("step_target", "script_step", "Defects", "Set Field", "CTC::<Field Missing>", "<Field Missing>", 0),
            ("sort", "script_step", "Defects", "Sort Records", "<unknown>", "<unknown>", 0),
            ("step_target", "script_step", "Defects", "Set Field", "<Table Missing>", "<Table Missing>", 1),
            ("go_to_layout", "script_step", "Defects", "Go to Related Record", "<unknown>", "<unknown>", 0),
            ("perform_script", "custom_menu", None, None, "<unknown>", "<unknown>", 0),
        ])
        # the Add Account and Open File steps have no row (text-only markers)
        self.assertEqual(self.conn.execute("""
            SELECT COUNT(*) FROM v_dangling
            WHERE step_type IN ('Add Account', 'Open File')""").fetchone()[0], 0)

    def test_v_dangling_broken_calcs_and_missing_source(self):
        rows = self.conn.execute("""
            SELECT context, source_kind, source_name, marker, disabled
            FROM v_dangling WHERE ref_id IS NULL ORDER BY context, source_kind, marker""").fetchall()
        self.assertEqual(rows, [
            ("broken_calc", "custom_function", "BrokenCF", "<Function Missing>", 0),
            ("broken_calc", "field", "broken_c", "<Field Missing>", 0),
            ("broken_calc", "field", "broken_c", "<Table Missing>", 0),
            ("broken_calc", "layout_object", None, "<Field Missing>", 0),   # the hide calc
            ("broken_calc", "layout_object", None, "<Table Missing>", 0),
            ("missing_data_source", "table_occurrence", "lost_Y", "<unknown>", 0),
        ])

    def test_stats_prints_dangling_block(self):
        import io, contextlib
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["stats", self.db])
        text = out.getvalue()
        self.assertIn("# Dangling references (defects the DDR marks)", text)
        self.assertRegex(text, r"step_target\s+<Table Missing>\s+1\s+1")
        self.assertRegex(text, r"broken_calc\s+<Function Missing>\s+1\s+0")
        # an index without the view gets the one-line hint and stats continues
        old = os.path.join(self.tmp, "old.db")
        shutil.copyfile(self.db, old)
        c = sqlite3.connect(old)
        c.execute("DROP VIEW v_dangling"); c.commit(); c.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["stats", old])          # must not raise SystemExit
        text = out.getvalue()
        self.assertIn("v_dangling: not in this index (built with parser < 1.13.0); "
                      "rebuild to get it", text)
        self.assertIn("# Unresolved by kind/context", text)

    def test_v_unresolved_keeps_three_columns(self):
        cols = [c[1] for c in self.conn.execute("PRAGMA table_info(v_unresolved)")]
        self.assertEqual(cols, ["target_kind", "context", "n"])

    def test_data_source_catalog_and_markers(self):
        rows = self.conn.execute(
            "SELECT name, json_extract(extra_json,'$.file'), json_extract(extra_json,'$.link') "
            "FROM entities WHERE kind='external_data_source' ORDER BY entity_id").fetchall()
        self.assertEqual(rows, [("Main", 1, "MicroTest_fmp12.xml"), ("MicroB", 1, None),
                                ("Gone", 1, None), ("Self", 1, "MicroC_fmp12.xml")])
        ext = dict(self.conn.execute(
            "SELECT name, ext_file FROM entities WHERE kind='table_occurrence'").fetchall())
        self.assertEqual(ext, {"CTC": None, "main_CTC": "Main", "gone_X": "Gone",
                               "self_CTC": "Self", "b_LOG": "MicroB",
                               "lost_Y": "<unknown>"})


class TestTableContexts(unittest.TestCase):
    """v1.14.0: the TO a step acts on (step_table), the TO a portal shows
    (portal), the "starting from" TO of a related-values list
    (value_list_context), field objects whose field is gone (layout_object
    field refs reading '<unknown>') and the raw menu flags. Single-file build
    of micro_ddr_d.xml plus a two-file build with micro_ddr.xml, whose TO
    "CTC" shares its name with MicroD's."""

    # QUERIES.md: scripts reachable from the Scripts menu if folders hide
    # their contents (the help's inference, unverified in FileMaker Pro)
    MENU_WALK = """
        WITH RECURSIVE up(script_id, eid) AS (
            SELECT entity_id, parent_entity_id FROM entities WHERE kind = 'script'
            UNION ALL
            SELECT up.script_id, g.parent_entity_id FROM up
            JOIN entities g ON g.entity_id = up.eid AND g.kind = 'script_group'
        )
        SELECT s.name FROM entities s
        WHERE s.kind = 'script' AND json_extract(s.extra_json, '$.in_menu') = 1
          AND NOT EXISTS (
            SELECT 1 FROM up JOIN entities g ON g.entity_id = up.eid
            WHERE up.script_id = s.entity_id AND g.kind = 'script_group'
              AND COALESCE(json_extract(g.extra_json, '$.in_menu'), 0) = 0)
        ORDER BY s.name"""

    # QUERIES.md: table occurrences with no live reference
    UNUSED_TO = """
        SELECT t.name FROM entities t
        WHERE t.kind = 'table_occurrence'
          AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.target_entity_id = t.entity_id
                          AND r.disabled = 0)
          AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.file_id = t.file_id
                          AND r.target_kind = 'field' AND r.target_to_name = t.name
                          AND r.disabled = 0)"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_v114_")
        cls.db = build_fixture_db(cls.tmp, src=FIXTURE_D)
        cls.conn = sqlite3.connect(cls.db)
        cls.multi = os.path.join(cls.tmp, "multi.db")
        build([FIXTURE, FIXTURE_D], cls.multi, label="multi")
        cls.mconn = sqlite3.connect(cls.multi)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.mconn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ctx_rows(self, context, conn=None):
        """(source_kind, source_name, step_type, target_raw, target_fm_id,
        resolved target name, disabled) per ref of that context."""
        return (conn or self.conn).execute("""
            SELECT r.source_kind, se.name, se.step_type, r.target_raw, r.target_fm_id,
                   te.name, r.disabled
            FROM refs r
            LEFT JOIN entities se ON se.entity_id = r.source_entity_id
            LEFT JOIN entities te ON te.entity_id = r.target_entity_id
            WHERE r.context = ? ORDER BY r.ref_id""", (context,)).fetchall()

    def dangling(self, context):
        return self.conn.execute("""
            SELECT source_kind, source_name, source_parent_name, step_type, marker,
                   target_fm_id, disabled
            FROM v_dangling WHERE context = ? ORDER BY ref_id""", (context,)).fetchall()

    def test_step_table(self):
        self.assertEqual(self.ctx_rows("step_table"), [
            ("layout_object", "btnLines", None, "ORD_lines", "2", "ORD_lines", 0),
            ("script_step", "Go to Related Record", "Go to Related Record",
             "ORD_lines", "2", "ORD_lines", 0),
            ("script_step", "Go to Related Record", "Go to Related Record",
             "<unknown>", "0", None, 0),
            ("script_step", "Go to Related Record", "Go to Related Record",
             "<unknown>", "9", None, 0),
            ("script_step", "Import Records", "Import Records", "ORD", "1", "ORD", 0),
        ])
        self.assertEqual(self.dangling("step_table"), [
            ("script_step", "Go to Related Record", "Navigate", "Go to Related Record",
             "<unknown>", "0", 0),
            ("script_step", "Go to Related Record", "Navigate", "Go to Related Record",
             "<unknown>", "9", 0),
        ])

    def test_portal(self):
        self.assertEqual(self.ctx_rows("portal"), [
            ("layout_object", "portalLines", None, "ORD_lines", None, "ORD_lines", 0),
            ("layout_object", "portalLost", None, "<unknown>", None, None, 0),
            ("layout_object", "portalCTC", None, "CTC", None, "CTC", 0),
        ])
        self.assertEqual(self.dangling("portal"), [
            ("layout_object", "portalLost", "Orders", None, "<unknown>", None, 0),
        ])

    def test_portal_resolves_inside_its_own_file(self):
        # MicroTest's TO "CTC" has id 1 and MicroD's has id 3: the name rule
        # keys on the file, so the portal lands on MicroD's
        rows = self.mconn.execute("""
            SELECT se.name, te.name, te.fm_id, fl.name
            FROM refs r
            JOIN entities se ON se.entity_id = r.source_entity_id
            JOIN entities te ON te.entity_id = r.target_entity_id
            JOIN files fl ON fl.file_id = te.file_id
            WHERE r.context = 'portal' AND r.target_raw = 'CTC'""").fetchall()
        self.assertEqual(rows, [("portalCTC", "CTC", "3", "MicroD.fmp12")])

    def test_value_list_context(self):
        self.assertEqual(self.ctx_rows("value_list_context"), [
            ("value_list", "VL Related", None, "ORD_lines", "2", "ORD_lines", 0),
            ("value_list", "VL Lost", None, "<unknown>", "0", None, 0),
        ])
        # VL Flat (ShowRelated False) emits none; show_related is still recorded
        self.assertEqual(self.conn.execute("""
            SELECT name, json_extract(extra_json, '$.show_related') FROM entities
            WHERE kind = 'value_list' ORDER BY entity_id""").fetchall(),
            [("VL Related", 1), ("VL Lost", 1), ("VL Flat", 0)])
        self.assertEqual(self.dangling("value_list_context"), [
            ("value_list", "VL Lost", None, None, "<unknown>", "0", 0),
        ])

    def test_field_objects_whose_field_is_gone(self):
        rows = self.conn.execute("""
            SELECT r.target_raw, r.target_name, r.target_to_name, r.target_entity_id IS NULL,
                   sp.name
            FROM refs r
            JOIN entities se ON se.entity_id = r.source_entity_id
            LEFT JOIN entities sp ON sp.entity_id = se.parent_entity_id
            WHERE r.context = 'layout_object' AND r.target_kind = 'field'
            ORDER BY r.ref_id""").fetchall()
        self.assertEqual(rows, [
            ("ORD_lines::amount", "amount", "ORD_lines", 0, "portalLines"),
            ("<unknown>", None, None, 1, "portalLost"),
            ("<unknown>", None, None, 1, "Orders"),
        ])
        self.assertEqual(self.dangling("layout_object"), [
            ("layout_object", None, "portalLost", None, "<unknown>", None, 0),
            ("layout_object", None, "Orders", None, "<unknown>", None, 0),
        ])

    def test_menu_flags(self):
        flags = {name: (kind, json.loads(ex) if ex else {}) for kind, name, ex in
                 self.conn.execute("""
            SELECT kind, name, extra_json FROM entities
            WHERE kind IN ('script', 'script_group', 'layout', 'layout_group')""")}
        self.assertEqual(flags["Navigate"], ("script", {"in_menu": True, "run_full_access": True}))
        self.assertEqual(flags["Visible"], ("script", {"in_menu": True}))
        self.assertEqual(flags["Loose"], ("script", {"in_menu": True}))
        self.assertEqual(flags["Plain"], ("script", {}))
        self.assertEqual(flags["Orders"], ("layout", {"in_menu": True}))
        self.assertEqual(flags["Hidden"], ("layout", {"in_menu": True}))
        self.assertEqual(flags["Shown"], ("layout_group", {"in_menu": True}))
        self.assertEqual(flags["Admin"], ("layout_group", {}))
        self.assertEqual(flags["Public"], ("script_group", {"in_menu": True}))
        self.assertEqual(flags["Deep"], ("script_group", {"in_menu": True}))
        self.assertEqual(flags["Utility"], ("script_group", {}))
        # no flag key is ever written false
        self.assertEqual(self.conn.execute("""
            SELECT COUNT(*) FROM entities
            WHERE json_extract(extra_json, '$.in_menu') = 0
               OR json_extract(extra_json, '$.run_full_access') = 0""").fetchone()[0], 0)

    def test_menu_walk_recipe(self):
        # Navigate is flagged but sits in the cleared folder Utility; Visible
        # sits under two flagged folders; Loose is in no folder
        self.assertEqual([r[0] for r in self.conn.execute(self.MENU_WALK)],
                         ["Loose", "Visible"])
        # all four scripts are orphans by call graph; the flag is the only hint
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT name FROM v_orphan_scripts")), ["Loose", "Navigate", "Plain", "Visible"])

    def test_no_unused_to(self):
        # ORD: layout context + Import Records; ORD_lines: step table, portal,
        # value list context; CTC: portal only
        self.assertEqual(self.conn.execute(self.UNUSED_TO).fetchall(), [])

    def test_where_shows_the_contexts(self):
        import io, contextlib
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["where", self.db, "ORD_lines"])
        text = out.getvalue()
        for ctx in ("step_table", "portal", "value_list_context"):
            self.assertIn(ctx, text)

    def test_stats_prints_the_new_contexts(self):
        import io, contextlib
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["stats", self.db])
        text = out.getvalue()
        self.assertRegex(text, r"step_table\s+<unknown>\s+2\s+0")
        self.assertRegex(text, r"portal\s+<unknown>\s+1\s+0")
        self.assertRegex(text, r"value_list_context\s+<unknown>\s+1\s+0")
        self.assertRegex(text, r"layout_object\s+<unknown>\s+2\s+0")


class TestRobustness(unittest.TestCase):
    """Hostile-input and error-path behavior (v1.10.0 hardening round)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_rob_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _truncated_fixture(self):
        with open(FIXTURE, encoding="utf-8") as f:
            fix = f.read()
        cut = fix.find("]]>", fix.find("CDATA"))
        p = os.path.join(self.tmp, "truncated.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(fix[:cut - 10])
        return p

    def test_missing_db_no_phantom_file(self):
        # sqlite3.connect would CREATE the file; _connect must refuse instead
        from fm_ddr import cli
        ghost = os.path.join(self.tmp, "ghost.db")
        with self.assertRaises(SystemExit):
            cli._connect(ghost)
        self.assertFalse(os.path.exists(ghost), "phantom .db file created")

    def test_old_index_without_v_cascades_gets_rebuild_hint(self):
        db = build_fixture_db(self.tmp)
        conn = sqlite3.connect(db)
        conn.execute("DROP VIEW v_cascades")
        conn.commit(); conn.close()
        from fm_ddr import cli
        args = type("A", (), {"db": db, "table": None, "limit": 10})()
        with self.assertRaises(SystemExit) as cm:
            cli.cmd_cascades(args)
        self.assertIn("1.10.0", str(cm.exception))

    def test_truncated_ddr_refused_by_python(self):
        with self.assertRaises(Exception):
            build(self._truncated_fixture(),
                  os.path.join(self.tmp, "t.db"), label="t")

    def test_failed_build_does_not_clobber_existing_index(self):
        db = os.path.join(self.tmp, "keep.db")
        build(FIXTURE, db, label="good")
        before = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM entities").fetchone()[0]
        try:
            build(self._truncated_fixture(), db, label="bad")
        except Exception:
            pass
        after = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM entities").fetchone()[0]
        self.assertEqual((before > 0, after), (True, before))

    def test_snippet_finds_apostrophe_names_both_encodings(self):
        # ' is legal both literally and as &apos; in attributes — the raw-XML
        # needle must match either, or O'Brien scripts are "not found"
        from fm_ddr.snippet import extract_script_xml
        with open(FIXTURE, encoding="utf-8") as f:
            fix = f.read()
        for variant in ("O'Brien Script", "O&apos;Brien Script"):
            p = os.path.join(self.tmp, "apos.xml")
            with open(p, "w", encoding="utf-8") as f:
                f.write(fix.replace('name="Helper Script"',
                                    f'name="{variant}"'))
            xml = extract_script_xml(p, "O'Brien Script")
            self.assertIn("<StepList", xml, f"variant {variant!r} not found")

    @unittest.skipIf(shutil.which("node") is None, "node not available")
    def test_truncated_ddr_refused_by_js(self):
        # the web parser must not build a silently-partial index
        out = subprocess.run(
            ["node", JS_RUNNER, WEB_APP, "13", self._truncated_fixture()],
            capture_output=True, text=True, timeout=120)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("truncated", (out.stderr + out.stdout).lower())


class TestTriggerMap(unittest.TestCase):
    """trigger-map: classify entry points from the TRANSITIVE chain.

    The fixture encodes the trap that motivated the command: a trigger script
    (Handle Plan Change) that writes nothing relevant DIRECTLY but calls a
    script that flips the set-membership flag and creates a record. Classifying
    from direct writes marks it a non-issue; the chain marks it a gap."""

    FIXTURE_TM = os.path.join(ROOT, "tests", "fixtures", "micro_ddr_tm.xml")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "tm.db")
        build(cls.FIXTURE_TM, cls.db, label="tm")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_map(self, *extra):
        import io, contextlib
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["trigger-map", self.db,
                  "--inputs", "LI::Amount_c",
                  "--set-flag", "LI::Picked_flag",
                  "--table", "LI",
                  "--recompute", "Recompute Order", *extra])
        return out.getvalue()

    def test_calc_expansion_reaches_writable_inputs(self):
        out = self.run_map()
        # Amount_c is a calc -> expands to Rate + Qty; the flag is watched as-is
        self.assertIn("LI::Rate", out)
        self.assertIn("LI::Qty", out)
        self.assertIn("3 writable field(s)", out)

    def test_transitive_gap_detected(self):
        out = self.run_map()
        # The trap: Handle Plan Change writes only LI::Notes directly, but its
        # callee Auto Add Line flips the flag -> must surface as a gap with the
        # chain that proves it
        self.assertIn("Handle Plan Change -> Auto Add Line", out)
        line = next(l for l in out.splitlines() if "Handle Plan Change" in l
                    and "->" in l)
        self.assertIn("gap candidate", line)

    def test_covered_path_sees_recompute_in_chain(self):
        out = self.run_map()
        entries = out.split("## Entry points")[1]
        line = next(l for l in entries.splitlines()
                    if l.strip().startswith("[3] Toggle Line"))
        self.assertIn("recompute-in-chain", line)

    def test_record_op_creator_counted_as_mutator(self):
        out = self.run_map()
        self.assertIn("Auto Add Line", out.split("## Entry points")[0])
        self.assertIn("New Record/Request", out.split("## Entry points")[0])

    def test_downstream_trigger_is_non_issue_not_entry(self):
        out = self.run_map("--layouts", "Order%")
        entries = out.split("## Entry points")[1].split("##")[0]
        self.assertNotIn("Handle Total Change", entries)
        quiet = out.split("no path to a mutator")[1]
        self.assertIn("Handle Total Change", quiet)

    def test_leaf_only_field_spec_refused(self):
        from fm_ddr.cli import main
        with self.assertRaises(SystemExit):
            main(["trigger-map", self.db, "--inputs", "Amount_c"])


class TestSaXMLRefusal(unittest.TestCase):
    """Save a Copy as XML exports fmsonar does not read are refused by name,
    before parsing. Synthetic three-line headers, never a lab export.
    Guard: TestGuardProofs.test_version_check."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxml_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def header(self, name, **attrs):
        path = os.path.join(self.tmp, name)
        root = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        with open(path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<FMSaveAsXML {root}>\n'
                    '</FMSaveAsXML>\n')
        return path

    def refused(self, paths):
        with self.assertRaises(ValueError) as cm:
            build(paths, os.path.join(self.tmp, "out.db"))
        return str(cm.exception)

    def test_unsupported_version_22(self):
        p = self.header("v22.xml", version="2.2.3.0", Source="22.0.6",
                        File="Lab.fmp12", Has_DDR_INFO="True")
        msg = self.refused(p)
        self.assertIn("Save a Copy as XML", msg)
        self.assertIn("2.2.3.0", msg)
        self.assertIn("22.0.6", msg)
        self.assertIn("2.3.0.0 and 2.3.0.1", msg)

    def test_unknown_version(self):
        p = self.header("v9.xml", version="9.9.9.9", Source="99.0.1",
                        File="Lab.fmp12", Has_DDR_INFO="True")
        msg = self.refused(p)
        self.assertIn("9.9.9.9", msg)
        self.assertIn("99.0.1", msg)
        self.assertIn("2.3.0.0 and 2.3.0.1", msg)

    def test_version_quoted(self):
        # A stray space shows in the message, not as "2.3.0.1 , FileMaker"
        p = self.header("vs.xml", version="2.3.0.1 ", Source="26.1.1",
                        File="Lab.fmp12", Has_DDR_INFO="True")
        self.assertIn('(FMSaveAsXML version "2.3.0.1 ", FileMaker 26.1.1)',
                      self.refused(p))

    def test_message_lists_supported(self):
        # The refusal names every version in SUPPORTED, whatever it holds:
        # a version added to the table is in the message with no string to
        # edit.
        from fm_ddr import saxml
        p = self.header("v9.xml", version="9.9.9.9", Source="99.0.1",
                        File="Lab.fmp12", Has_DDR_INFO="True")
        with mock.patch.dict(saxml.SUPPORTED, {"2.4.0.0": "saxml_2.4.0.0.xml"}):
            msg = self.refused(p)
        self.assertIn("2.3.0.0, 2.3.0.1 and 2.4.0.0", msg)
        with mock.patch.dict(saxml.SUPPORTED, {"2.3.0.0": "saxml_2.3.0.0.xml"},
                             clear=True):
            msg = self.refused(p)
        self.assertIn("reads version 2.3.0.0 (FileMaker 26.0)", msg)
        self.assertNotIn("2.3.0.1", msg)

    def test_no_details(self):
        p = self.header("nodetails.xml", version="2.3.0.1", Source="26.1.1",
                        File="Lab.fmp12")
        self.assertIn("Include details", self.refused(p))

    def test_split_catalogs(self):
        p = self.header("split.xml", version="2.3.0.1", Source="26.1.1",
                        File="Lab.fmp12", Has_DDR_INFO="True",
                        binary_under_lo="False", split_catalogs="True")
        self.assertIn("default export only", self.refused(p))

    def test_mixed_formats(self):
        p = self.header("ok.xml", version="2.3.0.1", Source="26.1.1",
                        File="Lab.fmp12", Has_DDR_INFO="True",
                        binary_under_lo="False", split_catalogs="False")
        msg = self.refused([FIXTURE, p])
        self.assertIn("one source format per index", msg)
        self.assertIn(FIXTURE, msg)
        self.assertIn(p, msg)

    def test_refusal_leaves_no_temp_db(self):
        p = self.header("v22.xml", version="2.2.3.0", Source="22.0.6")
        self.refused(p)
        self.assertEqual([n for n in os.listdir(self.tmp)
                          if n.startswith(".fmsonar-build-")], [])


class TestPrologSniff(unittest.TestCase):
    """The format is read from the real root element, wherever the prolog
    ends: comments, a long XML declaration or a padded DOCTYPE never move it
    out of reach, and nothing before the root decides it. A DOCTYPE with an
    internal subset, or an ENTITY declaration anywhere in the prolog, is
    refused before any parse; a root that is neither FMPReport nor
    FMSaveAsXML is refused by name."""

    SAX = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_prolog_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def variant(self, src, name, before_root="", root_extra=""):
        """src with text inserted between the XML declaration and the root,
        and an attribute string added to the root tag."""
        with open(src, encoding="utf-8") as f:
            text = f.read()
        decl_end = text.index("?>") + 2
        root = text.index("<", decl_end)
        head, rest = text[:decl_end], text[root:]
        if root_extra:
            sp = rest.index(" ")
            rest = rest[:sp] + " " + root_extra + rest[sp:]
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + "\n" + before_root + rest)
        return path

    def test_padded_doctype_with_subset_refused(self):
        # The subset opens 9 KB into the DOCTYPE, past any fixed window
        p = self.variant(self.SAX, "dtd.xml", before_root=(
            '<!DOCTYPE FMSaveAsXML SYSTEM "' + "x" * 9000 + '" [\n'
            '<!ENTITY a "AAAA">\n]>\n'))
        with self.assertRaises(ValueError) as cm:
            build(p, os.path.join(self.tmp, "dtd.db"))
        self.assertIn("DTD", str(cm.exception))

    def test_entity_after_long_comment_refused(self):
        p = self.variant(self.SAX, "ent.xml", before_root=(
            "<!-- " + "y" * 20000 + " -->\n" +
            '<!DOCTYPE FMSaveAsXML SYSTEM "x.dtd" [<!ENTITY b "B">]>\n'))
        with self.assertRaises(ValueError) as cm:
            build(p, os.path.join(self.tmp, "ent.db"))
        self.assertIn("DTD", str(cm.exception))

    def test_root_beyond_8kb_builds(self):
        ref = os.path.join(self.tmp, "ref.db")
        build(self.SAX, ref, label="ref")
        p = self.variant(self.SAX, "late.xml",
                         before_root="<!-- " + "z" * 20000 + " -->\n")
        db = os.path.join(self.tmp, "late.db")
        build(p, db, label="ref")
        self.assertEqual(normalize(*read_sqlite(ref)), normalize(*read_sqlite(db)))

    def test_long_root_tag_builds(self):
        p = self.variant(self.SAX, "longtag.xml",
                         root_extra='Junk="' + "j" * 20000 + '"')
        db = os.path.join(self.tmp, "longtag.db")
        build(p, db, label="longtag")
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute(
            "SELECT source_format, ddr_version FROM ddr_run").fetchone(),
            ("saxml", "2.3.0.1"))
        conn.close()

    def test_comment_naming_saxml_in_a_ddr(self):
        p = self.variant(FIXTURE, "ddr_comment.xml", before_root=(
            '<!-- not a <FMSaveAsXML version="2.3.0.1" Has_DDR_INFO="True"> -->\n'))
        db = os.path.join(self.tmp, "ddr_comment.db")
        build(p, db, label="fixture")
        self.assertEqual(normalize(*read_sqlite(db)),
                         normalize(*read_sqlite(build_fixture_db(self.tmp))))

    def test_comment_naming_ddr_in_a_saxml(self):
        p = self.variant(self.SAX, "sax_comment.xml",
                         before_root="<!-- <FMPReport type=\"Report\"> -->\n")
        db = os.path.join(self.tmp, "sax_comment.db")
        build(p, db, label="c")
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute(
            "SELECT source_format FROM ddr_run").fetchone(), ("saxml",))
        conn.close()

    def test_junk_root_refused_by_name(self):
        p = os.path.join(self.tmp, "junk.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?>\n<!-- <FMSaveAsXML version="2.3.0.1" '
                    'Has_DDR_INFO="True"> -->\n<ObjectList><TableViewLayoutObject/>'
                    '</ObjectList>\n')
        with self.assertRaises(ValueError) as cm:
            build(p, os.path.join(self.tmp, "junk.db"))
        msg = str(cm.exception)
        self.assertIn("ObjectList", msg)
        self.assertIn("FMPReport", msg)
        self.assertIn("FMSaveAsXML", msg)

    def test_not_xml_refused(self):
        # expat's own reason and place
        p = os.path.join(self.tmp, "notes.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("just some notes\n")
        with self.assertRaises(ValueError) as cm:
            build(p, os.path.join(self.tmp, "notes.db"))
        msg = str(cm.exception)
        self.assertIn("not well-formed XML", msg)
        self.assertIn("syntax error at line 1, column 0", msg)
        self.assertIn("FMSaveAsXML", msg)

    def test_empty_file_refused(self):
        p = os.path.join(self.tmp, "empty.xml")
        open(p, "w").close()
        with self.assertRaises(ValueError) as cm:
            build(p, os.path.join(self.tmp, "empty.db"))
        self.assertIn("is empty", str(cm.exception))

    def test_junk_root_cli_one_line(self):
        import contextlib
        import io
        from fm_ddr.cli import main
        p = os.path.join(self.tmp, "junk2.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("<Calculation><Text>1</Text></Calculation>\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main(["build", p, "-o", os.path.join(self.tmp, "j2.db")])
        lines = [l for l in err.getvalue().splitlines() if l.startswith("error:")]
        self.assertEqual(len(lines), 1)
        self.assertIn("Calculation", lines[0])
        self.assertNotIn("Traceback", err.getvalue())

    def test_root_attributes_decoded_as_the_parser_does(self):
        # The sniff reads the root with the parser itself: a character
        # reference in File reads as files.name does
        from fm_ddr import saxml
        p = self.variant(self.SAX, "ref.xml")
        with open(p, encoding="utf-8") as f:
            text = f.read().replace('File="SaxLab.fmp12"', 'File="caf&#233;.fmp12"', 1)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        self.assertEqual(saxml.sniff(p)["file"], "café.fmp12")

    def test_long_prolog_comment_builds(self):
        # No prolog limit: a comment of any length before the root is read
        # past, as the parser reads it
        from fm_ddr import saxml
        p = self.variant(self.SAX, "long.xml",
                         before_root="<!-- " + "c" * (65 * 1024 * 1024) + " -->\n")
        self.assertEqual(saxml.sniff(p)["root"], "FMSaveAsXML")

    def test_utf16_be_saxml_builds(self):
        ref = os.path.join(self.tmp, "ref16.db")
        build(self.SAX, ref, label="ref")
        with open(self.SAX, encoding="utf-8") as f:
            text = f.read().replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
        p = os.path.join(self.tmp, "be.xml")
        with open(p, "wb") as f:
            f.write(b"\xfe\xff" + text.encode("utf-16-be"))
        db = os.path.join(self.tmp, "be.db")
        build(p, db, label="ref")
        self.assertEqual(normalize(*read_sqlite(ref)), normalize(*read_sqlite(db)))


class TestSaXMLLocale(unittest.TestCase):
    """The root's locale. Every export measured so far is
    English; a few rules read English step text. Another locale, or none,
    builds with one warning that points at those rules in COVERAGE.md;
    English builds silent."""

    SAX = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_locale_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, locale):
        import contextlib
        import io
        from fm_ddr.cli import main
        with open(self.SAX, encoding="utf-8") as f:
            text = f.read()
        new = f'locale="{locale}" ' if locale is not None else ""
        text = text.replace('locale="English" ', new, 1)
        path = os.path.join(self.tmp, "loc.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        both = io.StringIO()
        db = os.path.join(self.tmp, "loc.db")
        with contextlib.redirect_stdout(both), contextlib.redirect_stderr(both):
            main(["build", path, "-o", db])
        self.assertTrue(os.path.exists(db))
        return both.getvalue()

    def test_english_silent(self):
        out = self.run_cli("English")
        self.assertNotIn("English exports only", out)
        self.assertNotIn('locale "', out)

    def test_other_locale_warns(self):
        out = self.run_cli("German")
        self.assertEqual(out.count('locale "German"'), 1, out)
        self.assertIn('WARNING: loc.xml: locale "German"', out)
        # One list of the rules, in COVERAGE.md; the warning points at it
        # and names the consequences
        self.assertIn('COVERAGE.md ("Locale")', out)
        self.assertIn("v_dangling", out)
        self.assertIn("deleted script", out)
        self.assertNotIn("from file:", out)
        self.assertLess(out.index("WARNING:"), out.index("Done:"))
        with open(os.path.join(ROOT, "COVERAGE.md"), encoding="utf-8") as f:
            cov = f.read()
        self.assertIn("**Locale.**", cov)
        for rule in ("Go to Layout", "Go to Related Record", "New Window",
                     "from file:", "Perform Script", "Insert Text"):
            self.assertIn(rule, cov[cov.index("**Locale.**"):][:1200])

    def test_absent_locale_warns(self):
        out = self.run_cli(None)
        self.assertIn("loc.xml: no locale", out)

    def test_warning_names_no_internal_constant(self):
        self.assertNotIn("STEP_NAMES", self.run_cli("German"))

class TestSaXMLInputRobustness(unittest.TestCase):
    """Input edge cases: the same export twice, an
    empty File attribute, Pro's XMLv2_Summary, crafted nesting and OS errors
    each end in one clear message, never a traceback or a silent build."""

    SAX = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxrob_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refused(self, paths):
        with self.assertRaises(ValueError) as cm:
            build(paths, os.path.join(self.tmp, "out.db"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out.db")))
        return str(cm.exception)

    def copy(self, name, old=None, new=None):
        with open(self.SAX, encoding="utf-8") as f:
            text = f.read()
        if old is not None:
            self.assertEqual(text.count(old), 1)
            text = text.replace(old, new)
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_same_export_twice(self):
        msg = self.refused([self.SAX, self.SAX])
        self.assertIn("SaxLab.fmp12", msg)
        self.assertIn("once", msg)

    def test_stale_copy_of_the_same_file(self):
        msg = self.refused([self.SAX, self.copy("stale.xml")])
        self.assertIn("SaxLab.fmp12", msg)
        self.assertIn("stale.xml", msg)

    def test_empty_file_attribute(self):
        p = self.copy("nofile.xml", 'File="SaxLab.fmp12"', 'File=""')
        self.assertIn("File attribute", self.refused(p))

    def test_xmlv2_summary(self):
        p = os.path.join(self.tmp, "Summary.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<FMPReport type="XMLv2_Summary" version="2.3.0.1">'
                    '<File name="Lab" path="/x"><XML path="Lab.xml"/></File>'
                    '</FMPReport>\n')
        msg = self.refused(p)
        self.assertIn("XMLv2_Summary", msg)
        self.assertIn("Save a Copy as XML", msg)
        self.assertIn("each file", msg)
        self.assertNotIn("Database Design Report", msg)

    def test_crafted_depth_is_a_value_error(self):
        # A root the sniff would refuse, forced past it: the handler's
        # ancestor checks never index past the stack
        from fm_ddr import saxml
        p = os.path.join(self.tmp, "deep.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('<ObjectList><TableViewLayoutObject/><LayoutReference/>'
                    '<Calculation><Text>1</Text></Calculation></ObjectList>\n')
        fake = {"fmt": "saxml", "root": "FMSaveAsXML", "type": None,
                "version": "2.3.0.1", "source": "26.1.1", "has_details": True,
                "split_catalogs": False, "binary_under_lo": False,
                "file": "X.fmp12", "locale": "English"}
        with mock.patch.object(saxml, "sniff", lambda path: dict(fake)):
            self.assertIn("no FMSaveAsXML root", self.refused(p))

    def cli_error(self, path):
        import contextlib
        import io
        from fm_ddr.cli import main
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main(["build", path, "-o", os.path.join(self.tmp, "o.db")])
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2, err.getvalue())   # "Parsing ..." + error
        self.assertTrue(lines[1].startswith("error: "), lines[1])
        self.assertNotIn("Errno", lines[1])
        return lines[1]

    def test_missing_file_one_line(self):
        line = self.cli_error(os.path.join(self.tmp, "absent.xml"))
        self.assertIn("absent.xml", line)
        self.assertIn("no such file", line.lower())

    def test_directory_one_line(self):
        line = self.cli_error(self.tmp)
        self.assertIn("folder", line)


class TestSaXMLPrivacy(unittest.TestCase):
    """The change stamps of a SaXML export name developers (UUID userName
    and accountName). The index keeps them (the stamps are free in the
    index); the shareable HTML report never carries them. The
    fixtures are scrubbed, so the names are planted here."""

    USER, ACCOUNT = "Sentinel Dev Person", "sentinel.dev.account"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxpriv_")
        src = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        text = text.replace('userName="" accountName=""',
                            f'userName="{cls.USER}" accountName="{cls.ACCOUNT}"')
        path = os.path.join(cls.tmp, "named.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        cls.db = os.path.join(cls.tmp, "named.db")
        build(path, cls.db, label="named")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_index_keeps_the_stamps(self):
        conn = sqlite3.connect(self.db)
        n = conn.execute(
            "SELECT count(*) FROM entities WHERE "
            "json_extract(extra_json, '$.modified_by') = ? AND "
            "json_extract(extra_json, '$.modified_account') = ?",
            (self.USER, self.ACCOUNT)).fetchone()[0]
        conn.close()
        self.assertGreater(n, 0)

    def test_report_carries_no_names(self):
        from fm_ddr.report import report
        out = report(self.db, os.path.join(self.tmp, "named.html"))
        with open(out, encoding="utf-8") as f:
            html = f.read()
        self.assertGreater(len(html), 1000)
        for needle in (self.USER, self.ACCOUNT, "modified_by",
                       "modified_account"):
            self.assertNotIn(needle, html)

    def test_the_check_sees_a_leak(self):
        # The guard is report's column list: with extra_json embedded per
        # entity, the planted name reaches the HTML and the check above
        # would fail
        from fm_ddr import report as rep
        real = rep._collect

        def leaky(db_path):
            data = real(db_path)
            conn = sqlite3.connect(db_path)
            extra = dict(conn.execute("SELECT entity_id, extra_json FROM entities"))
            conn.close()
            for e in data["entities"]:
                e["x"] = extra.get(e["id"])
            return data

        with mock.patch.object(rep, "_collect", leaky):
            out = rep.report(self.db, os.path.join(self.tmp, "leak.html"))
        with open(out, encoding="utf-8") as f:
            self.assertIn(self.USER, f.read())


class TestSaXMLInfoPass(unittest.TestCase):
    """Pass 1 (_InfoPass) against an independent ElementTree read of each
    SaXML fixture: every counted number comes from the fixture, none is a
    constant."""

    @staticmethod
    def fixtures():
        from fm_ddr.saxml import SUPPORTED
        out = []
        for name in SUPPORTED.values():
            path = os.path.join(ROOT, "tests", "fixtures", name)
            if os.path.exists(path):
                out.append(path)
        return out + [LAB3]

    @staticmethod
    def reference(path):
        """DDR_INFO entries and the DDRREFs before it, read with ElementTree."""
        import xml.etree.ElementTree as ET
        root = ET.parse(path).getroot()
        entries = {}
        for section in root.find("DDR_INFO"):
            for ent in section.find("ObjectList"):
                if not ent.tag.startswith("_"):
                    continue
                pair = (ent.tag, ent.get("hash"))
                if ent.get("datatype") == "StepText":
                    entries.setdefault(pair, ("StepText", (ent.text or "").strip()))
                else:
                    frefs = []
                    for ch in ent.iter("Chunk"):
                        if ch.get("type") != "FieldRef":
                            continue
                        fr = ch.find("FieldReference")
                        to = fr.find("TableOccurrenceReference")
                        frefs.append(("FieldRef", fr.get("id"), fr.get("name"),
                                      fr.get("repetition"),
                                      None if to is None else to.get("id"),
                                      None if to is None else to.get("name")))
                    entries.setdefault(pair, (ent.get("datatype"), frefs))
        refs = []
        for top in ("Structure", "Metadata"):
            for r in root.find(top).iter("DDRREF"):
                refs.append((r.get("kind"), r.text, r.get("hash")))
        return entries, refs

    def test_fixtures_present(self):
        self.assertTrue(self.fixtures(), "no SaXML fixture in tests/fixtures")

    def test_info_pass(self):
        from fm_ddr.saxml import _InfoPass
        for path in self.fixtures():
            with self.subTest(fixture=os.path.basename(path)):
                info = _InfoPass().parse(path)
                entries, refs = self.reference(path)
                self.assertGreater(len(entries), 0)
                self.assertEqual(
                    set(info), set(entries),
                    f"{len(info)} pass-1 pairs vs {len(entries)} distinct "
                    "(key, hash) in DDR_INFO")
                # A DDRREF with an empty hash has no entry by design (lab v3:
                # GTRR's layout calc under LayoutReferenceContainer, the
                # chunk-less class the build counts); every other one hits
                missed = [r for r in refs if (r[1], r[2]) not in info and r[2]]
                self.assertTrue(all(r[0] == "ChunkList" for r in refs if not r[2]))
                self.assertGreater(len(refs), 0)
                self.assertEqual(missed, [], f"{len(missed)} of {len(refs)} "
                                 "DDRREFs in Structure/Metadata miss the dict")
                n_text = n_fref = 0
                for pair, (dt, want) in entries.items():
                    got = info[pair]
                    if dt == "StepText":
                        n_text += 1
                        self.assertEqual(got, want, pair[0])
                    else:
                        frefs = [c for c in got if c[0] == "FieldRef"]
                        n_fref += len(frefs)
                        self.assertEqual(frefs, want, pair[0])
                        self.assertTrue(all(c[0] in ("FieldRef", "CustomFunctionRef")
                                            for c in got))
                self.assertGreater(n_text, 0, "no StepText entry compared")
                self.assertGreater(n_fref, 0, "no FieldRef chunk compared")
                self.assertTrue(any(c[5] for v in info.values()
                                    if isinstance(v, list) for c in v
                                    if c[0] == "FieldRef"),
                                "no FieldRef carries a TO name")


def _saxml_fixtures():
    """The per-version fixtures and the lab v3 fixture."""
    from fm_ddr.saxml import SUPPORTED
    return [p for p in [os.path.join(ROOT, "tests", "fixtures", n)
                        for n in SUPPORTED.values()] + [LAB3] if os.path.exists(p)]


# A synthetic export: the root, then empty stand-ins for the sections every
# FileMaker file writes (saxml.REQUIRED_SECTIONS) and for the catalogs whose
# kind a body may refer to (saxml.REFERRED_SECTIONS), so the partial-export
# refusal lets it through; the test body adds the catalogs it is about.
SAXML_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<FMSaveAsXML version="2.3.0.0" Source="26.0.1" File="Syn.fmp12" '
              'locale="English" Has_DDR_INFO="True" binary_under_lo="False" split_catalogs="False">'
              '<Metadata/><Structure><AddAction>'
              '<FileAccessCatalog/><ExternalDataSourceCatalog/><ScriptCatalog/>'
              '<ValueListCatalog/><CustomFunctionsCatalog/>'
              '<BaseTableCatalog/><TableOccurrenceCatalog/>'
              '<FieldsForTables/><ThemeCatalog/><LayoutCatalog/><PrivilegeSetsCatalog/>'
              '<ExtendedPrivilegesCatalog/><AccountsCatalog/>')
SAXML_TAIL = '</AddAction></Structure></FMSaveAsXML>\n'


class TestSaXMLCatalogs(unittest.TestCase):
    """Phase 4 catalogs: the fixture build against an ElementTree count of
    the same file, and the shapes the lab fixture lacks as synthetic
    exports. Guard: TestGuardProofs.test_catalog_order."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxcat_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def synth(self, body):
        path = os.path.join(self.tmp, "syn.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + body + SAXML_TAIL)
        return path

    def build_db(self, path):
        db = os.path.join(self.tmp, "out.db")
        build(path, db, label="saxml")
        return sqlite3.connect(db)

    def test_fixture_counts(self):
        import xml.etree.ElementTree as ET
        for path in _saxml_fixtures():
            with self.subTest(fixture=os.path.basename(path)):
                root = ET.parse(path).getroot()
                aa = root.find("Structure/AddAction")
                want = {
                    "base_table": len(aa.findall("BaseTableCatalog/BaseTable")),
                    "field": len(aa.findall("FieldsForTables/FieldCatalog/ObjectList/Field")),
                    "table_occurrence": len(aa.findall("TableOccurrenceCatalog/TableOccurrence")),
                    "relationship": len(aa.findall("RelationshipCatalog/Relationship")),
                    "custom_function": len(aa.findall(".//CustomFunctionsCatalog//CustomFunction")),
                    "value_list": len(aa.findall("ValueListCatalog/ValueList")),
                    "privilege_set": len(aa.findall("PrivilegeSetsCatalog/ObjectList/PrivilegeSet")),
                    "extended_privilege": len(aa.findall(
                        "ExtendedPrivilegesCatalog/ObjectList/ExtendedPrivilege")),
                    "account": len(aa.findall("AccountsCatalog/ObjectList/Account")),
                    "custom_menu": len(aa.findall("CustomMenuCatalog/CustomMenu")),
                    "custom_menu_set": len(aa.findall(
                        "CustomMenuSetCatalog/ObjectList/CustomMenuSet")),
                    "theme": len(aa.findall("ThemeCatalog/Theme")),
                    "external_data_source": len(aa.findall(
                        "ExternalDataSourceCatalog/ExternalDataSource")),
                }
                conn = self.build_db(path)
                got = dict(conn.execute(
                    "SELECT kind, COUNT(*) FROM entities GROUP BY kind").fetchall())
                for kind, n in want.items():
                    self.assertEqual(got.get(kind, 0), n, kind)
                self.assertGreater(want["field"], 0)
                # every field hangs under its base table, by id
                orphans = conn.execute(
                    "SELECT COUNT(*) FROM entities f LEFT JOIN entities b"
                    " ON b.entity_id = f.parent_entity_id AND b.kind = 'base_table'"
                    " AND b.name = f.base_table WHERE f.kind = 'field'"
                    " AND b.entity_id IS NULL").fetchone()[0]
                self.assertEqual(orphans, 0)
                # the account name is the Authentication/AccountName text
                self.assertEqual(
                    sorted(r[0] for r in conn.execute(
                        "SELECT name FROM entities WHERE kind = 'account'")),
                    sorted(e.text for e in aa.findall(
                        "AccountsCatalog/ObjectList/Account/Authentication/AccountName")))
                # no CR survives into stored text
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE instr(calc_text, char(13))"
                    " OR instr(auto_enter, char(13)) OR instr(extra_json, char(13))"
                    ).fetchone()[0], 0)
                conn.close()

    def test_uuid_metadata(self):
        # Five keys from the entity's own UUID child
        import xml.etree.ElementTree as ET
        for path in _saxml_fixtures():
            with self.subTest(fixture=os.path.basename(path)):
                root = ET.parse(path).getroot()
                aa = root.find("Structure/AddAction")
                want = {}
                for fc in aa.findall("FieldsForTables/FieldCatalog"):
                    bt = fc.find("BaseTableReference").get("name")
                    for f in fc.findall("ObjectList/Field"):
                        u = f.find("UUID")
                        want[(bt, f.get("name"))] = (u.text, u.get("timestamp"),
                                                     int(u.get("modifications")))
                conn = self.build_db(path)
                got = {(bt, n): json.loads(ex) for bt, n, ex in conn.execute(
                    "SELECT base_table, name, extra_json FROM entities"
                    " WHERE kind = 'field'")}
                self.assertEqual(set(got), set(want))
                for k, (uuid, ts, n) in want.items():
                    self.assertEqual((got[k]["uuid"], got[k]["modified_at"],
                                      got[k]["modified_n"]), (uuid, ts, n), k)
                # every entity carries one (script steps too)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE"
                    " json_extract(extra_json, '$.uuid') IS NULL").fetchone()[0], 0)
                conn.close()
        p = self.synth(
            '<BaseTableCatalog><UUID modifications="9">CATALOG</UUID>'
            '<BaseTable id="1" name="A"><UUID modifications="3" userName="lab"'
            ' accountName="Admin" timestamp="2026-10-07T09:44:05">U-1</UUID>'
            '</BaseTable><BaseTable id="2" name="B"><UUID modifications="0"'
            ' userName="" accountName="">U-2</UUID></BaseTable></BaseTableCatalog>')
        conn = self.build_db(p)
        got = {n: json.loads(ex) for n, ex in conn.execute(
            "SELECT name, extra_json FROM entities")}
        self.assertEqual(got["A"], {"uuid": "U-1", "modified_at": "2026-10-07T09:44:05",
                                    "modified_by": "lab", "modified_account": "Admin",
                                    "modified_n": 3})
        self.assertEqual(got["B"], {"uuid": "U-2", "modified_n": 0})
        conn.close()

    def test_ddr_spelling(self):
        # Timestamp -> TimeStamp; a theme by display name, id in hex
        p = self.synth(
            '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
            '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
            '<ObjectList>'
            '<Field id="1" name="T" datatype="Timestamp" fieldtype="Normal"/>'
            '<Field id="2" name="N" datatype="Number" fieldtype="Normal"/>'
            '</ObjectList></FieldCatalog></FieldsForTables>'
            '<ThemeCatalog><Theme Display="Apex Blue" name="com.filemaker.theme.'
            'apex_blue" id="15"/></ThemeCatalog>')
        conn = self.build_db(p)
        self.assertEqual(dict(conn.execute(
            "SELECT name, data_type FROM entities WHERE kind = 'field'")),
            {"T": "TimeStamp", "N": "Number"})
        self.assertEqual(conn.execute(
            "SELECT fm_id, name FROM entities WHERE kind = 'theme'").fetchone(),
            ("0F", "Apex Blue"))
        conn.close()

    def test_catalog_order_refused(self):
        p = self.synth(
            '<BaseTableCatalog><BaseTable id="129" name="A"/></BaseTableCatalog>'
            '<FieldsForTables><FieldCatalog>'
            '<BaseTableReference id="999" name="Gone"/>'
            '<ObjectList><Field id="1" name="F" datatype="Text" fieldtype="Normal"/>'
            '</ObjectList></FieldCatalog></FieldsForTables>')
        with self.assertRaises(ValueError) as cm:
            build(p, os.path.join(self.tmp, "out.db"))
        msg = str(cm.exception)
        # What is wrong and what to do, no internal vocabulary
        self.assertIn("before the catalog", msg)
        self.assertIn("FieldCatalog", msg)
        self.assertIn("Re-export", msg)
        self.assertNotIn("assumption", msg)

    def test_external_table_occurrences(self):
        # Filled (Pro with the source open) and empty (FMDeveloperTool)
        p = self.synth(
            '<ExternalDataSourceCatalog>'
            '<ExternalDataSource name="Sat" type="FileMaker" id="1"><File>'
            '<UniversalPathList>file:Sat.fmp12</UniversalPathList></File>'
            '</ExternalDataSource></ExternalDataSourceCatalog>'
            '<TableOccurrenceCatalog>'
            '<TableOccurrence id="1" name="ext_Full" type="External">'
            '<BaseTableSourceReference type="ExternalDataSourceReference">'
            '<DataSourceReference id="1" name="Sat"/>'
            '<BaseTableReference id="130" name="Item"/>'
            '</BaseTableSourceReference></TableOccurrence>'
            '<TableOccurrence id="2" name="ext_Empty" type="External">'
            '<BaseTableSourceReference type="ExternalDataSourceReference">'
            '</BaseTableSourceReference></TableOccurrence>'
            '</TableOccurrenceCatalog>')
        conn = self.build_db(p)
        from fm_ddr.saxml import external_to_warning
        warning = external_to_warning(conn)
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT name, ext_file, base_table, extra_json FROM entities"
            " WHERE kind = 'table_occurrence'")}
        self.assertEqual(rows["ext_Full"], ("Sat", "Item", None))
        self.assertEqual(rows["ext_Empty"], (None, None, '{"external": true}'))
        ds = conn.execute("SELECT name, extra_json FROM entities"
                          " WHERE kind = 'external_data_source'").fetchone()
        self.assertEqual(ds[0], "Sat")
        self.assertEqual(json.loads(ds[1]), {"file": True, "file_name": "Sat.fmp12"})
        self.assertIn("1 external table occurrence(s) in this Save a Copy as XML "
                      "export carry no data source", warning)
        conn.close()

    def test_relationship_translation(self):
        # Operator table, relationship sort (no TO child: the side's TO)
        # and an empty predicate FieldReference (TO::<unknown>)
        p = self.synth(
            '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
            '<TableOccurrenceCatalog>'
            '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
            '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
            '</TableOccurrence>'
            '<TableOccurrence id="2" name="R" type="Local"><BaseTableSourceReference>'
            '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
            '</TableOccurrence></TableOccurrenceCatalog>'
            '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
            '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
            '</ObjectList></FieldCatalog></FieldsForTables>'
            '<RelationshipCatalog><Relationship id="1">'
            '<LeftTable cascadeCreate="False" cascadeDelete="True">'
            '<TableOccurrenceReference id="1" name="L"/></LeftTable>'
            '<RightTable cascadeCreate="True" cascadeDelete="False">'
            '<TableOccurrenceReference id="2" name="R"/>'
            '<SortSpecification value="True"><SortList><Sort type="Ascending">'
            '<PrimaryField><FieldReference id="1" name="K"/></PrimaryField>'
            '</Sort></SortList></SortSpecification></RightTable>'
            '<JoinPredicateList><JoinPredicate type="GreaterOrEqual">'
            '<LeftField><FieldReference id="1" name="K">'
            '<TableOccurrenceReference id="1" name="L"/></FieldReference></LeftField>'
            '<RightField><FieldReference id="0" name=""/></RightField>'
            '</JoinPredicate></JoinPredicateList></Relationship></RelationshipCatalog>')
        conn = self.build_db(p)
        name, extra = conn.execute("SELECT name, extra_json FROM entities"
                                   " WHERE kind = 'relationship'").fetchone()
        self.assertEqual(name, "L :: R")
        d = json.loads(extra)
        self.assertEqual(d["predicates"], [
            {"op": "GreaterThanOrEqualTo", "left": "L::K", "right": "R::"}])
        self.assertEqual([(s["side"], s["cascade_create"], s["cascade_delete"])
                          for s in d["sides"]],
                         [("left", False, True), ("right", True, False)])
        refs = sorted(conn.execute(
            "SELECT context, target_kind, target_raw, target_entity_id IS NOT NULL"
            " FROM refs").fetchall())
        self.assertEqual(refs, [
            ("join_predicate", "field", "L::K", 1),
            ("join_predicate", "field", "R::<unknown>", 0),
            ("join_predicate", "table_occurrence", "L", 1),
            ("join_predicate", "table_occurrence", "R", 1),
            ("sort", "field", "R::K", 1),
        ])
        conn.close()

    def test_predicate_through_empty_external_to(self):
        # A predicate field in another file (empty name through an external
        # TO with no data source): name and raw NULL, unresolved, outside
        # v_dangling. An empty predicate field with no TO at all keeps
        # <unknown>.
        p = self.synth(
            '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
            '<TableOccurrenceCatalog>'
            '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
            '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
            '</TableOccurrence>'
            '<TableOccurrence id="2" name="ext_X" type="External">'
            '<BaseTableSourceReference type="ExternalDataSourceReference">'
            '</BaseTableSourceReference></TableOccurrence></TableOccurrenceCatalog>'
            '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
            '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
            '</ObjectList></FieldCatalog></FieldsForTables>'
            '<RelationshipCatalog>'
            '<Relationship id="1">'
            '<LeftTable><TableOccurrenceReference id="1" name="L"/></LeftTable>'
            '<RightTable><TableOccurrenceReference id="2" name="ext_X"/></RightTable>'
            '<JoinPredicateList><JoinPredicate type="Equal">'
            '<LeftField><FieldReference id="1" name="K">'
            '<TableOccurrenceReference id="1" name="L"/></FieldReference></LeftField>'
            '<RightField><FieldReference id="4" name="">'
            '<TableOccurrenceReference id="2" name="ext_X"/></FieldReference></RightField>'
            '</JoinPredicate></JoinPredicateList></Relationship>'
            '<Relationship id="2">'
            '<LeftTable></LeftTable>'
            '<RightTable><TableOccurrenceReference id="1" name="L"/></RightTable>'
            '<JoinPredicateList><JoinPredicate type="Equal">'
            '<LeftField><FieldReference id="0" name=""/></LeftField>'
            '<RightField><FieldReference id="1" name="K">'
            '<TableOccurrenceReference id="1" name="L"/></FieldReference></RightField>'
            '</JoinPredicate></JoinPredicateList></Relationship>'
            '</RelationshipCatalog>')
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            conn = self.build_db(p)
        refs = sorted(conn.execute(
            "SELECT COALESCE(target_name, ''), COALESCE(target_to_name, ''),"
            " target_raw IS NULL, COALESCE(target_raw, ''),"
            " target_entity_id IS NOT NULL FROM refs"
            " WHERE context = 'join_predicate' AND target_kind = 'field'").fetchall())
        self.assertEqual(refs, [
            ("", "", 0, "<unknown>", 0),
            ("", "ext_X", 1, "", 0),
            ("K", "L", 0, "L::K", 1),
            ("K", "L", 0, "L::K", 1),
        ])
        self.assertEqual(conn.execute(
            "SELECT context, marker FROM v_dangling").fetchall(),
            [("join_predicate", "<unknown>")])
        conn.close()


class TestSaXMLRules(unittest.TestCase):
    """Phase 5 rule assertions on the lab fixture build (2.3.0.1)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxrules_")
        cls.path = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")
        cls.db = os.path.join(cls.tmp, "rules.db")
        build(cls.path, cls.db, label="rules")
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def test_nested_folder(self):
        (grp, parent, pkind), = self.q(
            "SELECT s.grp, p.name, p.kind FROM entities s JOIN entities p"
            " ON p.entity_id = s.parent_entity_id"
            " WHERE s.kind = 'script' AND s.name = 'Target'")
        self.assertEqual((grp, parent, pkind), ("Inner", "Inner", "script_group"))
        (outer,), = self.q(
            "SELECT p.name FROM entities f JOIN entities p"
            " ON p.entity_id = f.parent_entity_id"
            " WHERE f.kind = 'script_group' AND f.name = 'Inner'")
        self.assertEqual(outer, "Outer")
        # separators and markers open no entity
        self.assertEqual(self.q("SELECT COUNT(*) FROM entities WHERE name IN ('-', '--')"),
                         [(0,)])

    def test_menu_flags(self):
        # hidden="False" means in the menu; the lab hides two entries
        rows = dict(self.q(
            "SELECT name, extra_json FROM entities"
            " WHERE kind IN ('script', 'script_group')"))
        off = {n for n, ex in rows.items() if not json.loads(ex).get("in_menu")}
        self.assertEqual(off, {"Hidden Folder", "Hidden Script"})
        full = {n for n, ex in rows.items() if json.loads(ex).get("run_full_access")}
        self.assertEqual(full, {"Full Access Script"})
        # keys only when true
        for ex in rows.values():
            d = json.loads(ex)
            self.assertNotIn(False, (d.get("in_menu"), d.get("run_full_access")))

    def test_steps(self):
        import xml.etree.ElementTree as ET
        aa = ET.parse(self.path).getroot().find("Structure/AddAction")
        want = {}
        for sc in aa.findall("StepsForScripts/Script"):
            owner = sc.find("ScriptReference").get("name")
            for s in sc.findall("ObjectList/Step"):
                want[(owner, int(s.get("index")) + 1)] = (
                    s.get("id"), s.get("name"), s.get("enable") == "False")
        got = {(o, seq): (fm, st, bool(dis)) for o, seq, fm, st, dis in self.q(
            "SELECT p.name, s.seq, s.fm_id, s.step_type,"
            " COALESCE(json_extract(s.extra_json, '$.disabled'), 0)"
            " FROM entities s JOIN entities p ON p.entity_id = s.parent_entity_id"
            " AND p.kind = 'script' WHERE s.kind = 'script_step'")}
        self.assertEqual(got, want)
        self.assertEqual(min(k[1] for k in got), 1)
        # a step sits in its script's folder, as the DDR's does
        self.assertEqual(self.q(
            "SELECT COUNT(*) FROM entities s JOIN entities p"
            " ON p.entity_id = s.parent_entity_id"
            " WHERE s.kind = 'script_step' AND s.grp IS NOT p.grp"), [(0,)])
        # an action step (button) is not an entity
        self.assertEqual(self.q("SELECT COUNT(*) FROM entities WHERE kind = 'script_step'"),
                         [(len(want),)])

    def test_step_text(self):
        # The DDR_INFO text, stripped, CR as LF (the DDR side is LF)
        from fm_ddr.saxml import _InfoPass
        info = _InfoPass().parse(self.path)
        import xml.etree.ElementTree as ET
        aa = ET.parse(self.path).getroot().find("Structure/AddAction")
        want = {}
        for sc in aa.findall("StepsForScripts/Script"):
            owner = sc.find("ScriptReference").get("name")
            for s in sc.findall("ObjectList/Step"):
                ref = s.find("DDRREF")
                text = info[(ref.text.strip(), ref.get("hash"))]
                want[(owner, int(s.get("index")) + 1)] = (
                    text.replace("\r\n", "\n").replace("\r", "\n") or None)
        got = {(o, seq): t for o, seq, t in self.q(
            "SELECT p.name, s.seq, json_extract(s.extra_json, '$.step_text')"
            " FROM entities s JOIN entities p ON p.entity_id = s.parent_entity_id"
            " WHERE s.kind = 'script_step'")}
        self.assertEqual(got, want)
        self.assertTrue(any("\n" in t for t in got.values() if t))

    def caller_refs(self, context):
        return sorted(self.q(
            "SELECT s.seq, r.target_fm_id, r.target_name, r.target_raw,"
            " r.target_entity_id IS NOT NULL FROM refs r"
            " JOIN entities s ON s.entity_id = r.source_entity_id"
            " JOIN entities p ON p.entity_id = s.parent_entity_id"
            " WHERE p.name = 'Caller' AND r.context = ?", context))

    def test_perform_script(self):
        # Target resolves; the deleted Gone leaves only <unknown> (end-of-step
        # rule), target_name NULL, listed in v_dangling.
        self.assertEqual(self.caller_refs("perform_script"), [
            (2, "20", "Target", "Target", 1),
            (3, None, None, "<unknown>", 0),
        ])
        self.assertEqual(self.q(
            "SELECT context, source_name, marker FROM v_dangling"
            " WHERE context = 'perform_script'"),
            [("perform_script", "Perform Script", "<unknown>")])

    def test_step_parameters(self):
        # GTRR: the TO, the layout once (the new-window copy is skipped); a
        # deleted TO is a marker element -> <unknown> from the step text
        self.assertEqual(self.caller_refs("step_table"), [
            (5, "1065093", "CUS_SAL__Sale", "CUS_SAL__Sale", 1),
            (6, None, None, "<unknown>", 0),
            (7, "1065093", "CUS_SAL__Sale", "CUS_SAL__Sale", 1),
        ])
        self.assertEqual([r[:4] for r in self.caller_refs("go_to_layout")], [
            (5, "7", "Sale Entry", "Sale Entry"),
            (6, "7", "Sale Entry", "Sale Entry"),
            (7, "7", "Sale Entry", "Sale Entry"),
            (10, "6", "Watch List", "Watch List"),
        ])
        # Set Field: the target; a deleted TO reads <Table Missing>
        self.assertEqual(self.caller_refs("step_target"), [
            (8, "6", "Name_First", "CUS__Customer::Name_First", 1),
            (9, "9", None, "<Table Missing>", 0),
        ])
        self.assertEqual(sorted(self.q(
            "SELECT context, marker FROM v_dangling")), [
            ("perform_script", "<unknown>"),
            ("step_table", "<unknown>"),
            ("step_target", "<Table Missing>"),
        ])

    def test_disabled_step(self):
        (dis, text), = self.q(
            "SELECT json_extract(extra_json, '$.disabled'),"
            " json_extract(extra_json, '$.step_text') FROM entities"
            " WHERE kind = 'script_step' AND step_type = 'Set Variable'"
            " AND json_extract(extra_json, '$.disabled') = 1")
        self.assertEqual(dis, 1)
        self.assertTrue(text.startswith("//"))

    def test_layouts(self):
        # Folder, menu flags (hidden="False" is in the menu), context TO
        rows = {n: (k, g, p, json.loads(ex or "{}").get("in_menu")) for n, k, g, p, ex in self.q(
            "SELECT e.name, e.kind, e.grp, p.name, e.extra_json FROM entities e"
            " LEFT JOIN entities p ON p.entity_id = e.parent_entity_id"
            " WHERE e.kind IN ('layout', 'layout_group')")}
        self.assertEqual(rows["Lab Layouts"], ("layout_group", None, None, True))
        self.assertEqual(rows["Lab Layout"], ("layout", "Lab Layouts", "Lab Layouts", True))
        self.assertEqual({n for n, r in rows.items() if not r[3]},
                         {"Global_blank", "CUS__Customer_blank", "WAT__Watch_blank",
                          "SAL__Sale_blank"})
        self.assertEqual(len(rows), 9)   # the marker opens nothing
        ctx = dict(self.q(
            "SELECT s.name, r.target_raw FROM refs r JOIN entities s"
            " ON s.entity_id = r.source_entity_id WHERE r.context = 'layout_context'"
            " AND r.target_entity_id IS NOT NULL"))
        self.assertEqual(ctx["Lab Layout"], "CUS__Customer")
        self.assertEqual(len(ctx), 8)

    def lab_objects(self):
        return {r[0]: r[1:] for r in self.q(
            "SELECT json_extract(o.extra_json, '$.key'), o.name,"
            " json_extract(o.extra_json, '$.object_type'), p.kind, o.extra_json"
            " FROM entities o JOIN entities p ON p.entity_id = o.parent_entity_id"
            " JOIN entities l ON l.entity_id = COALESCE(p.parent_entity_id, 0)"
            " OR l.entity_id = p.entity_id"
            " WHERE o.kind = 'layout_object' AND l.kind = 'layout'"
            " AND l.name = 'Lab Layout'")}

    def test_layout_objects(self):
        import xml.etree.ElementTree as ET
        lc = ET.parse(self.path).getroot().find("Structure/AddAction/LayoutCatalog")
        want = len(lc.findall(".//LayoutObject"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM entities WHERE kind = 'layout_object'"),
                         [(want,)])
        objs = self.lab_objects()
        name, otype, pkind, ex = objs["2"]
        d = json.loads(ex)
        self.assertEqual((name, otype, pkind), ("nameFirst", "Edit Box", "layout"))
        self.assertEqual(d["hide_calc"], "IsEmpty ( CUS__Customer::ID )")
        self.assertEqual(d["tooltip_calc"], '"First name"')
        self.assertEqual(d["bounds"], "22,224,50,424")
        self.assertEqual(json.loads(objs["1"][3])["text"], "Name_First")
        # a nested object's parent is the portal
        self.assertEqual(objs["8"][2], "layout_object")
        # a button's label is its text, as the DDR's button text
        self.assertEqual(json.loads(objs["5"][3])["text"], "Run Target")

    def obj_refs(self, context):
        return sorted(self.q(
            "SELECT COALESCE(json_extract(o.extra_json, '$.key'), ''), r.target_name,"
            " r.target_raw, r.target_entity_id IS NOT NULL, r.trigger_event"
            " FROM refs r JOIN entities o ON o.entity_id = r.source_entity_id"
            " JOIN entities l ON l.kind = 'layout' AND l.name = 'Lab Layout'"
            " AND (o.parent_entity_id = l.entity_id OR o.entity_id = l.entity_id"
            " OR o.parent_entity_id IN (SELECT entity_id FROM entities"
            " WHERE parent_entity_id = l.entity_id))"
            " WHERE r.context = ?", context))

    def test_object_refs(self):
        # Field objects, the repetition suffix in target_raw as the DDR
        # writes it (its FieldObj/Name) and a bare name that resolves, the
        # portal, buttons, object and layout triggers
        self.assertEqual(self.obj_refs("layout_object"), [
            ("2", "Name_First", "CUS__Customer::Name_First", 1, None),
            ("4", "Phone_r", "CUS__Customer::Phone_r[3]", 1, None),
            ("8", "SoldOn", "CUS_SAL__Sale::SoldOn", 1, None),
        ])
        self.assertEqual(self.obj_refs("portal"),
                         [("7", "CUS_SAL__Sale", "CUS_SAL__Sale", 1, None)])
        self.assertEqual(self.obj_refs("perform_script"),
                         [("5", "Target", "Target", 1, None)])
        self.assertEqual(self.obj_refs("step_target"),
                         [("6", "Name_Last", "CUS__Customer::Name_Last", 1, None)])
        self.assertEqual(self.obj_refs("trigger")[1:], [
            ("2", "TRIGGER_onObjectModify", "TRIGGER_onObjectModify", 1,
             "OnObjectModify"),
        ])
        # the layout's own trigger (source key '' = the layout)
        self.assertEqual(self.obj_refs("trigger")[0],
                         ("", "TRIGGER_onRecordLoad", "TRIGGER_onRecordLoad", 1,
                          "OnRecordLoad"))
        # the hide calc's reference, from the object
        self.assertEqual(self.obj_refs("calc"),
                         [("2", "ID", "CUS__Customer::ID", 1, None)])
        # the Pro copy has no file trigger (removed before the export)
        self.assertEqual(self.q(
            "SELECT count(*) FROM refs WHERE source_entity_id IS NULL"), [(0,)])

    def test_file_trigger(self):
        # A file trigger has no source; only the 2.3.0.0 lab export keeps one
        db = os.path.join(self.tmp, "ft.db")
        build(os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.0.xml"), db,
              label="ft")
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute(
            "SELECT target_raw, trigger_event, target_entity_id IS NOT NULL FROM refs"
            " WHERE source_entity_id IS NULL").fetchall(),
            [("Export SaXML", "OnFirstWindowOpen", 1)])
        conn.close()

    def test_chunk_replay(self):
        # Every FieldRef / CustomFunctionRef chunk of DDR_INFO a DDRREF names
        # becomes a ref from the owner; field id kept as fm_id
        rows = self.q("SELECT r.source_kind, r.context, r.target_raw,"
                      " r.target_fm_id, r.target_entity_id IS NOT NULL FROM refs r"
                      " WHERE r.context IN ('calc', 'auto_enter', 'validation',"
                      " 'function_ref')")
        self.assertIn(("script_step", "calc", "SAL__Sale::Price", "9", 1), rows)
        self.assertIn(("field", "function_ref", "_Str_FullName", None, 1), rows)
        self.assertTrue(all(r[4] for r in rows))


SAXML_STEPS = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence>'
    '<TableOccurrence id="2" name="ext_X" type="External">'
    '<BaseTableSourceReference type="ExternalDataSourceReference">'
    '</BaseTableSourceReference></TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
    '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '<ScriptCatalog><Script id="1" name="Callee"/><Script id="2" name="Caller"/>'
    '</ScriptCatalog>'
    '<StepsForScripts><Script><ScriptReference id="2" name="Caller"/><ObjectList>'
    # 1 Perform Script, empty reference, cross-file by the step text
    '<Step index="0" id="1" name="Perform Script" enable="True">'
    '<DDRREF kind="StepText" hash="h1">_S1</DDRREF><ParameterValues>'
    '<Parameter type="List"><List name="From list" value="1"></List></Parameter>'
    '</ParameterValues></Step>'
    # 2 Perform Script, filled cross-file shape
    '<Step index="1" id="1" name="Perform Script" enable="True">'
    '<DDRREF kind="StepText" hash="h2">_S2</DDRREF><ParameterValues>'
    '<Parameter type="List"><List name="From list" value="1">'
    '<DataSourceReference id="1" name="Sat"/><ScriptReference id="1" name="Ping"/>'
    '</List></Parameter></ParameterValues></Step>'
    # 3 Set Field through an external TO with no data source: empty name
    '<Step index="2" id="76" name="Set Field" enable="True">'
    '<DDRREF kind="StepText" hash="h3">_S3</DDRREF><ParameterValues>'
    '<Parameter type="FieldReference"><FieldReference id="5" name="">'
    '<TableOccurrenceReference id="2" name="ext_X"/></FieldReference></Parameter>'
    '<Parameter type="Calculation"><Calculation><Calculation>'
    '<DDRREF kind="ChunkList" hash="c3">_S3_0</DDRREF><Text>L::K</Text>'
    '</Calculation></Calculation></Parameter></ParameterValues></Step>'
    # 4 Go to Layout, deleted layout
    '<Step index="3" id="6" name="Go to Layout" enable="True">'
    '<DDRREF kind="StepText" hash="h4">_S4</DDRREF><ParameterValues>'
    '<Parameter type="LayoutReferenceContainer"><LayoutReferenceContainer value="5">'
    '</LayoutReferenceContainer></Parameter></ParameterValues></Step>'
    '</ObjectList></Script></StepsForScripts>'
    '</AddAction></Structure>'
    '<DDR_INFO><Script><ObjectList>'
    '<_S1 hash="h1" datatype="StepText">Perform Script [ “Ping” from file:'
    ' “Sat” ]</_S1>'
    '<_S2 hash="h2" datatype="StepText">Perform Script [ “Ping” from file:'
    ' “Sat” ]</_S2>'
    '<_S3 hash="h3" datatype="StepText">Set Field [ ext_X::&lt;Field Missing&gt;;'
    ' L::K ]</_S3>'
    '<_S4 hash="h4" datatype="StepText">Go to Layout [ &lt;unknown&gt; ]</_S4>'
    '</ObjectList></Script><Calculation><ObjectList>'
    '<_S3_0 hash="c3" datatype="ChunkList"><ChunkList hash="c3">'
    '<Chunk type="FieldRef"><FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></Chunk>'
    '</ChunkList></_S3_0></ObjectList></Calculation></DDR_INFO>'
    '</FMSaveAsXML>\n')


class TestSaXMLStepRules(unittest.TestCase):
    """End-of-step and cross-file rules the lab fixture cannot show, on a
    synthetic export, and the DDR_INFO miss-rate refusal."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxsteps_")
        self.path = os.path.join(self.tmp, "steps.xml")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_STEPS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cross_file_and_deleted(self):
        import contextlib
        import io
        db = os.path.join(self.tmp, "out.db")
        with contextlib.redirect_stderr(io.StringIO()):
            build(self.path, db, label="steps")
        conn = sqlite3.connect(db)
        rows = sorted(conn.execute(
            "SELECT s.seq, r.context, r.target_name, r.target_to_name, r.target_raw,"
            " r.target_file, r.target_entity_id IS NOT NULL FROM refs r"
            " JOIN entities s ON s.entity_id = r.source_entity_id").fetchall())
        self.assertEqual(rows, [
            # filled or not, a cross-file call carries target_file
            (1, "perform_script", None, None, None, "Sat", 0),
            (2, "perform_script", "Ping", None, "Ping", "Sat", 0),
            # a field in another file: unresolved, no marker; v_dangling
            # lists it as unverifiable_xfile
            (3, "calc", "K", "L", "L::K", None, 1),
            (3, "step_target", None, "ext_X", None, None, 0),
            # a deleted layout: the step text's <unknown>
            (4, "go_to_layout", None, None, "<unknown>", None, 0),
        ])
        self.assertEqual(conn.execute(
            "SELECT context, marker FROM v_dangling").fetchall(),
            [("go_to_layout", "<unknown>"), ("unverifiable_xfile", "ext_X::<unknown>")])
        conn.close()

    def test_layout_unknown_only_for_the_layout(self):
        # GTRR with a deleted TO and no layout element: the <unknown> in the
        # step text is the table, so no go_to_layout row.
        # Guard: TestGuardProofs.test_layout_unknown.
        body = (
            '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
            '<TableOccurrenceCatalog>'
            '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
            '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
            '</TableOccurrence></TableOccurrenceCatalog>'
            '<ScriptCatalog><Script id="1" name="Caller"/></ScriptCatalog>'
            '<StepsForScripts><Script><ScriptReference id="1" name="Caller"/><ObjectList>'
            '<Step index="0" id="74" name="Go to Related Record" enable="True">'
            '<DDRREF kind="StepText" hash="g1">_G1</DDRREF><ParameterValues>'
            '<Parameter type="TableOccurrenceReference">'
            '<TableOccurrenceReference id="-1" name="&lt;Table Missing&gt;"/>'
            '</Parameter></ParameterValues></Step>'
            '<Step index="1" id="74" name="Go to Related Record" enable="True">'
            '<DDRREF kind="StepText" hash="g2">_G2</DDRREF><ParameterValues>'
            '<Parameter type="TableOccurrenceReference">'
            '<TableOccurrenceReference id="1" name="L"/></Parameter>'
            '</ParameterValues></Step>'
            '</ObjectList></Script></StepsForScripts>'
            '</AddAction></Structure>'
            '<DDR_INFO><Script><ObjectList>'
            '<_G1 hash="g1" datatype="StepText">Go to Related Record [ From table:'
            ' &lt;unknown&gt;; Using layout: &lt;Current Layout&gt; ]</_G1>'
            '<_G2 hash="g2" datatype="StepText">Go to Related Record [ From table:'
            ' “L”; Using layout: &lt;unknown&gt; ]</_G2>'
            '</ObjectList></Script></DDR_INFO></FMSaveAsXML>\n')
        path = os.path.join(self.tmp, "gtrr.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + body)
        db = os.path.join(self.tmp, "gtrr.db")
        build(path, db, label="gtrr")
        conn = sqlite3.connect(db)
        rows = sorted(conn.execute(
            "SELECT s.seq, r.context, r.target_raw FROM refs r"
            " JOIN entities s ON s.entity_id = r.source_entity_id").fetchall())
        conn.close()
        self.assertEqual(rows, [
            (1, "step_table", "<unknown>"),
            (2, "go_to_layout", "<unknown>"),
            (2, "step_table", "L"),
        ])

    def test_step_name_spelled_as_the_ddr(self):
        # The SaXML writes "Set Field By Name", the DDR "Set Field by Name";
        # a step's name is part of every ref key it sources. Guard: TestGuardProofs.test_step_names.
        body = (
            '<ScriptCatalog><Script id="1" name="S"/></ScriptCatalog>'
            '<StepsForScripts><Script><ScriptReference id="1" name="S"/><ObjectList>'
            '<Step index="0" id="147" name="Set Field By Name" enable="True">'
            '<DDRREF kind="StepText" hash="s1">_S1</DDRREF></Step>'
            '<Step index="1" id="89" name="Comment" enable="True">'
            '<DDRREF kind="StepText" hash="s2">_S2</DDRREF></Step>'
            '</ObjectList></Script></StepsForScripts>'
            '</AddAction></Structure>'
            '<DDR_INFO><Script><ObjectList>'
            '<_S1 hash="s1" datatype="StepText">Set Field By Name [ "x" ; 1 ]</_S1>'
            '<_S2 hash="s2" datatype="StepText"># c</_S2>'
            '</ObjectList></Script></DDR_INFO></FMSaveAsXML>\n')
        path = os.path.join(self.tmp, "sfbn.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + body)
        db = os.path.join(self.tmp, "sfbn.db")
        build(path, db, label="sfbn")
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT seq, step_type, name FROM entities"
            " WHERE kind = 'script_step' ORDER BY seq").fetchall()
        conn.close()
        self.assertEqual(rows, [(1, "Set Field by Name", "Set Field by Name"),
                                (2, "Comment", "Comment")])

    def test_step_text_names_the_step_as_the_ddr(self):
        # The step text opens with the step's name in the export's spelling;
        # the DDR's text uses its own, as step_type does.
        # A disabled step's text starts with "//  ".
        body = (
            '<ScriptCatalog><Script id="1" name="S"/></ScriptCatalog>'
            '<StepsForScripts><Script><ScriptReference id="1" name="S"/><ObjectList>'
            '<Step index="0" id="147" name="Set Field By Name" enable="True">'
            '<DDRREF kind="StepText" hash="s1">_S1</DDRREF></Step>'
            '<Step index="1" id="147" name="Set Field By Name" enable="False">'
            '<DDRREF kind="StepText" hash="s2">_S2</DDRREF></Step>'
            '</ObjectList></Script></StepsForScripts>'
            '</AddAction></Structure>'
            '<DDR_INFO><Script><ObjectList>'
            '<_S1 hash="s1" datatype="StepText">Set Field By Name [ "x" ; 1 ]</_S1>'
            '<_S2 hash="s2" datatype="StepText">//  Set Field By Name [ "y" ; 2 ]</_S2>'
            '</ObjectList></Script></DDR_INFO></FMSaveAsXML>\n')
        path = os.path.join(self.tmp, "sfbn2.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + body)
        db = os.path.join(self.tmp, "sfbn2.db")
        build(path, db, label="sfbn2")
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT seq, json_extract(extra_json, '$.step_text') FROM entities"
            " WHERE kind = 'script_step' ORDER BY seq").fetchall()
        conn.close()
        self.assertEqual(rows, [(1, 'Set Field by Name [ "x" ; 1 ]'),
                                (2, '//  Set Field by Name [ "y" ; 2 ]')])

    def test_truncated_ddr_info_refused(self):
        # Guard: an empty DDR_INFO (every reference unmatched) must refuse
        from unittest import mock
        from fm_ddr import saxml
        with mock.patch.object(saxml._InfoPass, "parse", lambda self, p: {}):
            with self.assertRaises(ValueError) as cm:
                build(self.path, os.path.join(self.tmp, "t.db"))
        self.assertIn("DDR_INFO holds no entry for any of its 5 references",
                      str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "t.db")))

    def test_absent_ddr_info_refused(self):
        # The section cut off entirely (a file truncated just before it)
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        start, end = text.index("<DDR_INFO>"), text.index("</DDR_INFO>") + 11
        cut = os.path.join(self.tmp, "cut.xml")
        with open(cut, "w", encoding="utf-8") as f:
            f.write(text[:start] + text[end:])
        with self.assertRaises(ValueError) as cm:
            build(cut, os.path.join(self.tmp, "c.db"))
        self.assertIn("DDR_INFO holds no entry for any of its 5 references",
                      str(cm.exception))


class TestSaXMLChunklessCalcs(unittest.TestCase):
    """Calculations whose chunk list is not in DDR_INFO are a class a valid
    export has (a GTRR's layout calc written twice, button parameters):
    however many, the export builds and the build says how many on stderr
    (the 10 percent refusal is gone)."""

    def test_lab_export_with_chunkless_calcs_builds(self):
        import contextlib
        import io
        import re
        tmp = tempfile.mkdtemp(prefix="fmddr_saxchunk_")
        try:
            src = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")
            with open(src, encoding="utf-8") as f:
                text = f.read()
            head, info = text.split("<DDR_INFO>", 1)
            entry = re.compile(r'<(_[^ >]+) hash="[^"]*" datatype="ChunkList">.*?</\1>',
                               re.S)
            total = len(entry.findall(info))
            drop = total // 3      # a third: far past the old 10 percent line
            info = entry.sub(lambda m, n=iter(range(total)): ""
                             if next(n) < drop else m.group(0), info)
            path = os.path.join(tmp, "chunkless.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(head + "<DDR_INFO>" + info)
            summary = build(path, os.path.join(tmp, "c.db"))
            self.assertGreater(summary["_refs"], 0)
            # The count is a warning of the build (TestSaXMLDDRInfoMisses)
            m = re.search(r"WARNING: (\d+) calculation\(s\) without reference list",
                          "\n".join(summary["_warnings"]))
            self.assertIsNotNone(m, summary["_warnings"])
            self.assertGreaterEqual(int(m.group(1)), drop)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# Four calculations: two write the same chunk-less (key, hash) pair, one has
# its list, and one writes with an empty hash a key whose list under another
# hash names no field or function (nothing lost)
SAXML_CHUNKLESS_PAIRS = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="A" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/><ObjectList>'
    '<Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
    + "".join(
        f'<Field id="{i}" name="C{i}" datatype="Number" fieldtype="Calculated">'
        f'<Calculation><DDRREF kind="ChunkList" hash="{h}">{k}</DDRREF>'
        '<Text>A::K</Text></Calculation></Field>'
        for i, k, h in ((2, "_LOST", ""), (3, "_LOST", ""), (4, "_KEPT", "H1"),
                        (5, "_NOREF", "")))
    + '</ObjectList></FieldCatalog></FieldsForTables>'
    '</AddAction></Structure><DDR_INFO><Field><ObjectList>'
    '<_KEPT hash="H1" datatype="ChunkList"><Chunk type="FieldRef">'
    '<FieldReference id="1" name="K"><TableOccurrenceReference id="1" name="A"/>'
    '</FieldReference></Chunk></_KEPT>'
    '<_NOREF hash="H2" datatype="ChunkList"><Chunk type="NoRef">1</Chunk></_NOREF>'
    '</ObjectList></Field></DDR_INFO></FMSaveAsXML>\n')


class TestSaXMLChunklessCount(unittest.TestCase):
    """A calculation without reference list is counted once however often
    the export writes its (key, hash) pair: a real export writes such a
    pair in several DDRREF elements. A key whose list under another hash
    names no field or custom function is not counted: nothing is lost
    there."""

    def test_distinct_pairs(self):
        tmp = tempfile.mkdtemp(prefix="fmddr_saxpairs_")
        try:
            path = os.path.join(tmp, "pairs.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(SAXML_HEAD + SAXML_CHUNKLESS_PAIRS)
            db = os.path.join(tmp, "pairs.db")
            summary = build(path, db)
            c = sqlite3.connect(db)
            row = c.execute("SELECT ddrinfo_refs, ddrinfo_missing_steps, "
                            "ddrinfo_missing_calcs FROM ddr_run").fetchone()
            c.close()
            self.assertEqual(row, (4, 0, 1))
            warn = "\n".join(summary["_warnings"])
            self.assertIn("WARNING: 1 calculation(s) without reference list", warn)
            self.assertIn("names no field or custom function\n  is not counted", warn)
            import contextlib
            import io
            from fm_ddr.cli import main
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                main(["list", tmp])
                main(["stats", db])
            self.assertIn("1 calculation(s) without reference list", out.getvalue())
            self.assertNotIn("1 calculations", out.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# A custom function used only inside a calculation whose chunk list the
# export does not write, next to a field used from a calculation that has
# its list
SAXML_CHUNKLESS_CF = (
    '<CustomFunctionsCatalog><ObjectList>'
    '<CustomFunction id="1" name="MyCF" access="All"><Calculation><Text>x</Text>'
    '</Calculation><ObjectList><Parameter name="x"/></ObjectList></CustomFunction>'
    '</ObjectList></CustomFunctionsCatalog>'
    + SAXML_CHUNKLESS_PAIRS.replace(
        '<Field id="2" name="C2" datatype="Number" fieldtype="Calculated">'
        '<Calculation><DDRREF kind="ChunkList" hash="">_LOST</DDRREF>'
        '<Text>A::K</Text>',
        '<Field id="2" name="C2" datatype="Number" fieldtype="Calculated">'
        '<Calculation><DDRREF kind="ChunkList" hash="">_LOST</DDRREF>'
        '<Text>MyCF ( A::K )</Text>'))


class TestSaXMLWhereTextCheck(unittest.TestCase):
    """`where` on an index with calculations without reference list: an
    object with no reference gets the full-text check inline, so a used
    custom function never reads as "0 reference(s)" alone; an object with
    references gets no note."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxwhere_")
        path = os.path.join(cls.tmp, "cf.xml")
        assert "MyCF ( A::K )" in SAXML_CHUNKLESS_CF
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_CHUNKLESS_CF)
        cls.db = os.path.join(cls.tmp, "cf.db")
        build(path, cls.db)
        cls.ddr = build_fixture_db(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def cli(*argv):
        import contextlib
        import io
        from fm_ddr.cli import main
        both = io.StringIO()
        with contextlib.redirect_stdout(both), contextlib.redirect_stderr(both):
            main(list(argv))
        return both.getvalue()

    def test_unreferenced_cf_shows_its_text_mentions(self):
        out = self.cli("where", self.db, "MyCF")
        head = next(l for l in out.splitlines() if l.startswith("# MyCF"))
        self.assertIn("(custom_function)", head)
        self.assertTrue(head.endswith(" 0 reference(s)"), head)
        tail = out[out.index("0 reference(s)"):]
        self.assertIn("1 calculation(s) without a reference list", tail)
        self.assertIn("1 text mention(s)", tail)
        hit = next(l for l in tail.splitlines() if l.startswith("field"))
        self.assertIn("C2", hit)
        # The function's own definition is not a mention
        self.assertNotIn("custom_function  MyCF", tail)

    def test_referenced_field_prints_no_note(self):
        out = self.cli("where", self.db, "A::K")
        self.assertNotIn("0 reference(s)", out)
        self.assertNotIn("reference list", out)

    def test_no_mention_says_so(self):
        out = self.cli("where", self.db, "C3")
        self.assertIn("0 reference(s)", out)
        self.assertIn("no text mention", out)

    def test_ddr_index_unchanged(self):
        out = self.cli("where", self.ddr, "Orphan Script")
        self.assertNotIn("reference list", out)
        self.assertNotIn("text mention", out)


class TestSaXMLUnusedCaveat(unittest.TestCase):
    """`stats` and the report's health page say once that unused candidates
    of a SaXML index may include objects used only where the export lists
    no reference: in calculations without reference list or through an
    external TO with no data source (unverifiable_xfile). Nothing on an
    index without either, nor on a DDR index."""

    CAVEAT = "Unused candidates may include objects used only"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxcav_")
        path = os.path.join(cls.tmp, "pairs.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_CHUNKLESS_PAIRS)
        fx = os.path.join(ROOT, "tests", "fixtures")
        cls.dbs = {}
        for name, src in (("chunkless", path),
                          ("xfile", os.path.join(fx, "saxml_xfile_fmdt_2.3.0.0.xml")),
                          ("clean", os.path.join(fx, "saxml_2.3.0.1.xml"))):
            cls.dbs[name] = os.path.join(cls.tmp, name + ".db")
            build(src, cls.dbs[name])
        cls.dbs["ddr"] = build_fixture_db(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def stats(self, name):
        import contextlib
        import io
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            main(["stats", self.dbs[name]])
        return out.getvalue()

    def test_stats(self):
        for name in ("chunkless", "xfile"):
            self.assertEqual(self.stats(name).count(self.CAVEAT), 1, name)
        for name in ("clean", "ddr"):
            self.assertNotIn(self.CAVEAT, self.stats(name), name)

    def test_report_health(self):
        from fm_ddr.report import report
        out = report(self.dbs["chunkless"], os.path.join(self.tmp, "r.html"))
        with open(out, encoding="utf-8") as f:
            html = f.read()
        self.assertIn(self.CAVEAT, html)
        self.assertIn("DATA.meta.unused_caveat", html)   # rendered by renderHealth
        out = report(self.dbs["ddr"], os.path.join(self.tmp, "d.html"))
        with open(out, encoding="utf-8") as f:
            self.assertNotIn(self.CAVEAT, f.read())


SAXML_EXPECTED = os.path.join(ROOT, "tests", "saxml_expected")


def saxml_normalize(db_path):
    """A fixture build as entity counts per kind and every ref as (source
    key, context, target_kind, target_raw, resolved, disabled,
    trigger_event); source keys from the harness normalize()."""
    entities, refs = read_sqlite(db_path)
    counts = {}
    for e in entities:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    rows = sorted(
        [src, ctx, tk, raw, 1 if tgt else 0, dis, trig]
        for src, ctx, tk, raw, tgt, _amb, dis, trig, _tf
        in normalize(entities, refs)["refs"])
    return {"entity_counts": counts, "refs": rows}


class TestSaXMLVersions(unittest.TestCase):
    """Per-version gate: every fixture named in
    SUPPORTED that exists builds and equals tests/saxml_expected/<version>.json.
    UPDATE_SNAPSHOT=1 rewrites the files (as test_snapshot).
    Guard: TestGuardProofs.test_version_gate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxver_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build_version(self, version):
        from fm_ddr.saxml import SUPPORTED
        import contextlib
        import io
        db = os.path.join(self.tmp, version + ".db")
        with contextlib.redirect_stdout(io.StringIO()):
            build(os.path.join(ROOT, "tests", "fixtures", SUPPORTED[version]), db,
                  label=version)
        return saxml_normalize(db)

    def present(self):
        from fm_ddr.saxml import SUPPORTED
        return [v for v, n in SUPPORTED.items()
                if os.path.exists(os.path.join(ROOT, "tests", "fixtures", n))]

    def test_expected_per_version(self):
        self.assertTrue(self.present())
        for version in self.present():
            with self.subTest(version=version):
                got = self.build_version(version)
                path = os.path.join(SAXML_EXPECTED, version + ".json")
                if os.environ.get("UPDATE_SNAPSHOT"):
                    os.makedirs(SAXML_EXPECTED, exist_ok=True)
                    with open(path, "w") as f:
                        json.dump(got, f, indent=1, sort_keys=True)
                    continue
                self.assertTrue(os.path.exists(path),
                                f"no {version}.json; run with UPDATE_SNAPSHOT=1")
                with open(path) as f:
                    self.assertEqual(got, json.load(f))

    def test_versions_agree(self):
        # The two 26 exports of the lab agree on everything but the custom
        # menus (Pro writes the standard menus, FMDeveloperTool none) and the
        # OnFirstWindowOpen export trigger, removed from the copy Pro exported
        a = self.build_version("2.3.0.0")
        b = self.build_version("2.3.0.1")
        kinds = set(a["entity_counts"]) | set(b["entity_counts"])
        differ = {k: (a["entity_counts"].get(k, 0), b["entity_counts"].get(k, 0))
                  for k in kinds
                  if a["entity_counts"].get(k, 0) != b["entity_counts"].get(k, 0)}
        self.assertEqual(differ, {"custom_menu": (0, 24)})
        refs_a = [tuple(r) for r in a["refs"]]
        refs_b = [tuple(r) for r in b["refs"]]
        self.assertEqual([r for r in refs_b if r not in refs_a], [])
        self.assertEqual([r for r in refs_a if r not in refs_b],
                         [("", "trigger", "script", "Export SaXML", 1, 0,
                           "OnFirstWindowOpen")])


LAB3 = os.path.join(ROOT, "tests", "fixtures", "saxml_lab3_2.3.0.0.xml")


class TestSaXMLLab3(unittest.TestCase):
    """Lab v3: the synthetic lab file with the layout and
    action kinds real solutions carry, exported by FMDeveloperTool
    26.0.1 and scrubbed by tools/saxml_fixture.py. One assertion per kind
    on the real shape, and the whole build against
    tests/saxml_expected/lab3_2.3.0.0.json (UPDATE_SNAPSHOT=1 rewrites it)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_lab3_")
        cls.db = os.path.join(cls.tmp, "lab3.db")
        cls.summary = build(LAB3, cls.db, label="lab3")
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def refs_of(self, object_type=None, kind="layout_object", name=None):
        """(source object type or kind, parent object type or kind, context,
        target_raw, resolved, trigger_event) of the refs from matching
        sources."""
        return sorted(self.q(
            "SELECT coalesce(json_extract(s.extra_json, '$.object_type'), s.kind),"
            " coalesce(json_extract(p.extra_json, '$.object_type'), p.kind),"
            " r.context, r.target_raw, r.target_entity_id IS NOT NULL,"
            " r.trigger_event"
            " FROM refs r JOIN entities s ON s.entity_id = r.source_entity_id"
            " LEFT JOIN entities p ON p.entity_id = s.parent_entity_id"
            " WHERE s.kind = ? AND (? IS NULL OR"
            "   json_extract(s.extra_json, '$.object_type') = ?)"
            " AND (? IS NULL OR s.name = ? OR p.name = ?)",
            kind, object_type, object_type, name, name, name))

    def children(self, object_type):
        return sorted(r[0] for r in self.q(
            "SELECT json_extract(c.extra_json, '$.object_type') FROM entities c"
            " JOIN entities p ON p.entity_id = c.parent_entity_id"
            " WHERE json_extract(p.extra_json, '$.object_type') = ?", object_type))

    def test_source(self):
        self.assertEqual(self.q("SELECT source_format, ddr_version, source_version"
                                " FROM ddr_run"), [("saxml", "2.3.0.0", "26.0.1")])
        # No warning: the GTRR's layout calc the export writes twice, once
        # with no hash, has its list on the other copy (nothing lost)
        self.assertEqual(self.summary["_warnings"], [])

    def test_button_bar_segments(self):
        self.assertEqual(self.children("Button Bar"), ["Button"] * 3)
        rows = [r for r in self.refs_of("Button") if r[1] == "Button Bar"]
        self.assertEqual(rows, [
            ("Button", "Button Bar", "go_to_layout", "Watch List", 1, None),
            ("Button", "Button Bar", "perform_script", "Target", 1, None),
            ("Button", "Button Bar", "step_target", "CUS__Customer::Name_Last", 1, None),
        ])

    def test_grouped_button(self):
        self.assertEqual(self.children("Grouped Button"), ["Rectangle", "Text"])
        self.assertEqual(self.refs_of("Grouped Button"), [
            ("Grouped Button", "layout", "calc", "CUS__Customer::ID", 1, None),
            ("Grouped Button", "layout", "perform_script", "Full Access Script", 1, None),
        ])
        step = self.q("SELECT json_extract(extra_json, '$.step_text') FROM entities"
                      " WHERE json_extract(extra_json, '$.object_type') = 'Grouped Button'")
        self.assertEqual(step, [("Perform Script [ “Full Access Script”; "
                                 "Parameter: CUS__Customer::ID ]",)])

    def test_popover_with_title_calc(self):
        self.assertEqual(self.children("Popover Button"), ["PopoverPanel"])
        self.assertEqual(self.refs_of("PopoverPanel"), [
            ("PopoverPanel", "Popover Button", "calc", "CUS__Customer::ID", 1, None)])
        self.assertIn(
            ("Edit Box", "PopoverPanel", "layout_object",
             "CUS__Customer::Address_CountryCode", 1, None),
            self.refs_of("Edit Box"))

    def test_tab_and_slide_panels(self):
        self.assertEqual(self.children("Tab Control"), ["Panel", "Panel"])
        self.assertEqual(self.children("Slide Control"), ["Panel", "Panel"])
        self.assertEqual(self.refs_of("Panel"), [
            ("Panel", "Tab Control", "calc", "CUS_SAL__Sale::ID", 1, None)])
        in_panels = [r[2:5] for r in self.refs_of() if r[1] == "Panel"]
        self.assertEqual(sorted(in_panels), [
            ("calc", "CUS__Customer::Name_First", 1),        # the placeholder
            ("layout_object", "CUS__Customer::ID", 1),
            ("layout_object", "CUS__Customer::Name_Full_c", 1),
            ("layout_object", "CUS__Customer::Name_Last", 1),
            ("layout_object", "CUS__Customer::Phone_r[2]", 1),
            ("perform_script", "Target", 1),
        ])

    def test_web_viewer(self):
        self.assertEqual([r[2:5] for r in self.refs_of("Web Viewer")], [
            ("calc", "CUS__Customer::ID", 1),
            ("calc", "CUS__Customer::Name_First", 1),
            ("calc", "CUS__Customer::Name_Last", 1),
            ("function_ref", "_Str_FullName", 1),
        ])

    def test_custom_menu_items(self):
        self.assertEqual([r[2:5] for r in self.refs_of(kind="custom_menu")], [
            ("go_to_layout", "Watch List", 1),
            ("perform_script", "Menu Target", 1),
        ])
        calc = self.q("SELECT calc_text FROM entities WHERE kind = 'custom_menu'")[0][0]
        self.assertIn("not IsEmpty ( Get ( AccountName ) )", calc)   # install calc
        self.assertEqual(self.q("SELECT name FROM entities"
                                " WHERE kind = 'custom_menu_set'"), [("Lab Menu Set",)])

    def test_sub_summary_break_field(self):
        # A sub-summary part's break field is a reference from the layout,
        # context part_break, on the SaXML side only (the DDR writes no Part
        # element)
        rows = [r[2:5] for r in self.refs_of(kind="layout", name="Lab Report")]
        self.assertIn(("part_break", "SAL__Sale::ID_Customer", 1), rows)
        self.assertEqual([r for r in rows if r[0] == "part_break"],
                         [("part_break", "SAL__Sale::ID_Customer", 1)])
        self.assertEqual(self.q("SELECT count(*) FROM refs"
                                " WHERE context = 'part_break'"), [(1,)])

    def test_custom_menu_keeps_every_item_step(self):
        # On SaXML a custom menu keeps the step text of every item, one line
        # each in item order (the DDR path keeps only the last item's,
        # unchanged)
        text = self.q("SELECT json_extract(extra_json, '$.step_text') FROM entities"
                      " WHERE kind = 'custom_menu'")[0][0]
        lines = text.split("\n")
        self.assertEqual(len(lines), 2, text)
        self.assertTrue(lines[0].startswith("Perform Script [ "), lines[0])
        self.assertIn("Menu Target", lines[0])
        self.assertEqual(lines[1], "Go to Layout [ “Watch List” (WAT__Watch) ]")
        hits = self.q("SELECT count(*) FROM text_index WHERE kind = 'custom_menu'"
                      " AND text_index MATCH '\"Menu Target\"'")
        self.assertEqual(hits, [(1,)])

    def test_break_field_through_a_deleted_to(self):
        # The shape FileMaker writes for a break field whose TO
        # is gone: an empty field name under <Table Missing>. A defect,
        # listed in v_dangling
        with open(LAB3, encoding="utf-8") as f:
            text = f.read()
        old = ('<FieldReference id="6" name="ID_Customer" '
               'UUID="0C1AF394-626B-4A1C-8A1F-67E678BEDC53">\n'
               '\t\t\t\t\t\t\t\t\t<TableOccurrenceReference id="1065092" '
               'name="SAL__Sale" UUID="0A348502-C291-4142-9545-F286D5161185">')
        at = text.index(old, text.index('<Part type="Leading Sub-summary"'))
        text = (text[:at] + '<FieldReference id="6" name="">\n'
                '<TableOccurrenceReference id="-1" name="&lt;Table Missing&gt;">'
                + text[at + len(old):])
        path = os.path.join(self.tmp, "gone.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        db = os.path.join(self.tmp, "gone.db")
        build(path, db, label="gone")
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT marker, marker_class FROM v_dangling"
                            " WHERE context = 'part_break'").fetchall()
        conn.close()
        self.assertEqual(rows, [("<Table Missing>::<unknown>", "<Table Missing>")])

    def test_validation_calc(self):
        rows = self.refs_of(kind="field", name="Name_Last")
        self.assertEqual(sorted({r[2:5] for r in rows}), [
            ("validation", "CUS__Customer::Name_First", 1),
            ("validation", "CUS__Customer::Name_Last", 1)])
        self.assertEqual(len(rows), 4)

    def test_triggers(self):
        got = self.q("SELECT layout_name, trigger_event, script_name FROM v_triggers"
                     " WHERE script_name LIKE 'TRIGGER_on%' ORDER BY 2")
        self.assertIn(("Lab Layout", "OnLayoutEnter", "TRIGGER_onLayoutEnter"), got)
        self.assertIn(("Lab Layout", "OnPanelSwitch", "TRIGGER_onPanelSwitch"), got)
        self.assertEqual(self.refs_of("Tab Control"), [
            ("Tab Control", "layout", "calc", "CUS__Customer::ID", 1, None),
            ("Tab Control", "layout", "trigger", "TRIGGER_onPanelSwitch", 1,
             "OnPanelSwitch")])

    def test_send_mail_calcs(self):
        rows = self.refs_of(kind="script_step", name="Lab Mailer")
        self.assertEqual([r[2:5] for r in rows], [
            ("calc", "CUS_SAL__Sale::SoldOn", 1),
            ("calc", "CUS__Customer::ID", 1),
            ("calc", "CUS__Customer::Name_First", 1),
            ("calc", "CUS__Customer::Name_Last", 1),
            ("function_ref", "_Str_FullName", 1),
        ])

    def test_gtrr_new_window(self):
        rows = self.refs_of(kind="script_step", name="Lab Related Window")
        self.assertEqual([r[2:5] for r in rows],
                         [("step_table", "WAT_SAL__Sale", 1)])

    def test_expected(self):
        got = saxml_normalize(self.db)
        path = os.path.join(SAXML_EXPECTED, "lab3_2.3.0.0.json")
        if os.environ.get("UPDATE_SNAPSHOT"):
            with open(path, "w") as f:
                json.dump(got, f, indent=1, sort_keys=True)
            self.skipTest("lab3 expected regenerated")
        with open(path) as f:
            self.assertEqual(got, json.load(f))


class TestSaXMLCrossFile(unittest.TestCase):
    """The filled cross-file shapes of a Pro 26.1.1 export made with the
    satellite file open (two-file lab): the external TO names its data
    source and base table, the cross-file Perform Script carries the data
    source, and a calc field through the external TO keeps the field name,
    so nothing of it is dangling."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxxf_")
        cls.db = os.path.join(cls.tmp, "xf.db")
        cls.out = io.StringIO()
        with contextlib.redirect_stdout(cls.out):
            build(os.path.join(ROOT, "tests", "fixtures",
                               "saxml_xfile_2.3.0.1.xml"), cls.db, label="xf")
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql):
        return self.conn.execute(sql).fetchall()

    def test_external_to_filled(self):
        self.assertEqual(self.q(
            "SELECT name, ext_file, base_table FROM entities"
            " WHERE kind = 'table_occurrence' ORDER BY name"),
            [("Main", None, "Main"), ("ext_Item", "Sat", "Item")])
        self.assertNotIn("carry no data source", self.out.getvalue())

    def test_cross_file_perform_script(self):
        self.assertEqual(self.q(
            "SELECT s.kind, s.name, r.target_raw, r.target_file"
            " FROM refs r JOIN entities s ON s.entity_id = r.source_entity_id"
            " WHERE r.context = 'perform_script'"),
            [("script_step", "Perform Script", "Ping", "Sat")])

    def test_cross_file_calc_field(self):
        self.assertEqual(self.q(
            "SELECT s.name, r.target_raw, r.target_to_name, r.target_name"
            " FROM refs r JOIN entities s ON s.entity_id = r.source_entity_id"
            " WHERE r.context = 'calc'"),
            [("SatName", "ext_Item::Name", "ext_Item", "Name")])
        self.assertEqual(self.q(
            "SELECT s.name, r.target_raw FROM refs r"
            " JOIN entities s ON s.entity_id = r.source_entity_id"
            " WHERE r.context = 'step_target'"),
            [("Set Field", "ext_Item::Name")])
        self.assertEqual(self.q("SELECT count(*) FROM v_dangling"), [(0,)])


class TestSaXMLCrossFileFMDT(unittest.TestCase):
    """Both files of the two-file lab exported with FMDeveloperTool 26.0.1
    and built together: one index, one files row each,
    the data source maps to the satellite's row, and still nothing resolves
    across the files, because the main file's export leaves every
    reference into the other file empty. The filled shape needs a Pro
    export with every file open (TestSaXMLCrossFile)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxxfdt_")
        cls.db = os.path.join(cls.tmp, "both.db")
        fx = os.path.join(ROOT, "tests", "fixtures")
        cls.summary = build([os.path.join(fx, "saxml_xfile_fmdt_2.3.0.0.xml"),
                             os.path.join(fx, "saxml_xfile_sat_fmdt_2.3.0.0.xml")],
                            cls.db, label="both")
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql):
        return self.conn.execute(sql).fetchall()

    def test_two_files_one_index(self):
        self.assertEqual(self.q("SELECT name FROM files ORDER BY file_id"),
                         [("Main.fmp12",), ("Sat.fmp12",)])
        self.assertEqual(self.q("SELECT ds_name, target_file_name FROM v_data_sources"),
                         [("Sat", "Sat.fmp12")])

    def test_nothing_resolves_across_files(self):
        self.assertEqual(self.q(
            "SELECT name, ext_file, base_table FROM entities"
            " WHERE kind = 'table_occurrence' AND name = 'ext_Item'"),
            [("ext_Item", None, None)])
        self.assertEqual(self.q(
            "SELECT r.target_raw, r.target_file, r.target_entity_id FROM refs r"
            " WHERE r.context = 'perform_script'"), [(None, "Sat", None)])
        self.assertEqual(self.q(
            "SELECT count(*) FROM refs r JOIN entities t"
            " ON t.entity_id = r.target_entity_id JOIN files f ON f.file_id = t.file_id"
            " JOIN entities s ON s.entity_id = r.source_entity_id"
            " WHERE s.file_id <> t.file_id"), [(0,)])
        self.assertEqual(self.q("SELECT context FROM v_dangling"),
                         [("unverifiable_xfile",)] * 2)


class TestSaXMLMixed26(unittest.TestCase):
    """The FileMaker 26 versions share one rule set: a Pro 26.1 export of
    the main file (2.3.0.1, filled cross-file slots) and an FMDeveloperTool
    26.0 export of the satellite (2.3.0.0) build into one index, resolve
    across the files, and record each file's version."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxmix_")
        cls.db = os.path.join(cls.tmp, "mix.db")
        fx = os.path.join(ROOT, "tests", "fixtures")
        build([os.path.join(fx, "saxml_xfile_2.3.0.1.xml"),
               os.path.join(fx, "saxml_xfile_sat_fmdt_2.3.0.0.xml")], cls.db, label="mix")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_resolves_across_files(self):
        self.assertEqual(self.q(
            "SELECT r.context, f.name FROM refs r"
            " JOIN entities t ON t.entity_id = r.target_entity_id"
            " JOIN files f ON f.file_id = t.file_id"
            " JOIN entities s ON s.entity_id = r.source_entity_id"
            " WHERE s.file_id <> t.file_id ORDER BY 1"), [
            ("calc", "Sat.fmp12"), ("join_predicate", "Sat.fmp12"),
            ("perform_script", "Sat.fmp12"), ("step_target", "Sat.fmp12")])
        self.assertEqual(self.q("SELECT count(*) FROM v_dangling"), [(0,)])

    def test_version_per_file(self):
        self.assertEqual(self.q(
            "SELECT name, format_version, source_version FROM files ORDER BY file_id"),
            [("Main.fmp12", "2.3.0.1", "26.1.1"), ("Sat.fmp12", "2.3.0.0", "26.0.1")])

    def test_run_names_both_versions(self):
        # ddr_run holds the build, not its first export: a mixed build
        # names both versions
        self.assertEqual(self.q("SELECT source_format, ddr_version, source_version"
                                " FROM ddr_run"),
                         [("saxml", "2.3.0.0, 2.3.0.1", "26.0.1, 26.1.1")])

    def test_stats_lists_the_files(self):
        import contextlib
        import io
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(["stats", self.db])
        self.assertEqual(out.getvalue().splitlines()[0],
                         "source: saxml 2.3.0.0, 2.3.0.1 (FileMaker 26.0.1, 26.1.1)")
        self.assertIn("  Main.fmp12: saxml 2.3.0.1 (FileMaker 26.1.1)", out.getvalue())
        self.assertIn("  Sat.fmp12: saxml 2.3.0.0 (FileMaker 26.0.1)", out.getvalue())
        # A DDR index (no per-file version) prints no file list
        ddr = build_fixture_db(self.tmp)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(["stats", ddr])
        self.assertNotIn(": saxml", out.getvalue())


SAXML_EMPTY_EXTERNAL = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence>'
    '<TableOccurrence id="2" name="ext_X" type="External">'
    '<BaseTableSourceReference type="ExternalDataSourceReference">'
    '</BaseTableSourceReference></TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
    '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
    '<Field id="2" name="C" datatype="Text" fieldtype="Calculated">'
    '<Calculation><Text>ext_X::&lt;Field Missing&gt;</Text></Calculation></Field>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '</AddAction></Structure><DDR_INFO></DDR_INFO></FMSaveAsXML>\n')


class TestSaXMLEmptyExternalWarning(unittest.TestCase):
    """The build warning on a SaXML export whose external TOs carry no
    data source (the FMDeveloperTool route). A warning: the build stands.
    One block, printed once by build() before the CLI's "Done:"; the
    remedy names only the measured route."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxwarn_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_build(self, path):
        import contextlib
        import io
        from fm_ddr.cli import main
        err = io.StringIO()
        db = os.path.join(self.tmp, "w.db")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            main(["build", path, "-o", db])
        self.assertTrue(os.path.exists(db))
        return err.getvalue()

    def test_warns(self):
        path = os.path.join(self.tmp, "fmdt.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_EMPTY_EXTERNAL)
        err = self.run_build(path)
        self.assertIn(
            "WARNING: 1 external table occurrence(s) in this Save a Copy as XML "
            "export carry no data source and no base table.", err)
        self.assertIn("TO::<Field Missing>: 1 object(s) here show in v_dangling "
                      "as unverifiable_xfile, not broken_calc:", err)
        self.assertIn("the field may exist in the other file or may be gone; "
                      "this export cannot tell.", err)
        # The cause is measured per route, never attributed to this file
        self.assertIn("The export left these table occurrences empty "
                      "(FMDeveloperTool does this; measured: FileMaker Pro with "
                      "the files open fills them;\n  Server not yet measured).", err)
        self.assertNotIn("This is how FMDeveloperTool exports", err)
        self.assertNotIn("FileMaker Pro or FileMaker Server", err)

    def test_one_block_before_done(self):
        # The fact is said once, and every warning (this one and the low
        # resolution one) comes before "Done:" on the combined output
        import contextlib
        import io
        from fm_ddr.cli import main
        path = os.path.join(self.tmp, "fmdt.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_EMPTY_EXTERNAL)
        both = io.StringIO()
        with contextlib.redirect_stdout(both), contextlib.redirect_stderr(both):
            main(["build", path, "-o", os.path.join(self.tmp, "b.db")])
        out = both.getvalue()
        self.assertEqual(out.count("carry no data source"), 1, out)
        self.assertEqual(out.count("external table occurrence"), 1, out)
        self.assertLess(out.rindex("WARNING:"), out.index("Done:"), out)

    def test_low_resolution_before_done(self):
        import contextlib
        import io
        from fm_ddr.cli import main
        both = io.StringIO()
        with contextlib.redirect_stdout(both), contextlib.redirect_stderr(both):
            main(["build", FIXTURE_C, "-o", os.path.join(self.tmp, "c.db")])
        out = both.getvalue()
        self.assertEqual(out.count("WARNING:"), 1, out)
        self.assertLess(out.index("references resolved"), out.index("Done:"), out)

    def test_silent_when_filled(self):
        err = self.run_build(os.path.join(ROOT, "tests", "fixtures",
                                          "saxml_xfile_2.3.0.1.xml"))
        self.assertNotIn("external table occurrence", err)

    def test_counts_field_objects(self):
        # A field object through such a TO is counted with the calculations
        path = os.path.join(self.tmp, "lay.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_LAYOUTS)
        err = self.run_build(path)
        self.assertIn("1 field reference(s) from layout objects or script steps "
                      "through them name no field and show in v_dangling as "
                      "unverifiable_xfile", err)


SAXML_XFILE_CLASS = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence>'
    '<TableOccurrence id="3" name="a_ext_X" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence>'
    '<TableOccurrence id="2" name="ext_X" type="External">'
    '<BaseTableSourceReference type="ExternalDataSourceReference">'
    '</BaseTableSourceReference></TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
    '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
    # only through the empty external TO, twice: unverifiable
    '<Field id="2" name="Xonly" datatype="Text" fieldtype="Calculated">'
    '<Calculation><Text>ext_X::&lt;Field Missing&gt; &amp; ext_X::&lt;Field Missing&gt;'
    '</Text></Calculation></Field>'
    # one bare marker next to it: a defect
    '<Field id="3" name="Xbare" datatype="Text" fieldtype="Calculated">'
    '<Calculation><Text>ext_X::&lt;Field Missing&gt; &amp; &lt;Field Missing&gt;'
    '</Text></Calculation></Field>'
    # a local TO: a defect
    '<Field id="4" name="Local" datatype="Text" fieldtype="Calculated">'
    '<Calculation><Text>L::&lt;Field Missing&gt;</Text></Calculation></Field>'
    # a local TO whose name ends with the external one: a defect
    '<Field id="5" name="Suffix" datatype="Text" fieldtype="Calculated">'
    '<Calculation><Text>a_ext_X::&lt;Field Missing&gt;</Text></Calculation></Field>'
    # through the external TO plus a missing function: the function stays a defect
    '<Field id="6" name="Xfunc" datatype="Text" fieldtype="Calculated">'
    '<Calculation><Text>ext_X::&lt;Field Missing&gt; &amp; &lt;Function Missing&gt;'
    '</Text></Calculation></Field>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '</AddAction></Structure><DDR_INFO></DDR_INFO></FMSaveAsXML>\n')


class TestSaXMLUnverifiableXfile(unittest.TestCase):
    """1.15.0: an entity whose every <Field Missing> follows an
    empty external TO (the FMDeveloperTool route) is marked
    extra_json.xfile_marker and v_dangling lists that row as
    unverifiable_xfile, not broken_calc. A bare or local-TO marker keeps the
    entity a defect; other markers stay broken_calc. Guard: TestGuardProofs.test_mark_xfile."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxxfc_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dangling(self, path):
        import contextlib
        import io
        db = os.path.join(self.tmp, "x.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build(path, db, label="x")
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT context, source_kind, source_name, marker FROM v_dangling"
                " ORDER BY source_name, context, marker").fetchall()
            marked = conn.execute(
                "SELECT name FROM entities"
                " WHERE json_extract(extra_json, '$.xfile_marker') = 1"
                " ORDER BY name").fetchall()
        finally:
            conn.close()
        return rows, marked

    def test_fmdt_fixture(self):
        # The two-file lab exported by FMDeveloperTool: the calc field
        # through the empty external TO reads ext_Item::<Field Missing>; the
        # Set Field target through it names no field, live or deleted alike
        # (the DDR marks a deleted one <Field Missing>)
        rows, marked = self.dangling(os.path.join(
            ROOT, "tests", "fixtures", "saxml_xfile_fmdt_2.3.0.0.xml"))
        self.assertEqual(rows, [
            ("unverifiable_xfile", "field", "SatName", "<Field Missing>"),
            ("unverifiable_xfile", "script_step", "Set Field", "ext_Item::<unknown>")])
        self.assertEqual(marked, [("SatName",)])

    def test_pro_fixture_unmarked(self):
        rows, marked = self.dangling(os.path.join(
            ROOT, "tests", "fixtures", "saxml_xfile_2.3.0.1.xml"))
        self.assertEqual((rows, marked), ([], []))

    def test_rule(self):
        path = os.path.join(self.tmp, "c.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_XFILE_CLASS)
        rows, marked = self.dangling(path)
        self.assertEqual(marked, [("Xfunc",), ("Xonly",)])
        self.assertEqual(rows, [
            ("broken_calc", "field", "Local", "<Field Missing>"),
            ("broken_calc", "field", "Suffix", "<Field Missing>"),
            ("broken_calc", "field", "Xbare", "<Field Missing>"),
            ("broken_calc", "field", "Xfunc", "<Function Missing>"),
            ("unverifiable_xfile", "field", "Xfunc", "<Field Missing>"),
            ("unverifiable_xfile", "field", "Xonly", "<Field Missing>"),
        ])


SAXML_LAYOUTS = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence>'
    '<TableOccurrence id="2" name="ext_X" type="External">'
    '<BaseTableSourceReference type="ExternalDataSourceReference">'
    '</BaseTableSourceReference></TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
    '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '<ScriptCatalog><Script id="1" name="S"/></ScriptCatalog>'
    '<ValueListCatalog><ValueList id="1" name="V"><Source value="Custom"/>'
    '</ValueList></ValueListCatalog>'
    '<LayoutCatalog>'
    '<Layout id="9" name="-" isSeparatorItem="True"><Options hidden="False">0</Options>'
    '</Layout>'
    '<Layout id="1" name="Lay">'
    '<TableOccurrenceReference id="-1" name="&lt;Table Missing&gt;"/>'
    '<PartsList><Part type="Body"><ObjectList>'
    # 1 field object with a value list
    '<LayoutObject id="1" type="Edit Box" name=""><Field>'
    '<FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference>'
    '<Display Style="0"><ValueListReference id="1" name="V"/></Display></Field>'
    '</LayoutObject>'
    # 2 a field in another file: empty name through an external TO
    '<LayoutObject id="2" type="Edit Box" name=""><Field>'
    '<FieldReference id="5" name="" repetition="1">'
    '<TableOccurrenceReference id="2" name="ext_X"/></FieldReference></Field>'
    '</LayoutObject>'
    # 3 no TO child
    '<LayoutObject id="3" type="Edit Box" name=""><Field>'
    '<FieldReference id="6" name="Gone" repetition="1"/></Field></LayoutObject>'
    # 4 a deleted TO: the marker element
    '<LayoutObject id="4" type="Edit Box" name=""><Field>'
    '<FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="-1" name="&lt;Table Missing&gt;"/>'
    '</FieldReference></Field></LayoutObject>'
    # 5 a deleted field through a local TO
    '<LayoutObject id="5" type="Edit Box" name=""><Field>'
    '<FieldReference id="7" name="" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></Field>'
    '</LayoutObject>'
    # 6 portal: sort and filter
    '<LayoutObject id="6" type="Portal" name=""><Portal>'
    '<TableOccurrenceReference id="1" name="L"/>'
    '<SortSpecification value="True"><SortList><Sort type="Ascending">'
    '<PrimaryField><FieldReference id="1" name="K">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></PrimaryField>'
    '</Sort></SortList></SortSpecification>'
    '<Calculation><DDRREF kind="ChunkList" hash="p1">_P1</DDRREF><Text>L::K</Text>'
    '</Calculation><ObjectList/></Portal></LayoutObject>'
    # 7 portal whose TO is gone
    '<LayoutObject id="7" type="Portal" name=""><Portal><ObjectList/></Portal>'
    '</LayoutObject>'
    # 8 grouped button with a script
    '<LayoutObject id="8" type="Grouped Button" name=""><GroupedButton><action>'
    '<Options>4</Options><ScriptReference id="1" name="S"/></action>'
    '</GroupedButton></LayoutObject>'
    # 9 button whose script is gone; trigger whose script is gone
    '<LayoutObject id="9" type="Button" name=""><Button><action>'
    '<Options>4</Options></action></Button>'
    '<ScriptTriggers><ScriptTrigger id="1" action="OnObjectEnter"/></ScriptTriggers>'
    '</LayoutObject>'
    '</ObjectList></Part></PartsList>'
    '<Options hidden="True">0</Options>'
    '</Layout></LayoutCatalog>'
    '</AddAction></Structure>'
    '<DDR_INFO><Calculation><ObjectList>'
    '<_P1 hash="p1" datatype="ChunkList"><ChunkList hash="p1">'
    '<Chunk type="FieldRef"><FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></Chunk>'
    '</ChunkList></_P1></ObjectList></Calculation></DDR_INFO>'
    '</FMSaveAsXML>\n')


class TestSaXMLLayoutRules(unittest.TestCase):
    """Layout shapes the lab fixture lacks, on a synthetic export: a deleted
    context TO, field objects without a usable target, a value list binding,
    portal sort, filter and a deleted portal TO, a grouped button, a button
    and a trigger whose script is gone, a separator.
    Guard: TestGuardProofs.test_action_and_trigger_rules."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxlay_")
        path = os.path.join(cls.tmp, "lay.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_LAYOUTS)
        db = os.path.join(cls.tmp, "lay.db")
        with contextlib.redirect_stderr(io.StringIO()):
            build(path, db, label="lay")
        cls.conn = sqlite3.connect(db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_layout(self):
        self.assertEqual(self.conn.execute(
            "SELECT name, extra_json FROM entities WHERE kind = 'layout'").fetchall(),
            [("Lay", None)])   # separator skipped; hidden: no in_menu key
        self.assertEqual(self.conn.execute(
            "SELECT target_fm_id, target_name, target_raw FROM refs"
            " WHERE context = 'layout_context'").fetchall(),
            [(None, None, "<unknown>")])

    def test_object_refs(self):
        rows = sorted(self.conn.execute(
            "SELECT json_extract(o.extra_json, '$.key'), r.context, r.target_name,"
            " r.target_to_name, r.target_raw, r.target_entity_id IS NOT NULL,"
            " r.trigger_event FROM refs r"
            " JOIN entities o ON o.entity_id = r.source_entity_id"
            " WHERE o.kind = 'layout_object'").fetchall())
        self.assertEqual(rows, [
            ("1", "layout_object", "K", "L", "L::K", 1, None),
            ("1", "value_list_source", "V", None, "V", 1, None),
            ("2", "layout_object", None, "ext_X", None, 0, None),
            ("3", "layout_object", None, None, "<unknown>", 0, None),
            ("4", "layout_object", None, None, "<unknown>", 0, None),
            ("5", "layout_object", None, "L", "<unknown>", 0, None),
            ("6", "calc", "K", "L", "L::K", 1, None),
            ("6", "portal", "L", None, "L", 1, None),
            ("6", "sort", "K", "L", "L::K", 1, None),
            ("7", "portal", None, None, "<unknown>", 0, None),
            ("8", "perform_script", "S", None, "S", 1, None),
            ("9", "perform_script", None, None, "<unknown>", 0, None),
            ("9", "trigger", None, None, "<unknown>", 0, "OnObjectEnter"),
        ])

    def test_dangling(self):
        # Every deleted target is in; the field object through the empty
        # external TO (its name left out by the export: it may exist in the
        # other file or be gone) is in as unverifiable_xfile, never dropped
        self.assertEqual(sorted(self.conn.execute(
            "SELECT context, marker, marker_class, source_kind FROM v_dangling"
            ).fetchall()), [
            ("layout_context", "<unknown>", "<unknown>", "layout"),
            ("layout_object", "<unknown>", "<unknown>", "layout_object"),
            ("layout_object", "<unknown>", "<unknown>", "layout_object"),
            ("layout_object", "<unknown>", "<unknown>", "layout_object"),
            ("perform_script", "<unknown>", "<unknown>", "layout_object"),
            ("portal", "<unknown>", "<unknown>", "layout_object"),
            ("trigger", "<unknown>", "<unknown>", "layout_object"),
            ("unverifiable_xfile", "ext_X::<unknown>", "<unknown>", "layout_object"),
        ])


SAXML_TABLE_VIEW = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence>'
    '<TableOccurrence id="2" name="ext_X" type="External">'
    '<BaseTableSourceReference type="ExternalDataSourceReference">'
    '</BaseTableSourceReference></TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
    '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
    '<Field id="2" name="J" datatype="Text" fieldtype="Normal"/>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '<LayoutCatalog><Layout id="1" name="Lay">'
    '<TableOccurrenceReference id="1" name="L"/>'
    '<PartsList><Part type="Body"><ObjectList>'
    '<LayoutObject id="1" type="Edit Box" name=""><Field>'
    '<FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></Field>'
    '</LayoutObject>'
    '</ObjectList></Part></PartsList>'
    '<TableView><ObjectList membercount="5">'
    # 50: the form object's field again (another column id): not twice
    '<TableViewLayoutObject hidden="False" id="50" name="K" width="75">'
    '<FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference>'
    '</TableViewLayoutObject>'
    # 1: column ids are their own id space: the id of a form object that
    # shows another field; J is not on the form, so the column is read
    '<TableViewLayoutObject hidden="False" id="1" name="J" width="75">'
    '<FieldReference id="2" name="J" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference>'
    '</TableViewLayoutObject>'
    # 51: a table-view-only field, hidden in table view; a calculation
    # under the column is not read
    '<TableViewLayoutObject hidden="True" id="51" name="J" width="120">'
    '<FieldReference id="2" name="J" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference>'
    '<Calculation><DDRREF kind="ChunkList" hash="t1">_T1</DDRREF><Text>L::K</Text>'
    '</Calculation></TableViewLayoutObject>'
    # 52: a field in another file through an empty external TO
    '<TableViewLayoutObject hidden="False" id="52" name="" width="75">'
    '<FieldReference id="9" name="" repetition="1">'
    '<TableOccurrenceReference id="2" name="ext_X"/></FieldReference>'
    '</TableViewLayoutObject>'
    # 53: no TO
    '<TableViewLayoutObject hidden="False" id="53" name="Gone" width="75">'
    '<FieldReference id="7" name="Gone" repetition="1"/>'
    '</TableViewLayoutObject>'
    '</ObjectList></TableView>'
    '<Options hidden="False">0</Options>'
    '</Layout></LayoutCatalog>'
    '</AddAction></Structure>'
    '<DDR_INFO><Calculation><ObjectList>'
    '<_T1 hash="t1" datatype="ChunkList"><ChunkList hash="t1">'
    '<Chunk type="FieldRef"><FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></Chunk>'
    '</ChunkList></_T1></ObjectList></Calculation></DDR_INFO>'
    '</FMSaveAsXML>\n')


class TestSaXMLTableView(unittest.TestCase):
    """A table-view-only column is a field object of the layout, as the
    DDR lists it; a column of a form field object (same field id and TO id)
    is not counted twice, whatever its id; and nothing else under TableView
    is read."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxtv_")
        path = os.path.join(cls.tmp, "tv.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_TABLE_VIEW)
        db = os.path.join(cls.tmp, "tv.db")
        with contextlib.redirect_stderr(io.StringIO()):
            build(path, db, label="tv")
        cls.conn = sqlite3.connect(db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_columns(self):
        objs = sorted(self.conn.execute(
            "SELECT o.fm_id, o.extra_json, p.kind FROM entities o"
            " JOIN entities p ON p.entity_id = o.parent_entity_id"
            " WHERE o.kind = 'layout_object'").fetchall())
        self.assertEqual(sorted([(k, json.loads(x).get("table_view_only"), p)
                                 for k, x, p in objs], key=repr), [
            ("1", None, "layout"),
            ("1", True, "layout"),
            ("51", True, "layout"),
            ("52", True, "layout"),
            ("53", True, "layout"),
        ])
        rows = sorted(self.conn.execute(
            "SELECT o.fm_id, r.context, r.target_name, r.target_to_name,"
            " r.target_raw, r.target_entity_id IS NOT NULL FROM refs r"
            " JOIN entities o ON o.entity_id = r.source_entity_id"
            " WHERE o.kind = 'layout_object'").fetchall())
        self.assertEqual(rows, [
            ("1", "layout_object", "J", "L", "L::J", 1),
            ("1", "layout_object", "K", "L", "L::K", 1),
            ("51", "layout_object", "J", "L", "L::J", 1),
            ("52", "layout_object", None, "ext_X", None, 0),
            ("53", "layout_object", None, None, "<unknown>", 0),
        ])

    def test_nothing_else_read(self):
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM refs WHERE context NOT IN"
            " ('layout_object', 'layout_context')").fetchone(), (0,))


class TestSaXMLCredentials(unittest.TestCase):
    """FileMaker's credential elements never reach the index. The fixtures
    carry the sentinel in place of every credential text
    (tools/saxml_fixture.py); a build must not hold it in any text column.

    Broken-guard run inside the test: no rule of the real handler reads a
    skipped credential element, so emptying SKIP alone proves nothing. The
    proof therefore covers the account path: a handler that also takes
    Account/Authentication/PasswordEncrypted/Data as the account name
    (ACCOUNT_NAME_TAGS widened) keeps the sentinel out while SKIP holds,
    and writes it into entities.name once SKIP is emptied."""

    SENTINEL = "FIXTURE-SECRET-NEVER-INDEXED"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxcred_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hits(self, path):
        """Build `path`; {table.column: rows holding the sentinel}."""
        db = os.path.join(self.tmp, "cred.db")
        if os.path.exists(db):
            os.remove(db)
        build(path, db, label="cred")
        conn = sqlite3.connect(db)
        out = {}
        for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"):
            for col in conn.execute(f'PRAGMA table_info("{table}")'):
                n = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE '
                    f'instr(CAST("{col[1]}" AS TEXT), ?) > 0',
                    (self.SENTINEL,)).fetchone()[0]
                if n:
                    out[f"{table}.{col[1]}"] = n
        conn.close()
        return out

    @staticmethod
    def leaky_handler():
        from fm_ddr import saxml

        class Leaky(saxml.SaXMLHandler):
            ACCOUNT_NAME_TAGS = ("AccountName", "Data")

            def _start_Data(self, a):
                self._account_text("Data")
        return Leaky

    def test_fixtures_carry_no_user_names(self):
        import re
        for path in _saxml_fixtures():
            with self.subTest(fixture=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn(self.SENTINEL, text)
                self.assertIsNone(re.search(r'\buserName="[^"]', text))
                self.assertIsNone(re.search(r'\baccountName="[^"]', text))

    def test_sentinel_never_indexed(self):
        from unittest import mock
        from fm_ddr import saxml
        self.assertTrue(_saxml_fixtures())
        for path in _saxml_fixtures():
            with self.subTest(fixture=os.path.basename(path)):
                self.assertEqual(self.hits(path), {})
                with mock.patch.object(saxml, "SaXMLHandler", self.leaky_handler()):
                    # SKIP holds: the widened rule never sees the element
                    self.assertEqual(self.hits(path), {})
                    # Broken guard: SKIP emptied, the sentinel arrives
                    with mock.patch.object(saxml, "SKIP", frozenset()):
                        leaked = self.hits(path)
                self.assertIn("entities.name", leaked)


class TestGuardProofs(unittest.TestCase):
    """Each guard broken in turn, and the test that pins it must then fail:
    the proof that the test can see the guard go, run on every suite run
    Every case also
    checks the same test passes unbroken, so a proof is never vacuous."""

    def fails_under(self, case_cls, method, patcher):
        def run(p=None):
            res = unittest.TestResult()
            suite = unittest.TestSuite([case_cls(method)])
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                if p is None:
                    suite.run(res)
                else:
                    with p:
                        suite.run(res)
            return res.wasSuccessful()
        self.assertTrue(run(), f"{method} fails unbroken")
        self.assertFalse(run(patcher), f"{method} passes with the guard broken")

    def test_version_check(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLRefusal, "test_unsupported_version_22",
                         mock.patch.object(saxml, "check", lambda info, path: None))

    def test_catalog_order(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLCatalogs, "test_catalog_order_refused",
                         mock.patch.object(saxml.SaXMLHandler, "_order_error",
                                           lambda self: None))

    def test_layout_unknown(self):
        import re
        from fm_ddr import saxml
        self.fails_under(TestSaXMLStepRules, "test_layout_unknown_only_for_the_layout",
                         mock.patch.object(saxml, "LAYOUT_UNKNOWN",
                                           re.compile("<unknown>")))

    def test_step_names(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLStepRules, "test_step_name_spelled_as_the_ddr",
                         mock.patch.dict(saxml.STEP_NAMES, clear=True))

    def test_version_gate(self):
        # The per-version expected files see a rule go: the field-object
        # rule (every field on a layout) disabled
        from fm_ddr import saxml
        self.fails_under(TestSaXMLVersions, "test_expected_per_version",
                         mock.patch.object(saxml.SaXMLHandler, "_field_object",
                                           lambda self, fr: None))

    def test_mark_xfile(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLUnverifiableXfile, "test_rule",
                         mock.patch.object(saxml.SaXMLHandler, "_mark_xfile",
                                           lambda self: None))

    def test_external_to_warning(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLEmptyExternalWarning, "test_warns",
                         mock.patch.object(saxml, "external_to_warning",
                                           lambda conn: None))

    def test_action_and_trigger_rules(self):
        from fm_ddr import saxml
        for name in ("_end_action", "_end_ScriptTrigger"):
            with self.subTest(rule=name):
                self.fails_under(TestSaXMLLayoutRules, "test_dangling",
                                 mock.patch.object(saxml.SaXMLHandler, name,
                                                   lambda self: None))

    def test_dtd_refusal(self):
        from fm_ddr import saxml
        real = saxml._dtd_refusal
        self.fails_under(TestPrologSniff, "test_padded_doctype_with_subset_refused",
                         mock.patch.object(saxml, "_dtd_refusal",
                                           lambda path: RuntimeError(str(real(path)))))

    def test_duplicate_refusal(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLInputRobustness, "test_stale_copy_of_the_same_file",
                         mock.patch.object(saxml, "refuse_duplicates",
                                           lambda pairs: None))

    def test_locale_warning(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLLocale, "test_other_locale_warns",
                         mock.patch.object(saxml, "locale_warning",
                                           lambda infos: None))

    def test_ddrinfo_warning(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLDDRInfoMisses, "test_in_the_warning_block",
                         mock.patch.object(saxml, "ddrinfo_warning",
                                           lambda conn: None))

    def test_step_text_guard(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLStepTextGuard, "test_every_step_text_missing_refused",
                         mock.patch.object(saxml, "STEPTEXT_MISS_LIMIT", 2.0))

    def test_partial_refusal(self):
        from fm_ddr import saxml
        self.fails_under(TestSaXMLPartialExport, "test_catalog_list_export_refused",
                         mock.patch.object(saxml, "refuse_partial",
                                           lambda sections, kinds, path: None))


class TestSourceFormat(unittest.TestCase):
    """ddr_run.source_format / source_version and their readers (stats, list,
    report); an index built before the columns reads as a DDR."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_srcfmt_")
        cls.ddr = build_fixture_db(cls.tmp)
        cls.sax = os.path.join(cls.tmp, "sax.db")
        build(os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.0.xml"), cls.sax,
              label="sax")
        # An index from before the columns: drop both
        cls.old = os.path.join(cls.tmp, "old.db")
        shutil.copyfile(cls.ddr, cls.old)
        c = sqlite3.connect(cls.old)
        c.execute("ALTER TABLE ddr_run DROP COLUMN source_format")
        c.execute("ALTER TABLE ddr_run DROP COLUMN source_version")
        c.commit(); c.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_row(self, db):
        c = sqlite3.connect(db)
        row = c.execute("SELECT source_format, source_version, ddr_version FROM ddr_run").fetchone()
        c.close()
        return row

    def test_build_writes_both_columns(self):
        self.assertEqual(self.run_row(self.ddr)[:2], ("ddr", None))
        self.assertEqual(self.run_row(self.sax), ("saxml", "26.0.1", "2.3.0.0"))

    def stats(self, db):
        import io, contextlib
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(["stats", db])
        return out.getvalue()

    def test_stats_source_line(self):
        self.assertTrue(self.stats(self.sax).startswith(
            "source: saxml 2.3.0.0 (FileMaker 26.0.1)\n"))
        ver = self.run_row(self.ddr)[2]
        self.assertTrue(self.stats(self.ddr).startswith(f"source: ddr {ver}\n"))
        self.assertTrue(self.stats(self.old).startswith(f"source: ddr {ver}\n"))
        self.assertNotIn("external table occurrences", self.stats(self.sax))

    def test_stats_external_to_line(self):
        ext = os.path.join(self.tmp, "ext.db")
        shutil.copyfile(self.sax, ext)
        c = sqlite3.connect(ext)
        c.execute("""UPDATE entities SET extra_json = '{"external": true}'
                     WHERE entity_id = (SELECT MIN(entity_id) FROM entities
                                        WHERE kind = 'table_occurrence')""")
        c.commit(); c.close()
        self.assertIn("  1 external table occurrences with no data source", self.stats(ext))

    def test_list_format_column(self):
        import subprocess, sys as _sys
        r = subprocess.run([_sys.executable, "-m", "fm_ddr.cli", "list", self.tmp],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = {ln.split()[0]: ln.split() for ln in r.stdout.splitlines() if ".db" in ln}
        self.assertIn("format", r.stdout)
        self.assertIn("saxml", lines["sax.db"])
        self.assertIn("ddr", lines["old.db"])
        self.assertIn("ddr", lines[os.path.basename(self.ddr)])

    def test_stale_hints(self):
        # build overwrites an fmsonar index without --force, so the rebuild
        # hints do not ask for it; the list status carries no em dash
        import io, contextlib
        from fm_ddr.cli import main
        stale = os.path.join(self.tmp, "stale.db")
        shutil.copyfile(self.sax, stale)
        c = sqlite3.connect(stale)
        c.execute("UPDATE ddr_run SET parser_version = '1.0.0'")
        c.execute("DROP VIEW v_cascades")
        c.commit(); c.close()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            main(["stats", stale])
            with self.assertRaises(SystemExit) as cm:
                main(["cascades", stale])
        self.assertIn("Rebuild:", err.getvalue())
        self.assertNotIn("--force", err.getvalue())
        self.assertIn("Rebuild:", str(cm.exception))
        self.assertNotIn("--force", str(cm.exception))
        os.remove(stale)
        # A source touched after the build
        d = tempfile.mkdtemp(prefix="fmddr_srcnew_")
        try:
            src = os.path.join(d, "src.xml")
            shutil.copyfile(os.path.join(ROOT, "tests", "fixtures",
                                         "saxml_2.3.0.0.xml"), src)
            with contextlib.redirect_stdout(io.StringIO()):
                build(src, os.path.join(d, "new.db"))
            later = os.path.getmtime(src) + 60
            os.utime(src, (later, later))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["list", d])
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertIn("source newer than index, rebuild", out.getvalue())

    def test_report_meta(self):
        from fm_ddr.report import _collect as load_data
        m = load_data(self.sax)["meta"]
        self.assertEqual((m["source_format"], m["source_version"], m["ddr_version"]),
                         ("saxml", "26.0.1", "2.3.0.0"))
        m = load_data(self.old)["meta"]
        self.assertEqual((m["source_format"], m["source_version"]), ("ddr", None))


class TestSaXMLModule(unittest.TestCase):
    """The handler is a plain class statement and parse.py does not import
    saxml at its top. Each check runs in a fresh
    interpreter, so no earlier import in the suite can mask it."""

    def run_py(self, code):
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_handler_is_a_class_statement(self):
        self.run_py(
            "import inspect, fm_ddr.saxml as s\n"
            "assert 'SaXMLHandler' in vars(s), 'no class in the module dict'\n"
            "assert not hasattr(s, '__getattr__'), 'module __getattr__ present'\n"
            "assert inspect.isclass(s.SaXMLHandler)\n"
            "from fm_ddr.parse import DDRHandler\n"
            "assert issubclass(s.SaXMLHandler, DDRHandler)\n")

    def test_parse_does_not_import_saxml_at_top(self):
        self.run_py(
            "import sys, fm_ddr.parse as p\n"
            "assert 'saxml' not in vars(p), 'parse imports saxml at its top'\n"
            "assert 'fm_ddr.saxml' not in sys.modules\n")


class TestSaXMLLabPair(unittest.TestCase):
    """The lab file's DDR and its Save a Copy as XML (2.3.0.1, the same file
    state) built side by side: what the DDR path writes, the SaXML path
    writes too."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxpair_")
        cls.dbs = {}
        for kind, name in (("ddr", "saxml_lab_ddr.xml"), ("sax", "saxml_2.3.0.1.xml")):
            db = os.path.join(cls.tmp, kind + ".db")
            with contextlib.redirect_stderr(io.StringIO()):
                build(os.path.join(ROOT, "tests", "fixtures", name), db, label=kind)
            cls.dbs[kind] = db

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, kind, sql, *args):
        conn = sqlite3.connect(self.dbs[kind])
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    OBJECTS = (
        "WITH RECURSIVE up(obj, id, parent, kind) AS ("
        " SELECT entity_id, entity_id, parent_entity_id, kind FROM entities"
        " WHERE kind = 'layout_object'"
        " UNION ALL SELECT up.obj, e.entity_id, e.parent_entity_id, e.kind"
        " FROM up JOIN entities e ON e.entity_id = up.parent WHERE up.kind <> 'layout')"
        " SELECT l.name, json_extract(o.extra_json, '$.key'),"
        " json_extract(o.extra_json, '$.step_text')"
        " FROM up JOIN entities l ON l.entity_id = up.id AND l.kind = 'layout'"
        " JOIN entities o ON o.entity_id = up.obj")

    def objects(self, kind):
        return {(lay, key): text for lay, key, text in self.q(kind, self.OBJECTS)}

    def test_object_step_text(self):
        # A button's launch text (Perform Script with its parameter) is on
        # the object in both indexes; the DDR's object text ends lines with
        # CR on a real solution's DDR, so lines are compared, not bytes
        ddr, sax = self.objects("ddr"), self.objects("sax")
        with_text = {k: v for k, v in ddr.items() if v}
        self.assertGreaterEqual(len(with_text), 3)
        norm = lambda t: t.replace("\r\n", "\n").replace("\r", "\n") if t else t
        self.assertEqual({k: norm(sax.get(k)) for k in with_text},
                         {k: norm(v) for k, v in with_text.items()})
        self.assertEqual({k for k, v in sax.items() if v}, set(with_text))

    def test_repetition_field_object_resolves(self):
        # A field object showing repetition 3: the DDR writes TO::Field[3],
        # the export repetition="3". Both resolve to the field; the
        # repetition stays in target_raw only
        q = ("SELECT r.target_name, r.target_to_name, r.target_raw, t.kind, t.name"
             " FROM refs r LEFT JOIN entities t ON t.entity_id = r.target_entity_id"
             " WHERE r.context = 'layout_object' AND r.target_raw LIKE '%Phone_r%'")
        want = [("Phone_r", "CUS__Customer", "CUS__Customer::Phone_r[3]", "field", "Phone_r")]
        self.assertEqual(self.q("ddr", q), want)
        self.assertEqual(self.q("sax", q), want)

    def test_auto_enter_as_ddr(self):
        # The DDR writes always_evaluate / overwrite_existing only for an
        # auto-enter calculation or when one is on; Pro 26.1 writes both
        # attributes on every field
        q = ("SELECT base_table, name, auto_enter FROM entities"
             " WHERE kind = 'field' ORDER BY 1, 2")
        self.assertEqual(self.q("sax", q), self.q("ddr", q))

    def test_step_calc_text_as_ddr(self):
        # A Set Field's target repetition is not part of the step's
        # calculation text on the DDR
        q = ("SELECT p.name, s.seq, s.calc_text FROM entities s"
             " JOIN entities p ON p.entity_id = s.parent_entity_id"
             " WHERE s.kind = 'script_step' ORDER BY 1, 2")
        ddr = self.q("ddr", q)
        self.assertGreater(sum(1 for r in ddr if r[2]), 10)
        self.assertEqual(self.q("sax", q), ddr)

    def test_investigate_launch_sites(self):
        # The skill's step 5: investigate names each layout launch site with
        # its parameter, the same on both indexes
        import contextlib
        import io
        from fm_ddr.cli import main

        def sites(kind):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["investigate", self.dbs[kind], "Sell Watch ( watchId )"])
            text = out.getvalue()
            start = text.index("## Layout launch sites")
            return text[start:text.index("\n## ", start + 1)]
        ddr = sites("ddr")
        self.assertIn("Parameter:", ddr)
        self.assertEqual(sites("sax"), ddr)


# Insert Text: the literal is Parameter/Text@value; the analysis step text
# leaves it out when the target is a variable and quotes it when the target
# is a field (the shapes FileMaker writes)
SAXML_INSERT_TEXT = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
    '<ObjectList><Field id="1" name="K" datatype="Text" fieldtype="Normal"/>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '<ScriptCatalog><Script id="1" name="Query"/></ScriptCatalog>'
    '<StepsForScripts><Script><ScriptReference id="1" name="Query"/><ObjectList>'
    # 1 to a variable, a two-line literal (CR as the export writes it)
    '<Step index="0" id="61" name="Insert Text" enable="True">'
    '<DDRREF kind="StepText" hash="h1">_I1</DDRREF><ParameterValues>'
    '<Parameter type="Boolean"><Boolean type="Select" value="True"/></Parameter>'
    '<Parameter type="Target"><Variable value="$sql"/></Parameter>'
    '<Parameter type="Text"><Text value="SELECT zebrafield&#13;FROM A"/></Parameter>'
    '</ParameterValues></Step>'
    # 2 to a field: the step text already quotes the literal
    '<Step index="1" id="61" name="Insert Text" enable="True">'
    '<DDRREF kind="StepText" hash="h2">_I2</DDRREF><ParameterValues>'
    '<Parameter type="FieldReference"><FieldReference id="1" name="K">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></Parameter>'
    '<Parameter type="Text"><Text value="quagga"/></Parameter>'
    '</ParameterValues></Step>'
    # 3 to a variable, one line, no Select
    '<Step index="2" id="61" name="Insert Text" enable="True">'
    '<DDRREF kind="StepText" hash="h3">_I3</DDRREF><ParameterValues>'
    '<Parameter type="Target"><Variable value="$json"/></Parameter>'
    '<Parameter type="Text"><Text value="{ &quot;okapi&quot; : 1 }"/></Parameter>'
    '</ParameterValues></Step>'
    '</ObjectList></Script></StepsForScripts>'
    '</AddAction></Structure>'
    '<DDR_INFO><Script><ObjectList>'
    '<_I1 hash="h1" datatype="StepText">Insert Text [ $sql ]&#13;[ Select ]</_I1>'
    '<_I2 hash="h2" datatype="StepText">Insert Text [ L::K; “quagga” ]&#13;[ Select ]</_I2>'
    '<_I3 hash="h3" datatype="StepText">Insert Text [ $json ]</_I3>'
    '</ObjectList></Script></DDR_INFO>'
    '</FMSaveAsXML>\n')


class TestSaXMLInsertText(unittest.TestCase):
    """Insert Text's literal reaches step_text and the FTS index as the DDR
    writes it: Insert Text [ $var; “literal” ]."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxit_")
        path = os.path.join(cls.tmp, "it.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_INSERT_TEXT)
        cls.db = os.path.join(cls.tmp, "it.db")
        build(path, cls.db, label="it")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_step_text(self):
        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            "SELECT seq, json_extract(extra_json, '$.step_text') FROM entities"
            " WHERE kind = 'script_step' ORDER BY seq").fetchall()
        conn.close()
        self.assertEqual(rows, [
            (1, "Insert Text [ $sql; “SELECT zebrafield\nFROM A” ]\n[ Select ]"),
            (2, "Insert Text [ L::K; “quagga” ]\n[ Select ]"),
            (3, "Insert Text [ $json; “{ \"okapi\" : 1 }” ]"),
        ])

    def test_fts_finds_the_literal(self):
        conn = sqlite3.connect(self.db)
        for word, seq in (("zebrafield", 1), ("quagga", 2), ("okapi", 3)):
            with self.subTest(word=word):
                self.assertEqual(conn.execute(
                    "SELECT e.seq FROM text_index t JOIN entities e"
                    " ON e.entity_id = t.entity_id WHERE text_index MATCH ?",
                    (word,)).fetchall(), [(seq,)])
        conn.close()


class TestSaXMLCascadesXfile(unittest.TestCase):
    """A cascade delete into a table occurrence on an empty external TO
    (FMDeveloperTool route: no data source, no base table) is never hidden:
    `cascades <db> <table>` lists it as a candidate for any table, and the
    unfiltered run counts it."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxcas_")
        path = os.path.join(cls.tmp, "cas.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD +
                '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
                '<TableOccurrenceCatalog>'
                '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
                '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
                '</TableOccurrence>'
                '<TableOccurrence id="2" name="L2" type="Local"><BaseTableSourceReference>'
                '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
                '</TableOccurrence>'
                '<TableOccurrence id="3" name="ext_X" type="External">'
                '<BaseTableSourceReference type="ExternalDataSourceReference">'
                '</BaseTableSourceReference></TableOccurrence></TableOccurrenceCatalog>'
                '<RelationshipCatalog>'
                '<Relationship id="1">'
                '<LeftTable cascadeDelete="False"><TableOccurrenceReference id="1" name="L"/></LeftTable>'
                '<RightTable cascadeDelete="True"><TableOccurrenceReference id="3" name="ext_X"/></RightTable>'
                '</Relationship>'
                '<Relationship id="2">'
                '<LeftTable cascadeDelete="False"><TableOccurrenceReference id="1" name="L"/></LeftTable>'
                '<RightTable cascadeDelete="True"><TableOccurrenceReference id="2" name="L2"/></RightTable>'
                '</Relationship>'
                '</RelationshipCatalog>' + SAXML_TAIL)
        cls.db = os.path.join(cls.tmp, "cas.db")
        with contextlib.redirect_stderr(io.StringIO()):
            build(path, cls.db, label="cas")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, *args):
        import contextlib
        import io
        from fm_ddr.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["cascades", self.db, *args])
        return out.getvalue()

    def test_filtered_lists_unknown_base_table(self):
        # The table in the other file is named nowhere in this export
        out = self.run_cli("Remote")
        self.assertIn("ext_X", out)
        self.assertIn("base table", out)
        self.assertIn("1 relationship side(s) delete into a table occurrence", out)

    def test_filtered_known_table_keeps_its_rows(self):
        out = self.run_cli("A")
        self.assertIn("L :: L2", out)
        self.assertIn("ext_X", out)        # still a candidate for A

    def test_unfiltered_counts_them(self):
        out = self.run_cli()
        self.assertIn("2 relationship side(s)", out)
        self.assertIn("1 relationship side(s) delete into a table occurrence", out)


# A popover's title calculation: LayoutObject/Title holds the DDRREF and the
# Text directly, with no Calculation wrapper (lab v3 shape)
SAXML_POPOVER = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/>'
    '<ObjectList><Field id="1" name="K" datatype="Number" fieldtype="Normal"/>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '<LayoutCatalog><Layout id="1" name="Lay">'
    '<TableOccurrenceReference id="1" name="L"/>'
    '<PartsList><Part type="Body"><ObjectList>'
    '<LayoutObject id="17" type="Popover Button" name=""><PopoverButton>'
    '<LayoutObject id="16" type="PopoverPanel" name="">'
    '<Title><DDRREF kind="ChunkList" hash="p1">_P1_PopoverPanel</DDRREF>'
    '<Text><![CDATA["Customer " & L::K]]></Text></Title>'
    '<ObjectList></ObjectList></LayoutObject>'
    '</PopoverButton></LayoutObject>'
    '</ObjectList></Part></PartsList>'
    '</Layout></LayoutCatalog>'
    '</AddAction></Structure>'
    '<DDR_INFO><Calculation><ObjectList>'
    '<_P1_PopoverPanel hash="p1" datatype="ChunkList"><ChunkList hash="p1">'
    '<Chunk type="NoRef">"Customer " &amp; </Chunk>'
    '<Chunk type="FieldRef"><FieldReference id="1" name="K" repetition="1">'
    '<TableOccurrenceReference id="1" name="L"/></FieldReference></Chunk>'
    '</ChunkList></_P1_PopoverPanel></ObjectList></Calculation></DDR_INFO>'
    '</FMSaveAsXML>\n')


class TestSaXMLPopoverTitle(unittest.TestCase):
    """A popover's title calculation gives its field references and its text
    to the popover object, as the DDR's TitleCalc does."""

    def test_title_calc(self):
        tmp = tempfile.mkdtemp(prefix="fmddr_saxpop_")
        try:
            path = os.path.join(tmp, "pop.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(SAXML_HEAD + SAXML_POPOVER)
            db = os.path.join(tmp, "pop.db")
            build(path, db, label="pop")
            conn = sqlite3.connect(db)
            refs = conn.execute(
                "SELECT o.fm_id, r.context, r.target_raw, r.target_entity_id IS NOT NULL"
                " FROM refs r JOIN entities o ON o.entity_id = r.source_entity_id"
                " WHERE r.context = 'calc'").fetchall()
            calc = conn.execute(
                "SELECT calc_text FROM entities WHERE kind = 'layout_object'"
                " AND fm_id = '16'").fetchone()
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(refs, [("16", "calc", "L::K", 1)])
        self.assertEqual(calc, ('"Customer " & L::K',))


PARTIAL = os.path.join(ROOT, "tests", "fixtures", "saxml_partial_2.3.0.0.xml")


class TestSaXMLPartialExport(unittest.TestCase):
    """A partial export (FMDeveloperTool -catalog_list) is refused by name:
    an index of some catalogs would list every field unused and every
    script orphaned. The fixture is a real FMDeveloperTool 26.0.1 export of
    the lab file with -catalog_list BaseTableCatalog FieldCatalog
    TableOccurrenceCatalog (scrubbed by tools/saxml_fixture.py); the other
    cases cut a section out of a whole export."""

    SAX = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.0.xml")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxpart_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refused(self, path):
        db = os.path.join(self.tmp, "out.db")
        with self.assertRaises(ValueError) as cm:
            build(path, db)
        self.assertFalse(os.path.exists(db))
        return str(cm.exception)

    def cut(self, name, start, end):
        """The whole fixture without the element from `start` to `end`."""
        with open(self.SAX, encoding="utf-8") as f:
            text = f.read()
        i = text.index(start)
        j = text.index(end, i) + len(end)
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text[:i] + text[j:])
        return path

    def test_catalog_list_export_refused(self):
        msg = self.refused(PARTIAL)
        self.assertIn("-catalog_list", msg)
        for name in ("LayoutCatalog", "PrivilegeSetsCatalog", "AccountsCatalog",
                     "Metadata", "FileAccessCatalog", "ThemeCatalog",
                     "ExtendedPrivilegesCatalog"):
            self.assertIn(name, msg)
        for name in ("BaseTableCatalog", "TableOccurrenceCatalog"):
            self.assertNotIn(name, msg)

    def test_missing_layouts_refused(self):
        p = self.cut("nolayouts.xml", "<LayoutCatalog", "</LayoutCatalog>")
        msg = self.refused(p)
        self.assertIn("LayoutCatalog", msg)
        self.assertNotIn("Metadata", msg)

    def test_missing_metadata_refused(self):
        # -catalog_list leaves Metadata out unless it is named (measured):
        # the file triggers live there
        p = self.cut("nometa.xml", "<Metadata", "</Metadata>")
        self.assertIn("Metadata", self.refused(p))

    def test_always_present_catalogs_required(self):
        # Every whole export at hand writes these three; -catalog_list
        # writes each only when it is named (measured)
        for el in ("FileAccessCatalog", "ThemeCatalog", "ExtendedPrivilegesCatalog"):
            with self.subTest(catalog=el):
                p = self.cut(f"no_{el}.xml", f"<{el}", f"</{el}>")
                msg = self.refused(p)
                self.assertIn(el, msg)
                self.assertIn("-catalog_list", msg)

    def cut_lab3(self, name, *elements):
        """Lab v3 without the named sections: what FMDeveloperTool 26.0.1
        writes for a -catalog_list naming every catalog but those
        (measured: ScriptCatalog drops StepsForScripts too)."""
        with open(LAB3, encoding="utf-8") as f:
            text = f.read()
        for el in elements:
            self.assertEqual(text.count(f"<{el}"), 1)
            i = text.index(f"<{el}")
            j = text.index(f"</{el}>", i) + len(f"</{el}>")
            text = text[:i] + text[j:]
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_absent_catalog_with_references_refused(self):
        # A catalog a whole export leaves out only when the file has none,
        # while the export still refers to its kind
        for els, what in ((("ScriptCatalog", "StepsForScripts"), "scripts"),
                          (("ValueListCatalog",), "value lists"),
                          (("CustomFunctionsCatalog",), "custom functions")):
            with self.subTest(catalog=els[0]):
                msg = self.refused(self.cut_lab3(f"no_{els[0]}.xml", *els))
                self.assertIn(els[0], msg)
                self.assertIn(what, msg)
                self.assertIn("-catalog_list", msg)
                self.assertNotIn("StepsForScripts", msg)

    def test_external_tos_need_their_data_sources(self):
        src = os.path.join(ROOT, "tests", "fixtures", "saxml_xfile_fmdt_2.3.0.0.xml")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        i = text.index("<ExternalDataSourceCatalog")
        j = text.index("</ExternalDataSourceCatalog>") + len("</ExternalDataSourceCatalog>")
        p = os.path.join(self.tmp, "nods.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text[:i] + text[j:])
        msg = self.refused(p)
        self.assertIn("ExternalDataSourceCatalog", msg)
        self.assertIn("data sources", msg)

    def test_absent_catalog_without_references_builds(self):
        # A whole export of a file with no scripts, value lists or custom
        # functions has none of the three: no refusal (the satellite of the
        # two-file lab has no value list and no custom function)
        sat = os.path.join(ROOT, "tests", "fixtures", "saxml_xfile_sat_fmdt_2.3.0.0.xml")
        self.assertGreater(build(sat, os.path.join(self.tmp, "sat.db"))["_refs"], 0)

    def test_fields_without_tables_names_the_catalog(self):
        # -catalog_list FieldCatalog: named as a missing catalog, not as an
        # internal order assumption
        p = self.cut("notables.xml", "<BaseTableCatalog", "</BaseTableCatalog>")
        msg = self.refused(p)
        self.assertIn("BaseTableCatalog", msg)
        self.assertNotIn("assumption", msg)

    def test_whole_exports_build(self):
        for path in _saxml_fixtures() + [
                os.path.join(ROOT, "tests", "fixtures", "saxml_xfile_2.3.0.1.xml"),
                os.path.join(ROOT, "tests", "fixtures", "saxml_xfile_fmdt_2.3.0.0.xml")]:
            with self.subTest(fixture=os.path.basename(path)):
                db = os.path.join(self.tmp, "ok.db")
                if os.path.exists(db):
                    os.remove(db)
                self.assertGreater(build(path, db)["_refs"], 0)

    def test_split_catalogs_file_refused(self):
        # The root FMDeveloperTool writes on each -split_catalogs file
        # (measured: both attributes True)
        with open(self.SAX, encoding="utf-8") as f:
            text = f.read()
        old = 'binary_under_lo="False" split_catalogs="False"'
        self.assertEqual(text.count(old), 1)
        p = os.path.join(self.tmp, "SaxLab_LayoutCatalog.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.replace(old, 'binary_under_lo="True" split_catalogs="True"'))
        self.assertIn("split_catalogs", self.refused(p))


class TestSaXMLMessages(unittest.TestCase):
    """A malformed or cut-off export is named as such with expat's place in
    the file; the low-resolution warning does not fire on the healthy lab
    exports."""

    SAX = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxmsg_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def fixture(self):
        with open(self.SAX, encoding="utf-8") as f:
            return f.read()

    def refused(self, path):
        with self.assertRaises(ValueError) as cm:
            build(path, os.path.join(self.tmp, "out.db"))
        return str(cm.exception)

    def test_undefined_entity_on_the_root(self):
        text = self.fixture()
        self.assertEqual(text.count('locale="English"'), 1)
        msg = self.refused(self.write("ent.xml", text.replace(
            'locale="English"', 'locale="&nope;"')))
        self.assertIn("ent.xml", msg)
        self.assertIn("undefined entity at line 2, column", msg)
        self.assertNotIn("no XML root element", msg)

    def test_cut_off_export(self):
        text = self.fixture()
        for frac in (0.1, 0.5, 0.97):
            with self.subTest(cut=frac):
                msg = self.refused(self.write("cut.xml", text[:int(len(text) * frac)]))
                self.assertIn("cut.xml is cut off or damaged", msg)
                self.assertRegex(msg, r"at line \d+, column \d+")
                self.assertIn("Re-export", msg)

    def test_lab_exports_get_no_resolution_warning(self):
        fx = os.path.join(ROOT, "tests", "fixtures")
        for names in (["saxml_2.3.0.0.xml"], ["saxml_2.3.0.1.xml"], [LAB3],
                      ["saxml_xfile_2.3.0.1.xml"], ["saxml_xfile_fmdt_2.3.0.0.xml"]):
            with self.subTest(fixture=names[0]):
                db = os.path.join(self.tmp, "lab.db")
                if os.path.exists(db):
                    os.remove(db)
                s = build([os.path.join(fx, n) for n in names], db)
                self.assertNotIn("WARNING: only", "\n".join(s["_warnings"]))

    def test_unexplained_misses_still_warn(self):
        # A field the export names and the index does not have: neither a
        # marked defect nor another file's
        body = SAXML_CHUNKLESS_PAIRS.replace(
            '<FieldReference id="1" name="K"><TableOccurrenceReference id="1" name="A"/>'
            '</FieldReference></Chunk></_KEPT>',
            '<FieldReference id="9" name="Nope"><TableOccurrenceReference id="1" name="A"/>'
            '</FieldReference></Chunk></_KEPT>')
        self.assertNotEqual(body, SAXML_CHUNKLESS_PAIRS)
        s = build(self.write("nope.xml", SAXML_HEAD + body),
                  os.path.join(self.tmp, "nope.db"))
        w = "\n".join(s["_warnings"])
        self.assertIn("WARNING: only 0% of references resolved", w)
        self.assertIn("v_dangling", w)
        self.assertNotIn("source version", w)

    def test_ddr_warning_unchanged(self):
        with open(FIXTURE, encoding="utf-8") as f:
            text = f.read()
        # Every script call pointed at a script the DDR does not have
        import re
        text2 = re.sub(r'(<Script [^>]*name=")([^"]*")', r'\1Gone_\2', text)
        self.assertNotEqual(text, text2)
        s = build(self.write("ddr.xml", text2), os.path.join(self.tmp, "ddr.db"))
        w = "\n".join(s["_warnings"])
        if "WARNING: only" in w:
            self.assertIn("(source version", w)
            self.assertIn("On a healthy single-file solution expect ~98%", w)


class TestSaXMLStepTextGuard(unittest.TestCase):
    """A step text missing from DDR_INFO is damage: every whole export
    measured has none (the real file: 0 of its step texts). More than 10
    percent missing refuses the export; calculations without reference
    list never refuse."""

    SAX = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmddr_saxstep_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def without_step_texts(self, k):
        """The fixture with the hash of k StepText DDRREFs altered (93 in
        all): their entries no longer match."""
        import re
        with open(self.SAX, encoding="utf-8") as f:
            text = f.read()
        n = iter(range(10**6))
        text = re.sub(r'<DDRREF kind="StepText" hash="',
                      lambda m: m.group(0) + ("X" if next(n) < k else ""), text)
        path = os.path.join(self.tmp, f"step{k}.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_every_step_text_missing_refused(self):
        db = os.path.join(self.tmp, "all.db")
        with self.assertRaises(ValueError) as cm:
            build(self.without_step_texts(93), db)
        msg = str(cm.exception)
        self.assertIn("93 of 93", msg)
        self.assertIn("step text", msg)
        self.assertIn("Re-export", msg)
        self.assertFalse(os.path.exists(db))

    def test_ten_percent_line(self):
        # 10 of 93 is past the line, 9 of 93 is under it
        with self.assertRaises(ValueError):
            build(self.without_step_texts(10), os.path.join(self.tmp, "ten.db"))
        summary = build(self.without_step_texts(9), os.path.join(self.tmp, "nine.db"))
        self.assertIn("9 step(s) without step text", "\n".join(summary["_warnings"]))


class TestSaXMLDDRInfoMisses(unittest.TestCase):
    """A DDRREF whose DDR_INFO entry is absent loses a step text or a
    calculation's references. A whole export has a few (no refusal); every
    miss is counted into ddr_run, printed in the one
    WARNING block before "Done:", shown by `stats` and `list`, and `where`
    and `investigate` say that `search` is the check."""

    SAX = os.path.join(ROOT, "tests", "fixtures", "saxml_2.3.0.1.xml")

    @classmethod
    def setUpClass(cls):
        import re
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxmiss_")
        with open(cls.SAX, encoding="utf-8") as f:
            text = f.read()
        head, info = text.split("<DDR_INFO>", 1)
        # Half of the chunk lists removed, and 5 of the 93 step texts (under
        # the 10 percent that refuses, TestSaXMLStepTextGuard)
        for dt, cut in (("ChunkList", lambda total: total // 2),
                        ("StepText", lambda total: 5)):
            entry = re.compile(
                r'<(_[^ >]+) hash="[^"]*" datatype="%s">.*?</\1>' % dt, re.S)
            total = len(entry.findall(info))
            info = entry.sub(lambda m, n=iter(range(total)), k=cut(total): ""
                             if next(n) < k else m.group(0), info)
        cls.half = os.path.join(cls.tmp, "half.xml")
        with open(cls.half, "w", encoding="utf-8") as f:
            f.write(head + "<DDR_INFO>" + info)
        cls.db = os.path.join(cls.tmp, "half.db")
        cls.build_out = cls.cli("build", cls.half, "-o", cls.db)
        cls.ddr = build_fixture_db(cls.tmp)
        cls.old = os.path.join(cls.tmp, "old.db")
        shutil.copyfile(cls.db, cls.old)
        c = sqlite3.connect(cls.old)
        for col in ("ddrinfo_refs", "ddrinfo_missing_steps", "ddrinfo_missing_calcs"):
            c.execute(f"ALTER TABLE ddr_run DROP COLUMN {col}")
        c.commit(); c.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def cli(*argv):
        import contextlib
        import io
        from fm_ddr.cli import main
        both = io.StringIO()
        with contextlib.redirect_stdout(both), contextlib.redirect_stderr(both):
            main(list(argv))
        return both.getvalue()

    def counts(self, db=None):
        c = sqlite3.connect(db or self.db)
        row = c.execute("SELECT ddrinfo_refs, ddrinfo_missing_steps, "
                        "ddrinfo_missing_calcs FROM ddr_run").fetchone()
        c.close()
        return row

    def test_recorded(self):
        refs, steps, calcs = self.counts()
        self.assertGreater(refs, 0)
        self.assertGreater(steps, 0)
        self.assertGreater(calcs, 0)
        # A DDR has no DDR_INFO: nothing to count
        self.assertEqual(self.counts(self.ddr), (None, None, None))

    def test_in_the_warning_block(self):
        out = self.build_out
        _, steps, calcs = self.counts()
        line = (f"WARNING: {calcs} calculation(s) without reference list and "
                f"{steps} step(s) without step text")
        self.assertEqual(out.count(line), 1, out)
        self.assertLess(out.index(line), out.index("Done:"))
        self.assertIn("search", out[out.index(line):out.index("Done:")])
        # No stray line outside the block
        self.assertNotIn("are not in DDR_INFO", out)

    def test_stats(self):
        _, steps, calcs = self.counts()
        out = self.cli("stats", self.db)
        self.assertIn(f"{calcs} calculation(s) without reference list", out)
        self.assertIn(f"{steps} step(s) without step text", out)
        for db in (self.ddr, self.old):
            self.assertNotIn("without reference list", self.cli("stats", db))

    def test_list(self):
        _, _, calcs = self.counts()
        out = self.cli("list", self.tmp)
        line = next(l for l in out.splitlines() if l.startswith("half.db"))
        self.assertIn(f"{calcs} calculation(s) without reference list", line)
        old = next(l for l in out.splitlines() if l.startswith("old.db"))
        self.assertNotIn("reference list", old)

    def test_investigate_prints_no_note(self):
        # Callers, callees and the body come from step references and step
        # text, never from a calculation's chunk list: nothing to point at
        c = sqlite3.connect(self.db)
        script = c.execute("SELECT name FROM entities WHERE kind = 'script' "
                           "ORDER BY entity_id LIMIT 1").fetchone()[0]
        c.close()
        self.assertNotIn("reference list", self.cli("investigate", self.db, script))

    def test_where_on_a_used_object_prints_no_note(self):
        c = sqlite3.connect(self.db)
        name = c.execute(
            "SELECT t.name FROM refs r JOIN entities t"
            " ON t.entity_id = r.target_entity_id WHERE t.kind = 'script'"
            " ORDER BY t.entity_id LIMIT 1").fetchone()[0]
        c.close()
        out = self.cli("where", self.db, name)
        self.assertNotIn("reference list", out)
        self.assertNotIn("0 reference(s)", out)


SAXML_DDR_FORMS = (
    '<BaseTableCatalog><BaseTable id="1" name="A"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="L" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="A"/></BaseTableSourceReference>'
    '</TableOccurrence></TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="A"/><ObjectList>'
    '<Field id="1" name="K" datatype="Number" fieldtype="Normal">'
    '<AutoEnter type="" alwaysEvaluate="False" overwriteExisting="False"></AutoEnter>'
    '</Field>'
    '<Field id="2" name="C" datatype="Text" fieldtype="Normal">'
    '<AutoEnter type="Calculated" prohibitModification="False" alwaysEvaluate="False"'
    ' overwriteExisting="False"><Calculated enable="True"/><Calculation><Calculation>'
    '<Text>L::K</Text></Calculation></Calculation></AutoEnter></Field>'
    '<Field id="3" name="M" datatype="Text" fieldtype="Normal">'
    '<AutoEnter type="ModificationAccountName" alwaysEvaluate="False"'
    ' overwriteExisting="True"></AutoEnter></Field>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '<ScriptCatalog><Script id="1" name="S"/></ScriptCatalog>'
    '<StepsForScripts><Script><ScriptReference id="1" name="S"/><ObjectList>'
    # Set Field with the default repetition and a value
    '<Step index="0" id="76" name="Set Field" enable="True"><ParameterValues>'
    '<Parameter type="FieldReference"><FieldReference id="1" name="K">'
    '<repetition><Calculation><Calculation><Text>1</Text></Calculation></Calculation>'
    '</repetition><TableOccurrenceReference id="1" name="L"/></FieldReference>'
    '</Parameter><Parameter type="Calculation"><Calculation><Calculation>'
    '<Text>L::K + 1</Text></Calculation></Calculation></Parameter>'
    '</ParameterValues></Step>'
    # Go to Field with a repetition by calculation and nothing else
    '<Step index="1" id="17" name="Go to Field" enable="True"><ParameterValues>'
    '<Parameter type="FieldReference"><FieldReference id="1" name="K">'
    '<repetition><Calculation><Calculation><Text>$i</Text></Calculation></Calculation>'
    '</repetition><TableOccurrenceReference id="1" name="L"/></FieldReference>'
    '</Parameter></ParameterValues></Step>'
    # Show Custom Dialog: the labels are attributes; the DDR writes each
    # as a quoted calculation after the title and the message
    '<Step index="2" id="87" name="Show Custom Dialog" enable="True"><ParameterValues>'
    '<Parameter type="Title"><Calculation><Calculation><Text>"T"</Text>'
    '</Calculation></Calculation></Parameter>'
    '<Parameter type="Message"><Calculation><Calculation><Text>"M"</Text>'
    '</Calculation></Calculation></Parameter>'
    '<Parameter type="Button1" value="OK"><Boolean type="Commit" value="True"/></Parameter>'
    '<Parameter type="Button2" value="Say &quot;no&quot;"><Boolean type="Commit" value="False"/>'
    '</Parameter><Parameter type="Button3"><Boolean type="Commit" value="False"/></Parameter>'
    '</ParameterValues></Step>'
    # Insert Text into a variable: the variable's repetition, as a field's
    '<Step index="3" id="61" name="Insert Text" enable="True"><ParameterValues>'
    '<Parameter type="Target"><Variable value="$v"><repetition><Calculation>'
    '<Calculation><Text>1</Text></Calculation></Calculation></repetition></Variable>'
    '</Parameter></ParameterValues></Step>'
    # Set Variable: the DDR keeps the variable's repetition in calc_text
    '<Step index="4" id="141" name="Set Variable" enable="True"><ParameterValues>'
    '<Parameter type="Variable"><value><Calculation><Calculation><Text>2</Text>'
    '</Calculation></Calculation></value><Name value="$w"/><repetition><Calculation>'
    '<Calculation><Text>3</Text></Calculation></Calculation></repetition></Parameter>'
    '</ParameterValues></Step>'
    '</ObjectList></Script></StepsForScripts>'
    + SAXML_TAIL)


class TestSaXMLDDRForms(unittest.TestCase):
    """Where the export spells a value differently, the index keeps the
    DDR's form: a field target's repetition is not step
    calculation text, a dialog's button labels are, and a field's
    auto-enter JSON carries the evaluate/overwrite flags only where the DDR
    writes them."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_saxforms_")
        path = os.path.join(cls.tmp, "forms.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_DDR_FORMS)
        cls.db = os.path.join(cls.tmp, "forms.db")
        build(path, cls.db, label="forms")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_step_calc_text(self):
        self.assertEqual(self.q(
            "SELECT step_type, calc_text FROM entities WHERE kind = 'script_step'"
            " ORDER BY seq"), [
            ("Set Field", "L::K + 1"),
            ("Go to Field", None),
            ("Show Custom Dialog", '"T"\n"M"\n"OK"\n"Say \\"no\\""'),
            ("Insert Text", None),
            ("Set Variable", "2\n3"),
        ])

    def test_auto_enter(self):
        self.assertEqual(self.q(
            "SELECT name, auto_enter FROM entities WHERE kind = 'field' ORDER BY fm_id"), [
            ("K", None),
            ("C", '{"calc": "L::K", "calc_active": true, "always_evaluate": false, '
                  '"overwrite_existing": false}'),
            ("M", '{"type": "ModificationAccountName", "always_evaluate": false, '
                  '"overwrite_existing": true}'),
        ])


class TestVersionFlag(unittest.TestCase):
    """--version and -V print the bare version on stdout and exit 0."""

    def test_version_flags(self):
        import io, contextlib
        from fm_ddr import __version__
        from fm_ddr.cli import main
        for flag in ("--version", "-V"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                main([flag])
            self.assertEqual(cm.exception.code, 0)
            self.assertEqual(out.getvalue(), f"fmsonar {__version__}\n")


FIXTURE_E = os.path.join(ROOT, "tests", "fixtures", "micro_ddr_e.xml")

# The Save a Copy as XML twin of micro_ddr_e.xml (no Match map and no
# group-by: neither shape is in a measured export)
SAXML_IMPORT_EXPORT = (
    '<BaseTableCatalog><BaseTable id="1" name="CUS"/></BaseTableCatalog>'
    '<TableOccurrenceCatalog>'
    '<TableOccurrence id="1" name="CUS" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="CUS"/></BaseTableSourceReference></TableOccurrence>'
    '<TableOccurrence id="2" name="CUS_alt" type="Local"><BaseTableSourceReference>'
    '<BaseTableReference id="1" name="CUS"/></BaseTableSourceReference></TableOccurrence>'
    '</TableOccurrenceCatalog>'
    '<FieldsForTables><FieldCatalog><BaseTableReference id="1" name="CUS"/><ObjectList>'
    '<Field id="1" name="id" datatype="Number" fieldtype="Normal"/>'
    '<Field id="2" name="name" datatype="Text" fieldtype="Normal"/>'
    '<Field id="3" name="email" datatype="Text" fieldtype="Normal"/>'
    '<Field id="4" name="total" datatype="Number" fieldtype="Normal"/>'
    '<Field id="5" name="skipped" datatype="Text" fieldtype="Normal"/>'
    '<Field id="6" name="matchkey" datatype="Text" fieldtype="Normal"/>'
    '<Field id="7" name="disabledonly" datatype="Text" fieldtype="Normal"/>'
    '</ObjectList></FieldCatalog></FieldsForTables>'
    '<ScriptCatalog><Script id="1" name="Import customers"/>'
    '<Script id="2" name="Export customers"/><Script id="3" name="Find customers"/>'
    '</ScriptCatalog>'
    '<StepsForScripts>'
    '<Script><ScriptReference id="1" name="Import customers"/><ObjectList>'
    '<Step index="0" id="35" name="Import Records" enable="True"><ParameterValues>'
    '<Parameter type="ImportField"><ImportField addasnew="False">'
    '<Target><TableOccurrenceReference id="1" name="CUS"/></Target>'
    '<Field membercount="4">'
    '<Map New="False" name="Source field 1 import to " index="1" id="1" kind="0" Options="0">'
    '<FieldReference id="1" name="id"><TableOccurrenceReference id="1" name="CUS"/>'
    '</FieldReference></Map>'
    '<Map New="False" name="Source field 2 import to " index="2" id="2" kind="0" Options="0">'
    '<FieldReference id="2" name="name"><TableOccurrenceReference id="1" name="CUS"/>'
    '</FieldReference></Map>'
    '<Map New="False" name="Source field 3 not import to " index="3" id="5" kind="1" Options="0">'
    '<FieldReference id="5" name="skipped"><TableOccurrenceReference id="1" name="CUS"/>'
    '</FieldReference></Map>'
    '<Map New="False" name="Source field 4 not import to " index="4" id="0" kind="1" Options="0">'
    '<FieldReference id="0" name=""><TableOccurrenceReference id="1" name="CUS"/>'
    '</FieldReference></Map>'
    '</Field></ImportField></Parameter></ParameterValues></Step>'
    '</ObjectList></Script>'
    '<Script><ScriptReference id="2" name="Export customers"/><ObjectList>'
    '<Step index="0" id="36" name="Export Records" enable="True"><ParameterValues>'
    '<Parameter type="Export"><Export><Order type="Group"></Order><Order type="Field">'
    '<Field value="1"><FieldReference id="3" name="email">'
    '<TableOccurrenceReference id="1" name="CUS"/></FieldReference></Field>'
    '</Order></Export></Parameter></ParameterValues></Step>'
    '</ObjectList></Script>'
    '<Script><ScriptReference id="3" name="Find customers"/><ObjectList>'
    '<Step index="0" id="28" name="Perform Find" enable="True"><ParameterValues>'
    '<Parameter type="FindRequest"><FindRequestSet membercount="2">'
    '<FindRequest membercount="1" action="find" index="1"><find criteria="0">'
    '<FieldReference id="2" name="name" repetition="1">'
    '<TableOccurrenceReference id="1" name="CUS"/></FieldReference></find></FindRequest>'
    '<FindRequest membercount="1" action="omit" index="2"><find criteria="0">'
    '<FieldReference id="3" name="email" repetition="1">'
    '<TableOccurrenceReference id="2" name="CUS_alt"/></FieldReference></find></FindRequest>'
    '</FindRequestSet></Parameter></ParameterValues></Step>'
    '<Step index="1" id="28" name="Perform Find" enable="False"><ParameterValues>'
    '<Parameter type="FindRequest"><FindRequestSet membercount="1">'
    '<FindRequest membercount="1" action="find" index="1"><find criteria="0">'
    '<FieldReference id="7" name="disabledonly" repetition="1">'
    '<TableOccurrenceReference id="1" name="CUS"/></FieldReference></find></FindRequest>'
    '</FindRequestSet></Parameter></ParameterValues></Step>'
    '</ObjectList></Script>'
    '</StepsForScripts>' + SAXML_TAIL)


class TestImportExportFind(unittest.TestCase):
    """Import Records field mappings, Export Records field orders and stored
    find requests are field usages, on the DDR (micro_ddr_e.xml) and on its
    Save a Copy as XML twin."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.tmp = tempfile.mkdtemp(prefix="fmddr_iefind_")
        sax = os.path.join(cls.tmp, "e_saxml.xml")
        with open(sax, "w", encoding="utf-8") as f:
            f.write(SAXML_HEAD + SAXML_IMPORT_EXPORT)
        cls.dbs = {}
        for kind, src in (("ddr", FIXTURE_E), ("sax", sax)):
            db = os.path.join(cls.tmp, kind + ".db")
            with contextlib.redirect_stderr(io.StringIO()):
                build(src, db, label=kind)
            cls.dbs[kind] = db

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, kind, sql):
        conn = sqlite3.connect(self.dbs[kind])
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def refs(self, kind, context):
        return self.q(kind,
            "SELECT p.name, r.target_raw, t.name, r.disabled FROM refs r"
            " JOIN entities s ON s.entity_id = r.source_entity_id"
            " JOIN entities p ON p.entity_id = s.parent_entity_id"
            " LEFT JOIN entities t ON t.entity_id = r.target_entity_id"
            f" WHERE r.context = '{context}' ORDER BY r.ref_id")

    def unused(self, kind):
        return [r[0] for r in self.q(kind, "SELECT name FROM v_unused_fields ORDER BY name")]

    def test_import_fields(self):
        # Import and Match maps are usages; a DoNotImport map (and its empty
        # row) emits nothing. The DDR names the TO once, on the step's
        # <Table>; the export names it on every map
        want = [("Import customers", "CUS::id", "id", 0),
                ("Import customers", "CUS::name", "name", 0)]
        self.assertEqual(self.refs("ddr", "import_field"),
                         want + [("Import customers", "CUS::matchkey", "matchkey", 0)])
        self.assertEqual(self.refs("sax", "import_field"), want)

    def test_export_fields(self):
        # The field order, and on the DDR the group-by field
        want = [("Export customers", "CUS::email", "email", 0)]
        self.assertEqual(self.refs("ddr", "export_field"),
                         want + [("Export customers", "CUS_alt::total", "total", 0)])
        self.assertEqual(self.refs("sax", "export_field"), want)

    def test_unused_import_export(self):
        for kind in ("ddr", "sax"):
            unused = self.unused(kind)
            self.assertIn("skipped", unused)
            for used in ("id", "email"):
                self.assertNotIn(used, unused, kind)
        self.assertNotIn("matchkey", self.unused("ddr"))
        self.assertNotIn("total", self.unused("ddr"))

    def test_find_requests(self):
        # A stored find request's fields (find and omit rows alike); a
        # disabled Perform Find's refs are disabled = 1
        want = [("Find customers", "CUS::name", "name", 0),
                ("Find customers", "CUS_alt::email", "email", 0),
                ("Find customers", "CUS::disabledonly", "disabledonly", 1)]
        for kind in ("ddr", "sax"):
            self.assertEqual(self.refs(kind, "find_request"), want, kind)
            only_disabled = self.q(kind, "SELECT name, only_disabled_refs FROM v_unused_fields"
                                         " WHERE name = 'disabledonly'")
            self.assertEqual(only_disabled, [("disabledonly", 1)], kind)


if __name__ == "__main__":
    unittest.main()
