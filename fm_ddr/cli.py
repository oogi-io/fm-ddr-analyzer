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
    # sqlite3.connect CREATES a missing file — a typo'd path would leave a
    # phantom empty .db behind and produce a misleading "<1.2.0" stale warning
    if not os.path.exists(db_path):
        raise SystemExit(f"error: {db_path} does not exist. "
                         f"Build an index first: fm-ddr build <ddr.xml...> -o {db_path}")
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


def cmd_cascades(args):
    """What deletes into a table by relationship cascade? (v1.10.0)

    Deleting a record on deleting_table cascades into victim_table. This is a
    DDR fact no step census can see — a Delete Record on the deleting side
    silently removes victim-side records."""
    conn = _connect(args.db)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='v_cascades'").fetchone():
        raise SystemExit("error: this index has no v_cascades view (built with "
                         "parser < 1.10.0). Rebuild: fm-ddr build <ddr.xml...> "
                         f"-o {args.db} --force")
    where, params = "", ()
    if args.table:
        where = "WHERE victim_table = ? OR victim_to = ?"
        params = (args.table, args.table)
    cols, rows = _rows(conn,
        f"""SELECT deleting_table, deleting_to, victim_table, victim_to, relationship
            FROM v_cascades {where}
            ORDER BY victim_table, deleting_table""", params)
    scope = f" into {args.table}" if args.table else ""
    print(f"# Cascade deletes{scope} — {len(rows)} relationship side(s)\n")
    if rows:
        _print_table(cols, rows, limit=args.limit)
    else:
        print("(none — no relationship side has cascade delete enabled"
              + (f" for {args.table}" if args.table else "") + ")")


def cmd_valuelist(args):
    """Show a value list's DEFINITION (source, fields, custom values) + where
    it's bound. (v1.10.0)"""
    import json as _json_mod
    conn = _connect(args.db)
    rows = conn.execute(
        """SELECT entity_id, name, extra_json, file_id FROM entities
           WHERE kind='value_list' AND name LIKE ? ORDER BY name""",
        (args.name,)).fetchall()
    if not rows:
        print(f"No value list matching '{args.name}'. LIKE patterns allowed (%).")
        return
    for eid, name, extra, _f in rows:
        d = _json_mod.loads(extra) if extra else {}
        print(f"# Value list: {name}")
        src = d.get("source")
        if src == "Custom":
            vals = d.get("custom_values") or []
            print(f"  source: Custom — {len(vals)} value(s)")
            for v in vals[:args.limit]:
                print(f"    • {v}")
            if len(vals) > args.limit:
                print(f"    ... {len(vals) - args.limit} more")
        elif src == "Field":
            for slot in ("primary", "secondary"):
                if d.get(slot):
                    s = d[slot]
                    opts = ", ".join(k for k in ("show", "sort") if s.get(k))
                    print(f"  {slot}: {s.get('field')}"
                          + (f"  ({opts})" if opts else ""))
            if "show_related" in d:
                print(f"  show related values only: {d['show_related']}")
        else:
            print("  (no definition captured — index built with parser < 1.10.0?)")
        _c, urows = _rows(conn,
            """SELECT context, source_kind, source_parent_name, source_name
               FROM v_usage WHERE target_id = ? ORDER BY context, source_name""",
            (eid,))
        print(f"  bound in {len(urows)} place(s)")
        if urows:
            _print_table(_c, urows, limit=args.limit)
        print()


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


def _resolve_field_specs(conn, spec):
    """Parse a comma list of 'BaseTable::Field' into entity rows. Leaf-only
    names are refused: same-named fields exist in many tables and a silent
    wrong pick would poison the whole map."""
    out = []
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        if "::" not in item:
            raise SystemExit(f"error: '{item}' — write it as BaseTable::FieldName "
                             "(leaf names are ambiguous across tables)")
        bt, name = item.split("::", 1)
        rows = conn.execute(
            "SELECT entity_id, base_table, name, field_type FROM entities "
            "WHERE kind='field' AND base_table=? AND name=?", (bt, name)).fetchall()
        if not rows:
            raise SystemExit(f"error: field {item} not found in the index")
        out.extend(rows)
    return out


def _expand_calc_inputs(conn, fields):
    """Recursively expand calc fields into the WRITABLE fields they depend on
    (live calc/auto_enter refs, cross-table included — an aggregate's related
    source fields matter as much as same-table ones). Returns
    ({writable_eid: (base_table, name, [derived-from chain])}, all_seen_eids)."""
    writable, seen = {}, set()
    frontier = [(eid, bt, nm, ft, []) for eid, bt, nm, ft in fields]
    while frontier:
        eid, bt, nm, ft, via = frontier.pop()
        if eid in seen:
            continue
        seen.add(eid)
        label = f"{bt}::{nm}"
        if ft != "Calculated":
            writable[eid] = (bt, nm, via)
            continue
        deps = conn.execute(
            "SELECT tgt.entity_id, tgt.base_table, tgt.name, tgt.field_type "
            "FROM refs r JOIN entities tgt ON tgt.entity_id = r.target_entity_id "
            "WHERE r.source_entity_id = ? AND r.context IN ('calc','auto_enter') "
            "AND r.disabled = 0 AND tgt.kind='field'", (eid,)).fetchall()
        frontier.extend((d[0], d[1], d[2], d[3], via + [label]) for d in deps)
    return writable, seen


def _launch_sites(conn, eid):
    """Human-readable non-script launch sites for a script: layout/object
    triggers (via v_triggers) and layout-object button launches."""
    sites = []
    for ev, sk, lay, obj in conn.execute(
            "SELECT v.trigger_event, v.source_kind, v.layout_name, v.object_name "
            "FROM v_triggers v JOIN refs r ON r.ref_id = v.ref_id "
            "WHERE r.target_entity_id = ?", (eid,)).fetchall():
        where = f"layout '{lay}'" if lay else "file-level"
        sites.append(f"{ev or 'trigger'} · {where}"
                     + (f" · obj '{obj}'" if obj and sk == 'layout_object' else ""))
    for (oname,) in conn.execute(
            "SELECT COALESCE(NULLIF(o.name,''),'(unnamed)') FROM refs r "
            "JOIN entities o ON o.entity_id = r.source_entity_id "
            "AND o.kind='layout_object' "
            "WHERE r.context='perform_script' AND r.disabled=0 "
            "AND r.target_entity_id = ?", (eid,)).fetchall():
        sites.append(f"button {oname!r}")
    return sites


def cmd_trigger_map(args):
    """Classify every entry point that can change a cached value's inputs.

    The deliverable of a caching/'keep X in sync' ticket is the COMPLETE set of
    mutation entry points, classified from the TRANSITIVE call chain — a trigger
    that writes nothing relevant itself can still be the biggest stamp-point via
    a callee (real case: a finance-change trigger whose callee auto-adds a
    record and flips the selection flag). Humans (and LLMs) reliably stop one
    hop short under time pressure; this walk cannot."""
    conn = _connect(args.db)
    from collections import defaultdict

    # ---- 1. watched inputs: resolve, then expand calcs to writable fields ----
    specs = _resolve_field_specs(conn, args.inputs)
    if args.set_flag:
        specs += _resolve_field_specs(conn, args.set_flag)
    writable, _seen = _expand_calc_inputs(conn, specs)
    print(f"# Trigger map — {len(specs)} input(s) -> "
          f"{len(writable)} writable field(s) after calc expansion")
    for eid, (bt, nm, via) in sorted(writable.items(), key=lambda kv: (kv[1][0], kv[1][1])):
        src = f"   (via {' -> '.join(via)})" if via else ""
        print(f"  {bt}::{nm}{src}")

    # ---- 2. mutators: field writers + record ops on --table ----
    mutators = defaultdict(list)          # script_eid -> [evidence strings]
    names = {}                            # script_eid -> (fm_id, name)
    if writable:
        ph = ",".join("?" * len(writable))
        for seid, fmid, snm, written in conn.execute(
                "SELECT scr.entity_id, scr.fm_id, scr.name, "
                "tgt.base_table || '::' || tgt.name "
                "FROM refs r "
                "JOIN entities st ON st.entity_id = r.source_entity_id "
                "AND st.kind='script_step' "
                "JOIN entities scr ON scr.entity_id = st.parent_entity_id "
                "AND scr.kind='script' "
                "JOIN entities tgt ON tgt.entity_id = r.target_entity_id "
                f"WHERE r.context='step_target' AND r.disabled=0 "
                f"AND r.target_entity_id IN ({ph})",
                list(writable)).fetchall():
            names[seid] = (fmid, snm)
            if f"writes {written}" not in mutators[seid]:
                mutators[seid].append(f"writes {written}")
    unresolved_ops = []                   # caller-set context: verify, don't count
    if args.table:
        rows = _script_steps_ordered(conn, "", ())
        per_script = defaultdict(list)
        meta = {}
        for fmid, nm, seq, st, txt, dis in rows:
            per_script[fmid].append((seq, st, txt, dis))
            meta[fmid] = nm
        want = args.table.lower()
        for fmid, steps in per_script.items():
            for op in _scan_record_ops(steps):
                if op["state"] != "live" or op["kind"] not in ("create", "delete"):
                    continue
                ctx = (op["context"] or "").lower()
                if want in ctx:
                    # context clue names our table -> counts as a mutator
                    row = conn.execute(
                        "SELECT entity_id, fm_id, name FROM entities "
                        "WHERE kind='script' AND fm_id=?", (fmid,)).fetchone()
                    if row:
                        seid = row[0]
                        names[seid] = (row[1], row[2])
                        ev = (f"record op {op['step_type']} ({op['tier']}"
                              + (f", ctx {op['context']}" if op["context"] else "")
                              + ")")
                        if ev not in mutators[seid]:
                            mutators[seid].append(ev)
                elif not ctx:
                    # no clue at all (context set by caller): can't rule the
                    # table in OR out — surface for a human pass, but do NOT
                    # let it flood the entry-point closure
                    unresolved_ops.append(
                        [f"[{fmid}] {meta[fmid]}", op["step_type"],
                         op["note"] or "context set by caller"])
                # a clue naming a DIFFERENT table: excluded (footer says so)

    print(f"\n## Mutators ({len(mutators)} script(s))")
    for seid in sorted(mutators, key=lambda e: names[e][1]):
        fmid, nm = names[seid]
        print(f"  [{fmid}] {nm} — " + "; ".join(mutators[seid]))
    if not mutators:
        print("  (none — check the field specs, or the writes may be "
              "ExecuteSQL/import-only; see COVERAGE.md)")
        return

    # ---- 3. upward closure with next-hop pointers (chain reconstruction) ----
    callers_of = defaultdict(set)
    for caller, callee in conn.execute(
            "SELECT scr.entity_id, r.target_entity_id FROM refs r "
            "JOIN entities st ON st.entity_id = r.source_entity_id "
            "AND st.kind='script_step' "
            "JOIN entities scr ON scr.entity_id = st.parent_entity_id "
            "AND scr.kind='script' "
            "JOIN entities tgt ON tgt.entity_id = r.target_entity_id "
            "AND tgt.kind='script' "
            "WHERE r.context IN ('perform_script','trigger') AND r.disabled=0"):
        callers_of[callee].add(caller)
    next_hop, reach = {}, set(mutators)
    frontier = list(mutators)
    while frontier:
        nxt = []
        for s in frontier:
            for c in callers_of.get(s, ()):
                if c not in reach:
                    reach.add(c)
                    next_hop[c] = s
                    nxt.append(c)
        frontier = nxt
    for eid in reach:
        if eid not in names:
            row = conn.execute("SELECT fm_id, name FROM entities WHERE entity_id=?",
                               (eid,)).fetchone()
            names[eid] = row or ("?", "?")

    def chain_str(eid):
        parts = [names[eid][1]]
        while eid in next_hop:
            eid = next_hop[eid]
            parts.append(names[eid][1])
        return " -> ".join(parts) + f"  [{mutators[eid][0]}]"

    # ---- 4. entry points = closure members that are triggered, button-launched,
    #         or roots (no script callers) ----
    recompute_eid = None
    if args.recompute:
        row = conn.execute("SELECT entity_id FROM entities WHERE kind='script' "
                           "AND name = ?", (args.recompute,)).fetchone()
        if not row:
            raise SystemExit(f"error: recompute script '{args.recompute}' not found")
        recompute_eid = row[0]
    entries = []
    for eid in sorted(reach, key=lambda e: names[e][1]):
        sites = _launch_sites(conn, eid)
        is_root = not callers_of.get(eid)
        if not sites and not is_root:
            continue                       # interior script — reached via its callers
        launched = "; ".join(sites) if sites else "(no script callers — menu/root)"
        verdict = "-"
        if recompute_eid is not None:
            chain, _, _ = _chain_scripts(conn, eid)
            verdict = ("recompute-in-chain" if recompute_eid in chain
                       else "NO RECOMPUTE — gap candidate")
        entries.append([f"[{names[eid][0]}] {names[eid][1]}", launched,
                        chain_str(eid), verdict])
    print(f"\n## Entry points ({len(entries)}) — every launchable path that can "
          "change the watched inputs")
    _print_table(["entry point", "launched by", "chain to mutation",
                  "recompute?"], entries, limit=100000, full=args.full)

    if unresolved_ops:
        print(f"\n## Record ops with NO context clue ({len(unresolved_ops)}) — "
              "could target any table incl. yours; verify, not counted above")
        _print_table(["script", "step", "note"], unresolved_ops,
                     limit=100000, full=args.full)

    # ---- 5. optional: triggers on named layouts with NO path to a mutator ----
    if args.layouts:
        quiet = conn.execute(
            "SELECT DISTINCT v.script_name, v.trigger_event, v.layout_name "
            "FROM v_triggers v JOIN refs r ON r.ref_id = v.ref_id "
            "WHERE v.layout_name LIKE ? AND (r.target_entity_id IS NULL "
            f"OR r.target_entity_id NOT IN ({','.join('?'*len(reach))}))",
            [args.layouts] + list(reach)).fetchall()
        print(f"\n## Triggers on layouts LIKE {args.layouts!r} with no path to a "
              f"mutator ({len(quiet)}) — non-issue candidates")
        _print_table(["script", "event", "layout"],
                     [[s, e, l] for s, e, l in quiet], limit=100000)

    print("\nThis is static call-graph analysis: conditionals, loops and execution "
          "ORDER are not\nmodeled — 'recompute-in-chain' means the orchestrator is "
          "reachable from this entry,\nnot that it runs AFTER the mutation; confirm "
          "order by reading the entry script.\nExecuteSQL writes and import mappings "
          "are not captured (COVERAGE.md). Record-op\ncontext clues inherit the "
          "mutations-command caveats; ops whose clue names a\nDIFFERENT table are "
          "excluded entirely, and clue-less ops are listed but not\ncounted — run "
          "'fm-ddr mutations <db>' for the untriaged inventory.")


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


_WORDMARK = [
    r"    ____                                     ",
    r"   / __/___ ___  _________  ____  ____ ______",
    r"  / /_/ __ `__ \/ ___/ __ \/ __ \/ __ `/ ___/",
    r" / __/ / / / / (__  ) /_/ / / / / /_/ / /    ",
    r"/_/ /_/ /_/ /_/____/\____/_/ /_/\__,_/_/     ",
]
_WORD_GRAD = ("38;5;51", "38;5;45", "38;5;44", "38;5;38", "38;5;37")


def _splash(stream=None):
    """Neofetch-style splash for bare `fmsonar`: teal wordmark + a live summary
    of what is indexed. Color only on a real terminal (honor NO_COLOR); plain
    text otherwise, so a piped `fmsonar | ...` stays clean."""
    import platform
    from fm_ddr import __version__
    if stream is None:
        stream = sys.stdout
    on = stream.isatty() and not os.environ.get("NO_COLOR")
    def c(code): return f"\033[{code}m" if on else ""
    def bg(n):   return f"\033[48;5;{n}m" if on else ""
    GLOW = c("38;5;51"); LBL = c("1;38;5;38"); VAL = c("38;5;253")
    DIM = c("38;5;244"); R = c("0")

    out = [""]
    for i, line in enumerate(_WORDMARK):
        out.append(f"  {c(_WORD_GRAD[i])}{line}{R}")
    out.append(f"\n  {GLOW}◎{R} {DIM}v{__version__}{R}")
    out.append(f"  {DIM}{'─' * len(_WORDMARK[1])}{R}")

    def kv(k, v): out.append(f"  {LBL}{k:<9}{R}{VAL}{v}{R}")
    _os = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(
        platform.system(), platform.system())
    kv("Runtime", f"Python {platform.python_version()} · {_os} {platform.machine()}")

    dbs_dir = os.path.expanduser("~/.fmsonar/dbs")
    dbs = ([os.path.join(dbs_dir, f) for f in os.listdir(dbs_dir) if f.endswith(".db")]
           if os.path.isdir(dbs_dir) else [])
    if dbs:
        kv("Cache", f"~/.fmsonar/dbs · {len(dbs)} indexed")
        newest = max(dbs, key=os.path.getmtime)
        try:
            conn = sqlite3.connect(f"file:{newest}?mode=ro", uri=True)
            label = (conn.execute("SELECT label FROM ddr_run LIMIT 1").fetchone() or [""])[0]
            n = dict(conn.execute("SELECT kind, COUNT(*) FROM entities GROUP BY kind").fetchall())
            tot = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
            res = conn.execute("SELECT COUNT(*) FROM refs WHERE target_entity_id IS NOT NULL").fetchone()[0]
            conn.close()
            kv("Latest", f"{label} · {_human_size(os.path.getsize(newest))}")
            kv("Scripts", f"{n.get('script', 0):,} · CFs {n.get('custom_function', 0):,}")
            kv("Fields", f"{n.get('field', 0):,} · TOs {n.get('table_occurrence', 0):,}")
            kv("Refs", f"{tot:,} · {GLOW}{100 * res // max(tot, 1)}% resolved{R}")
        except (sqlite3.DatabaseError, TypeError):
            pass
    else:
        kv("Cache", f"~/.fmsonar/dbs · empty — run {GLOW}fmsonar build <DDR.xml>{R}")

    skill = os.path.exists(os.path.expanduser("~/.claude/skills/fmsonar/SKILL.md"))
    kv("Skill", f"{GLOW}installed{R}" if skill else f"run {GLOW}fmsonar install-skill{R}")

    if on:
        out.append("")
        out.append("  " + "".join(f"{bg(x)}  {R} " for x in (24, 30, 37, 44, 51, 87)))
    out.append("")
    print("\n".join(out), file=stream)


def main(argv=None):
    p = argparse.ArgumentParser(prog="fmsonar", description=_banner(),
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--debug", action="store_true",
                   help="show the full Python traceback on error")
    sub = p.add_subparsers(dest="cmd")

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

    tm = sub.add_parser("trigger-map",
                        help="classify every entry point that can change a "
                             "cached value's inputs (caching/'keep in sync' "
                             "tickets)")
    tm.add_argument("db")
    tm.add_argument("--inputs", required=True,
                    help="comma list of BaseTable::Field the cached value "
                         "depends on; calc fields auto-expand recursively to "
                         "their writable inputs")
    tm.add_argument("--set-flag", dest="set_flag", default=None,
                    help="set-membership flag field (BaseTable::Field) whose "
                         "writes change WHICH records the aggregate sums")
    tm.add_argument("--table", default=None,
                    help="base table whose record create/delete shifts the "
                         "aggregate (runs the record-ops engine)")
    tm.add_argument("--recompute", default=None,
                    help="orchestrator script that re-stamps the cache; each "
                         "entry point is checked for it in its callee chain")
    tm.add_argument("--layouts", default=None,
                    help="also list triggers on layouts LIKE this pattern with "
                         "NO path to a mutator (non-issue candidates)")
    tm.add_argument("--full", action="store_true",
                    help="don't truncate table cells")
    tm.set_defaults(func=cmd_trigger_map)

    ca = sub.add_parser("cascades",
                        help="cascade deletes: what deletes into a table via "
                             "relationship options (v1.10.0)")
    ca.add_argument("db")
    ca.add_argument("table", nargs="?", default=None,
                    help="victim base table or TO name (default: all)")
    ca.add_argument("--limit", type=int, default=200)
    ca.set_defaults(func=cmd_cascades)

    vl = sub.add_parser("valuelist",
                        help="value-list definition (source, fields, custom "
                             "values) + bindings (v1.10.0)")
    vl.add_argument("db")
    vl.add_argument("name", help="value-list name (LIKE pattern allowed)")
    vl.add_argument("--limit", type=int, default=50)
    vl.set_defaults(func=cmd_valuelist)

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
    # Bare `fmsonar` (no subcommand): show the splash instead of an argparse error.
    if args.cmd is None:
        _splash(sys.stdout)
        return
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
