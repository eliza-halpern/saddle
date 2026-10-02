"""Every Chrome the browser drivers start gets a DevTools port no other test holds.

The drivers once drew a random port in 9300-9899 each. Under eight test workers
two Chromes now and then drew the same one: the second could not bind it, ran
without DevTools, and its driver drove the first test's page, so a different
Chrome-driven test failed on a different run. Now each Chrome is started with
`--remote-debugging-port=0` and the driver reads the port the OS gave it from
`DevToolsActivePort` (`tests/fixtures/cdp_port.mjs`).

Known-good: a complete `DevToolsActivePort` gives its port; every driver that
starts Chrome asks for port 0 and reads it back; two real Chromes started
together get two ports, each listing only its own page. Known-bad: a missing,
empty, half-written or malformed file gives 0 (poll again), never a port; a
driver that draws its own port, or that never reads the file, is named.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from browser_guard import BROWSER

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
needs_checkout = pytest.mark.skipif(
    not (REPO / ".git").exists(), reason="needs a git checkout with the driver fixtures"
)
needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def _node(script: str, timeout: float = 60) -> object:
    ran = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return json.loads(ran.stdout)


def own_port_problems(name: str, text: str) -> list[str]:
    """Why a driver that starts Chrome could share a DevTools port with another test."""
    problems = []
    if "--remote-debugging-port=0" not in text:
        problems.append(f"{name}: does not start Chrome with --remote-debugging-port=0")
    if re.search(r"--remote-debugging-port=\$\{", text):
        problems.append(f"{name}: computes a port of its own")
    if not re.search(r'import \{ activePort \} from "\./cdp_port\.mjs";', text):
        problems.append(f"{name}: does not read the port Chrome got (activePort)")
    return problems


def chrome_drivers() -> list[Path]:
    """Every fixture script that starts Chrome: the census, not a list kept by hand."""
    return sorted(p for p in FIXTURES.glob("*.mjs") if '"google-chrome"' in p.read_text())


@needs_checkout
def test_the_port_is_read_only_from_a_complete_file(tmp_path: Path) -> None:
    cases = {
        "good": "43279\n/devtools/browser/b6936a25",
        "empty": "",
        "port-only": "43279",
        "half-port": "432",
        "no-path": "43279\n",
        "other-path": "43279\n/json",
        "not-a-number": "abc\n/devtools/browser/x",
        "zero": "0\n/devtools/browser/x",
        "too-big": "70000\n/devtools/browser/x",
    }
    dirs = {}
    for name, text in cases.items():
        d = tmp_path / name
        d.mkdir()
        (d / "DevToolsActivePort").write_text(text)
        dirs[name] = str(d)
    dirs["missing"] = str(tmp_path / "missing")
    got = _node(
        "import { activePort } from './tests/fixtures/cdp_port.mjs';"
        f"const dirs = {json.dumps(dirs)};"
        "console.log(JSON.stringify(Object.fromEntries("
        "Object.entries(dirs).map(([k, d]) => [k, activePort(d)]))));"
    )
    assert got == {name: (43279 if name == "good" else 0) for name in dirs}


@needs_checkout
def test_no_driver_picks_its_own_devtools_port() -> None:
    drivers = chrome_drivers()
    assert len(drivers) >= 11, [p.name for p in drivers]
    assert [x for p in drivers for x in own_port_problems(p.name, p.read_text())] == []


def test_a_driver_that_draws_its_own_port_is_named() -> None:
    old = (
        "const port = 9300 + Math.floor(Math.random() * 600);\n"
        'spawn("google-chrome", [`--remote-debugging-port=${port}`]);\n'
    )
    assert own_port_problems("old.mjs", old) == [
        "old.mjs: does not start Chrome with --remote-debugging-port=0",
        "old.mjs: computes a port of its own",
        "old.mjs: does not read the port Chrome got (activePort)",
    ]
    unread = 'spawn("google-chrome", ["--remote-debugging-port=0"]);\n'
    assert own_port_problems("unread.mjs", unread) == [
        "unread.mjs: does not read the port Chrome got (activePort)"
    ]


@needs_checkout
@needs_chrome
def test_two_chromes_started_together_each_get_their_own_page() -> None:
    script = """
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { activePort } from "./tests/fixtures/cdp_port.mjs";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const launch = (title) => {
  const prof = mkdtempSync(join(tmpdir(), "cdp-port-test-"));
  const chrome = spawn("google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu",
    "--remote-debugging-port=0", `--user-data-dir=${prof}`,
    `data:text/html,<title>${title}</title>`], { stdio: "ignore" });
  return { title, prof, chrome };
};
const pages = async (c) => {
  for (let i = 0; i < 150; i++) {
    const port = activePort(c.prof);
    if (port) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
        const titles = list.filter((t) => t.type === "page").map((t) => t.title);
        // Until the page has parsed its <title>, Chrome titles it with its URL.
        if (titles.length && !titles.some((t) => t.startsWith("data:"))) return { port, titles };
      } catch {}
    }
    await sleep(100);
  }
  return { port: 0, titles: [] };
};
const both = [launch("first"), launch("second")];
try {
  console.log(JSON.stringify(await Promise.all(both.map(pages))));
} finally {
  for (const c of both) c.chrome.kill();
  await sleep(300);
  for (const c of both) rmSync(c.prof, { recursive: true, force: true });
}
"""
    got = _node(script, timeout=90)
    assert isinstance(got, list)
    first, second = got
    assert first["titles"] == ["first"]
    assert second["titles"] == ["second"]
    assert first["port"] != second["port"]
    assert first["port"] > 0
    assert second["port"] > 0
