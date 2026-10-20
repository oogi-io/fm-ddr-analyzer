# Security

A design export is a client's whole schema. This is what fmsonar does with it.

## Nothing leaves your machine

- **fmsonar.com** parses in your browser. There is no server and no network request
  that carries your data: the page's Content-Security-Policy has `connect-src 'none'`,
  set in the page and at the CDN, so even an injected script could not send anything.
- **The CLI** writes one file on disk, next to the export unless `-o` says otherwise,
  and transmits nothing.
- **Reports and entity exports** are self-contained HTML with the same policy, holding
  only what you chose to include.

## The index can name your developers

A Save a Copy as XML export stamps every object with its last change: when, how many
times, and the FileMaker user name and account name of the developer who made it. A
DDR carries none of this. An index built from such an export keeps the stamps in
`entities.extra_json` (`modified_at`, `modified_n`, `modified_by`, `modified_account`),
so anyone who gets the `.db` file can read who last touched what. The HTML report
never contains them; a test plants a name in an export and checks the report for it.
To share an index without the names, share the report, or clear them on a copy:

```sql
UPDATE entities SET extra_json = json_remove(extra_json, '$.modified_by', '$.modified_account')
```

## An export from a stranger is untrusted input

- Every string from the export is HTML-escaped before it reaches the page, so a script
  named `"><img onerror=...>` runs nothing in the app or in a report.
- A file that declares entities or an internal DTD subset before its root element is
  refused, so an entity-expansion file cannot exhaust memory. An external DTD is never
  fetched: no file reads, no network.
- `Summary.xml` can only point at files inside its own folder tree.
- `build` writes to a temporary file and replaces the target only on success; a broken
  export never destroys an existing index.

## Copied script steps are the author's, not fmsonar's

"Copy FM snippet" and `fm-ddr clip` reproduce script steps from the export as a
FileMaker clipboard snippet. If the export came from someone else, review those steps
in Script Workspace before running them, as you would any script a third party hands
you.

## Reporting a vulnerability

Email tdesmet@oogi.io with details and a reproduction. Please do not open a public
issue for anything exploitable until it is fixed.
