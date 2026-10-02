// The DevTools port of a Chrome the drivers start with --remote-debugging-port=0.
//
// Each *_cdp.mjs driver once drew a random port in 9300-9899. With eight test
// workers, two Chromes now and then drew the same one: the second could not
// bind it ("Cannot start http server for devtools"), kept running without
// DevTools, and its driver found the first Chrome's page on that port and drove
// it. Port 0 lets the OS pick a free port, and Chrome writes the one it got,
// then the browser's WebSocket path, to DevToolsActivePort in its profile.
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * The port Chrome wrote to `prof`/DevToolsActivePort, or 0 while there is none yet.
 * The file counts only once both lines are there: a port alone may be half written.
 * @param {string} prof
 */
export function activePort(prof) {
  let text;
  try {
    text = readFileSync(join(prof, "DevToolsActivePort"), "utf8");
  } catch {
    return 0;
  }
  const [first, second = ""] = text.split("\n");
  const port = Number(first);
  if (!/^\d+$/.test(first) || !second.startsWith("/devtools/browser/")) return 0;
  return port > 0 && port < 65536 ? port : 0;
}
