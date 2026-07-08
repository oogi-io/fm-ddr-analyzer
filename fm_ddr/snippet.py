"""DDR -> FileMaker clipboard snippet (fmxmlsnippet).

Transform a script's steps as found in a DDR into the XML FileMaker itself
puts on the clipboard when you copy script steps, and (on macOS) place it on
the clipboard's private XMSS flavor so it pastes straight into Script
Workspace.

The transform is reverse-engineered by diffing a real copied snippet against
the DDR of the same script; it reproduces FileMaker's own output exactly
(268/268 steps on the reference script). See SNIPPET_FORMAT.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile


def extract_script_xml(ddr_path: str, script_name: str) -> str:
    """Stream a DDR and return the raw XML of the named script definition."""
    enc = "utf-16-le"
    with open(ddr_path, "rb") as f:
        if f.read(2) != b"\xff\xfe":
            enc = "utf-8"
    needle = 'name="%s"' % script_name.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    open_re = re.compile(r"<Script\b[^>]*[^/]>")
    cap, buf, depth = False, [], 0
    with open(ddr_path, encoding=enc, errors="replace") as f:
        for line in f:
            if not cap:
                # a definition opens (not self-closing) and carries the name
                if "<Script" in line and needle in line and not line.rstrip().endswith("/>"):
                    cap = True
                else:
                    continue
            buf.append(line)
            depth += len(open_re.findall(line)) - line.count("</Script>")
            if depth <= 0:
                return "".join(buf)
    raise ValueError(f'script "{script_name}" not found in {ddr_path}')


def _encode_text(m: re.Match) -> str:
    body = m.group(1)
    body = (body.replace("\r\n", "&#13;").replace("\r", "&#13;")
                .replace("\n", "&#13;").replace("\t", "&#09;"))
    return "<Text>" + body + "</Text>"


def _expand_selfclosing(s: str) -> str:
    # FileMaker emits <X ...></X>, the DDR <X .../> - XML-equivalent, but
    # mirror FileMaker exactly to minimize paste risk
    return re.sub(r"<([A-Za-z0-9]+)((?:\s[^<>]*?)?)\s*/>", r"<\1\2></\1>", s)


def ddr_steps_to_snippet(script_xml: str) -> tuple[str, int]:
    """Transform the steps of a DDR script definition into fmxmlsnippet XML."""
    steps = re.findall(r"<Step\b.*?</Step>", script_xml, re.S)
    out = []
    for st in steps:
        s = re.sub(r">\s+<", "><", st).strip()
        s = re.sub(r"<StepText>.*?</StepText>", "", s, flags=re.S)
        s = re.sub(r"<StepText\s*/>", "", s)
        s = re.sub(r"<DisplayCalculation>.*?</DisplayCalculation>", "", s, flags=re.S)
        s = re.sub(r"<DisplayCalculation\s*/>", "", s)
        s = re.sub(r"<Text>(.*?)</Text>", _encode_text, s, flags=re.S)
        if 'name="# (comment)"' in s and "<Restore" not in s:
            s = re.sub(r"(<Step\b[^>]*>)", r'\1<Restore state="False"></Restore>', s, count=1)
        s = _expand_selfclosing(s)
        out.append(s)
    return '<fmxmlsnippet type="FMObjectList">' + "".join(out) + "</fmxmlsnippet>", len(out)


def set_clipboard_xmss(snippet_xml: str) -> None:
    """Place snippet XML on the macOS clipboard as the FileMaker XMSS flavor."""
    hexdata = snippet_xml.encode("utf-8").hex().upper()
    # the script can exceed argv limits; run osascript from a temp file
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
        f.write("set the clipboard to «data XMSS%s»\n" % hexdata)
        path = f.name
    try:
        subprocess.run(["osascript", path], check=True, capture_output=True, text=True)
    finally:
        os.unlink(path)


def clip_text_to_fm() -> int:
    """Convert fmxmlsnippet XML text on the clipboard into the FileMaker
    clipboard flavor (macOS). Returns the byte size placed."""
    import sys
    if sys.platform != "darwin":
        raise RuntimeError("clip is macOS-only here; on Windows use "
                           "helpers/fm-snippet-helper.ps1")
    text = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    if not text.startswith("<fmxmlsnippet"):
        raise ValueError("clipboard does not contain fmxmlsnippet XML")
    set_clipboard_xmss(text)
    return len(text.encode("utf-8"))


def snippet(ddr_path: str, script_name: str, out_path: str | None = None,
            to_clipboard: bool = False) -> dict:
    script_xml = extract_script_xml(ddr_path, script_name)
    xml, nsteps = ddr_steps_to_snippet(script_xml)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(xml)
    if to_clipboard:
        set_clipboard_xmss(xml)
    return {"steps": nsteps, "bytes": len(xml.encode("utf-8")),
            "out": out_path, "clipboard": to_clipboard}
