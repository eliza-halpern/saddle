// Which top-level functions, classes and constants a change added to a JavaScript
// file does production code reach?  Run by `saddle.jsdead`, never by the page.
//
//   node jsdead.mjs <root> <tools-root> '<json: {"added": {rel: [[first, last]]}, "tests": [rel]}>'
//
// Prints one JSON object on stdout:
//   {"findings": [{file, line, name, kind, callers}], "unresolved": [{file, line, name, why}],
//    "skipped": [{file, why}], "judged": N, "also": [names only a finding uses]}
// or {"unavailable": "<why>"} when the TypeScript parser cannot be found or started.
//
// The parser is TypeScript 7's (`typescript/unstable/sync`, which drives the native
// binary): it only parses here, nothing is type-checked. References are matched by
// NAME, like the Python rule, so a common name another file also uses reads as use:
// the rule leans to accepting. A name counts as used when it appears as an identifier
// (a call, a read, a property name, an import or destructured `require`) in a non-test
// file outside the definition's own body, in an inline handler or inline script of an
// html file, or inside a definition that is itself reached. Export sites alone
// (`module.exports = { f }`, `exports.f = f`, `export { f }`) are not use: the page
// scripts share globals through <script> tags, so the use is another script's.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

// The parser's own typings (typescript/unstable/*) type the nodes and the program; the
// package is loaded by file path at run time, so these imports are for the checker only.
/** @typedef {import("typescript/unstable/ast").Node} Node */
/** @typedef {import("typescript/unstable/ast").SourceFile} SourceFile */
/**
 * The fields of a node this script reads after checking its `kind` (the parser's typings do
 * not narrow on `kind` through a variable), so one local view stands in for the union.
 * @typedef {Node & {
 *   parent: Syn, text: string, name: Syn, left: Syn, right: Syn, operatorToken: Syn,
 *   expression: Syn, arguments: Syn[], argumentExpression: Syn, moduleSpecifier?: Syn,
 *   declarationList: {declarations: Syn[]}
 * }} Syn
 */
/** @typedef {{file: string, name: string, pos: number, test: boolean}} Reference */
/** @typedef {{file: string, name: string, line: number, start: number, end: number}} Candidate */

const [rootArg, toolsArg, specText] = process.argv.slice(2);
const root = path.resolve(rootArg);
const spec = JSON.parse(specText);
const SKIPPED_DIRS = new Set(["node_modules", ".git", ".saddle", ".stryker-tmp", ".venv", "__pycache__"]);
const JS = /\.(js|mjs|cjs)$/;
const HTML = /\.html?$/;

/** @type {(code: number) => never} */
const stop = (code) => {
  process.exit(code);
  throw new Error("unreachable");
};

/** @param {unknown} object */
function say(object) {
  console.log(JSON.stringify(object));
}

/**
 * @param {string} dir
 * @param {string[]} out
 * @returns {string[]}
 */
function walk(dir, out) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!SKIPPED_DIRS.has(entry.name)) walk(path.join(dir, entry.name), out);
    } else if (entry.isFile() && (JS.test(entry.name) || HTML.test(entry.name))) {
      out.push(path.join(dir, entry.name));
    }
  }
  return out;
}

function toolEntry() {
  for (const base of [root, toolsArg]) {
    if (!base) continue;
    const modules = path.join(path.resolve(base), "node_modules", "typescript");
    const api = path.join(modules, "dist/api/sync/api.js");
    if (fs.existsSync(api)) return { api, ast: path.join(modules, "dist/ast/index.js") };
  }
  return null;
}

const entry = toolEntry();
if (entry === null) {
  say({ unavailable: "typescript was not found in node_modules" });
  stop(0);
}

let API;
/** @type {typeof import("typescript/unstable/ast").SyntaxKind} */
let K;
try {
  ({ API } = await import(pathToFileURL(fs.realpathSync(entry.api)).href));
  ({ SyntaxKind: K } = await import(pathToFileURL(fs.realpathSync(entry.ast)).href));
} catch (error) {
  say({ unavailable: `typescript's parser could not be loaded: ${error instanceof Error ? error.message : error}` });
  stop(0);
}

const files = walk(root, []).sort();
const jsFiles = files.filter((f) => JS.test(f));
/** @param {string} abs */
const rel = (abs) => path.relative(root, abs).split(path.sep).join("/");
const isTest = new Set(spec.tests);
/** @type {Map<string, (line: number) => boolean>} */
const ranges = new Map(
  Object.entries(/** @type {Record<string, [number, number][]>} */ (spec.added)).map(([file, spans]) => [
    file,
    (/** @type {number} */ line) => spans.some(([first, last]) => first <= line && line <= last),
  ]),
);

// The scratch config lives outside the tree: the audit runs other tools over the same
// tree at the same time, and a directory made and removed there broke their walks.
const configDir = fs.mkdtempSync(path.join(os.tmpdir(), "saddle-jsdead-config-"));
const configFile = path.join(configDir, "jsdead.tsconfig.json");
let api;
let program;
try {
  fs.writeFileSync(
    configFile,
    JSON.stringify({
      compilerOptions: { allowJs: true, noEmit: true, noResolve: true, types: [], skipLibCheck: true },
      files: jsFiles,
    }),
  );
  api = new API({ cwd: root });
  program = api.updateSnapshot({ openProject: configFile }).getProjects()[0].program;
} catch (error) {
  if (api) api.close();
  fs.rmSync(configDir, { recursive: true, force: true });
  say({
    unavailable: `typescript's parser could not start: ${String(error instanceof Error ? error.message : error).split("\n")[0]}`,
  });
  stop(0);
}

/** @type {Candidate[]} */
const candidates = [];
/** @type {Reference[]} */
const references = [];
const specifiers = new Set();
const unparsed = new Map();
/** @type {string[]} */
const dynamicGlobals = [];
const pageNames = new Set();
const scriptSources = [];
const texts = new Map();

/**
 * @param {SourceFile} sf
 * @param {number} pos
 */
const lineOf = (sf, pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;
/**
 * @param {Node} node
 * @param {SourceFile} sf
 */
const textOf = (node, sf) => node.getText(sf);

/**
 * @param {Syn} left
 * @param {SourceFile} sf
 */
function moduleExportsTarget(left, sf) {
  return /^(module\.)?exports(\.[\w$]+)?$/.test(textOf(left, sf));
}

/**
 * @param {Syn} literal
 * @param {SourceFile} sf
 */
function isExportsObject(literal, sf) {
  const p = literal.parent;
  return (
    p.kind === K.BinaryExpression &&
    p.operatorToken.kind === K.EqualsToken &&
    p.right === literal &&
    /^(module\.)?exports$/.test(textOf(p.left, sf))
  );
}

// An identifier that only exports a name (the use is somebody else's) is not use.
/**
 * @param {Syn} node
 * @param {SourceFile} sf
 */
function isExportSite(node, sf) {
  const p = node.parent;
  if (p.kind === K.ExportSpecifier) return p.parent.parent.moduleSpecifier === undefined;
  if (p.kind === K.ExportAssignment) return true;
  if (p.kind === K.ShorthandPropertyAssignment) return isExportsObject(p.parent, sf);
  if (p.kind === K.PropertyAssignment && p.parent.kind === K.ObjectLiteralExpression) {
    return isExportsObject(p.parent, sf);
  }
  if (p.kind === K.BinaryExpression && p.operatorToken.kind === K.EqualsToken && p.right === node) {
    return moduleExportsTarget(p.left, sf);
  }
  if (p.kind === K.PropertyAccessExpression && p.name === node) {
    const assign = p.parent;
    return (
      assign.kind === K.BinaryExpression &&
      assign.operatorToken.kind === K.EqualsToken &&
      assign.left === p &&
      moduleExportsTarget(p, sf)
    );
  }
  return false;
}

/**
 * @param {Syn} statement
 * @returns {Syn[]}
 */
function declared(statement) {
  if (statement.kind === K.FunctionDeclaration || statement.kind === K.ClassDeclaration) {
    return statement.name ? [statement.name] : [];
  }
  if (statement.kind === K.VariableStatement) {
    return statement.declarationList.declarations.map((d) => d.name).filter((name) => name.kind === K.Identifier);
  }
  return [];
}

for (const abs of jsFiles) {
  const file = rel(abs);
  const sf = program.getSourceFile(abs);
  if (sf === undefined) continue;
  const text = fs.readFileSync(abs, "utf8");
  texts.set(file, text);
  if (program.getSyntacticDiagnostics(abs).length > 0) unparsed.set(file, text);
  if (text.startsWith("#!") || /require\.main\s*===\s*module/.test(text)) specifiers.add(`<script>${file}`);
  const test = isTest.has(file);
  const own = new Set();
  const inside = ranges.get(file);
  if (!test && inside !== undefined) {
    for (const statement of /** @type {Syn[]} */ (/** @type {unknown} */ (sf.statements))) {
      const first = lineOf(sf, statement.getStart(sf));
      const last = lineOf(sf, Math.max(statement.getStart(sf), statement.end - 1));
      for (const name of declared(statement)) {
        own.add(name.pos);
        let whole = true;
        for (let n = first; n <= last; n += 1) whole = whole && inside(n);
        if (whole) {
          candidates.push({
            file,
            name: name.text,
            line: first,
            start: statement.getStart(sf),
            end: statement.end,
          });
        }
      }
    }
  }
  /** @param {Node} visited */
  const visit = (visited) => {
    const node = /** @type {Syn} */ (visited);
    if (node.kind === K.Identifier) {
      if (!own.has(node.pos) && !isExportSite(node, sf)) {
        references.push({ file, name: node.text, pos: node.getStart(sf), test });
      }
    } else if (node.kind === K.ImportDeclaration || node.kind === K.ExportDeclaration) {
      if (node.moduleSpecifier && !test) specifiers.add(node.moduleSpecifier.text);
    } else if (node.kind === K.CallExpression) {
      const callee = node.expression;
      const dynamicImport = callee.kind === K.ImportKeyword;
      if ((dynamicImport || (callee.kind === K.Identifier && callee.text === "require")) && !test) {
        const first = node.arguments[0];
        if (first && first.kind === K.StringLiteral) specifiers.add(first.text);
      }
    } else if (node.kind === K.ElementAccessExpression && !test) {
      const target = textOf(node.expression, sf);
      const key = node.argumentExpression.kind;
      if (
        ["window", "globalThis", "self"].includes(target) &&
        key !== K.StringLiteral &&
        key !== K.NoSubstitutionTemplateLiteral
      ) {
        dynamicGlobals.push(`${file}:${lineOf(sf, node.getStart(sf))}`);
      }
    }
    visited.forEachChild(visit);
  };
  visit(sf);
}
api.close();
fs.rmSync(configDir, { recursive: true, force: true });

// The page: <script src> files and the names an inline handler or inline script uses.
for (const abs of files.filter((f) => HTML.test(f))) {
  const html = fs.readFileSync(abs, "utf8");
  for (const m of html.matchAll(/<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']/gi)) scriptSources.push(m[1]);
  const code = [];
  for (const m of html.matchAll(/\bon[a-z]+\s*=\s*"([^"]*)"|\bon[a-z]+\s*=\s*'([^']*)'/gi)) {
    code.push(m[1] ?? m[2]);
  }
  for (const m of html.matchAll(/<script\b(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)<\/script>/gi)) code.push(m[1]);
  for (const piece of code) for (const id of piece.match(/[A-Za-z_$][\w$]*/g) ?? []) pageNames.add(id);
}

let packageText = "";
try {
  packageText = fs.readFileSync(path.join(root, "package.json"), "utf8");
} catch {
  // no package.json: nothing there names an entry file
}

/** @param {string} name */
const stemOf = (name) => path.basename(name).replace(/\.(js|mjs|cjs)$/, "");
const loadedStems = new Set([...specifiers].filter((s) => !s.startsWith("<script>")).map((s) => stemOf(s)));
const loadedFiles = new Set(scriptSources.map((s) => path.basename(s)));

/** @param {string} file */
function reached(file) {
  const base = path.basename(file);
  return (
    loadedFiles.has(base) ||
    loadedStems.has(stemOf(file)) ||
    specifiers.has(`<script>${file}`) ||
    packageText.includes(base)
  );
}

/** @type {{file: string, why: string}[]} */
const skipped = [];
/** @type {Candidate[]} */
const judged = [];
for (const c of candidates) {
  if (reached(c.file)) judged.push(c);
  else if (!skipped.some((s) => s.file === c.file)) {
    skipped.push({ file: c.file, why: "no page script, import or package entry loads it" });
  }
}

const holders = judged.map(() => new Set());
const callers = judged.map(() => new Set());
judged.forEach((c, i) => {
  if (pageNames.has(c.name)) holders[i].add(null);
});
for (const ref of references) {
  const inside = judged.findIndex((c) => c.file === ref.file && c.start <= ref.pos && ref.pos < c.end);
  judged.forEach((c, i) => {
    if (c.name !== ref.name || inside === i) return;
    if (ref.test) callers[i].add(ref.file);
    else holders[i].add(inside === -1 ? null : inside);
  });
}

const live = new Set(judged.map((_, i) => i).filter((i) => holders[i].has(null)));
for (let grew = true; grew;) {
  grew = false;
  holders.forEach((held, i) => {
    if (!live.has(i) && [...held].some((h) => h !== null && live.has(h))) {
      live.add(i);
      grew = true;
    }
  });
}
const dead = judged.map((_, i) => i).filter((i) => !live.has(i));
const roots = dead.filter((i) => ![...holders[i]].some((h) => dead.includes(h)));
const shown = roots.length > 0 ? roots : dead;
const rest = dead.filter((i) => !shown.includes(i)).map((i) => judged[i].name);

const findings = [];
const unresolved = [];
for (const i of shown) {
  const c = judged[i];
  const word = new RegExp(`(?<![\\w$])${c.name.replace(/\$/g, "\\$")}(?![\\w$])`);
  const blind = [...unparsed].filter(([, text]) => word.test(text)).map(([file]) => file);
  const base = { file: c.file, line: c.line, name: c.name };
  if (blind.length > 0) {
    unresolved.push({ ...base, why: `${blind.join(", ")} does not parse and spells ${c.name}` });
  } else if (dynamicGlobals.length > 0) {
    unresolved.push({
      ...base,
      why: `a global is looked up by a computed name at ${dynamicGlobals[0]}, so use cannot be ruled out`,
    });
  } else {
    const how = [...callers[i]].sort();
    findings.push({
      ...base,
      kind: how.length > 0 ? "test-only" : holders[i].size > 0 ? "chain" : "nothing",
      callers: how,
    });
  }
}

say({ findings, unresolved, skipped, judged: judged.length, also: rest });
