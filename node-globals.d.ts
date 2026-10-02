// Node's own typings (@types/node) are not a dependency, so the node side of
// the test files is declared here loosely: a shorthand module is `any`, which
// leaves the node APIs unchecked and the files' own code (JSDoc types, null
// checks, untyped parameters) fully checked.
declare module "node:*";
declare const require: (id: string) => any;
declare const module: { exports: any };
declare const __dirname: string;
declare const process: any;
declare const global: any;
declare const Buffer: any;
declare module "pngjs";
