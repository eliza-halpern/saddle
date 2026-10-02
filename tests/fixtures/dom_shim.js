"use strict";
/* A DOM, but only the parts the browser scripts touch: small enough to
   read, with no browser and no network. It installs itself as the global
   `document` (and a working `navigator`) when required, so a script loaded
   after it sees the same names it sees in a page. */

class TextNode {
  constructor(text) {
    this.data = String(text);
    this.parent = null;
  }
  get textContent() {
    return this.data;
  }
  // Escaped, like a real serializer: the renderer's safety property is that
  // model output lands in text nodes, and a shim that does not escape would
  // report that as safe no matter what the renderer did.
  get html() {
    return this.data.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
}

/* The two live collections a real node hands out. They are views over the
   element's own list, read at each access, and they offer exactly what the
   platform offers: `length`, index access, `item()` and iteration, plus
   `namedItem()` on an HTMLCollection and `forEach`/`keys`/`values`/`entries`
   on a NodeList. Neither has `find`, `filter`, `map` or `some`: a renderer
   that calls `row.children.find(...)` threw in every real page while a shim
   that returned an Array let the whole node suite pass. */
const READ = Symbol("read");

function live(collection) {
  return new Proxy(collection, {
    get(target, key, receiver) {
      if (typeof key === "string" && /^(0|[1-9]\d*)$/.test(key)) {
        return target[READ]()[Number(key)];
      }
      return Reflect.get(target, key, receiver);
    },
    has(target, key) {
      if (typeof key === "string" && /^(0|[1-9]\d*)$/.test(key)) {
        return Number(key) < target[READ]().length;
      }
      return Reflect.has(target, key);
    },
  });
}

class HTMLCollection {
  constructor(read) {
    this[READ] = read;
    return live(this);
  }
  get length() {
    return this[READ]().length;
  }
  item(index) {
    return this[READ]()[index] || null;
  }
  namedItem(name) {
    return this[READ]().find((n) => n.id === name || (n.attrs && n.attrs.name === name)) || null;
  }
  [Symbol.iterator]() {
    return this[READ]()[Symbol.iterator]();
  }
}

class NodeList {
  constructor(read) {
    this[READ] = read;
    return live(this);
  }
  get length() {
    return this[READ]().length;
  }
  item(index) {
    return this[READ]()[index] || null;
  }
  forEach(callback, thisArg) {
    this[READ]().forEach((n, i) => callback.call(thisArg, n, i, this));
  }
  keys() {
    return this[READ]().keys();
  }
  values() {
    return this[READ]().values();
  }
  entries() {
    return this[READ]().entries();
  }
  [Symbol.iterator]() {
    return this[READ]().values();
  }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this._kids = [];
    this.dataset = {};
    this.parent = null;
    this._cls = "";
    this.style = {};
  }
  get childNodes() {
    return new NodeList(() => this._kids);
  }
  get children() {
    return new HTMLCollection(() => this._kids.filter((n) => n instanceof Element));
  }
  get firstChild() {
    return this._kids[0] || null;
  }
  get firstElementChild() {
    return this.children.item(0);
  }
  get lastElementChild() {
    return this.children.item(this.children.length - 1);
  }
  set className(v) {
    this._cls = v || "";
  }
  get className() {
    return this._cls;
  }
  get classList() {
    const self = this;
    return {
      contains: (c) => self._cls.split(/\s+/).includes(c),
      add: (c) => {
        if (!self.classList.contains(c)) self._cls = (self._cls + " " + c).trim();
      },
      remove: (c) => {
        self._cls = self._cls
          .split(/\s+/)
          .filter((x) => x !== c)
          .join(" ");
      },
    };
  }
  setAttribute(name, value) {
    (this.attrs ||= {})[name] = value;
  }
  // The legacy copy path hands a selected field to execCommand("copy");
  // the shim remembers the last selection the way the selection range does.
  select() {
    global.document.__lastSelected = this;
  }
  remove() {
    if (this.parent) {
      const kids = this.parent._kids;
      kids.splice(kids.indexOf(this), 1);
      this.parent = null;
    }
  }
  appendChild(node) {
    // Real appendChild *moves* a node. The incremental painter relies on it:
    // `while (chunk.firstChild) stable.appendChild(chunk.firstChild)` never
    // terminates against a shim that copies.
    if (node.parent) {
      const kids = node.parent._kids;
      kids.splice(kids.indexOf(node), 1);
    }
    node.parent = this;
    this._kids.push(node);
    return node;
  }
  append(...nodes) {
    for (const n of nodes) this.appendChild(n);
  }
  get textContent() {
    return this._kids.map((n) => n.textContent).join("");
  }
  set textContent(v) {
    for (const kid of this._kids) kid.parent = null;
    this._kids = [];
    if (v !== "" && v !== undefined && v !== null) this._kids.push(new TextNode(v));
  }
  get html() {
    const cls = this._cls ? ` class="${this._cls}"` : "";
    const tag = this.tagName.toLowerCase();
    return `<${tag}${cls}>` + this._kids.map((n) => n.html).join("") + `</${tag}>`;
  }
}

/* What a copy reached: every successful write, whichever path made it, and
   how many went through the select-and-execute path. The default
   execCommand models the browser's contract: "copy" copies the value of
   the last field that was selected. */
const clipboard = { writes: [], viaExecCommand: 0 };

global.document = {
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => new TextNode(text),
  body: new Element("body"),
  __lastSelected: null,
  execCommand: (command) => {
    if (command !== "copy") return false;
    const sel = document.__lastSelected;
    if (!sel || typeof sel.value !== "string") return false;
    clipboard.writes.push(sel.value);
    clipboard.viaExecCommand += 1;
    return true;
  },
};

/* The page's origin decides which clipboard API exists: a secure-context
   page has navigator.clipboard, a plain-http page (the phone path) has
   none. The test stands in for that by swapping the navigator before a
   click, because the renderer reads it at click time, not load time. */
function setNavigator(value) {
  Object.defineProperty(globalThis, "navigator", {
    value,
    configurable: true,
    writable: true,
  });
}

function workingNavigator() {
  return {
    clipboard: {
      writeText: (text) => {
        clipboard.writes.push(text);
        return Promise.resolve();
      },
    },
  };
}

setNavigator(workingNavigator());

function resetClipboard() {
  clipboard.writes.length = 0;
  clipboard.viaExecCommand = 0;
  document.__lastSelected = null;
  setNavigator(workingNavigator());
}

module.exports = {
  TextNode,
  Element,
  HTMLCollection,
  NodeList,
  clipboard,
  setNavigator,
  workingNavigator,
  resetClipboard,
};
