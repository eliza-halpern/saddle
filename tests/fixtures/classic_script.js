"use strict";
/* Loads one of the browser's classic scripts into its own global scope, the
   way a page does, without editing the file: top-level functions and
   consts become names on the sandbox, so a test reaches them as it would
   from a sibling script. The script's page-wiring (event listeners, a poll
   timer) runs against the stubs the caller passes in; a test that only wants
   the pure functions passes inert ones. app.js is the exception: load it
   with `loadPage`, below, which loads all the page's scripts together. */

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { pathToFileURL } = require("node:url");

const STATIC = path.join(__dirname, "..", "..", "src", "saddle", "web", "static");

/* What every sandbox gets from node. StrykerJS switches a mutant on from
   `process.env` inside the mutated script, so a sandbox without it runs every
   mutant switched off: each one survives whatever the test asserts. Only the
   environment is shown, not `process` itself. */
const NODE_GLOBALS = { console, Date, Math, process: { env: process.env } };

function loadClassic(/** @type {string} */ file, globals = {}) {
  const full = path.join(STATIC, file);
  const sandbox = vm.createContext({ ...NODE_GLOBALS, ...globals });
  // A file URL, not a bare path, so the coverage collector attributes the
  // run to the file.
  vm.runInContext(fs.readFileSync(full, "utf8"), sandbox, { filename: pathToFileURL(full).href });
  return sandbox;
}

/* A top-level `const` or `let` is not a property of the sandbox, only a name
   in its script scope; evaluate the name there to reach it. */
function inScope(/** @type {any} */ sandbox, /** @type {string} */ expression) {
  return vm.runInContext(expression, sandbox);
}

/* The page's scripts in index.html's order. app.js cannot be loaded alone:
   it queries the document at load and reads names the others define. */
const PAGE_SCRIPTS = ["markdown.js", "tasks.js", "notify.js", "runs.js", "app.js"];

/* An element with what the page scripts touch at load and in their small
   painters: attributes, classes, text, listeners. A test asserts on these. */
class PageElement {
  /** @param {string} tag */
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    /** @type {Record<string, string>} */
    this.attrs = {};
    /** @type {Record<string, unknown[]>} */
    this.listeners = {};
    /** @type {PageElement[]} */
    this.children = [];
    /** @type {Record<string, string>} */
    this.dataset = {};
    /** @type {Record<string, string>} */
    this.style = {};
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.title = "";
    this.hidden = false;
    this.disabled = false;
  }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    const set = (/** @type {string[]} */ list) => (this.className = list.join(" "));
    return {
      add: (/** @type {string[]} */ ...add) => set([...new Set([...names(), ...add])]),
      remove: (/** @type {string[]} */ ...drop) => set(names().filter((n) => !drop.includes(n))),
      toggle: (/** @type {string} */ name, /** @type {boolean | undefined} */ force) => {
        const on = force === undefined ? !names().includes(name) : force;
        set(on ? [...new Set([...names(), name])] : names().filter((n) => n !== name));
        return on;
      },
      contains: (/** @type {string} */ name) => names().includes(name),
    };
  }
  /** @param {string} name @param {unknown} value */
  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }
  /** @param {string} name */
  getAttribute(name) {
    return name in this.attrs ? this.attrs[name] : null;
  }
  /** @param {string} name */
  removeAttribute(name) {
    delete this.attrs[name];
  }
  /** @param {string} type @param {unknown} handler */
  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  /** @param {PageElement[]} nodes */
  append(...nodes) {
    this.children.push(...nodes);
  }
  /** @param {PageElement} node */
  appendChild(node) {
    this.children.push(node);
    return node;
  }
  querySelector() {
    return new PageElement("div");
  }
  querySelectorAll() {
    return [];
  }
}

/* A document whose `querySelector` hands out one element per selector, so a
   test reads back what the page wrote: `doc.querySelector("#status")` is the
   element `$("#status")` painted. */
function pageDocument() {
  /** @type {Map<string, PageElement>} */
  const bySelector = new Map();
  return {
    /** @param {string} selector */
    querySelector(selector) {
      if (!bySelector.has(selector)) bySelector.set(selector, new PageElement("div"));
      return bySelector.get(selector);
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
    /** @param {string} tag */
    createElement: (tag) => new PageElement(tag),
    /** @param {unknown} text */
    createTextNode: (text) => ({ textContent: String(text) }),
    body: new PageElement("body"),
    documentElement: new PageElement("html"),
  };
}

/* Loads every page script into one shared scope, the way index.html does, with
   inert page wiring: timers never fire and requests never settle, so `boot`
   waits forever instead of failing. Each script runs under its file URL, as in
   `loadClassic`: mutation testing counts a node test only when V8 attributes
   the run to the file, and a bare name makes every mutant read as untested.
   Pass `globals` to replace any stub, `document` included. */
function loadPage(globals = {}) {
  const inert = {
    ...NODE_GLOBALS,
    document: pageDocument(),
    setInterval() {},
    setTimeout() {},
    clearTimeout() {},
    clearInterval() {},
    requestAnimationFrame() {},
    fetch: () => new Promise(() => {}),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: {},
    location: { href: "http://127.0.0.1/", origin: "http://127.0.0.1" },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
  };
  /** @type {Record<string, unknown>} */
  const scope = { ...inert, ...globals };
  scope.window = scope;
  const sandbox = vm.createContext(scope);
  for (const file of PAGE_SCRIPTS) {
    const full = path.join(STATIC, file);
    vm.runInContext(fs.readFileSync(full, "utf8"), sandbox, { filename: pathToFileURL(full).href });
  }
  return sandbox;
}

module.exports = { loadClassic, inScope, loadPage, pageDocument, PageElement, PAGE_SCRIPTS, STATIC };
