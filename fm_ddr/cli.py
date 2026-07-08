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


def _print_table(cols, rows, limit=200):
    if not rows:
        print("(no rows)")
        return
    widths = [len(c) for c in cols]
    shown = rows[:limit]
    for r in shown:
        for i, v in enumerate(r):
            widths[i] = min(max(widths[i], len(str(v)) if v is not None else 4), 80)
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(cols))))
    for r in shown:
        print("  ".join((str(v) if v is not None else "").ljust(widths[i])[:80]
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


def cmd_where(args):
    """Where is a field / script / layout / TO / CF used?

    Resolves the term to concrete entities first, then reports references per
    entity — a bare field name that exists in several tables is reported per
    table, never lumped together."""
    conn = sqlite3.connect(args.db)
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
    conn = sqlite3.connect(args.db)
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
    conn = sqlite3.connect(args.db)
    cols, rows = _rows(conn, args.query)
    _print_table(cols, rows, limit=args.limit)


def cmd_snippet(args):
    from .snippet import snippet
    res = snippet(args.ddr, args.script, out_path=args.out,
                  to_clipboard=args.clip, script_id=getattr(args, "id", None))
    print(f"{res['steps']} steps -> {res['bytes']} bytes of fmxmlsnippet")
    if res["out"]:
        print(f"written to {res['out']}")
    if res["clipboard"]:
        print("on the clipboard (XMSS) - paste into FileMaker Script Workspace")


def cmd_install_skill(args):
    import shutil
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill", "SKILL.md")
    dest_dir = os.path.expanduser("~/.claude/skills/fmsonar")
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copy(src, os.path.join(dest_dir, "SKILL.md"))
    print(f"Installed the 'fmsonar' skill -> {dest_dir}")
    print("Claude Code can now analyze FileMaker DDRs from ANY directory -")
    print("just mention a DDR or ask a where-used question.")


def cmd_clip(args):
    from .snippet import clip_text_to_fm
    n = clip_text_to_fm()
    print(f"{n} bytes -> FileMaker clipboard (XMSS); paste into Script Workspace")


def cmd_report(args):
    from .report import report
    out = report(args.db, args.out)
    print(f"Wrote {out}")


def cmd_stats(args):
    conn = sqlite3.connect(args.db)
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
    q.set_defaults(func=cmd_sql)

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
