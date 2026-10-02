"""The Chrome test helper: a browser test in a few lines, failing loudly when it fails.

`chrome_page.drive_page` runs a body on a fresh page through
tests/fixtures/page_cdp.mjs (the page API is in tests/fixtures/cdp_page.mjs).

Known-good: the helper's own docstring example passes; a value computed in the
page comes back; values reach a page function as JSON; `chat` waits for the
session's first render, which rebuilds the transcript, even when its
session.info is late; `click` finds an element
that appears only later; `type` and `key` reach the page; with
`SADDLE_JS_COVERAGE_DIR` set a run leaves page-script coverage there; every test
module that imports the helper is listed under `chrome_tests`. Known-bad: a wait
that never comes true fails naming what it waited for; an exception in the page
fails with the page's message; values given with a string of page JavaScript are
refused; without a browser the test is skipped, never passed; a module that
imports the helper but is not listed is named.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat

from saddle.jsevidence import read_coverage_scope

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


@needs_chrome
def test_the_docstring_example_passes(tmp_path: Path) -> None:
    with served_chat(tmp_path, title="parser work") as site:
        got = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            return await page.js(() => document.querySelector("#title").value);
            """,
            sid=site.sid,
        )
    assert got == "parser work"


@needs_chrome
def test_values_reach_the_page_as_json_and_click_type_and_key_reach_it(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site:
        got = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            const sum = await page.js((a, b) => a.x + b, { x: 2 }, 3);
            // An element that appears only later: click must wait for it, then click once.
            await page.js(() => {
              setTimeout(() => {
                const b = document.createElement("button");
                b.id = "late";
                b.onclick = () => { window.clicks = (window.clicks || 0) + 1; };
                document.body.appendChild(b);
              }, 300);
            });
            await page.click("#late");
            await page.js(() => {
              const i = document.createElement("input");
              i.id = "probe";
              i.onkeydown = (e) => { window.keys = (window.keys || "") + e.key; };
              document.body.appendChild(i);
            });
            await page.type("#probe", "typed");
            await page.key("Escape");
            return {
              sum,
              clicks: await page.js(() => window.clicks),
              text: await page.js(() => document.querySelector("#probe").value),
              keys: await page.js(() => window.keys),
            };
            """,
            sid=site.sid,
        )
    assert got == {"sum": 5, "clicks": 1, "text": "typed", "keys": "Escape"}


# Installed before the page's own scripts: the session's session.info event is
# held back 1.5 s, as a slow server (or a loaded machine) delivers it.
LATE_SESSION_INFO = """
const real = Object.getOwnPropertyDescriptor(EventSource.prototype, "onmessage");
Object.defineProperty(EventSource.prototype, "onmessage", {
  get() { return real.get.call(this); },
  set(handler) {
    real.set.call(this, (message) => {
      let late = false;
      try { late = JSON.parse(message.data).kind === "session.info"; } catch {}
      if (late) setTimeout(() => handler(message), 1500); else handler(message);
    });
  },
});
"""

# A turn painted, then looked at again once the late session.info has arrived.
TURN_THEN_LOOK = """
async (text) => {
  const count = () => document.querySelector("#transcript").textContent.split(text).length - 1;
  handle({ kind: "turn.start" });
  handle({ kind: "content.delta", text });
  handle({ kind: "turn.end" });
  const painted = count();
  await new Promise((r) => setTimeout(r, 2500));
  return { painted, later: count() };
}
"""


@needs_chrome
def test_chat_waits_for_the_sessions_first_render_even_when_it_is_late(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site:
        got = drive_page(
            site.base,
            f"""
            const late = {json.dumps(LATE_SESSION_INFO)};
            const look = {json.dumps(TURN_THEN_LOOK)};
            await page.send("Page.addScriptToEvaluateOnNewDocument", {{ source: late }});
            // Ready as soon as the session is selected: what the drivers used to wait for.
            await page.goto("/");
            await page.until(
              (s) => typeof state !== "undefined" && state.sessionId === s,
              args.sid,
            );
            const early = await page.js(`(${{look}})("started too early")`);
            // Ready once the session's first render is done: what page.chat waits for.
            await page.chat(args.sid);
            const ready = await page.js(`(${{look}})("started once rendered")`);
            return {{ early, ready }};
            """,
            sid=site.sid,
        )
    # The early turn was painted, then wiped by the first session.info's rebuild.
    assert got == {"early": {"painted": 1, "later": 0}, "ready": {"painted": 1, "later": 1}}


@needs_chrome
def test_a_wait_that_never_comes_true_fails_naming_it(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site, pytest.raises(pytest.fail.Exception) as failed:
        drive_page(
            site.base,
            """
            await page.chat(args.sid);
            page.timeout = 400;
            await page.until(() => document.querySelector("#never-there"));
            """,
            sid=site.sid,
        )
    assert "timed out after 400 ms waiting for: () => document.querySelector(" in str(failed.value)


@needs_chrome
def test_an_exception_in_the_page_fails_with_its_message(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site, pytest.raises(pytest.fail.Exception) as failed:
        drive_page(
            site.base,
            """
            await page.chat(args.sid);
            await page.js(() => { throw new Error("boom in the page"); });
            """,
            sid=site.sid,
        )
    assert "the page threw: Error: boom in the page" in str(failed.value)


@needs_chrome
def test_values_with_a_string_of_page_javascript_are_refused(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site, pytest.raises(pytest.fail.Exception) as failed:
        drive_page(site.base, "await page.js('1 + 1', 2);")
    assert "values go to a function, not to a string of page JavaScript" in str(failed.value)


@needs_chrome
def test_a_run_leaves_page_script_coverage_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "coverage"
    monkeypatch.setenv("SADDLE_JS_COVERAGE_DIR", str(out))
    with served_chat(tmp_path) as site:
        drive_page(site.base, "await page.chat(args.sid);", sid=site.sid)
    urls = {
        entry["url"]
        for path in out.glob("coverage-*.json")
        for entry in json.loads(path.read_text())["result"]
    }
    assert (REPO / "src/saddle/web/static/app.js").as_uri() in urls


def test_without_a_browser_the_test_is_skipped_never_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrome_page

    monkeypatch.setattr(chrome_page, "BROWSER", None)
    with pytest.raises(pytest.skip.Exception, match="needs node and google-chrome"):
        drive_page("http://127.0.0.1:1", "return 1;")


def page_modules(tests: Path) -> list[str]:
    """The test modules that import the helper, by file name."""
    found = []
    for path in sorted(tests.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.ImportFrom) and node.module == "chrome_page") or (
                isinstance(node, ast.Import) and any(a.name == "chrome_page" for a in node.names)
            ):
                found.append(path.name)
                break
    return found


def unlisted(tests: Path, listed: list[str]) -> list[str]:
    """Modules that drive a page but whose coverage the audit would never read."""
    names = {Path(t).name for t in listed}
    return [m for m in page_modules(tests) if m not in names]


def test_every_module_that_drives_a_page_is_a_listed_chrome_test() -> None:
    assert "test_chrome_page.py" in page_modules(HERE)
    assert unlisted(HERE, list(read_coverage_scope(REPO).chrome_tests)) == []


def test_a_module_that_drives_a_page_but_is_not_listed_is_named(tmp_path: Path) -> None:
    (tmp_path / "test_new_ui.py").write_text("from chrome_page import drive_page\n")
    (tmp_path / "test_other.py").write_text("import chrome_page\n")
    (tmp_path / "test_plain.py").write_text("import json\n")
    assert unlisted(tmp_path, ["tests/test_other.py"]) == ["test_new_ui.py"]
