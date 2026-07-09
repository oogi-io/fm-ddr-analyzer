"""Command-line interface for the FM DDR analyzer.

    python -m fm_ddr.cli build   DDR.xml [-o out.db] [--label NAME]
    python -m fm_ddr.cli where   out.db  "TO::Field" | "ScriptName" | "Layout"
    python -m fm_ddr.cli search  out.db  "some text"
    python -m fm_ddr.cli sql     out.db  "SELECT ..."
    python -m fm_ddr.cli stats   out.db
"""

import argparse
import os
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

    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill", "SKILL.md")
    dest_dir = os.path.expanduser("~/.claude/skills/fmsonar")
    dest = os.path.join(dest_dir, "SKILL.md")
    from fm_ddr import __version__

    def digest(b):
        return hashlib.sha256(b).hexdigest()[:12]

    if args.check or args.remote:
        if not os.path.exists(dest):
            print(f"fmsonar skill is not installed ({dest} missing). "
                  f"Run: fm-ddr install-skill")
            sys.exit(2)
        installed = open(dest, "rb").read()
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
            ref = open(src, "rb").read()
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
    shutil.copy(src, dest)
    print(f"Installed the 'fmsonar' skill (fm_ddr {__version__}) -> {dest_dir}")
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
    print(f"\n# References: {tot} total, {res} resolved ({100*res//max(tot,1)}%)")
    print("\n# Unresolved by kind/context (external / built-in / broken)")
    _, rows = _rows(conn, "SELECT * FROM v_unresolved")
    _print_table(["target_kind", "context", "n"], rows)


def main(argv=None):
    p = argparse.ArgumentParser(prog="fm_ddr", description="FileMaker DDR analyzer")
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
