"""The browser page's HTML and CSS: validators, and two cross-file contracts.

Contracts, each shown both ways (a known-good tree passes, a known-bad copy
fails and names the culprit):

1. `html-validate` (config `.htmlvalidate.json`) and `stylelint` (config
   `stylelint.config.mjs`) exit clean on the real `index.html` and `app.css`
   and exit non-zero on a copy with one real defect put back.
2. Every element id the scripts look up exists in `index.html`.
3. Every custom property read with `var(--x)` is defined for both themes.

Why the cross-file checks exist: neither validator can see that
`$("#send")` in a script names an element the HTML no longer has, or that a
renamed palette variable left a `var()` reading nothing. Both fail silently in
a browser (a null lookup, an unset colour), so a person finds them, not a gate.

How the id check reads the scripts. A lookup is a call of `$`,
`getElementById`, `querySelector`, `querySelectorAll`, `closest` or `matches`.
The rule per call site, none of them silently dropped:

* a string literal: every `#id` token in it is looked up (for
  `getElementById` the whole literal is the id); `#id` inside an attribute
  selector `[...]` is a value, not an id;
* a template literal: the same, over its static text; a `#name` that runs
  straight into `${` is a runtime-built id and must be a named exception;
* any other argument (a variable) is a dynamic lookup and must be a named
  exception in `KNOWN_DYNAMIC_LOOKUPS`, which lists the ids it can take and
  is itself checked against the script text;
* an id the script creates (`x.id = "lit"`, `setAttribute("id", "lit")`) is
  not required in the HTML; a non-literal assignment must be a named
  exception.

Every call site is counted against a plain substring count of the callee
spellings, so a collector that misses sites cannot pass.

How the theme check decides. `app.css` defines the dark palette in a bare
`:root` block and the light palette as overrides in `:root[data-theme="light"]`.
A custom property is defined for a theme when the bare block defines it (the
light block inherits it) or the theme's own block does. A name used anywhere
must satisfy both themes if any root-level block mentions it. A name no
root-level block defines, but some other rule or a script sets (`.prow`'s
`--s`), is component-scoped: it is accepted, and listed by
`test_scoped_custom_properties_are_named`. Everything else is undefined.

Rules switched off, with reasons (the reasons for the stylelint ones are
beside each disable in `stylelint.config.mjs` and `app.css`; html-validate's
are inline `html-validate-disable-next` comments in `index.html`, so none is
global):

* html-validate `multiple-labeled-controls` and `no-redundant-for` on the two
  temperature labels: an `<output>` is labelable and comes first, so without
  `for` the label names the output and the slider has no name. The fix is the
  `for` attribute; `test_a_label_around_an_output_names_its_control` pins it.
* `prefer-native-element` on the lane popup: its options carry a description.
* `autocomplete-password` on the command-password field: the browser must not
  offer to save or fill a one-time password.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "saddle" / "web" / "static"
BIN = ROOT / "node_modules" / ".bin"

LOOKUP_CALLEES = ("$", "getElementById", "querySelectorAll", "querySelector", "closest", "matches")
_CALL = re.compile(r"(?:\$|getElementById|querySelectorAll|querySelector|closest|matches)\(")
_ID_ASSIGN = re.compile(r"\.id\s*=(?!=)\s*")
_SET_ID = re.compile(r"setAttribute\(\s*[\"']id[\"']\s*,\s*")

# Call sites whose argument is not a literal. Key: (file, call text up to its
# closing paren). Value: the ids the site can look up (checked against the
# HTML, and each must appear in the file as a quoted selector so the entry
# cannot rot). An empty tuple is a site that looks up no id.
KNOWN_DYNAMIC_LOOKUPS: dict[tuple[str, str], tuple[str, ...]] = {
    # The helper itself: `const $ = (sel) => document.querySelector(sel);`
    ("app.js", "querySelector(sel)"): (),
    # `for (const id of ["#persona", "#default-persona", "#persona-pick"])`
    ("app.js", "$(id)"): ("persona", "default-persona", "persona-pick"),
}
# Assignments of a runtime-built id: same shape, none exist today.
KNOWN_DYNAMIC_ID_ASSIGNMENTS: dict[tuple[str, str], str] = {}


@dataclass
class Lookups:
    """What one script's id lookups come to."""

    sites: int = 0
    ids: list[tuple[str, int]] = field(default_factory=list)  # (id, line)
    created: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)  # "file:line text"
    dynamic_used: set[tuple[str, str]] = field(default_factory=set)


def _read_literal(text: str, start: int) -> tuple[str, bool, int]:
    """The quoted literal beginning at `text[start]`: (body, has ${, end)."""
    quote = text[start]
    i = start + 1
    while i < len(text) and text[i] != quote:
        i += 2 if text[i] == "\\" else 1
    body = text[start + 1 : i]
    return body, quote == "`" and "${" in body, i + 1


def _selector_ids(body: str) -> tuple[list[str], bool]:
    """(ids named by a selector string, True if one is built at runtime)."""
    runtime = bool(re.search(r"#[\w-]*\$\{", body))
    static = re.sub(r"\$\{[^}]*\}", "", re.sub(r"\[[^\]]*\]", "", body))
    return re.findall(r"#([A-Za-z_][\w-]*)", static), runtime


def collect_lookups(name: str, text: str) -> Lookups:
    """Classify every id lookup and id assignment in one script."""
    out = Lookups()
    for match in _CALL.finditer(text):
        out.sites += 1
        line = text.count("\n", 0, match.start()) + 1
        callee = match.group(0)[:-1]
        arg_at = match.end()
        while text[arg_at].isspace():
            arg_at += 1
        if text[arg_at] in "\"'`":
            body, templated, _ = _read_literal(text, arg_at)
            if callee == "getElementById":
                if templated:
                    out.unresolved.append(f"{name}:{line} getElementById(`{body}`)")
                else:
                    out.ids.append((body, line))
                continue
            ids, runtime = _selector_ids(body)
            if runtime:
                out.unresolved.append(f"{name}:{line} {callee}({text[arg_at : arg_at + 40]}")
            out.ids.extend((i, line) for i in ids)
            continue
        close = text.index(")", arg_at)
        site = f"{text[match.start() : close + 1]}"
        if (name, site) in KNOWN_DYNAMIC_LOOKUPS:
            out.dynamic_used.add((name, site))
        else:
            out.unresolved.append(f"{name}:{line} {site}")
    for pattern in (_ID_ASSIGN, _SET_ID):
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            if text[match.end()] in "\"'":
                body, templated, _ = _read_literal(text, match.end())
                if not templated:
                    out.created.add(body)
                    continue
            expr = text[match.end() : text.index("\n", match.end())].strip()
            if (name, expr) not in KNOWN_DYNAMIC_ID_ASSIGNMENTS:
                out.unresolved.append(f"{name}:{line} id assigned from {expr}")
    return out


def html_ids(html: str) -> set[str]:
    """Every id attribute in the page. Counted against a plain `id=` count."""
    found = re.findall(r"""\sid\s*=\s*["']([^"']+)["']""", html)
    assert len(found) == len(re.findall(r"\sid\s*=", html)), "an id attribute was not read"
    return set(found)


def missing_ids(html: str, scripts: dict[str, str]) -> list[str]:
    """Every problem with the scripts' id lookups against the page, as text."""
    present = html_ids(html)
    problems: list[str] = []
    created: set[str] = set()
    per_file = {name: collect_lookups(name, text) for name, text in scripts.items()}
    for found in per_file.values():
        created |= found.created
    for name, found in per_file.items():
        problems += [f"unclassified lookup {u}" for u in found.unresolved]
        for ident, line in found.ids:
            if ident not in present and ident not in created:
                problems.append(f"{name}:{line} looks up #{ident}, which index.html lacks")
    for (name, site), ids in KNOWN_DYNAMIC_LOOKUPS.items():
        if name not in scripts:
            continue
        for ident in ids:
            if f"#{ident}" not in scripts[name]:
                problems.append(f"{name}: exception {site} names #{ident}, which it never quotes")
            if ident not in present and ident not in created:
                problems.append(f"{name}: {site} can look up #{ident}, which index.html lacks")
    return problems


def _scripts() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(STATIC.glob("*.js"))}


def test_every_script_id_lookup_names_an_element_in_the_page() -> None:
    html = (STATIC / "index.html").read_text()
    assert missing_ids(html, _scripts()) == []


def test_the_collector_sees_every_call_site_in_every_script() -> None:
    """The denominator: sites found equal a substring count of the callees."""
    scripts = _scripts()
    assert scripts, "no scripts found under the static directory"
    for name, text in scripts.items():
        plain = sum(text.count(f"{callee}(") for callee in LOOKUP_CALLEES)
        found = collect_lookups(name, text)
        assert found.sites == plain, f"{name}: collector saw {found.sites} sites, grep {plain}"
        literal_lookups = len(
            re.findall(r"""(?:\$|getElementById|querySelector\w*)\(\s*["'`]""", text)
        )
        if literal_lookups:
            assert found.ids or found.created, f"{name}: has literal lookups, collected no id"
    assert sum(len(collect_lookups(n, t).ids) for n, t in scripts.items()) > 100


def test_every_named_dynamic_exception_is_still_used() -> None:
    """An exception for a site that is gone would hide a future real one."""
    scripts = _scripts()
    used = set().union(*(collect_lookups(n, t).dynamic_used for n, t in scripts.items()))
    assert used == set(KNOWN_DYNAMIC_LOOKUPS)


@pytest.mark.parametrize(
    "removed",
    [
        "jump",  # a plain `$("#jump")`
        "run-live",  # only through getElementById
        "lane-menu",  # only inside compound selectors such as `#lane-menu li`
        "persona-pick",  # only through the dynamic list in loadPersonas
    ],
)
def test_a_page_missing_a_looked_up_id_is_named(removed: str) -> None:
    html = (STATIC / "index.html").read_text()
    mutated = html.replace(f'id="{removed}"', 'data-gone=""')
    assert mutated != html, f"id {removed} is not in the page: the mutation did not apply"
    problems = missing_ids(mutated, _scripts())
    assert problems, f"removing #{removed} was not noticed"
    assert all(f"#{removed}" in p for p in problems), problems


def test_dynamic_and_created_ids_are_classified_not_dropped() -> None:
    html = '<div id="here"></div>'
    # known-good: present ids, an attribute value that is not an id, a created id
    good = {"demo.js": '$("#here"); $(\'a[href="#nope"]\'); n.id = "made"; $("#made");'}
    assert missing_ids(html, good) == []
    # known-bad: a variable argument, a runtime-built id, a runtime id assignment
    bad = {
        "x.js": "$(which);\n$(`#row-${n}`);\nnode.id = prefix + n;\n"
        "document.getElementById(`a${n}`);"
    }
    problems = missing_ids(html, bad)
    assert len([p for p in problems if p.startswith("unclassified")]) == 4, problems
    # a template whose static text is a class, with a runtime attribute value, is fine
    assert (
        collect_lookups("y.js", 'document.querySelector(`.row[data-sid="${sid}"]`);').unresolved
        == []
    )


def test_a_label_around_an_output_names_its_control() -> None:
    """An `<output>` inside a label is that label's control, so `for` is needed."""

    def unnamed(html: str) -> list[str]:
        return re.findall(r"<label(?![^>]*\bfor=)[^>]*>[^<]*<output[^>]*id=\"([^\"]+)\"", html)

    html = (STATIC / "index.html").read_text()
    assert re.search(r"<label[^>]*>[^<]*<output", html), "no label around an output: stale test"
    assert unnamed(html) == []
    broken = html.replace('<label for="temp">', "<label>")
    assert broken != html
    assert unnamed(broken) == ["temp-value"]


# --- the validators ----------------------------------------------------------


def is_mutant_copy(root: Path) -> bool:
    """mutmut runs the suite from `mutants/`, which holds src and tests only."""
    return root.name == "mutants"


def test_the_checkout_is_not_taken_for_a_mutant_copy() -> None:
    assert not is_mutant_copy(Path("/work/saddle"))
    assert is_mutant_copy(Path("/work/saddle/mutants"))


def _run(tool: str, *args: str) -> subprocess.CompletedProcess[str]:
    if is_mutant_copy(ROOT):
        pytest.skip("the mutant work copy has no node_modules or linter configs")
    exe = BIN / tool
    assert exe.exists(), f"{exe} is missing: run `npm ci --ignore-scripts` (check.sh does)"
    return subprocess.run(
        [str(exe), *args], capture_output=True, text=True, cwd=ROOT, timeout=120, check=False
    )


def _html_validate(path: Path) -> subprocess.CompletedProcess[str]:
    return _run("html-validate", "-c", str(ROOT / ".htmlvalidate.json"), str(path))


def _stylelint(path: Path) -> subprocess.CompletedProcess[str]:
    return _run("stylelint", "--config", str(ROOT / "stylelint.config.mjs"), str(path))


def test_the_page_passes_html_validate() -> None:
    result = _html_validate(STATIC / "index.html")
    assert (result.returncode, result.stdout) == (0, ""), result.stdout + result.stderr


@pytest.mark.parametrize(
    ("old", "new", "rule"),
    [
        ('<button type="button" id="settings"', '<button id="settings"', "no-implicit-button-type"),
        ('<input type="text" id="title"', '<input id="title"', "no-implicit-input-type"),
        ("<!DOCTYPE html>", "<!doctype html>", "doctype-style"),
        ('<div class="folder-actions">', "<menu>", "element-permitted-content"),
        # the inline exceptions are exceptions, not a global switch-off
        ("[html-validate-disable-next autocomplete-password", "[x", "autocomplete-password"),
        ("[html-validate-disable-next prefer-native-element", "[x", "prefer-native-element"),
    ],
)
def test_html_validate_rejects_the_page_with_one_defect_put_back(
    tmp_path: Path, old: str, new: str, rule: str
) -> None:
    html = (STATIC / "index.html").read_text()
    assert old in html, f"{old!r} is not in the page: the mutation did not apply"
    broken = tmp_path / "index.html"
    broken.write_text(html.replace(old, new, 1))
    result = _html_validate(broken)
    assert result.returncode == 1, result.stdout + result.stderr
    assert rule in result.stdout, result.stdout


def test_the_stylesheet_passes_stylelint() -> None:
    result = _stylelint(STATIC / "app.css")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", ""), result.stderr


@pytest.mark.parametrize(
    ("old", "new", "rule"),
    [
        ("rgb(63 185 80 / 16%)", "rgba(63, 185, 80, .16)", "alpha-value-notation"),
        ("--bg-raise: #ffffff;", "--bg-raise: #fff;", "color-hex-length"),
        (".sr-only {", ".sr-only { colour: red;", "property-no-unknown"),
        ("rs-needs_you", "rsNeedsYou", "selector-class-pattern"),
        # the inline exceptions are exceptions, not a global switch-off
        ("stylelint-disable-next-line property-no-deprecated", "x", "property-no-deprecated"),
    ],
)
def test_stylelint_rejects_the_stylesheet_with_one_defect_put_back(
    tmp_path: Path, old: str, new: str, rule: str
) -> None:
    css = (STATIC / "app.css").read_text()
    assert old in css, f"{old!r} is not in the stylesheet: the mutation did not apply"
    broken = tmp_path / "app.css"
    broken.write_text(css.replace(old, new, 1))
    result = _stylelint(broken)
    assert result.returncode == 2, result.stdout + result.stderr
    assert rule in result.stderr + result.stdout, result.stderr


# --- custom properties and the two themes --------------------------------------

# The names `app.css` sets on one element rather than on the root, each read by
# a rule that sits inside that element (a status row, a meter, a card).
EXPECTED_SCOPED = {"--s", "--tone", "--track", "--wash", "--t", "--b"}

_DARK = ":root"
_LIGHT = ':root[data-theme="light"]'


def parse_blocks(css: str) -> list[tuple[str, list[str]]]:
    """(selector, declarations) for every rule block, at-rule nesting flattened."""
    unmodelled = "themes by media query are not modelled here: teach this parser"
    assert "prefers-color-scheme" not in css, unmodelled
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    blocks: list[tuple[str, list[str]]] = []
    stack: list[tuple[str, list[str]]] = []
    buf = ""
    depth = 0
    for ch in css:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and ch in "{};":
            if ch == "{":
                stack.append((" ".join(buf.split()), []))
            else:
                if buf.strip() and stack:
                    stack[-1][1].append(buf.strip())
                if ch == "}":
                    blocks.append(stack.pop())
            buf = ""
        else:
            buf += ch
    assert not stack, "unbalanced braces in the stylesheet"
    assert depth == 0, "unbalanced parentheses in the stylesheet"
    return blocks


def custom_property_definitions(css: str) -> dict[str, set[str]]:
    """Where each `--name` is declared: a set of 'dark', 'light', 'scoped'."""
    where: dict[str, set[str]] = {}
    for selector, decls in parse_blocks(css):
        selectors = {s.strip() for s in selector.split(",")}
        scope = "dark" if selectors == {_DARK} else "light" if selectors == {_LIGHT} else "scoped"
        for decl in decls:
            found = re.match(r"(--[\w-]+)\s*:", decl)
            if found:
                unmodelled = f"{selector}: a theme-scoped custom property is not modelled"
                assert scope != "scoped" or "data-theme" not in selector, unmodelled
                where.setdefault(found.group(1), set()).add(scope)
    return where


def js_set_properties(scripts: dict[str, str]) -> set[str]:
    """Custom properties a script sets on an element (always component-scoped)."""
    names: set[str] = set()
    for name, text in scripts.items():
        calls = len(re.findall(r"setProperty\(", text))
        literal = re.findall(r"""setProperty\(\s*["'`](--[\w-]+)["'`]""", text)
        assert calls >= len(literal), name
        dashed = len(re.findall(r"""setProperty\(\s*["'`]--""", text))
        assert dashed == len(literal), f"{name}: a custom property name is not a plain literal"
        names.update(literal)
    return names


def theme_problems(css: str, other_sources: dict[str, str], scripted: set[str]) -> list[str]:
    """Every `var(--x)` that is undefined, or defined for the light theme only.

    The light block overrides the bare block on the same element, so a name the
    bare block defines is defined for both themes; a name only the light block
    defines is missing in dark, whatever else also sets it on some element.
    """
    defs = custom_property_definitions(css)
    problems: list[str] = []
    for source, text in {"app.css": css, **other_sources}.items():
        names = re.findall(r"var\(\s*(--[\w-]+)", text)
        assert text.count("var(") == len(names), f"{source}: a var() is not var(--name)"
        for name in sorted(set(names)):
            scopes = defs.get(name, set())
            if not scopes and name not in scripted:
                problems.append(f"{source}: var({name}) is defined nowhere")
            elif scopes & {"dark", "light"} and "dark" not in scopes:
                problems.append(f"{source}: {name} is defined for the light theme only")
    return problems


def _other_sources() -> dict[str, str]:
    sources = {"index.html": (STATIC / "index.html").read_text()}
    sources.update(_scripts())
    return sources


def test_every_custom_property_is_defined_for_both_themes() -> None:
    css = (STATIC / "app.css").read_text()
    assert theme_problems(css, _other_sources(), js_set_properties(_scripts())) == []


def _with(css: str, old: str, new: str) -> str:
    assert old in css, f"{old!r} is not in the stylesheet: the mutation did not apply"
    return css.replace(old, new, 1)


def test_scoped_custom_properties_are_named() -> None:
    """The accepted component-scoped names are exactly these: a new one is a decision."""
    defs = custom_property_definitions((STATIC / "app.css").read_text())
    scoped = {name for name, scopes in defs.items() if scopes == {"scoped"}}
    assert scoped == EXPECTED_SCOPED, sorted(scoped ^ EXPECTED_SCOPED)


def test_a_dark_theme_definition_removed_is_named() -> None:
    css = (STATIC / "app.css").read_text()
    # `--bg-sunk` is defined in the bare (dark) block and again in the light block
    bare = css.index(":root {")
    end = css.index("}", bare)
    block = css[bare:end]
    assert "  --bg-sunk: #1a171b;\n" in block
    mutated = css[:bare] + block.replace("  --bg-sunk: #1a171b;\n", "") + css[end:]
    assert mutated != css
    problems = theme_problems(mutated, _other_sources(), js_set_properties(_scripts()))
    assert problems
    assert all("--bg-sunk" in p and "light theme only" in p for p in problems), problems


def test_a_name_used_but_defined_nowhere_is_named() -> None:
    css = (STATIC / "app.css").read_text() + "\n.x { color: var(--typo, red); }\n"
    problems = theme_problems(css, _other_sources(), js_set_properties(_scripts()))
    assert problems == ["app.css: var(--typo) is defined nowhere"]


def test_a_light_only_definition_is_not_rescued_by_a_scoped_one() -> None:
    css = (STATIC / "app.css").read_text()
    light = ':root[data-theme="light"] {'
    css = _with(css, light, light + "\n  --only-light: red;")
    css += "\n.x { --only-light: blue; color: var(--only-light); }\n"
    problems = theme_problems(css, {}, set())
    assert problems == ["app.css: --only-light is defined for the light theme only"]


def test_scoped_and_script_set_names_are_accepted_but_other_uses_are_checked() -> None:
    css = ".a { --z: 1; } .b { width: var(--z); }"
    assert theme_problems(css, {"index.html": "<i style='width: var(--z)'>"}, set()) == []
    assert theme_problems(css, {"x.js": "e.style.width = 'var(--set)';"}, {"--set"}) == []
    bad = theme_problems(css, {"index.html": '<i style="color: var(--nope)">'}, set())
    assert bad == ["index.html: var(--nope) is defined nowhere"]
    bad = theme_problems(css, {"x.js": "e.style.color = 'var(--nope)';"}, set())
    assert bad == ["x.js: var(--nope) is defined nowhere"]


def test_the_collector_reads_every_var_and_every_declaration() -> None:
    """The denominator: var() uses found equal a substring count; declarations too."""
    css = (STATIC / "app.css").read_text()
    uses = re.findall(r"var\(\s*(--[\w-]+)", css)
    assert len(uses) == css.count("var(") > 100
    defs = custom_property_definitions(css)
    assert sum(1 for s in defs.values() if "dark" in s) > 10
    assert sum(1 for s in defs.values() if "light" in s) > 5
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    declared = len(re.findall(r"(?<![\w-])--[\w-]+\s*:", plain))
    found = sum(1 for _, decls in parse_blocks(css) for d in decls if d.startswith("--"))
    assert found == declared


def test_unmodelled_theme_mechanisms_are_refused_not_ignored() -> None:
    with pytest.raises(AssertionError, match="media query"):
        parse_blocks("@media (prefers-color-scheme: dark) { :root { --a: 1; } }")
    with pytest.raises(AssertionError, match="theme-scoped"):
        custom_property_definitions(':root[data-theme="light"] body { --a: 1; }')
