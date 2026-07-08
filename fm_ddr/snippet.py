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


def extract_script_xml(ddr_path: str, script_name: str,
                       script_id: str | None = None) -> str:
    """Stream a DDR and return the raw XML of the named script definition.

    FileMaker allows two scripts to share a name; by default the first matching
    definition is returned (streaming stops there, which matters on huge DDRs).
    Pass script_id (FileMaker's own id) to select a specific one when the name
    is ambiguous.
    """
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
                # candidate: opens (not self-closing) and carries the name.
                # NOTE: trigger references use the same open+close form, so a
                # candidate only counts as the definition if it turns out to
                # contain a StepList - otherwise we keep scanning.
                if "<Script" in line and needle in line and not line.rstrip().endswith("/>"):
                    cap, buf, depth = True, [], 0
                else:
                    continue
            buf.append(line)
            depth += len(open_re.findall(line)) - line.count("</Script>")
            if depth <= 0:
                xml = "".join(buf)
                if "<StepList" in xml:
                    if script_id is None:
                        return xml
                    m = re.search(r'<Script\b[^>]*\bid="([^"]*)"', xml)
                    if m and m.group(1) == str(script_id):
                        return xml
                cap = False  # a reference/trigger or the wrong id - keep looking
    where = f'script "{script_name}"' + (f' with id {script_id}' if script_id else "")
    raise ValueError(f'{where} not found in {ddr_path}')


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


def _sniff_class(snippet_xml: str) -> str:
    """Clipboard class from the first object type inside the snippet."""
    for tag, cls in (("<Step ", "XMSS"), ("<CustomFunction ", "XMFN"),
                     ("<BaseTable ", "XMTB"), ("<Script ", "XMSC"),
                     ("<Layout", "XML2"), ("<Field ", "XMFD")):
        if tag in snippet_xml:
            return cls
    return "XMSS"


def set_clipboard_xmss(snippet_xml: str) -> None:
    """Place snippet XML on the macOS clipboard as the right FileMaker flavor."""
    import sys
    if sys.platform != "darwin":
        raise RuntimeError(
            "Writing the FileMaker clipboard requires macOS (osascript). "
            "Use -o to write the snippet XML to a file instead.")
    cls = _sniff_class(snippet_xml)
    hexdata = snippet_xml.encode("utf-8").hex().upper()
    # the script can exceed argv limits; run osascript from a temp file
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
        f.write("set the clipboard to «data %s%s»\n" % (cls, hexdata))
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
            to_clipboard: bool = False, script_id: str | None = None) -> dict:
    script_xml = extract_script_xml(ddr_path, script_name, script_id=script_id)
    xml, nsteps = ddr_steps_to_snippet(script_xml)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(xml)
    if to_clipboard:
        set_clipboard_xmss(xml)
    return {"steps": nsteps, "bytes": len(xml.encode("utf-8")),
            "out": out_path, "clipboard": to_clipboard}
