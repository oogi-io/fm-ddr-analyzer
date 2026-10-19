# Security

fmsonar reads FileMaker DDR exports, which contain a client's entire schema.
Its guarantees and its limits:

## Where your data goes

- **The web app ([fmsonar.com](https://fmsonar.com)) uploads nothing.** The DDR
  is parsed entirely in your browser; there is no server component and no
  network request carrying your data. A Content-Security-Policy with
  `connect-src 'none'` is enforced (both as a `<meta>` tag and at the CDN edge),
  so even a hypothetical script injection cannot exfiltrate the parsed schema.
- **The CLI is local.** Databases are written to disk, next to the export
  unless `-o` says otherwise, and never transmitted.
- **Shared reports and entity exports are self-contained HTML** carrying only
  the data you chose to include, with the same restrictive CSP.

## Developer names in a SaXML-built index

A Save a Copy as XML export stamps every object with its last change: a
timestamp, a change count, and the FileMaker **user name and account name of
the developer** who made it. A DDR carries none of this. An index built from
a SaXML export stores those stamps per object in `entities.extra_json`
(`modified_at`, `modified_n`, `modified_by`, `modified_account`), so anyone you
hand the `.db` file can read who last touched what. The HTML `report` leaves
them out: it embeds names, calculations and step text, never the change
stamps (a test plants a name in an export and checks the report for it). To
share an index without the names, share the report, or clear them first:
`UPDATE entities SET extra_json = json_remove(extra_json, '$.modified_by',
'$.modified_account')` on a copy.

## Threat model — treat DDRs as untrusted

A DDR is XML from an outside party ("here's my solution, take a look"). fmsonar
is built to parse a hostile DDR safely:

- **Output escaping** — all DDR-derived strings are HTML-escaped before entering
  the DOM, so a crafted entity name (e.g. a script named `"><img onerror=...>`)
  cannot execute script in the app or in a shared report.
- **No entity expansion:** a DDR or Save a Copy as XML export whose DOCTYPE
  carries an internal subset, or that declares an `<!ENTITY>`, before its root
  element is rejected (the XML parser reads the prolog up to the root, however
  long), so "billion laughs" cannot exhaust memory regardless of the host expat
  version. A DOCTYPE that only names an external DTD is accepted, but the DTD
  is never fetched: external entity resolution is disabled (no XXE file reads
  or network requests), and an entity reference such a DTD would define is
  skipped, never expanded.
- **No path traversal** — a `Summary.xml` manifest can only reference files
  within its own directory tree; links that escape it are ignored.
- **Safe builds** — `build` writes to a temp file and atomically replaces the
  target only on success, so a malformed DDR never destroys an existing file.

## The clipboard-paste trust boundary

"Copy FM snippet" and `fm-ddr clip` reproduce script steps from the DDR as a
FileMaker clipboard snippet. **Those steps are attacker-controlled if the DDR
came from someone else** — review them in Script Workspace before running,
exactly as you would any script a third party hands you. fmsonar copies them
faithfully; it does not vet them.

## Reporting a vulnerability

Email **tdesmet@oogi.io** with details and a reproduction. Please do not open a
public issue for anything exploitable until it is fixed.
