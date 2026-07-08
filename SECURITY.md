# Security

FMSonar reads FileMaker DDR exports, which contain a client's entire schema.
Its guarantees and its limits:

## Where your data goes

- **The web app ([fmsonar.com](https://fmsonar.com)) uploads nothing.** The DDR
  is parsed entirely in your browser; there is no server component and no
  network request carrying your data. A Content-Security-Policy with
  `connect-src 'none'` is enforced (both as a `<meta>` tag and at the CDN edge),
  so even a hypothetical script injection cannot exfiltrate the parsed schema.
- **The CLI is local.** Databases are written to disk (or the central cache
  `~/.fmsonar/dbs/`) and never transmitted.
- **Shared reports and entity exports are self-contained HTML** carrying only
  the data you chose to include, with the same restrictive CSP.

## Threat model — treat DDRs as untrusted

A DDR is XML from an outside party ("here's my solution, take a look"). FMSonar
is built to parse a hostile DDR safely:

- **Output escaping** — all DDR-derived strings are HTML-escaped before entering
  the DOM, so a crafted entity name (e.g. a script named `"><img onerror=...>`)
  cannot execute script in the app or in a shared report.
- **No entity expansion** — a DDR declaring a DTD internal subset / `<!ENTITY>`
  is rejected, so "billion laughs" cannot exhaust memory regardless of the host
  expat version. External entity resolution is disabled (no XXE file reads).
- **No path traversal** — a `Summary.xml` manifest can only reference files
  within its own directory tree; links that escape it are ignored.
- **Safe builds** — `build` writes to a temp file and atomically replaces the
  target only on success, so a malformed DDR never destroys an existing file.

## The clipboard-paste trust boundary

"Copy FM snippet" and `fm-ddr clip` reproduce script steps from the DDR as a
FileMaker clipboard snippet. **Those steps are attacker-controlled if the DDR
came from someone else** — review them in Script Workspace before running,
exactly as you would any script a third party hands you. FMSonar copies them
faithfully; it does not vet them.

## Reporting a vulnerability

Email **tdesmet@oogi.io** with details and a reproduction. Please do not open a
public issue for anything exploitable until it is fixed.
