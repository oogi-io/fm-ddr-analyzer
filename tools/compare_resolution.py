"""Compare the RESOLVED references of two fmsonar indexes built from the same DDR.

A development check for parser releases: build the same DDR with the old and the
new parser, then run this on both indexes. Every resolved edge is keyed by content
(file, source kind, source parent, source name, step seq, context, target kind,
target raw, trigger event) and valued by its target (file, kind, base table, FM id).
The report lists keys LOST (resolved before, not now), CHANGED (resolved to
something else) and GAINED (new), with the first rows of each. Lost or changed
means a regression; gained is what the release added. Read-only on both indexes;
stdlib only.

usage: python3 tools/compare_resolution.py OLD.db NEW.db
exit status: 1 on any lost or changed key, else 0
"""
import sqlite3, sys
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

def load(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    dup = Counter()
    for row in conn.execute(SQL):
        key, val = tuple(row[:9]), tuple(row[9:])
        if key in out:
            # identical duplicate edges (same step, same target twice) are collapsed; keep a count
            dup[key] += 1
            continue
        out[key] = val
    conn.close()
    return out, dup

def main(old_path, new_path):
    old, odup = load(old_path)
    new, ndup = load(new_path)
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
    sys.exit(main(sys.argv[1], sys.argv[2]))
