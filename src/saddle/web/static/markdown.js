"use strict";
/* The markdown renderer and the streaming painter, kept apart from the app
   so they can be exercised without a browser -- tests/test_markdown.mjs
   loads this file against a small DOM shim.

   Nothing here touches the network, the session or the event stream: given
   text it produces nodes, and that is the whole contract. */

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* ---------- content rendering ---------- */

/* A small markdown renderer. Everything is inserted as text nodes and
   built elements -- never innerHTML -- so model output cannot inject
   markup. It covers what a coding assistant actually emits: fenced code,
   headings, bullet and numbered lists, blockquotes, bold, italic, inline
   code and links. Anything it does not know stays as literal text, which
   is the safe failure: an unrendered asterisk is ugly, a swallowed line is
   a lie about what was said. */

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]]+\]\([^)\s]+\))/g;

function inlineInto(parent, text) {
  for (const bit of text.split(INLINE)) {
    if (!bit) continue;
    if (bit.startsWith("`") && bit.endsWith("`") && bit.length > 2) {
      parent.appendChild(el("code", null, bit.slice(1, -1)));
    } else if ((bit.startsWith("**") && bit.endsWith("**") && bit.length > 4) ||
               (bit.startsWith("__") && bit.endsWith("__") && bit.length > 4)) {
      parent.appendChild(el("strong", null, bit.slice(2, -2)));
    } else if ((bit.startsWith("*") && bit.endsWith("*") && bit.length > 2) ||
               (bit.startsWith("_") && bit.endsWith("_") && bit.length > 2)) {
      parent.appendChild(el("em", null, bit.slice(1, -1)));
    } else if (bit.startsWith("[") && bit.includes("](")) {
      const cut = bit.indexOf("](");
      const link = el("a", null, bit.slice(1, cut));
      const href = bit.slice(cut + 2, -1);
      link.href = /^https?:|^\//.test(href) ? href : "#";  // no javascript: hrefs
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.appendChild(link);
    } else {
      parent.appendChild(document.createTextNode(bit));
    }
  }
}

function renderMarkdown(target, text) {
  target.textContent = "";
  const lines = text.split("\n");
  let index = 0;
  let list = null;

  const closeList = () => { list = null; };

  while (index < lines.length) {
    const line = lines[index];

    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {                                   // fenced code
      closeList();
      const body = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) body.push(lines[index++]);
      index += 1;
      const pre = el("pre");
      pre.appendChild(el("code", fence[1] ? `lang-${fence[1]}` : null, body.join("\n")));
      target.appendChild(pre);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeList();
      const node = el(`h${Math.min(heading[1].length + 2, 6)}`);
      inlineInto(node, heading[2]);
      target.appendChild(node);
      index += 1;
      continue;
    }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const wanted = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== wanted) {
        list = el(wanted);
        target.appendChild(list);
      }
      const item = el("li");
      inlineInto(item, (bullet || numbered)[1]);
      list.appendChild(item);
      index += 1;
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      closeList();
      const node = el("blockquote");
      inlineInto(node, quote[1]);
      target.appendChild(node);
      index += 1;
      continue;
    }

    if (!line.trim()) { closeList(); index += 1; continue; }

    closeList();                                    // paragraph: gather until blank
    const para = [];
    while (index < lines.length && lines[index].trim() &&
           !/^\s*```|^#{1,4}\s|^\s*[-*+]\s|^\s*\d+[.)]\s|^\s*>/.test(lines[index])) {
      para.push(lines[index++]);
    }
    const node = el("p");
    inlineInto(node, para.join(" "));
    target.appendChild(node);
  }
}

function atBottom() {
  const t = $("#transcript");
  return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
}
function stickToBottom(was) {
  if (was) $("#transcript").scrollTop = $("#transcript").scrollHeight;
}

/* ---------- streaming paint ----------

   Two costs make naive streaming feel bad, and both grow with the answer:
   re-parsing the whole message per token is O(n^2), and rebuilding its DOM
   throws away any selection the reader made in the part that already
   finished. So: coalesce to one paint per animation frame, and re-parse
   only the unsettled tail. Everything before the last blank line cannot
   change any more, so it is parsed once and then left alone. */

function splitStable(raw) {
  // A blank line inside an open code fence is not a paragraph break, so the
  // search for the last settled point has to start before the fence opened.
  // Without this a streaming code block -- the common case here -- settles
  // nothing and the whole message is re-parsed per token.
  const fences = [];
  for (let i = raw.indexOf("```"); i >= 0; i = raw.indexOf("```", i + 3)) fences.push(i);
  // Start the search before an unclosed fence's opener. This is purely a
  // shortcut -- the even-count test below rejects everything it skips, and a
  // 300k-input differential fuzz found no disagreement -- so no test can kill
  // it. It stays because it is worth 20x on a long code block streaming in
  // (600 blank lines: 0.2ms vs 3.9ms), which is the common case here.
  const open = fences.length % 2 ? fences[fences.length - 1] : raw.length;
  let cut = raw.lastIndexOf("\n\n", open - 1);
  while (cut >= 0) {
    // ...and a break inside a *closed* fence is not a paragraph break either,
    // so keep walking back until the break has an even number of fences
    // before it. Missing this split a code block in half mid-stream.
    let before = 0;
    for (const f of fences) { if (f >= cut) break; before++; }
    if (before % 2 === 0) return [raw.slice(0, cut + 1), raw.slice(cut + 1)];
    cut = raw.lastIndexOf("\n\n", cut - 1);
  }
  return ["", raw];
}

function paintStream(node) {
  let stable = node.firstElementChild;
  let live = node.lastElementChild;
  if (!stable || !stable.classList.contains("md-stable")) {
    node.textContent = "";
    stable = el("div", "md-stable");
    live = el("div", "md-live");
    node.append(stable, live);
  }
  const [head, tail] = splitStable(node.dataset.raw);
  const done = stable.dataset.raw || "";
  if (head !== done) {
    if (head.startsWith(done)) {
      // Settled paragraphs never change again, so parse only the ones that
      // just closed and append them. The finished part of a long answer is
      // then never rebuilt at all, which is what keeps this linear.
      const chunk = el("div");
      renderMarkdown(chunk, head.slice(done.length));
      while (chunk.firstChild) stable.appendChild(chunk.firstChild);
    } else {
      renderMarkdown(stable, head);   // a fence opened: the split point moved
    }
    stable.dataset.raw = head;
  }
  renderMarkdown(live, tail);
}

/* Exported only when loaded by the test runner; in the browser these are
   plain globals that app.js picks up from the shared script scope. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { el, inlineInto, renderMarkdown, splitStable, paintStream };
}
