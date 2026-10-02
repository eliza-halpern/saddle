// Counts the pixels that differ between two PNGs of the same size and, when
// any do, writes a diff image (differing pixels in red over a faded copy of
// the reference). Used by tests/test_ui_snapshots.py; node >= 22 and the
// repository's pixelmatch and pngjs packages only. No browser.
//
// Every difference counts, anti-aliased edges included: a text edge that moved
// by a pixel is the change this comparison exists to see, and the allowed
// count is the caller's decision, not a mode of the library.
//
// usage: node snapshot_compare.mjs <reference.png> <actual.png> <diff.png> [threshold]
// stdout: one JSON line, {"width", "height", "differing"} for two images of
// one size, or {"error": "size", "reference": [w, h], "actual": [w, h]}.
import { readFileSync, writeFileSync } from "node:fs";
import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";

const [referencePath, actualPath, diffPath, threshold = "0.1"] = process.argv.slice(2);
const reference = PNG.sync.read(readFileSync(referencePath));
const actual = PNG.sync.read(readFileSync(actualPath));

if (reference.width !== actual.width || reference.height !== actual.height) {
  console.log(
    JSON.stringify({
      error: "size",
      reference: [reference.width, reference.height],
      actual: [actual.width, actual.height],
    }),
  );
} else {
  const { width, height } = reference;
  const diff = new PNG({ width, height });
  const differing = pixelmatch(reference.data, actual.data, diff.data, width, height, {
    threshold: Number(threshold),
    includeAA: true,
  });
  if (differing > 0) writeFileSync(diffPath, PNG.sync.write(diff));
  console.log(JSON.stringify({ width, height, differing }));
}
