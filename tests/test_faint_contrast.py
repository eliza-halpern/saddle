"""--ink-faint stays readable (WCAG AA) in both themes, and stays faint.

Labels, record hashes and the Merge-off note are all drawn in
`--ink-faint`, the faintest of the three ink tones. #86 found that tone
below the 4.5:1 WCAG AA floor for small text against every surface it is
painted on -- `--bg`, `--bg-sunk`, `--bg-raise` and `--card` -- in both
the dark theme (the `:root` tokens) and the light theme (the
`prefers-color-scheme: light` block).

The fix may change the two colour values and nothing else, so this test
reads them back out of `app.css` instead of hard-coding them, computes
the WCAG relative-luminance contrast ratio of `--ink-faint` against each
of those four surfaces in each theme, and requires the 4.5:1 floor. It
also requires `--ink-faint` to keep its rank in the hierarchy -- still
below `--ink-dim` in contrast on every surface -- so a fix that raises
the faint tone up to the dim tone is rejected as well.
"""

from __future__ import annotations

import re
from pathlib import Path

# Every test runs from a disposable cwd (conftest), so anchor to the test
# file rather than to wherever pytest was started.
_ROOT = Path(__file__).resolve().parents[1]

# The surfaces `--ink-faint` text is drawn on: the page, sunk panels,
# raised panels and cards.
_SURFACES = ("--bg", "--bg-sunk", "--bg-raise", "--card")

# WCAG 2.x AA minimum for small (normal-weight) text.
_AA = 4.5

_DARK_ROOT = re.compile(r":root\s*{([^{}]*)}")
_LIGHT_ROOT = re.compile(r':root\[data-theme="light"]\s*{([^{}]*)}')
_TOKEN = re.compile(r"([a-z-]+-(?:faint|dim)|--[a-z-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;")


def _stylesheet() -> str:
    css = (_ROOT / "src/saddle/web/static/app.css").read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _root_tokens(css: str, *, light: bool) -> dict[str, str]:
    """Hex-colour custom properties the theme resolves to.

    Dark is the base: its tokens split across the plain `:root { }` blocks (the
    second carries `--card` and the state colours), merged here. Light is that
    base overlaid by the explicit `:root[data-theme="light"] { }` block, which
    the page applies when the reader's chosen or system theme is light.
    """
    tokens: dict[str, str] = {}
    for match in _DARK_ROOT.finditer(css):
        tokens.update(dict(_TOKEN.findall(match.group(1))))
    if light:
        for match in _LIGHT_ROOT.finditer(css):
            tokens.update(dict(_TOKEN.findall(match.group(1))))
    return tokens


def _hex(value: str) -> tuple[int, int, int]:
    is_srgb = value.startswith("#") and len(value) == 7
    assert is_srgb, f"not an sRGB hex colour: {value!r}"
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x relative luminance: sRGB channels linearised, then weighted."""

    def lin(c: int) -> float:
        f = c / 255.0
        return f / 12.92 if f <= 0.04045 else ((f + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    light, dark = sorted((_relative_luminance(c) for c in (fg, bg)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_ink_faint_reaches_aa_on_every_surface_it_is_drawn_on() -> None:
    """#86: `--ink-faint` is AA-contrast in both themes, and still under `--ink-dim`.

    Fails on the original colours: the dark theme reads 3.72-4.46:1 and
    the light theme 3.82-4.45:1 against those surfaces, all below the
    4.5:1 floor. Both themes are computed before the single assertion,
    and each violation message is built before it is selected, so a
    passing run still exercises every line of the check.
    """
    css = _stylesheet()
    pairs: list[tuple[str, dict[str, str], str, float, float]] = []
    violations: list[str] = []
    for name, light in (("dark", False), ("light", True)):
        tokens = _root_tokens(css, light=light)
        missing = [t for t in (*_SURFACES, "--ink-dim", "--ink-faint") if t not in tokens]
        violations.extend(f"{name} theme declares no hex values for: {t}" for t in missing)
        faint = _hex(tokens["--ink-faint"])
        dim = _hex(tokens["--ink-dim"])
        for surface in _SURFACES:
            got = _contrast(faint, _hex(tokens[surface]))
            dimmed = _contrast(dim, _hex(tokens[surface]))
            pairs.append((name, tokens, surface, got, dimmed))
    aa_results = [
        (
            f"{name} theme: {tokens['--ink-faint']} on {s} ({tokens[s]}) reads {got:.2f}:1, "
            f"below the {_AA}:1 WCAG AA floor for small text",
            got >= _AA,
        )
        for name, tokens, s, got, _ in pairs
    ]
    violations.extend(msg for msg, ok in aa_results if not ok)
    rank_results = [
        (
            f"{name} theme: {tokens['--ink-faint']} on {s} reads {got:.2f}:1, not "
            f"below {tokens['--ink-dim']}'s {dimmed:.2f}:1; the faint tone must stay faint",
            got < dimmed,
        )
        for name, tokens, s, got, dimmed in pairs
    ]
    violations.extend(msg for msg, ok in rank_results if not ok)
    assert not violations, "\n".join(violations)
