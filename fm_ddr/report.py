"""Generate a self-contained interactive HTML viewer from a parsed DDR database.

    python -m fm_ddr.cli report solution.db -o solution.html

The stylesheet and viewer JS are extracted from web/index.html at generation
time, so the browser app and the CLI report share ONE viewer implementation —
they cannot drift apart. The DB's entities and edges are embedded as JSON;
the resulting file opens anywhere with no server and no external assets.
"""

import json
import os
import re
import sqlite3

WEB_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")

# Kinds shown in the browsable list. Others (script_step, groups) still power
# the reference graph but aren't listed on their own.
BROWSABLE = ["base_table", "field", "table_occurrence", "relationship",
             "layout", "script", "custom_function", "value_list"]


def _collect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    files = [r["name"] for r in conn.execute("SELECT name FROM files ORDER BY file_id")]
    file_idx = {r["file_id"]: i for i, r in enumerate(
        conn.execute("SELECT file_id FROM files ORDER BY file_id"))}

    ents = []
    for r in conn.execute(
        "SELECT entity_id,kind,name,base_table,grp,parent_entity_id,step_type,file_id,"
        "CASE WHEN kind='custom_function' THEN substr(calc_text,1,65536) "
        "ELSE substr(calc_text,1,4000) END AS calc,"
        "substr(json_extract(extra_json,'$.step_text'),1,2000) AS step_text"
        " FROM entities"):
        ents.append({
            "id": r["entity_id"], "k": r["kind"], "n": r["name"],
            "bt": r["base_table"], "g": r["grp"], "p": r["parent_entity_id"],
            "f": file_idx.get(r["file_id"], 0),
            "st": r["step_type"], "c": r["calc"], "sx": r["step_text"],
        })

    edges = []
    for r in conn.execute(
        "SELECT source_entity_id,target_entity_id,target_raw,target_kind,context,"
        "ambiguous FROM refs"):
        edges.append({
            "s": r["source_entity_id"], "t": r["target_entity_id"],
            "tr": r["target_raw"], "tk": r["target_kind"], "c": r["context"],
            "a": r["ambiguous"] or 0,
        })

    row = conn.execute(
        "SELECT label, ddr_version FROM ddr_run ORDER BY run_id LIMIT 1").fetchone()
    meta = {"label": row["label"] if row else os.path.basename(db_path),
            "ddr_version": row["ddr_version"] if row else ""}
    counts = {k: n for k, n in conn.execute(
        "SELECT kind, COUNT(*) FROM entities GROUP BY kind")}
    total = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM refs WHERE target_entity_id IS NOT NULL").fetchone()[0]
    conn.close()
    return {"meta": meta, "counts": counts, "refsTotal": total,
            "refsResolved": resolved, "files": files,
            "entities": ents, "edges": edges, "browsable": BROWSABLE}


def _extract_viewer_assets():
    """Pull the stylesheet and the shared viewer JS out of web/index.html.
    The <style> block and <script id="viewer-src"> tag are the extraction API."""
    with open(WEB_APP, encoding="utf-8") as f:
        html = f.read()
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    viewer = re.search(r'<script id="viewer-src">(.*?)</script>', html, re.S)
    if not style or not viewer:
        raise RuntimeError(f"could not extract viewer assets from {WEB_APP}")
    return style.group(1), viewer.group(1)


def report(db_path, out_path=None):
    out_path = out_path or os.path.splitext(db_path)[0] + ".html"
    data = _collect(db_path)
    style, viewer = _extract_viewer_assets()
    # Escape '<' so calc text containing '</script>' (FileMaker web-viewer HTML)
    # can't terminate the inline <script> early. '<' decodes back to '<'.
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    title = (data["meta"]["label"] or "DDR").replace("<", "").replace(">", "")
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DDR - {title}</title>
<style>{style}</style>
</head>
<body>
<header><h1>FM DDR Analyzer</h1><span class="sub" id="meta"></span></header>
<div class="wrap" id="wrap" style="display:flex">
  <div class="sidebar">
    <div class="controls">
      <div class="filechips" id="filechips"></div>
      <div class="kinds" id="kinds"></div>
      <input id="q" placeholder="Search entities..." autocomplete="off">
    </div>
    <div class="list" id="list"></div>
  </div>
  <div class="detail" id="detail"></div>
</div>
<script>{viewer}
initViewer({blob});</script>
</body>
</html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
