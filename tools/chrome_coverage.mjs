// Judges the line coverage the Chrome-driven tests collected for the browser
// scripts (tests/fixtures/cdp_coverage.mjs writes it while they run).
//
// usage: node tools/chrome_coverage.mjs check [coverage-dir] [--scope <scope.json>]
//
// The directory (default: SADDLE_JS_COVERAGE_DIR, else the one check.sh uses)
// holds V8 coverage files. `c8 report` merges them under .c8rc.chrome.json, and
// each file listed under `chrome_measured` in tests/fixtures/js_coverage_scope.json
// must reach its recorded floor for lines, branches and functions. A floor of 100
// is the end state; a lower one is a ratchet that a regression fails and that
// is raised whenever the tests are extended.
//
// No coverage files at all means no Chrome ran. That is "not measured here" and
// exits 0, unless SADDLE_REQUIRE_BROWSER is set (CI), where a missing browser is
// an error and so is this: the tests that should have produced the data skipped.
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_DIR = join(ROOT, "node_modules", ".cache", "chrome-coverage");
const METRICS = ["lines", "branches", "functions"];

/** Every way a file's measured percentages fall short of its floors, one line each. */
export function shortfalls(floors, summary) {
  const problems = [];
  for (const [file, want] of Object.entries(floors)) {
    const got = summary[join(ROOT, file)];
    if (!got) {
      problems.push(`${file}: no coverage was reported for it`);
      continue;
    }
    for (const metric of METRICS) {
      if (got[metric].pct < want[metric]) {
        problems.push(`${file}: ${metric} ${got[metric].pct}% is below the floor ${want[metric]}%`);
      }
    }
  }
  return problems;
}

function main(argv) {
  const [command, ...rest] = argv;
  const flag = rest.indexOf("--scope");
  const scope = flag < 0 ? join(ROOT, "tests/fixtures/js_coverage_scope.json") : resolve(rest.splice(flag, 2)[1]);
  const [dirArg] = rest;
  if (command !== "check") {
    console.error("usage: node tools/chrome_coverage.mjs check [coverage-dir] [--scope <scope.json>]");
    return 2;
  }
  const dir = resolve(dirArg ?? process.env.SADDLE_JS_COVERAGE_DIR ?? DEFAULT_DIR);
  const collected = existsSync(dir) ? readdirSync(dir).filter((n) => /^coverage-.*\.json$/.test(n)) : [];
  if (!collected.length) {
    if (process.env.SADDLE_REQUIRE_BROWSER) {
      console.error(`chrome coverage: no coverage in ${dir}, but SADDLE_REQUIRE_BROWSER is set`);
      return 1;
    }
    console.log(`chrome coverage: not measured here (no Chrome ran: nothing in ${dir})`);
    return 0;
  }
  const floors = JSON.parse(readFileSync(scope, "utf8")).chrome_measured;
  const out = mkdtempSync(join(tmpdir(), "chrome-cov-"));
  try {
    const c8 = spawnSync(
      join(ROOT, "node_modules", ".bin", "c8"),
      [
        "report",
        "--config",
        join(ROOT, ".c8rc.chrome.json"),
        "--temp-directory",
        dir,
        "--reporter",
        "text",
        "--reporter",
        "json-summary",
        "--reports-dir",
        out,
      ],
      { cwd: ROOT, encoding: "utf8" },
    );
    process.stdout.write(c8.stdout ?? "");
    if (c8.status !== 0) {
      console.error(c8.stderr);
      return 1;
    }
    const problems = shortfalls(floors, JSON.parse(readFileSync(join(out, "coverage-summary.json"), "utf8")));
    for (const p of problems) console.error(`chrome coverage: ${p}`);
    return problems.length ? 1 : 0;
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) process.exit(main(process.argv.slice(2)));
