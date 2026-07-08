# DDR → FileMaker clipboard snippet (fmxmlsnippet)

Reverse-engineered by diffing a real copied-from-FileMaker snippet against the
DDR for the same script (a production solution's script, 268 steps). The transform
below reproduces FileMaker's own output **268/268 steps** (modulo one editor
UI flag, see below).

## The clipboard

FileMaker puts script steps on a **private pasteboard flavor** `XMSS` (not the
plain-text flavor). Read/write it on macOS with AppleScript:

```bash
# read what's on the clipboard (returns «data XMSS<hex>»)
osascript -e 'the clipboard as «class XMSS»'
# write a snippet so it pastes into Script Workspace (HEX = uppercase hex of UTF-8 bytes)
osascript -e 'set the clipboard to «data XMSS<HEX>»'
```

The hex decodes to UTF-8 XML: `<fmxmlsnippet type="FMObjectList">…steps…</fmxmlsnippet>`.
Class codes: `XMSS` script steps, `XMSC` scripts, `XMFN` custom functions,
`XMFD` fields, `XMTB` tables, `XML2`/`XMLO` layout objects.

A browser cannot set the `XMSS` flavor (only text/html), so **direct
paste-into-FileMaker is a macOS CLI feature**; the web app can only offer the
snippet XML as text (paste via a helper like FmClipTools or the osascript above).

## The transform (DDR `<Step>` → snippet `<Step>`)

Wrap all steps in `<fmxmlsnippet type="FMObjectList">…</fmxmlsnippet>`, then per
step:

1. **Keep** `<Step enable id name>` attributes verbatim.
2. **Remove** `<StepText>…</StepText>` — DDR's human-readable rendering; not in snippets.
3. **Remove** `<DisplayCalculation>…</DisplayCalculation>` — DDR's chunked calc breakdown; not in snippets.
4. **`<Text>` bodies** (comment step content): encode CR and LF as `&#13;` and
   tabs as `&#09;`. (FileMaker uses CR as its newline.)
5. **Comment steps** (`name="# (comment)"`): add `<Restore state="False"></Restore>`
   — the DDR omits it, the snippet includes it.
6. **Everything else passes through unchanged** — this is the key finding:
   - `<Calculation><![CDATA[…]]></Calculation>` — verbatim, incl. literal LF newlines
   - `<Value>`, `<Repetition>`, `<Name>` (Set Variable/Field)
   - `<Field table="…" id="…" name="…">` — **identical to DDR**
   - `<Layout id="…" name="…">`, `<LayoutDestination value="…">` — identical
   - `<Script id="…" name="…">` (Perform Script targets) — identical
   - `<Set state>`, `<WaitForCompletion>`, `<Calculated>`, etc. — identical
7. `<DisableStepCollapsed state="False">` is an **editor UI flag** FileMaker adds
   (position varies per step type). Optional for pasting — omit it. Ignoring it,
   the transform matched all 268 steps exactly.

## Why references come free

The reference elements a valid paste needs (`Field table/id/name`,
`Layout id/name`, `Script id/name`) are encoded **identically** in the DDR and
the clipboard snippet. The DDR is not lossy about references — only about the
rendered `StepText` and the `DisplayCalculation` chunk breakdown, both of which
we discard. That is what makes DDR → pasteable-snippet feasible.

## Verification status

**Paste-verified in FileMaker (2026-07-08):** a DDR-generated snippet pasted
into Script Workspace and reconstructed correctly — end of chain confirmed
(DDR -> transform -> clipboard -> FileMaker). `DisableStepCollapsed` omission
confirmed harmless.

Remaining caveats:
- IDs are the source file's internal ids. Pasting into the **same** file/version
  is the verified case. Pasting into a different file may re-map or break
  references — same limitation as FileMaker's own copy/paste across files.
- Only script steps (`XMSS`) mapped so far. Custom functions (`XMFN`) and
  fields (`XMFD`) need their own (simpler) mappings — reference copies from
  FileMaker required to diff, same method.
