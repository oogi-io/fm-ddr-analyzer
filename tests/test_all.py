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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fm_ddr.parse import build  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "micro_ddr.xml")
FIXTURE_B = os.path.join(ROOT, "tests", "fixtures", "micro_ddr_b.xml")
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
         _canon_v10(e.get("v10"))]
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


def _canon_v10(v10):
    if not v10:
        return ""
    d = {k: v for k, v in v10.items() if k in V10_KEYS and v is not None}
    return json.dumps(d, sort_keys=True, ensure_ascii=False) if d else ""


def read_sqlite(db_path):
    conn = sqlite3.connect(db_path)
    entities = [
        {"id": i, "kind": k, "name": n, "parent_id": p, "base_table": bt, "grp": g,
         "stored": st, "indexed": ix, "is_global": ig, "auto_enter": ae,
         "v10": (json.loads(ex) if ex and k in ("relationship", "value_list",
                                                "layout_object") else None)}
        for i, k, n, p, bt, g, st, ix, ig, ae, ex in conn.execute(
            "SELECT entity_id,kind,name,parent_entity_id,base_table,grp,"
            "stored,indexed,is_global,auto_enter,extra_json FROM entities")
    ]
    refs = [
        {"source_id": s, "context": c, "target_kind": tk, "target_raw": tr,
         "target_id": t, "ambiguous": a or 0, "disabled": d or 0, "trigger_event": ev}
        for s, c, tk, tr, t, a, d, ev in conn.execute(
            "SELECT source_entity_id,context,target_kind,target_raw,target_entity_id,"
            "ambiguous,disabled,trigger_event FROM refs")
    ]
    conn.close()
    return entities, refs


def read_js(js_json):
    entities = [
        {"id": e["id"], "kind": e["k"], "name": e["n"], "parent_id": e["p"],
         "base_table": e["bt"], "grp": e["g"],
         "stored": e.get("sto"), "indexed": e.get("idx"),
         "is_global": e.get("glb"), "auto_enter": e.get("ae"),
         "v10": (dict(e["ex"] or {}, **({"text": e["tx"]} if e.get("tx") else {}))
                 if (e.get("ex") or e.get("tx")) else None)}
        for e in js_json["entities"]
    ]
    refs = [
        {"source_id": r["s"], "context": r["c"], "target_kind": r["tk"],
         "target_raw": r["tr"], "target_id": r["t"], "ambiguous": r.get("a", 0),
         "disabled": r.get("dis", 0), "trigger_event": r.get("ev")}
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


if __name__ == "__main__":
    unittest.main()
