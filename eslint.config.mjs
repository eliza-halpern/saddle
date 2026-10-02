// The browser files are classic scripts loaded by static/index.html in one
// page, so a top-level name in one is a global in the others. `provides`
// names, per file, the top-level names another file relies on; every file
// gets the other files' names as read-only globals, so `no-undef` stays on
// and a name that is neither local nor listed here is a real defect.
import js from "@eslint/js";
import globals from "globals";

const STATIC = "src/saddle/web/static/";
const provides = {
  "markdown.js": [
    "el", "fillToolDetail", "attachCopy", "renderMarkdown", "paintStream",
    "renderDiff", "atBottom", "stickToBottom", "followBottom", "watchScrolling",
  ],
  "tasks.js": [
    "handleTask", "recapCard", "stopTask", "startTask", "openRunConfirm",
    "closeRunConfirm", "paintTestPolicy",
  ],
  "notify.js": ["runState", "noteSessions", "runSelected"],
  "runs.js": ["deleteSession", "loadRuns"],
  "app.js": [
    "$", "state", "api", "notice", "setMode", "setStatus", "newTurn",
    "select", "boot", "loadSessions", "errorText",
  ],
};

const sharedGlobals = (file) => Object.fromEntries(
  Object.entries(provides)
    .filter(([name]) => name !== file)
    .flatMap(([, names]) => names)
    .map((name) => [name, "readonly"]),
);

const staticFiles = Object.keys(provides).map((file) => ({
  files: [STATIC + file],
  languageOptions: {
    sourceType: "script",
    globals: {
      ...globals.browser,
      ...sharedGlobals(file),
      // markdown.js exports for the node test only when `module` exists.
      ...(file === "markdown.js" ? { module: "readonly" } : {}),
    },
  },
}));

export default [
  { ignores: ["node_modules/**", ".venv/**", "benchmark/**"] },
  js.configs.recommended,
  {
    rules: {
      eqeqeq: ["error", "always"],
      "no-shadow": "error",
      "consistent-return": "error",
      "no-throw-literal": "error",
      "no-var": "error",
      "prefer-const": "error",
      // A leading underscore marks a parameter kept for the caller's signature.
      "no-unused-vars": ["error", { args: "after-used", argsIgnorePattern: "^_", caughtErrors: "all" }],
    },
  },
  ...staticFiles,
  {
    files: [STATIC + "*.js"],
    rules: {
      // A top-level name in one script is another script's global, so a
      // name this file never reads may be read by a sibling.
      "no-unused-vars": [
        "error",
        { vars: "local", args: "after-used", argsIgnorePattern: "^_", caughtErrors: "all" },
      ],
    },
  },
  {
    files: ["tests/*.js", "tests/fixtures/*.js"],
    languageOptions: { sourceType: "commonjs", globals: { ...globals.node } },
  },
  {
    // The node test and the shared DOM shim install their own fake `document` on the global object.
    files: ["tests/markdown.test.js", "tests/fixtures/dom_shim.js"],
    languageOptions: { globals: { document: "writable", $: "writable" } },
  },
  {
    files: ["tests/fixtures/*.mjs", "*.mjs"],
    languageOptions: { sourceType: "module", globals: { ...globals.node } },
  },
];
