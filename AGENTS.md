# Notes for coding agents working on saddle

These are the repository's instructions for any coding agent, saddle's own
worker included. CLAUDE.md imports this file, so there is one source. The
contributor rules follow these notes and apply in full.

## Testing the web page

The page scripts in `src/saddle/web/static/` (`app.js`, `tasks.js`,
`notify.js`, `runs.js`) are measured two ways, by two different kinds of test:

- **Line coverage** comes from Chrome-driven pytest tests. Do not write a
  page driver of your own: `tests/chrome_page.py` has `served_chat` (a
  served chat with a session) and `drive_page` (runs a body on a fresh
  headless Chrome page). `docs/CHAT-UI.md` shows a complete test. A module
  that drives a page must be listed under `chrome_tests` in
  `tests/fixtures/js_coverage_scope.json`.
- **Mutation testing** of JavaScript runs StrykerJS over the `node --test`
  files only (`tests/*.test.js`). Chrome-driven tests never count toward
  it. To test a page script's logic where mutation can see it, load the
  script in node with `loadClassic` from `tests/fixtures/classic_script.js`
  (as `tests/classic_scripts.test.js` does) and call its functions directly,
  passing inert stubs for the page wiring.
- **app.js** cannot be loaded alone: it queries the document at load and
  reads names the other scripts define. Load it with `loadPage()` from the
  same file, which loads every page script into one scope with an inert
  page, then call a function and read back what it painted:
  `page.setStatus("working")`, then
  `page.document.querySelector("#status").textContent`. The
  "app.js loads beside its siblings" test in `tests/classic_scripts.test.js`
  shows the whole shape.
- Load page scripts only through these two helpers. Mutation testing counts
  a node test only when the script runs under its file URL and with
  `process.env`, which StrykerJS reads to switch a mutant on; a hand-rolled
  `vm` loader without both makes every mutant survive or read as untested.
- `tests/fixtures/dom_shim.js` is a fake DOM for `markdown.js`'s renderers
  (see `tests/markdown.test.js`), not for the page as a whole. A test file
  that reads its global `document` must be added to that file's group in
  `eslint.config.mjs`, or ESLint fails it with `no-undef`.

A change to a page script usually needs both: a Chrome test for what the
page shows, and a node test for the logic behind it. A new page script goes
in exactly one list of `tests/fixtures/js_coverage_scope.json` (`measured`,
or `not_measured` with its reason) and gets an entry in `eslint.config.mjs`'s
`provides`, as does any new top-level name another page script reads.

## Writing tests

Every pytest test starts in an empty temporary directory
(`tests/conftest.py`, `_empty_cwd`): open files relative to
`Path(__file__)`, never the working directory. To update the UI snapshot
references after an intended visual change, see `docs/CHAT-UI.md`,
"Snapshot tests".

## Contributor rules

@CONTRIBUTING.md
