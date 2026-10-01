"use strict";
/* A DOM, but only the parts the browser scripts touch: small enough to
   read, with no browser and no network. It installs itself as the global
   `document` (and a working `navigator`) when required, so a script loaded
   after it sees the same names it sees in a page. */

class TextNode {
  constructor(text) { this.data = String(text); this.parent = null; }
  get textContent() { return this.data; }
  // Escaped, like a real serializer: the renderer's safety property is that
  // model output lands in text nodes, and a shim that does not escape would
  // report that as safe no matter what the renderer did.
  get html() {
    return this.data.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.dataset = {};
    this.parent = null;
    this._cls = "";
    this.style = {};
  }
  get children() { return this.childNodes.filter((n) => n instanceof Element); }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  set className(v) { this._cls = v || ""; }
  get className() { return this._cls; }
  get classList() {
    const self = this;
    return {
      contains: (c) => self._cls.split(/\s+/).includes(c),
      add: (c) => { if (!self.classList.contains(c)) self._cls = (self._cls + " " + c).trim(); },
      remove: (c) => { self._cls = self._cls.split(/\s+/).filter((x) => x !== c).join(" "); },
    };
  }
  setAttribute(name, value) { (this.attrs ||= {})[name] = value; }
  // The legacy copy path hands a selected field to execCommand("copy");
  // the shim remembers the last selection the way the selection range does.
  select() { global.document.__lastSelected = this; }
  remove() {
    if (this.parent) {
      const kids = this.parent.childNodes;
      kids.splice(kids.indexOf(this), 1);
      this.parent = null;
    }
  }
  appendChild(node) {
    // Real appendChild *moves* a node. The incremental painter relies on it:
    // `while (chunk.firstChild) stable.appendChild(chunk.firstChild)` never
    // terminates against a shim that copies.
    if (node.parent) {
      const kids = node.parent.childNodes;
      kids.splice(kids.indexOf(node), 1);
    }
    node.parent = this;
    this.childNodes.push(node);
    return node;
  }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) {
    for (const kid of this.childNodes) kid.parent = null;
    this.childNodes = [];
    if (v !== "" && v !== undefined && v !== null) this.childNodes.push(new TextNode(v));
  }
  get html() {
    const cls = this._cls ? ` class="${this._cls}"` : "";
    const tag = this.tagName.toLowerCase();
    return `<${tag}${cls}>` + this.childNodes.map((n) => n.html).join("") + `</${tag}>`;
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
    value, configurable: true, writable: true,
  });
}

function workingNavigator() {
  return {
    clipboard: {
      writeText: (text) => { clipboard.writes.push(text); return Promise.resolve(); },
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
  TextNode, Element, clipboard, setNavigator, workingNavigator, resetClipboard,
};
