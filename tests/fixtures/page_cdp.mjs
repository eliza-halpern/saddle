// Runs one Chrome-driven test body on a fresh page: the driver behind
// tests/chrome_page.py (drive_page), so a new browser test needs no driver file.
//
// usage: node page_cdp.mjs <base-url>
//   stdin: {"body": "<the inside of an async function of (page, args)>",
//           "args": {...}, "width": 1280, "scheme": "light", "shots": ""}
// What the body returns is printed as the last line, as JSON. A body that throws,
// or a wait that times out, exits 1 with the message. The page API is the one in
// cdp_page.mjs.
import { readFileSync } from "node:fs";
import { withPage } from "./cdp_page.mjs";

const [base] = process.argv.slice(2);
const input = JSON.parse(readFileSync(0, "utf8"));
const AsyncFunction = /** @type {new (...names: string[]) => (...values: any[]) => Promise<unknown>} */ (
  Object.getPrototypeOf(async () => {}).constructor
);
try {
  const run = new AsyncFunction("page", "args", String(input.body));
  const result = await withPage(base, (page) => run(page, input.args ?? {}), {
    width: input.width,
    scheme: input.scheme,
    shots: input.shots,
  });
  console.log(JSON.stringify(result ?? null));
  process.exit(0);
} catch (error) {
  console.error(String((error instanceof Error && error.stack) || error));
  process.exit(1);
}
