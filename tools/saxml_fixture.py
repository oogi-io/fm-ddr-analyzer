"""Scrub a Save a Copy as XML export of the synthetic lab file into a test fixture.

    python3 tools/saxml_fixture.py in.xml out.xml

Replaces the text of every credential element with a sentinel the credentials
test looks for, blanks the user-identifying attributes and SourceUUID texts,
names the file-access pairings "Lab", and drops the bulk subtrees fmsonar never
reads (fm_ddr.saxml.SKIP). ElementTree also drops comments and processing
instructions (no lab export measured has any). Element order, attributes,
calculation text and DDR_INFO text keep their content (a CDATA wrapper becomes
escaped text and a CR a character reference; both parse to the same string).
For lab exports only: it is not a way to make client data publishable.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fm_ddr.saxml import SKIP, SKIP_UNDER  # noqa: E402

SENTINEL = "FIXTURE-SECRET-NEVER-INDEXED"
# Credential subtrees: every leaf inside gets the sentinel. Login keeps its
# element and type; only its AccountName is a credential.
CREDENTIAL = {"PasswordEncrypted", "INSECURE_PASSWORD", "INSECURE_TEXT"}
CREDENTIAL_UNDER = {"Authentication": "Authorization", "AccountName": "Login"}
BULK = SKIP - CREDENTIAL - {"Login"}
BULK_UNDER = {"Metadata": "Theme"}
USER_ATTRS = ("userName", "accountName")


def scrub(root: ET.Element) -> dict:
    counts = {"credential": 0, "bulk": 0, "user_attrs": 0, "source_uuid": 0, "display": 0}
    assert BULK_UNDER.items() <= SKIP_UNDER.items(), "saxml.SKIP_UNDER changed"
    stack = [root]
    while stack:
        el = stack.pop()
        for child in list(el):
            tag = child.tag
            if tag in BULK or BULK_UNDER.get(tag) == el.tag:
                el.remove(child)
                counts["bulk"] += 1
                continue
            if tag in CREDENTIAL or CREDENTIAL_UNDER.get(tag) == el.tag:
                for leaf in child.iter():
                    if len(leaf) == 0:
                        leaf.text = SENTINEL
                        counts["credential"] += 1
                continue
            if tag == "SourceUUID":
                child.text = ""
                counts["source_uuid"] += 1
            elif tag == "Display" and el.tag == "Authorization":
                child.text = "Lab"
                counts["display"] += 1
            stack.append(child)
    for el in root.iter("UUID"):
        for a in USER_ATTRS:
            if el.get(a):
                el.set(a, "")
                counts["user_attrs"] += 1
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    src, dst = argv[1], argv[2]
    tree = ET.parse(src)
    root = tree.getroot()
    if root.tag != "FMSaveAsXML":
        print(f"{src}: not a Save a Copy as XML export", file=sys.stderr)
        return 1
    counts = scrub(root)
    # ElementTree writes a CR in text raw, which a parser reads back as LF;
    # FileMaker's step texts carry CR, so keep it as a character reference.
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    body = body.replace("\r", "&#13;")
    with open(dst, "w", encoding="utf-8", newline="") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(body)
        f.write("\n")
    print(dst, os.path.getsize(dst), "bytes", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
