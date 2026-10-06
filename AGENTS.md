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

A change to a page script usually needs both: a Chrome test for what the
page shows, and a node test for the logic behind it.

## Contributor rules

@CONTRIBUTING.md
