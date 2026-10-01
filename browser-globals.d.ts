// Names the browser scripts read that the DOM library does not declare.
// `module` exists only under node, where tests/markdown.test.js loads
// markdown.js; in the page it is absent and the script checks `typeof`.
declare var module: { exports: object } | undefined;
