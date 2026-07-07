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
        [keys[e["id"]], e["base_table"] or "", e["grp"] or ""] for e in entities
    )
    ref_rows = sorted(
        [
            keys.get(r["source_id"]) or "",
            r["context"],
            r["target_kind"],
            r["target_raw"] or "",
            keys.get(r["target_id"]) or "" if r["target_id"] is not None else "",
            r["ambiguous"],
        ]
        for r in refs
    )
    return {"entities": ent_rows, "refs": ref_rows}


def read_sqlite(db_path):
    conn = sqlite3.connect(db_path)
    entities = [
        {"id": i, "kind": k, "name": n, "parent_id": p, "base_table": bt, "grp": g}
        for i, k, n, p, bt, g in conn.execute(
            "SELECT entity_id,kind,name,parent_entity_id,base_table,grp FROM entities")
    ]
    refs = [
        {"source_id": s, "context": c, "target_kind": tk, "target_raw": tr,
         "target_id": t, "ambiguous": a or 0}
        for s, c, tk, tr, t, a in conn.execute(
            "SELECT source_entity_id,context,target_kind,target_raw,target_entity_id,"
            "ambiguous FROM refs")
    ]
    conn.close()
    return entities, refs


def read_js(js_json):
    entities = [
        {"id": e["id"], "kind": e["k"], "name": e["n"], "parent_id": e["p"],
         "base_table": e["bt"], "grp": e["g"]}
        for e in js_json["entities"]
    ]
    refs = [
        {"source_id": r["s"], "context": r["c"], "target_kind": r["tk"],
         "target_raw": r["tr"], "target_id": r["t"], "ambiguous": r.get("a", 0)}
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
        self.assertEqual(c["field"], 6)
        self.assertEqual(c["table_occurrence"], 4)  # incl. external ext_LOG
        self.assertEqual(c["relationship"], 1)
        # 2 layout DEFINITIONS; the go-to-layout button on the layout is a ref
        self.assertEqual(c["layout"], 2)
        self.assertEqual(c["layout_group"], 1)
        self.assertEqual(c["script"], 3)
        self.assertEqual(c["script_group"], 1)
        self.assertEqual(c["script_step"], 7)
        self.assertEqual(c["custom_function"], 1)
        self.assertEqual(c["value_list"], 2)

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
        self.assertEqual({r[0] for r in rows}, {"script_step", "layout"})
        self.assertTrue(all(r[1] for r in rows))

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


if __name__ == "__main__":
    unittest.main()
