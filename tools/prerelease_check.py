"""Pre-release gate: nothing about to be published may name a client.

Reads a private deny list (one entry per line, '#' comments; a plain entry is
matched case-insensitively as a whole word, an entry starting with 're:' is a
case-insensitive regular expression) and scans what a push of RANGE would
publish: every commit message, every changed path, every line the range adds
(git diff BASE..HEAD), and the whole content at HEAD of every binary file it
adds or changes (UTF-16 decoded, so a DDR fixture is read too). Lines already
on BASE are not this push's to answer for. It also fails on any
Co-Authored-By trailer and on a LICENSE whose copyright line lacks the owner.

Hits are printed by location and deny-list line number only, never the matched
text, so the gate's own output names no client. The list lives outside git
(docs/design/ is gitignored); a missing or empty list fails closed.

usage: python3 tools/prerelease_check.py [RANGE] [--list PATH]
       RANGE defaults to origin/main..HEAD
exit status: 0 clean, 1 on any hit, 2 on a usage or list error
stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_LIST = os.path.join(ROOT, "docs", "design", "client-names.txt")
OWNER = "OOGI BV"


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", ROOT, *args], check=True,
                          capture_output=True).stdout


def load_list(path: str) -> list[tuple[int, re.Pattern]]:
    pats = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("re:"):
                rx = s[3:]
            else:
                rx = r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])"
            pats.append((n, re.compile(rx, re.IGNORECASE)))
    return pats


def decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace")


def scan(where: str, text: str, pats, hits: list) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        for n, p in pats:
            if p.search(line):
                hits.append(f"{where}:{i}: deny-list line {n}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("range", nargs="?", default="origin/main..HEAD")
    ap.add_argument("--list", default=DEFAULT_LIST)
    a = ap.parse_args(argv)
    if ".." not in a.range:
        print("prerelease_check: RANGE must be BASE..HEAD", file=sys.stderr)
        return 2
    if not os.path.isfile(a.list):
        print(f"prerelease_check: deny list not found: {a.list}", file=sys.stderr)
        return 2
    pats = load_list(a.list)
    if not pats:
        print("prerelease_check: deny list is empty", file=sys.stderr)
        return 2
    head = a.range.split("..", 1)[1] or "HEAD"

    hits: list[str] = []
    commits = git("rev-list", a.range).decode().split()
    for c in commits:
        msg = git("log", "-1", "--format=%B", c).decode("utf-8", "replace")
        scan(f"commit {c[:7]} message", msg, pats, hits)
        if re.search(r"^co-authored-by:", msg, re.IGNORECASE | re.MULTILINE):
            hits.append(f"commit {c[:7]} message: Co-Authored-By trailer")
    paths, binary = [], []
    for row in git("diff", "--numstat", "--no-renames", a.range).decode().splitlines():
        added, _, p = row.split("\t", 2)
        paths.append(p)
        scan(f"path {p}", p, pats, hits)
        if added == "-":
            binary.append(p)
    for p in binary:
        try:
            data = git("show", f"{head}:{p}")
        except subprocess.CalledProcessError:
            continue        # deleted in the range: nothing published
        scan(p, decode(data), pats, hits)
    # Added lines of the text diff, with their line numbers at HEAD
    cur, ln = None, 0
    diff = git("diff", "-U0", "--no-renames", "--no-color", a.range)
    for raw in diff.decode("utf-8", "replace").splitlines():
        if raw.startswith("+++ "):
            cur = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            ln = int(m.group(1)) if m else 0
        elif raw.startswith("+") and cur is not None:
            for n, pat in pats:
                if pat.search(raw[1:]):
                    hits.append(f"{cur}:{ln}: deny-list line {n}")
            ln += 1
    lic = decode(git("show", f"{head}:LICENSE")).splitlines()
    if len(lic) < 3 or OWNER not in lic[2]:
        hits.append(f"LICENSE:3: copyright line does not name {OWNER}")

    for h in hits:
        print(h)
    print(f"prerelease_check: {a.range}: {len(commits)} commit(s), {len(paths)} "
          f"changed file(s), {len(pats)} deny entries, {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
