// stylelint-config-standard, with the rules below switched off. Each is a
// formatting or notation preference the stylesheet's compact, one-rule-per-line
// layout does not follow; applying it would rewrite hundreds of lines (or
// reorder the cascade) and the computed styles must not change.
export default {
  extends: ["stylelint-config-standard"],
  rules: {
    // Colours are written with six hex digits: the contrast test
    // (tests/test_faint_contrast.py) reads each theme token as #rrggbb.
    "color-hex-length": "long",
    // The sheet puts a short rule's declarations on one line.
    "declaration-block-single-line-max-declarations": null,
    // Blank-line placement between rules, comments, at-rules, declarations and
    // custom properties: spacing only.
    "rule-empty-line-before": null,
    "comment-empty-line-before": null,
    "at-rule-empty-line-before": null,
    "declaration-empty-line-before": null,
    "custom-property-empty-line-before": null,
    // Reordering rules to satisfy this changes which rule wins. The sheet is
    // ordered by feature, and the check cannot know which elements share a
    // selector's matches.
    "no-descending-specificity": null,
    // Font family names (Menlo, Georgia) are not keywords, and currentColor is
    // case-insensitive: spelling only.
    "value-keyword-case": null,
    // (max-width: 760px) versus the range form: the same query, spelled the
    // other way.
    "media-feature-range-notation": null,
    // The -webkit- spellings sit next to the standard ones on purpose, for
    // browsers that only know the prefixed form.
    "property-no-vendor-prefix": null,
    // State classes are built from run states in the scripts (rs- plus the
    // state's snake_case name), so they cannot be kebab-case.
    "selector-class-pattern": [
      "^(rs-[a-z]+(_[a-z]+)*|[a-z][a-z0-9]*(-[a-z0-9]+)*)$",
      { message: "Expected a kebab-case class, or rs- plus a snake_case run state" },
    ],
  },
};
