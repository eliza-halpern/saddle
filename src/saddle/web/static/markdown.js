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
      const label = bit.slice(1, cut);
      const href = bit.slice(cut + 2, -1);
      if (/^https?:\/\//i.test(href)) {
        const link = el("a", null, label);
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.appendChild(link);
      } else {
        // Anything else -- a file path, a bare fragment, a javascript: URL --
        // is shown, not linked. The old code gave these href="#" with
        // target="_blank", which opened a second tab of the chat itself: the
        // reader clicked a link and got their own session again. A path the
        // model mentions is information, so keep it legible and inert.
        const shown = el("span", "ref", label);
        shown.title = href;
        parent.appendChild(shown);
        if (href && href !== label) {
          parent.appendChild(document.createTextNode(` (${href})`));
        }
      }
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
      const code = el("code", fence[1] ? `lang-${fence[1]}` : null);
      highlight(code, body.join("\n"), fence[1]);
      pre.appendChild(code);
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

/* ---------- syntax highlighting ----------

   Small on purpose. A real parser per language is not what a transcript
   needs, and pulling one in would mean a CDN, a build step, or a few
   hundred KB of someone else's regexes. What a reader needs is the shape of
   the code: where the strings and comments are, where a definition starts.

   Tokens are spans holding text nodes, never innerHTML, so the property the
   markdown renderer has -- model output cannot inject markup -- holds here
   too. An unknown language falls back to plain text, which is the right
   failure: uncoloured code is readable, mangled code is not.

   Order matters inside a grammar. Comments and strings come first, so a
   keyword inside a string stays a string. */

const COMMON_NUMBER = "\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b";
const DQ_STRING = '"(?:\\\\.|[^"\\\\\\n])*"';
const SQ_STRING = "'(?:\\\\.|[^'\\\\\\n])*'";

const GRAMMARS = {
  python: [
    ["c-comment", "#[^\\n]*"],
    ["c-string", "'''[\\s\\S]*?'''|\"\"\"[\\s\\S]*?\"\"\"|" + SQ_STRING + "|" + DQ_STRING],
    ["c-decorator", "@[\\w.]+"],
    ["c-keyword", "\\b(?:def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|raise|yield|lambda|pass|break|continue|and|or|not|in|is|None|True|False|async|await|global|nonlocal|assert|del|match|case)\\b"],
    ["c-builtin", "\\b(?:self|cls|print|len|range|str|int|float|bool|list|dict|set|tuple|open|enumerate|zip|sorted|any|all|isinstance|super|Exception|ValueError|TypeError|OSError|KeyError|RuntimeError)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  javascript: [
    ["c-comment", "//[^\\n]*|/\\*[\\s\\S]*?\\*/"],
    ["c-string", "`(?:\\\\.|[^`\\\\])*`|" + SQ_STRING + "|" + DQ_STRING],
    ["c-keyword", "\\b(?:const|let|var|function|return|if|else|for|while|do|of|in|new|class|extends|import|export|default|from|async|await|try|catch|finally|throw|typeof|instanceof|delete|void|yield|switch|case|break|continue|null|undefined|true|false|this|super)\\b"],
    ["c-builtin", "\\b(?:console|document|window|Math|JSON|Object|Array|String|Number|Boolean|Promise|Set|Map|Error)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  typescript: null,
  json: [
    ["c-property", '"(?:\\\\.|[^"\\\\])*"(?=\\s*:)'],
    ["c-string", '"(?:\\\\.|[^"\\\\])*"'],
    ["c-keyword", "\\b(?:true|false|null)\\b"],
    ["c-number", "-?\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b"],
  ],
  bash: [
    ["c-comment", "#[^\\n]*"],
    ["c-string", "'[^'\\n]*'|" + DQ_STRING],
    ["c-keyword", "\\b(?:if|then|else|elif|fi|for|in|do|done|while|until|case|esac|function|return|export|local|readonly|set|shift|source|trap|exit)\\b"],
    ["c-builtin", "\\b(?:echo|cd|ls|cat|grep|sed|awk|find|curl|git|python|pip|make|sudo|chmod|mkdir|rm|cp|mv)\\b"],
    ["c-variable", "\\$\\{?[A-Za-z_][\\w]*\\}?|\\$[0-9@*?#]"],
    ["c-number", "\\b\\d+\\b"],
  ],
  xml: [
    ["c-comment", "<!--[\\s\\S]*?-->"],
    ["c-string", DQ_STRING + "|" + SQ_STRING],
    ["c-tag", "</?[A-Za-z_][\\w:.-]*|/?>"],
    ["c-attr", "\\b[A-Za-z_][\\w:.-]*(?=\\s*=)"],
    ["c-number", COMMON_NUMBER],
  ],
  css: [
    ["c-comment", "/\\*[\\s\\S]*?\\*/"],
    ["c-string", DQ_STRING + "|" + SQ_STRING],
    ["c-property", "[-a-zA-Z]+(?=\\s*:)"],
    ["c-builtin", "@[a-z-]+|:[a-z-]+"],
    ["c-number", "#[0-9a-fA-F]{3,8}\\b|\\b\\d*\\.?\\d+(?:px|em|rem|%|s|ms|vh|vw|fr|deg)?\\b"],
  ],
  yaml: [
    ["c-comment", "#[^\\n]*"],
    ["c-string", DQ_STRING + "|" + SQ_STRING],
    ["c-property", "^\\s*-?\\s*[\\w.-]+(?=\\s*:)"],
    ["c-keyword", "\\b(?:true|false|null|yes|no|on|off)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  toml: [
    ["c-comment", "#[^\\n]*"],
    ["c-string", '"""[\\s\\S]*?"""|' + DQ_STRING + "|" + SQ_STRING],
    ["c-decorator", "^\\s*\\[[^\\]\\n]+\\]"],
    ["c-property", "^\\s*[\\w.-]+(?=\\s*=)"],
    ["c-keyword", "\\b(?:true|false)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  sql: [
    ["c-comment", "--[^\\n]*|/\\*[\\s\\S]*?\\*/"],
    ["c-string", SQ_STRING],
    ["c-keyword", "\\b(?:SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|INDEX|DROP|ALTER|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP|BY|ORDER|HAVING|LIMIT|OFFSET|AS|AND|OR|NOT|NULL|PRIMARY|KEY|FOREIGN|REFERENCES|DISTINCT|UNION|CASE|WHEN|THEN|ELSE|END)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  rust: [
    ["c-comment", "//[^\\n]*|/\\*[\\s\\S]*?\\*/"],
    ["c-string", 'r?#*"(?:\\\\.|[^"\\\\])*"#*'],
    ["c-decorator", "#!?\\[[^\\]\\n]*\\]"],
    ["c-keyword", "\\b(?:fn|let|mut|const|static|struct|enum|impl|trait|for|in|if|else|match|while|loop|break|continue|return|use|mod|pub|crate|self|super|where|as|dyn|move|async|await|ref|unsafe|type)\\b"],
    ["c-builtin", "\\b(?:Option|Result|Some|None|Ok|Err|Vec|String|Box|Rc|Arc|HashMap|bool|u8|u16|u32|u64|usize|i8|i16|i32|i64|isize|f32|f64|str)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  go: [
    ["c-comment", "//[^\\n]*|/\\*[\\s\\S]*?\\*/"],
    ["c-string", "`[^`]*`|" + DQ_STRING],
    ["c-keyword", "\\b(?:func|var|const|type|struct|interface|map|chan|package|import|return|if|else|for|range|switch|case|default|break|continue|go|defer|select|fallthrough|nil|true|false)\\b"],
    ["c-builtin", "\\b(?:string|int|int8|int16|int32|int64|uint|uint8|byte|rune|float32|float64|bool|error|make|new|len|cap|append|copy|delete|panic|recover)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  c: [
    ["c-comment", "//[^\\n]*|/\\*[\\s\\S]*?\\*/"],
    ["c-string", DQ_STRING + "|" + SQ_STRING],
    ["c-decorator", "^\\s*#\\s*\\w+"],
    ["c-keyword", "\\b(?:auto|break|case|char|const|continue|default|do|double|else|enum|extern|float|for|goto|if|inline|int|long|register|return|short|signed|sizeof|static|struct|switch|typedef|union|unsigned|void|volatile|while|class|public|private|protected|virtual|template|typename|namespace|using|new|delete|this|nullptr|true|false)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  java: [
    ["c-comment", "//[^\\n]*|/\\*[\\s\\S]*?\\*/"],
    ["c-string", DQ_STRING + "|" + SQ_STRING],
    ["c-decorator", "@[A-Za-z_]\\w*"],
    ["c-keyword", "\\b(?:public|private|protected|static|final|abstract|class|interface|extends|implements|new|return|if|else|for|while|do|switch|case|default|break|continue|try|catch|finally|throw|throws|import|package|void|this|super|null|true|false|instanceof|synchronized|volatile|transient|enum|record)\\b"],
    ["c-builtin", "\\b(?:String|Integer|Long|Double|Boolean|Object|List|Map|Set|Optional|Exception|System|int|long|double|float|char|byte|short|boolean)\\b"],
    ["c-number", COMMON_NUMBER],
  ],
  diff: [
    ["c-comment", "^@@[^\\n]*"],
    ["c-string", "^\\+[^\\n]*"],
    ["c-keyword", "^-[^\\n]*"],
  ],
};

/* svg is xml; the rest are the names people actually type in a fence. */
const LANGUAGE_ALIASES = {
  py: "python", python3: "python",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "javascript", tsx: "javascript", typescript: "javascript", node: "javascript",
  sh: "bash", shell: "bash", zsh: "bash", console: "bash", terminal: "bash",
  svg: "xml", html: "xml", xhtml: "xml", vue: "xml", markup: "xml",
  scss: "css", sass: "css", less: "css",
  yml: "yaml",
  postgres: "sql", postgresql: "sql", psql: "sql", mysql: "sql", sqlite: "sql",
  rs: "rust", golang: "go",
  "c++": "c", cpp: "c", cc: "c", h: "c", hpp: "c", objc: "c", cs: "c", csharp: "c",
  patch: "diff",
  json5: "json", jsonc: "json",
};

function grammarFor(language) {
  const name = String(language || "").toLowerCase();
  return GRAMMARS[LANGUAGE_ALIASES[name] || name] || null;
}

function highlight(target, code, language) {
  const rules = grammarFor(language);
  if (!rules) {
    target.textContent = code;        // unknown language: readable, uncoloured
    return;
  }
  target.textContent = "";
  const pattern = new RegExp(
    rules.map(([, source]) => `(${source})`).join("|"), "gm");
  let last = 0;
  for (const match of code.matchAll(pattern)) {
    if (match.index > last) {
      target.appendChild(document.createTextNode(code.slice(last, match.index)));
    }
    const which = match.slice(1).findIndex((group) => group !== undefined);
    target.appendChild(el("span", rules[which][0], match[0]));
    last = match.index + match[0].length;
  }
  if (last < code.length) {
    target.appendChild(document.createTextNode(code.slice(last)));
  }
}

/* ---------- tool output ---------- */

function isDiff(text) {
  return typeof text === "string" && text.startsWith("--- a/");
}

/* A diff is the point of a write: "wrote 'x.py'" says nothing about what
   changed. Colour the sides so the eye finds the change without reading. */
function renderDiff(target, text) {
  target.textContent = "";
  for (const line of text.split("\n")) {
    const cls =
      line.startsWith("+++") || line.startsWith("---") ? "d-file"
      : line.startsWith("@@") ? "d-hunk"
      : line.startsWith("+") ? "d-add"
      : line.startsWith("-") ? "d-del"
      : "d-ctx";
    target.appendChild(el("div", cls, line || " "));
  }
}

/* A newly written file: the header line, then the file itself.
 *
 * `created 'x.py' (120 bytes)` says nothing about what was written, and a
 * new file is exactly the case where there is no diff to read instead. The
 * tool returns the content after the header, so this is what both a live
 * row and a reloaded one have to work from -- the same text, so they cannot
 * disagree the way the diff renderer once did. */

const CREATED = /^created '([^']*)' \((\d+) bytes\)\n([\s\S]*)$/;

const EXTENSIONS = {
  py: "python", pyi: "python",
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "javascript", tsx: "javascript",
  json: "json", jsonl: "json",
  sh: "bash", bash: "bash", zsh: "bash",
  svg: "xml", html: "xml", htm: "xml", xml: "xml", vue: "xml",
  css: "css", scss: "css", sass: "css", less: "css",
  yaml: "yaml", yml: "yaml",
  toml: "toml", sql: "sql", rs: "rust", go: "go",
  c: "c", h: "c", cpp: "c", hpp: "c", cc: "c", cs: "c",
  java: "java", patch: "diff", diff: "diff",
};

function languageForPath(name) {
  const dot = String(name || "").lastIndexOf(".");
  if (dot < 0) return null;
  return EXTENSIONS[name.slice(dot + 1).toLowerCase()] || null;
}

function renderCreated(target, match) {
  target.textContent = "";
  const [, name, bytes, body] = match;
  target.appendChild(el("div", "c-head", `created ${name} (${bytes} bytes)`));
  const code = el("code", "created-body");
  highlight(code, body, languageForPath(name));
  target.appendChild(code);
}

/* One filling for a tool row's body, used by the live stream and by a
   reloaded transcript alike. They had separate code and disagreed: the
   live one coloured a diff, the stored one printed it flat. */
function fillToolDetail(row, detail, text) {
  const created = typeof text === "string" ? text.match(CREATED) : null;
  if (isDiff(text)) {
    renderDiff(detail, text);
    row.classList.add("has-diff");
  } else if (created) {
    renderCreated(detail, created);
    row.classList.add("has-diff");
  } else {
    detail.textContent = text || "(no output)";
  }
}

/* Following the bottom is an intent, not a measurement.
 *
 * It used to be re-derived every frame as "within 120px of the bottom", so
 * scrolling up during a long reasoning stream put you back within that band
 * and the next frame yanked you down again -- you could not read what had
 * already streamed while more was arriving. Now scrolling away turns
 * following off and it stays off until you come back to the bottom
 * yourself.
 *
 * Our own scrolls have to be told apart from the reader's, or setting
 * scrollTop would immediately look like a scroll away. `lastSet` is what we
 * last wrote; a scroll event at that position is ours. */

let following = true;
let lastSet = -1;

function atBottom() {
  const t = $("#transcript");
  return t.scrollHeight - t.scrollTop - t.clientHeight < 24;
}

function watchScrolling() {
  const t = $("#transcript");
  t.addEventListener("scroll", () => {
    if (Math.abs(t.scrollTop - lastSet) < 2) return;   // our own
    following = atBottom();
    const jump = $("#jump");
    if (jump) jump.hidden = following;
  }, { passive: true });
}

function stickToBottom(_was) {
  if (!following) return;
  const t = $("#transcript");
  t.scrollTop = t.scrollHeight;
  lastSet = t.scrollTop;
}

function followBottom() {
  following = true;
  const jump = $("#jump");
  if (jump) jump.hidden = true;
  stickToBottom(true);
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
  module.exports = {
    el, inlineInto, renderMarkdown, splitStable, paintStream,
    isDiff, renderDiff, fillToolDetail, highlight, grammarFor,
    languageForPath, CREATED,
  };
}
