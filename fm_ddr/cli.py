"""Command-line interface for the FM DDR analyzer.

    python -m fm_ddr.cli build   DDR.xml [-o out.db] [--label NAME]
    python -m fm_ddr.cli where   out.db  "TO::Field" | "ScriptName" | "Layout"
    python -m fm_ddr.cli search  out.db  "some text"
    python -m fm_ddr.cli sql     out.db  "SELECT ..."
    python -m fm_ddr.cli stats   out.db
"""

import argparse
import os
import re
import sqlite3
import sys


def _rows(conn, q, params=()):
    cur = conn.execute(q, params)
    cols = [c[0] for c in cur.description]
    return cols, cur.fetchall()


def _print_table(cols, rows, limit=200, full=False):
    if not rows:
        print("(no rows)")
        return
    cap = 10**9 if full else 80
    widths = [len(c) for c in cols]
    shown = rows[:limit]
    for r in shown:
        for i, v in enumerate(r):
            widths[i] = min(max(widths[i], len(str(v)) if v is not None else 4), cap)
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(cols))))
    for r in shown:
        print("  ".join((str(v) if v is not None else "").ljust(widths[i])[:cap]
                         for i, v in enumerate(r)))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more rows")


def cmd_build(args):
    from .parse import build
    out = args.out or os.path.splitext(args.ddr[0])[0] + ".db"
    print(f"Parsing {len(args.ddr)} file(s) -> {out} ...", file=sys.stderr)
    summary = build(args.ddr, out, label=args.label, force=getattr(args, "force", False))
    print(f"Done: {out}")
    for k, v in sorted(summary.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {k:24s} {v}")
    total, resolved = summary.get("_refs", 0), summary.get("_refs_resolved", 0)
    if total and resolved / total < 0.95:
        conn = sqlite3.connect(out)
        ver = conn.execute("SELECT ddr_version FROM ddr_run LIMIT 1").fetchone()[0]
        conn.close()
        print(f"\nWARNING: only {100*resolved//total}% of references resolved "
              f"(DDR version {ver}). On a healthy single-file solution expect ~98%. "
              f"Low resolution usually means an unmapped FM-version construct or a "
              f"multi-file solution — check `stats` / v_unresolved.", file=sys.stderr)


def _resolve_term(conn, term):
    """Resolve a search term to concrete entities. 'TO::field' resolves through
    the table occurrence; a bare name matches any entity kind by exact name."""
    if "::" in term:
        to_name, leaf = term.split("::", 1)
        return conn.execute("""
            SELECT f.entity_id, f.kind, f.base_table || '::' || f.name
            FROM entities f
            WHERE f.kind = 'field' AND f.name = ?
              AND f.base_table = (SELECT t.base_table FROM entities t
                                  WHERE t.kind = 'table_occurrence' AND t.name = ?
                                  LIMIT 1)
            ORDER BY f.entity_id""", (leaf, to_name)).fetchall()
    return conn.execute("""
        SELECT entity_id, kind,
               CASE WHEN kind = 'field' THEN base_table || '::' || name ELSE name END
        FROM entities
        WHERE name = ? AND kind IN ('field','script','layout','table_occurrence',
                                    'custom_function','value_list','base_table')
        ORDER BY kind, entity_id""", (term,)).fetchall()



def _connect(db_path):
    """Open an index and warn if it was built by an older parser than the one
    running now — a stale index silently lacks newer reference types (learned
    the hard way: an index built pre-step_target reported 98% healthy while
    missing every Set Field write target)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT parser_version FROM ddr_run LIMIT 1").fetchone()
        built_with = row[0] if row else None
    except sqlite3.OperationalError:
        built_with = None  # pre-1.2.0 schema: column doesn't exist
    from fm_ddr import __version__
    if built_with != __version__:
        print(f"WARNING: index built with fm_ddr {built_with or '<1.2.0'}, "
              f"running {__version__} — newer parsers capture more references. "
              f"Rebuild: fm-ddr build <ddr.xml...> -o {db_path} --force",
              file=sys.stderr)
    return conn


def cmd_where(args):
    """Where is a field / script / layout / TO / CF used?

    Resolves the term to concrete entities first, then reports references per
    entity — a bare field name that exists in several tables is reported per
    table, never lumped together."""
    conn = _connect(args.db)
    term = args.name
    targets = _resolve_term(conn, term)
    if not targets:
        print(f"No entity named '{term}' found. Try `search` for free text, "
              f"or qualify a field as TO::field.")
        return
    if len(targets) > 1:
        labels = ", ".join(f"{label} ({kind})" for _, kind, label in targets)
        print(f"'{term}' matches {len(targets)} entities: {labels}")
        print("Showing references per entity. Qualify as TO::field to narrow.\n")
    for entity_id, kind, label in targets:
        cols, rows = _rows(conn,
            """SELECT context, source_kind, source_parent_name, source_name,
                      ambiguous
               FROM v_usage WHERE target_id = ?
               ORDER BY context, source_kind, source_parent_name, source_name""",
            (entity_id,))
        print(f"# {label} ({kind}) — {len(rows)} reference(s)")
        if rows:
            _print_table(cols, rows, limit=args.limit)
        else:
            print("(none — but see COVERAGE.md for what references are not captured)")
        print()


def cmd_search(args):
    conn = _connect(args.db)
    sql = ("""SELECT ti.kind, ti.name, snippet(text_index, 4, '[', ']', '...', 12) AS match
              FROM text_index ti
              WHERE text_index MATCH ? ORDER BY rank LIMIT ?""")
    try:
        cols, rows = _rows(conn, sql, (args.query, args.limit))
    except sqlite3.OperationalError:
        # The query used FTS5 operator syntax that didn't parse (unbalanced
        # quotes/parens, a bare AND/NEAR, a colon). Fall back to treating the
        # whole thing as a literal phrase so a plain search never crashes.
        literal = '"' + args.query.replace('"', '""') + '"'
        cols, rows = _rows(conn, sql, (literal, args.limit))
    print(f"# Text search '{args.query}'  ({len(rows)} hits)\n")
    _print_table(cols, rows, limit=args.limit)


def cmd_sql(args):
    conn = _connect(args.db)
    cols, rows = _rows(conn, args.query)
    _print_table(cols, rows, limit=args.limit, full=getattr(args, "full", False))


def cmd_snippet(args):
    from .snippet import snippet
    res = snippet(args.ddr, args.script, out_path=args.out,
                  to_clipboard=args.clip, script_id=getattr(args, "id", None))
    print(f"{res['steps']} steps -> {res['bytes']} bytes of fmxmlsnippet")
    if res["out"]:
        print(f"written to {res['out']}")
    if res["clipboard"]:
        print("on the clipboard (XMSS) - paste into FileMaker Script Workspace")



def cmd_investigate(args):
    """One-shot neighborhood report for a script: everything the investigation
    protocol demands, in one command — survey context (callers incl. layout
    buttons WITH their launch params), callees, $$global hygiene, and the full
    body. Exists because every A/B-judged miss traced to one of these being
    skipped when it took a separate query."""
    conn = _connect(args.db)
    # resolve the script (exact name, LIKE, or fm_id)
    rows = conn.execute(
        "SELECT entity_id, fm_id, name, grp FROM entities WHERE kind='script' "
        "AND (name = ? OR fm_id = ?)", (args.script, args.script)).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT entity_id, fm_id, name, grp FROM entities WHERE kind='script' "
            "AND name LIKE ? ORDER BY name", (f"%{args.script}%",)).fetchall()
    if not rows:
        print(f"No script matching '{args.script}'.")
        return
    if len(rows) > 1:
        print(f"'{args.script}' matches {len(rows)} scripts — pick one (name or fm_id):")
        for _, fmid, nm, grp in rows[:30]:
            print(f"  [{fmid}] {nm}" + (f"   ({grp})" if grp else ""))
        return
    eid, fmid, name, grp = rows[0]
    nsteps = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE parent_entity_id=? AND kind='script_step'",
        (eid,)).fetchone()[0]
    print(f"# {name}")
    print(f"fm_id {fmid} · group {grp or '-'} · {nsteps} steps\n")

    # ---- callers (scripts, triggers, layout buttons) ----
    callers = conn.execute("""
        SELECT context, source_kind, source_parent_name, source_name, COUNT(*)
        FROM v_usage WHERE target_id = ?
        GROUP BY context, source_kind, source_parent_name, source_name
        ORDER BY context, source_parent_name""", (eid,)).fetchall()
    print(f"## Callers ({len(callers)})")
    if not callers:
        print("(none found — may still run from a menu, external file, or schedule)")
    for ctx, sk, spn, sn, n in callers:
        where = f"{spn} > {sn}" if spn and sn else (sn or spn or "?")
        print(f"  {ctx:15} {sk:14} {where}" + (f"  x{n}" if n > 1 else ""))

    # ---- layout launch sites with their params (the round-2 lesson) ----
    launches = conn.execute("""
        WITH RECURSIVE up(entity_id, parent, name, kind, top) AS (
          SELECT o.entity_id, o.parent_entity_id, o.name, o.kind, o.entity_id
          FROM refs r JOIN entities o ON o.entity_id = r.source_entity_id
          WHERE r.target_entity_id = ? AND o.kind='layout_object'
          UNION ALL
          SELECT p.entity_id, p.parent_entity_id, p.name, p.kind, up.top
          FROM up JOIN entities p ON p.entity_id = up.parent
          WHERE up.kind != 'layout'
        )
        SELECT DISTINCT
          (SELECT name FROM up u2 WHERE u2.top = up.top AND u2.kind='layout'),
          o.name,
          json_extract(o.extra_json,'$.object_type'),
          json_extract(o.extra_json,'$.step_text'),
          json_extract(o.extra_json,'$.hide_calc')
        FROM up JOIN entities o ON o.entity_id = up.top
        WHERE up.kind='layout'""", (eid,)).fetchall()
    if launches:
        print(f"\n## Layout launch sites ({len(launches)}) — check the params")
        for lay, oname, otype, stext, hide in launches:
            print(f"  layout {lay!r} · {otype or 'object'}"
                  + (f" {oname!r}" if oname else ""))
            if stext:
                print("    " + (stext or "").replace("\n", "\n    ")[:2000])
            if hide:
                print(f"    hidden when: {hide[:300]}")

    # ---- callees ----
    callees = conn.execute("""
        SELECT DISTINCT u.target_name FROM refs r
        JOIN entities st ON st.entity_id = r.source_entity_id
                        AND st.parent_entity_id = ?
        JOIN v_usage u ON u.ref_id = r.ref_id
        WHERE u.context='perform_script' ORDER BY 1""", (eid,)).fetchall()
    print(f"\n## Calls out to ({len(callees)})")
    for (nm,) in callees:
        print(f"  {nm}")

    # ---- $$global hygiene (write-only detector) ----
    hygiene = conn.execute("""
        WITH t AS (
          SELECT json_extract(s.extra_json,'$.step_text') AS txt
          FROM entities s WHERE s.parent_entity_id = ? AND s.kind='script_step'
            AND json_extract(s.extra_json,'$.step_text') LIKE 'Set Variable [ $$%'
        ), w AS (
          SELECT DISTINCT substr(txt, instr(txt,'$$'),
            CASE WHEN instr(substr(txt,instr(txt,'$$')),';') > 0
                 THEN instr(substr(txt,instr(txt,'$$')),';') - 1 ELSE 40 END) AS var
          FROM t
        )
        SELECT w.var,
          (SELECT COUNT(*) FROM entities st WHERE st.kind='script_step'
             AND json_extract(st.extra_json,'$.step_text')
                 LIKE '%'||w.var||'%'
             AND json_extract(st.extra_json,'$.step_text')
                 NOT LIKE 'Set Variable [ '||w.var||';%') AS other_mentions
        FROM w ORDER BY other_mentions""", (eid,)).fetchall()
    if hygiene:
        print(f"\n## $$globals written here ({len(hygiene)})")
        for var, om in hygiene:
            flag = "  <-- WRITE-ONLY in steps? confirm with: fm-ddr search <db> '\"%s\"'" % var.lstrip("$") if om == 0 else ""
            print(f"  {var:48} step-mentions elsewhere: {om}{flag}")

    # ---- record operations (same engine as cmd_mutations) ----
    steps_rows = conn.execute(
        "SELECT s.seq, s.step_type, json_extract(s.extra_json,'$.step_text'), "
        "COALESCE(json_extract(s.extra_json,'$.disabled'),0) "
        "FROM entities s WHERE s.parent_entity_id=? AND s.kind='script_step' "
        "ORDER BY s.entity_id", (eid,)).fetchall()
    rec_ops = _scan_record_ops(steps_rows)
    if rec_ops:
        print(f"\n## Record operations ({len(rec_ops)}) — context clues, not resolved claims")
        for op in rec_ops:
            tag = op["tier"] if op["state"] == "live" else op["state"]
            line = f"  {tag:9} {op['step_type']:24} ctx: {op['context'] or '-'}"
            if op["note"]:
                line += f"   ({op['note']})"
            print(line)

    # ---- chain profile (default-on, compact; --chain expands) ----
    # Round-5 lesson: a capability behind an unknown flag is undiscovered.
    # The compact rollup is ALWAYS printed when the script has callees.
    chain, n_direct, cycles = _chain_scripts(conn, eid)
    if n_direct:
        ops = _chain_record_ops(conn, chain)
        live = [o for o in ops if o["state"] == "live"]
        dels = [o for o in live if o["kind"] == "delete"]
        crs = [o for o in live if o["kind"] == "create"]
        tagged = len(ops) - len(live)
        print(f"\n## Chain (recursive callees): {len(chain)} scripts "
              f"({n_direct} direct{'; cycles marked' if cycles else ''})")
        from collections import Counter
        dc = Counter(o["step_type"] for o in dels)
        dsum = ", ".join(f"{k} x{v}" for k, v in dc.most_common())
        print(f"   deleters: {len(dels)} site(s) — {dsum or 'none'}")
        for o in sorted(dels, key=lambda o: (o["step_type"], o["name"])):
            extra = f" → {o['context']}" if o["step_type"] == "Truncate Table" and o["tier"] == "confident" else \
                    (f"  ctx: {o['context']}" if o["context"] else "  ctx: caller-set")
            print(f"     {o['tier']:9} [{o['fmid']}] {o['name'][:52]} — {o['step_type']}{extra}")
        cc = Counter(o["step_type"] for o in crs)
        csum = ", ".join(f"{k} x{v}" for k, v in cc.most_common())
        nscripts = len({o["fmid"] for o in crs})
        print(f"   creators: {len(crs)} site(s) in {nscripts} script(s) — {csum or 'none'}")
        if tagged:
            print(f"   tagged (find-mode requests / disabled), not counted above: {tagged}")
        if not args.chain:
            print("   (--chain for the full per-site census)")
        else:
            tier_rank = {"confident": 0, "likely": 1, "check": 2}
            hdr = ["tier", "script", "step", "context clue", "note"]
            for kind, label in (("create", "CHAIN CREATORS"), ("delete", "CHAIN DELETERS")):
                rows_ = sorted([o for o in live if o["kind"] == kind],
                               key=lambda o: (tier_rank[o["tier"]], o["name"], o["seq"]))
                print(f"\n### {label} ({len(rows_)})")
                _print_table(hdr, [[o["tier"], f"[{o['fmid']}] {o['name']}",
                                    o["step_type"], o["context"] or "-",
                                    o["note"] or ""] for o in rows_], limit=100000)
            tg = [o for o in ops if o["state"] != "live"]
            if tg:
                print(f"\n### CHAIN TAGGED ({len(tg)})")
                _print_table(["tag", "script", "step", "context clue"],
                             [[("find request" if o["state"] == "find" else "disabled"),
                               f"[{o['fmid']}] {o['name']}", o["step_type"],
                               o["context"] or "-"] for o in tg], limit=100000)

    # ---- body ----
    if not args.no_body:
        print(f"\n## Body ({nsteps} steps)")
        for seq, stype, txt in conn.execute("""
            SELECT seq, step_type, json_extract(extra_json,'$.step_text')
            FROM entities WHERE parent_entity_id=? AND kind='script_step'
            ORDER BY entity_id""", (eid,)):
            print(f"  {txt or stype or ''}")


SKILL_RAW_URL = ("https://raw.githubusercontent.com/oogi-io/fm-ddr-analyzer/"
                 "main/fm_ddr/skill/SKILL.md")



def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} GB"


def cmd_list(args):
    """List fmsonar databases in a folder — solution label, build time, parser
    version, size, and staleness (older parser / source DDR changed since the
    build). Default folder is the central cache the Claude skill uses."""
    from fm_ddr import __version__
    d = os.path.expanduser(args.dir or "~/.fmsonar/dbs")
    if not os.path.isdir(d):
        print(f"No such folder: {d}")
        if not args.dir:
            print("The central cache is created the first time a database is "
                  "built there. Point at another folder: fm-ddr list <dir>")
        sys.exit(1)
    rows = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".db"):
            continue
        path = os.path.join(d, name)
        size = _human_size(os.path.getsize(path))
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            label, source_path, parsed_at = conn.execute(
                "SELECT label, source_path, parsed_at FROM ddr_run LIMIT 1"
            ).fetchone()
            try:
                built_with = conn.execute(
                    "SELECT parser_version FROM ddr_run LIMIT 1").fetchone()[0]
            except sqlite3.OperationalError:
                built_with = None  # pre-1.2.0 index
            nfiles = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError, TypeError):
            rows.append([name, "(not an fmsonar database)", "", "", size, ""])
            continue
        issues = []
        if built_with != __version__:
            issues.append(f"parser {built_with or '<1.2.0'} (now {__version__}) — rebuild")
        # source DDR changed since the build?
        try:
            from datetime import datetime
            built_ts = datetime.fromisoformat(parsed_at).timestamp()
            sources = [q for q in (source_path or "").split(";") if q]
            seen = False
            for q in sources:
                if os.path.exists(q):
                    seen = True
                    if os.path.getmtime(q) > built_ts:
                        issues.append("DDR newer than index — rebuild")
                        break
            if sources and not seen:
                issues.append("source DDR not found")
        except (ValueError, OSError):
            pass
        built = (parsed_at or "")[:16].replace("T", " ")
        rows.append([name, label or "", built, built_with or "<1.2.0",
                     f"{size} · {nfiles}f", " · ".join(issues) or "ok"])
    if not rows:
        print(f"No databases in {d}")
        return
    print(f"# {d}\n")
    _print_table(["db", "label", "built (UTC)", "parser", "size", "status"], rows)



# ---- record operations (mutations) ------------------------------------
# Record-op steps carry no table: they act on the runtime layout context.
# fmsonar therefore SURFACES them with a context clue and a confidence tier,
# and never resolves or drops a row. Ambiguity rounds down to 'check'.

RECORD_CREATE = {"New Record/Request", "Duplicate Record/Request", "Import Records"}
RECORD_DELETE = {"Delete Record/Request", "Delete All Records",
                 "Delete Portal Row", "Truncate Table"}
_FIND_ON = {"Enter Find Mode", "Modify Last Find"}
_FIND_OFF = {"Perform Find", "Enter Browse Mode", "Constrain Found Set",
             "Extend Found Set", "Show All Records"}
_BLOCK_OPEN = {"If", "Loop"}
_BLOCK_CLOSE = {"End If", "End Loop"}
_BRANCH_ALT = {"Else", "Else If"}

_TO_RE = re.compile(r"\(([A-Za-z0-9_ .\u2022-]+)\)\s*\]\s*$")
_TRUNC_RE = re.compile(r"Table:\s*[\u201c\"]([^\u201d\"]+)[\u201d\"]")


def _parse_layout_to(txt):
    m = _TO_RE.search((txt or "").strip())
    return m.group(1) if m else None


def _scan_record_ops(steps):
    """steps: ordered (seq, step_type, step_text) in document order.
    Returns op dicts: kind, step_type, seq, tier, context, note, state.
    state: live | find (find-mode request, not a mutation) | disabled."""
    ops = []
    ctx = None          # (to_name, set_depth, conditional, via_gtrr)
    depth = 0
    find = False
    for row in steps:
        seq, st, txt = row[0], row[1], row[2]
        flag = row[3] if len(row) > 3 else 0
        txt = txt or ""
        disabled = bool(flag) or txt.lstrip().startswith("//")
        if not disabled:
            if st in _BLOCK_OPEN:
                depth += 1
            elif st in _BLOCK_CLOSE:
                depth = max(0, depth - 1)
                if ctx and ctx[1] > depth:
                    ctx = (ctx[0], ctx[1], True, ctx[3])   # its block closed
            elif st in _BRANCH_ALT:
                if ctx and ctx[1] >= depth:
                    ctx = (ctx[0], ctx[1], True, ctx[3])   # other branch runs instead
            elif st == "Go to Layout":
                to = _parse_layout_to(txt)
                ctx = (to, depth, False, False) if to else None
            elif st.startswith("Go to Related"):
                to = _parse_layout_to(txt)
                ctx = (to, depth, True, True) if to else None  # GTRR can no-match
            elif st in _FIND_ON:
                find = True
            elif st in _FIND_OFF:
                find = False
        if st not in RECORD_CREATE and st not in RECORD_DELETE:
            continue
        kind = "create" if st in RECORD_CREATE else "delete"
        state = "disabled" if disabled else ("find" if find else "live")
        tier, context, note = _tier_record_op(st, txt, ctx)
        ops.append(dict(kind=kind, step_type=st, seq=seq, tier=tier,
                        context=context, note=note, state=state))
    return ops


def _tier_record_op(st, txt, ctx):
    if st == "Truncate Table":
        m = _TRUNC_RE.search(txt)
        if m:
            return "confident", m.group(1), "table named in the step"
        # no table named: acts on current context, falls through
    if st == "Delete Portal Row":
        clue = ctx[0] if ctx else None
        return "check", clue, "portal row: layout context is NOT the target table"
    if st == "Import Records":
        clue = ctx[0] if ctx else None
        return "check", clue, "import target set by its field mapping (not in DDR text)"
    if ctx is None:
        return "check", None, "context set by caller"
    to, _d, cond, gtrr = ctx
    if cond or gtrr:
        return "check", to, "conditional context" + (" (via Go to Related Record)" if gtrr else "")
    return "likely", to, None


def _script_steps_ordered(conn, where_sql, params):
    return conn.execute(
        "SELECT scr.fm_id, scr.name, s.seq, s.step_type, "
        "json_extract(s.extra_json,'$.step_text'), "
        "COALESCE(json_extract(s.extra_json,'$.disabled'),0) "
        "FROM entities s JOIN entities scr ON scr.entity_id = s.parent_entity_id "
        "AND scr.kind='script' WHERE s.kind='script_step' " + where_sql +
        " ORDER BY scr.entity_id, s.entity_id", params).fetchall()



def _script_callees(conn, eid):
    """Entity ids of scripts this script's steps perform (incl. trigger installs)."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT tgt.entity_id FROM refs r "
        "JOIN entities st ON st.entity_id = r.source_entity_id AND st.parent_entity_id = ? "
        "JOIN entities tgt ON tgt.entity_id = r.target_entity_id AND tgt.kind='script' "
        "WHERE r.context IN ('perform_script','trigger') "
        "AND r.disabled = 0 "  # Dead code never calls
        "AND r.target_entity_id IS NOT NULL", (eid,)).fetchall()]


def _chain_scripts(conn, root_eid, max_depth=None):
    """Cycle-safe BFS over recursive callees. Returns (ordered entity ids incl.
    root, direct-callee count, cycles_seen)."""
    direct = _script_callees(conn, root_eid)
    seen = {root_eid}
    order = [root_eid]
    cycles = False
    frontier = list(direct)
    depth = 1
    while frontier and (max_depth is None or depth <= max_depth):
        nxt = []
        for eid in frontier:
            if eid in seen:
                cycles = True
                continue
            seen.add(eid)
            order.append(eid)
            nxt.extend(_script_callees(conn, eid))
        frontier = nxt
        depth += 1
    return order, len(direct), cycles


def _chain_record_ops(conn, eids):
    """Run the record-op engine over every script in the chain."""
    hits = []
    for eid in eids:
        fmid, name = conn.execute(
            "SELECT fm_id, name FROM entities WHERE entity_id=?", (eid,)).fetchone()
        steps = conn.execute(
            "SELECT s.seq, s.step_type, json_extract(s.extra_json,'$.step_text'), "
            "COALESCE(json_extract(s.extra_json,'$.disabled'),0) "
            "FROM entities s WHERE s.parent_entity_id=? AND s.kind='script_step' "
            "ORDER BY s.entity_id", (eid,)).fetchall()
        for op in _scan_record_ops(steps):
            op["fmid"], op["name"] = fmid, name
            hits.append(op)
    return hits


def cmd_mutations(args):
    """Inventory of record operations (create/delete/duplicate/import/truncate)
    with a context CLUE and a confidence tier - never a resolved claim.
    Complete by construction: find-mode and disabled ops are shown and tagged,
    not dropped."""
    conn = _connect(args.db)
    rows = _script_steps_ordered(conn, "", ())
    from collections import defaultdict
    per_script = defaultdict(list)
    names = {}
    for fmid, nm, seq, st, txt, dis in rows:
        per_script[fmid].append((seq, st, txt, dis))
        names[fmid] = nm
    hits = []
    like = (args.like or "").lower()
    for fmid, steps in per_script.items():
        for op in _scan_record_ops(steps):
            hay = (names[fmid] + " " + (op["context"] or "")).lower()
            if like and like not in hay:
                continue
            op["fmid"], op["name"] = fmid, names[fmid]
            hits.append(op)
    if not hits:
        print("No record operations matched" + (f" '{args.like}'" if like else "") + ".")
        return
    tier_rank = {"confident": 0, "likely": 1, "check": 2}
    header = ["tier", "script", "step", "context clue", "note"]
    for kind, label in (("create", "CREATORS"), ("delete", "DELETERS")):
        live = sorted([h for h in hits if h["kind"] == kind and h["state"] == "live"],
                      key=lambda h: (tier_rank[h["tier"]], h["name"], h["seq"]))
        print(f"\n## {label} ({len(live)})")
        _print_table(header, [[h["tier"], f"[{h['fmid']}] {h['name']}",
                               h["step_type"], h["context"] or "-",
                               h["note"] or ""] for h in live],
                     limit=getattr(args, "limit", 200))
    tagged = [h for h in hits if h["state"] != "live"]
    if tagged:
        print(f"\n## Tagged, not counted above ({len(tagged)})")
        _print_table(["tag", "script", "step", "context clue"],
                     [[("find request (not a mutation)" if h["state"] == "find"
                        else "disabled"),
                       f"[{h['fmid']}] {h['name']}", h["step_type"],
                       h["context"] or "-"] for h in tagged],
                     limit=getattr(args, "limit", 200))
    print("\nContext clue = nearest preceding Go to Layout. Layout context and "
          "browse/find mode\nare runtime state a script can inherit from its "
          "caller: treat 'likely' as a strong\nhint, not a guarantee; 'check' "
          "rows tell you what to verify. Only 'confident'\n(Truncate with a "
          "named table) is stated by the DDR itself.")


def cmd_install_skill(args):
    """Install the fmsonar skill for Claude Code, or check its freshness.

    --check   compare the installed skill against the one shipped with this
              fm_ddr version (fast, offline). Exit 0 = up to date, 1 = differs,
              2 = not installed.
    --remote  compare against the current GitHub main instead (network).
    Neither flag: (re)install the packaged skill.
    """
    import hashlib
    import shutil

    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill")
    skill_files = sorted(f for f in os.listdir(src_dir) if f.endswith(".md"))
    src = os.path.join(src_dir, "SKILL.md")
    dest_dir = os.path.expanduser("~/.claude/skills/fmsonar")
    dest = os.path.join(dest_dir, "SKILL.md")
    from fm_ddr import __version__

    def digest(b):
        return hashlib.sha256(b).hexdigest()[:12]

    def tree_bytes(base, names):
        # Aggregate digest input over every shipped skill file, missing files included
        out = b""
        for n in names:
            p = os.path.join(base, n)
            out += n.encode() + b"\x00"
            out += open(p, "rb").read() if os.path.exists(p) else b"<missing>"
        return out

    if args.check or args.remote:
        if not os.path.exists(dest):
            print(f"fmsonar skill is not installed ({dest} missing). "
                  f"Run: fm-ddr install-skill")
            sys.exit(2)
        installed = (open(dest, "rb").read() if args.remote
                     else tree_bytes(dest_dir, skill_files))
        if args.remote:
            import urllib.request
            try:
                ref = urllib.request.urlopen(SKILL_RAW_URL, timeout=10).read()
            except Exception as e:  # noqa: BLE001 — report and bail, offline is fine
                print(f"Could not fetch {SKILL_RAW_URL}: {e}")
                sys.exit(2)
            ref_label = "GitHub main"
            hint = ("update your install first (pipx upgrade fmsonar / pipx reinstall fmsonar, "
                    "or git pull in a clone), then: fm-ddr install-skill")
        else:
            ref = tree_bytes(src_dir, skill_files)
            ref_label = f"the skill shipped with fm_ddr {__version__}"
            hint = "run: fm-ddr install-skill"
        if digest(installed) == digest(ref):
            print(f"fmsonar skill is up to date with {ref_label}.")
            sys.exit(0)
        print(f"fmsonar skill DIFFERS from {ref_label} "
              f"(installed {digest(installed)}, reference {digest(ref)}).")
        print(f"To update: {hint}")
        sys.exit(1)

    os.makedirs(dest_dir, exist_ok=True)
    for name in skill_files:
        shutil.copy(os.path.join(src_dir, name), os.path.join(dest_dir, name))
    print(f"Installed the 'fmsonar' skill (fm_ddr {__version__}, "
          f"{', '.join(skill_files)}) -> {dest_dir}")
    print("Claude Code can now analyze FileMaker DDRs from ANY directory -")
    print("just mention a DDR or ask a where-used question.")
    print("Freshness check anytime: fm-ddr install-skill --check   "
          "(or --remote to compare against GitHub main)")


def cmd_clip(args):
    from .snippet import clip_text_to_fm
    n = clip_text_to_fm()
    print(f"{n} bytes -> FileMaker clipboard (XMSS); paste into Script Workspace")


def cmd_report(args):
    from .report import report
    out = report(args.db, args.out)
    print(f"Wrote {out}")


def cmd_stats(args):
    conn = _connect(args.db)
    print("# Entity counts")
    _, rows = _rows(conn, "SELECT kind, COUNT(*) n FROM entities GROUP BY kind ORDER BY n DESC")
    for kind, n in rows:
        print(f"  {kind:22s} {n}")
    tot = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    res = conn.execute("SELECT COUNT(*) FROM refs WHERE target_entity_id IS NOT NULL").fetchone()[0]
    dis = conn.execute("SELECT COUNT(*) FROM refs WHERE disabled = 1").fetchone()[0]
    print(f"\n# References: {tot} total, {res} resolved ({100*res//max(tot,1)}%)")
    if dis:
        print(f"  {dis} from disabled (commented-out) steps — excluded from v_usage, see v_usage_disabled")
    print("\n# Unresolved by kind/context (external / built-in / broken)")
    _, rows = _rows(conn, "SELECT * FROM v_unresolved")
    _print_table(["target_kind", "context", "n"], rows)


def _banner(stream=None):
    """One-line brand header: teal concentric-ring glyph + two-tone wordmark.
    Color only on a real terminal (and honor NO_COLOR); plain text otherwise."""
    from fm_ddr import __version__
    if stream is None:
        stream = sys.stdout
    if stream.isatty() and not os.environ.get("NO_COLOR"):
        t = "\033[38;5;38m"; b = "\033[1m"; d = "\033[38;5;244m"; r = "\033[0m"
        return f"{t}◎{r} {b}{t}fm{r}{b}sonar{r} {d}{__version__}{r}  FileMaker DDR explorer"
    return f"◎ fmsonar {__version__}  FileMaker DDR explorer"


def main(argv=None):
    p = argparse.ArgumentParser(prog="fmsonar", description=_banner(),
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--debug", action="store_true",
                   help="show the full Python traceback on error")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build",
                       help="parse DDR XML(s) or a Summary.xml manifest into SQLite")
    b.add_argument("ddr", nargs="+",
                   help="one or more *_fmp12.xml files, or a single Summary.xml")
    b.add_argument("-o", "--out")
    b.add_argument("--label")
    b.add_argument("--force", action="store_true",
                   help="overwrite the -o file even if it is not an fmsonar database")
    b.set_defaults(func=cmd_build)

    w = sub.add_parser("where", help="where is a field/script/layout/TO/CF used")
    w.add_argument("db")
    w.add_argument("name")
    w.add_argument("--limit", type=int, default=200)
    w.set_defaults(func=cmd_where)

    s = sub.add_parser("search", help="full-text search across calcs/steps/names")
    s.add_argument("db")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_search)

    q = sub.add_parser("sql", help="run arbitrary SQL")
    q.add_argument("db")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=200)
    q.add_argument("--full", action="store_true",
                   help="no cell truncation (default clips cells at 80 chars)")
    q.set_defaults(func=cmd_sql)

    inv = sub.add_parser("investigate",
                         help="one-shot neighborhood report for a script "
                              "(callers, layout launch params, callees, "
                              "$$global hygiene, body)")
    inv.add_argument("db")
    inv.add_argument("script", help="script name (exact or partial) or fm_id")
    inv.add_argument("--no-body", action="store_true",
                     help="omit the full step body")
    inv.add_argument("--chain", action="store_true",
                     help="expand the (always-shown) chain rollup into the full "
                          "per-site record-op census")
    inv.set_defaults(func=cmd_investigate)

    sn = sub.add_parser("snippet",
                        help="copy a script's steps as a FileMaker clipboard snippet")
    sn.add_argument("ddr", help="DDR XML file containing the script")
    sn.add_argument("script", help="script name (exact)")
    sn.add_argument("--id", help="FileMaker script id, to disambiguate a duplicate name")
    sn.add_argument("-o", "--out", help="write fmxmlsnippet XML to this file")
    sn.add_argument("--clip", action="store_true",
                    help="place on the macOS clipboard (XMSS flavor) for direct paste")
    sn.set_defaults(func=cmd_snippet)

    ls = sub.add_parser("list",
                        help="list databases in a folder (default ~/.fmsonar/dbs) "
                             "with label, build time, parser version, staleness")
    ls.add_argument("dir", nargs="?", default=None,
                    help="folder to scan (default: the central cache)")
    ls.set_defaults(func=cmd_list)

    mu = sub.add_parser("mutations",
                        help="record operations (create/delete/duplicate/import/"
                             "truncate) with context clues and confidence tiers")
    mu.add_argument("db")
    mu.add_argument("--like", default=None,
                    help="loose filter: substring of script name or context clue")
    mu.add_argument("--limit", type=int, default=100000,
                    help="row cap per section (default: effectively unlimited — an inventory must be complete)")
    mu.set_defaults(func=cmd_mutations)

    ins = sub.add_parser("install-skill",
                         help="install the Claude Code skill globally (~/.claude/skills)")
    ins.add_argument("--check", action="store_true",
                     help="don't install; report whether the installed skill "
                          "matches the one shipped with this fm_ddr version")
    ins.add_argument("--remote", action="store_true",
                     help="don't install; compare the installed skill against "
                          "GitHub main (network)")
    ins.set_defaults(func=cmd_install_skill)

    cl = sub.add_parser("clip",
                        help="convert snippet XML text on the clipboard to FileMaker objects (macOS)")
    cl.set_defaults(func=cmd_clip)

    rp = sub.add_parser("report", help="generate a self-contained interactive HTML viewer")
    rp.add_argument("db")
    rp.add_argument("-o", "--out")
    rp.set_defaults(func=cmd_report)

    st = sub.add_parser("stats", help="entity + reference counts")
    st.add_argument("db")
    st.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    # Brand line on every interactive run; stderr so piped/parsed stdout stays clean
    if sys.stderr.isatty():
        print(_banner(sys.stderr), file=sys.stderr)
    try:
        args.func(args)
    except (BrokenPipeError, KeyboardInterrupt):
        raise
    except Exception as e:
        if args.debug:
            raise
        msg = str(e) or e.__class__.__name__
        print(f"error: {msg}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
