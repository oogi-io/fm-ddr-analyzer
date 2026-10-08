"""Compare the RESOLVED references of two fmsonar indexes built from the same DDR.

A development check for parser releases: build the same DDR with the old and the
new parser, then run this on both indexes. Every resolved edge is keyed by content
(file, source kind, source parent, source name, step seq, context, target kind,
target raw, trigger event) and valued by its target (file, kind, base table, FM id).
The report lists keys LOST (resolved before, not now), CHANGED (resolved to
something else) and GAINED (new), with the first rows of each. Lost or changed
means a regression; gained is what the release added. Read-only on both indexes;
stdlib only.

The header names each index's source (ddr_run.source_format and source_version;
an index without them is a DDR). A DDR index against a SaXML index is a
cross-source pair: the banner says expected differences apply, and the gate is
read against the expected-loss list in COVERAGE.md. --file NAME keeps only the
refs whose source file is NAME on both sides (a multi-file DDR index against a
one-file SaXML index).

usage: python3 tools/compare_resolution.py OLD.db NEW.db [--file NAME]
exit status: 1 on any lost or changed key, else 0
"""
import argparse, sqlite3, sys
from collections import Counter

SQL = """
SELECT fl.name, se.kind, sp.name, se.name, se.seq, r.context, r.target_kind, r.target_raw,
       r.trigger_event,
       tfl.name, te.kind, te.base_table, te.fm_id
FROM refs r
JOIN files fl ON fl.file_id = r.file_id
LEFT JOIN entities se ON se.entity_id = r.source_entity_id
LEFT JOIN entities sp ON sp.entity_id = se.parent_entity_id
JOIN entities te ON te.entity_id = r.target_entity_id
JOIN files tfl ON tfl.file_id = te.file_id
WHERE r.target_entity_id IS NOT NULL
"""

def source(path):
    """(source_format, source_version, ddr_version); absent columns = a DDR."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT source_format, source_version, ddr_version "
                           "FROM ddr_run LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        row = conn.execute("SELECT NULL, NULL, ddr_version FROM ddr_run LIMIT 1").fetchone()
    conn.close()
    row = row or (None, None, None)
    return row[0] or "ddr", row[1], row[2]

def describe(src):
    fmt, src_ver, ver = src
    text = f"{fmt} {ver or '?'}"
    return text + (f" (FileMaker {src_ver})" if fmt == "saxml" and src_ver else "")

def load(path, file_name=None):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    dup = Counter()
    for row in conn.execute(SQL):
        if file_name is not None and row[0] != file_name:
            continue
        key, val = tuple(row[:9]), tuple(row[9:])
        if key in out:
            # identical duplicate edges (same step, same target twice) are collapsed; keep a count
            dup[key] += 1
            continue
        out[key] = val
    conn.close()
    return out, dup

def main(old_path, new_path, file_name=None):
    osrc, nsrc = source(old_path), source(new_path)
    print(f"old source: {describe(osrc)}")
    print(f"new source: {describe(nsrc)}")
    if osrc[0] != nsrc[0]:
        print('cross-source pair: expected differences apply (COVERAGE.md, "SaXML next to the DDR")')
    if file_name is not None:
        print(f"file filter: refs whose source file is {file_name}")
        for path in (old_path, new_path):
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            found = conn.execute("SELECT 1 FROM files WHERE name = ?", (file_name,)).fetchone()
            conn.close()
            if not found:
                # A typo would compare two empty sets and pass
                print(f"error: no file named {file_name} in {path}", file=sys.stderr)
                return 2
    old, odup = load(old_path, file_name)
    new, ndup = load(new_path, file_name)
    lost = [k for k in old if k not in new]
    changed = [k for k in old if k in new and new[k] != old[k]]
    gained = [k for k in new if k not in old]
    print(f"old resolved keys: {len(old):,} (+{sum(odup.values()):,} duplicate edges)")
    print(f"new resolved keys: {len(new):,} (+{sum(ndup.values()):,} duplicate edges)")
    print(f"lost: {len(lost):,}  changed: {len(changed):,}  gained: {len(gained):,}")
    def show(label, keys, limit=12):
        if not keys:
            return
        print(f"\n# {label} by (file, context):")
        for (f, c), n in Counter((k[0], k[5]) for k in keys).most_common(20):
            print(f"  {f:40s} {c:18s} {n:,}")
        print(f"# first {min(limit, len(keys))} {label}:")
        for k in keys[:limit]:
            v = (old.get(k), new.get(k))
            print("  ", k, "->", v)
    show("lost", lost)
    show("changed", changed)
    show("gained", gained, limit=5)
    return 0 if not lost and not changed else 1

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Compare the resolved references of two fmsonar indexes.")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--file", metavar="NAME",
                    help="keep only refs whose source file name is NAME, on both sides")
    a = ap.parse_args()
    sys.exit(main(a.old, a.new, a.file))
