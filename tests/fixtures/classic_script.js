"use strict";
/* Loads one of the browser's classic scripts into its own global scope, the
   way a page does, without editing the file: top-level functions and
   consts become names on the sandbox, so a test reaches them as it would
   from a sibling script. The script's page-wiring (event listeners, a poll
   timer) runs against the stubs the caller passes in; a test that only wants
   the pure functions passes inert ones. */

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { pathToFileURL } = require("node:url");

const STATIC = path.join(__dirname, "..", "..", "src", "saddle", "web", "static");

function loadClassic(file, globals = {}) {
  const full = path.join(STATIC, file);
  const sandbox = vm.createContext({ console, Date, Math, ...globals });
  // A file URL, not a bare path, so the coverage collector attributes the
  // run to the file.
  vm.runInContext(fs.readFileSync(full, "utf8"), sandbox, { filename: pathToFileURL(full).href });
  return sandbox;
}

/* A top-level `const` or `let` is not a property of the sandbox, only a name
   in its script scope; evaluate the name there to reach it. */
function inScope(sandbox, expression) {
  return vm.runInContext(expression, sandbox);
}

module.exports = { loadClassic, inScope, STATIC };
