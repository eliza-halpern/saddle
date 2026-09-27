# Copied from the internal benchmark's M3 dry-run fake model server for the UXFIX2 e2e; ruff-formatted, logic unchanged.
# ruff: noqa: E401, E501, I001
"""Fake OpenAI-compatible model server for M3's dry run. NO model, NO GPU.

Serves the three endpoints `saddle auto` and the runner touch:
  POST /<key>/v1/chat/completions  (stream) -> the scripted tool calls for draw <key>
  POST /<key>/tokenize                     -> 404, so saddle falls back to its estimate
  GET  /metrics                            -> vllm-shaped counters (generation_tokens_total)
A script is a list of turns; each turn is a list of tool calls [name, {args}] or the
string "HTTP500" (a model error, which saddle must record as a stop). Turn i answers the
i-th request for that key. Every request's temperature and reasoning_effort are logged
to --log, so the dry run can show the frozen sampling values reach the wire.
Usage: fake_server.py --port P --scripts scripts.json --log requests.jsonl
"""

import argparse, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--scripts", required=True)
ap.add_argument("--log", required=True)
A = ap.parse_args()
SCRIPTS = json.load(open(A.scripts))
TURN: dict[str, int] = {}
GEN = [0]
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/metrics":
            with LOCK:
                g = GEN[0]
            self._send(
                200,
                'vllm:num_requests_running{m="fake"} 0.0\n'
                'vllm:num_requests_waiting{m="fake"} 0.0\n'
                f'vllm:generation_tokens_total{{m="fake"}} {float(g)}\n',
                "text/plain",
            )
        else:
            self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        parts = self.path.strip("/").split("/")
        key = parts[0]
        if parts[-1] == "tokenize":
            return self._send(404, "{}")
        with LOCK:
            i = TURN.get(key, 0)
            TURN[key] = i + 1
        with open(A.log, "a") as f:
            f.write(
                json.dumps(
                    {
                        "key": key,
                        "turn": i,
                        "temperature": payload.get("temperature"),
                        "reasoning_effort": payload.get("reasoning_effort"),
                        "stream": payload.get("stream"),
                        "tools": sorted(t["function"]["name"] for t in payload.get("tools") or []),
                    }
                )
                + "\n"
            )
        script = SCRIPTS.get(key, [])
        turn = script[i] if i < len(script) else [["finish", {"summary": "script exhausted"}]]
        if turn == "HTTP500":
            return self._send(500, '{"error": "fake model error"}')
        chunks = []
        for idx, (name, args) in enumerate(turn):
            a = json.dumps(args)
            with LOCK:
                GEN[0] += max(1, len(a) // 4)
            chunks.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": idx,
                                        "id": f"c{i}_{idx}",
                                        "type": "function",
                                        "function": {"name": name, "arguments": a},
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        chunks.insert(
            0, {"choices": [{"index": 0, "delta": {"reasoning": "(fake) scripted turn."}}]}
        )
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        self._send(200, body, "text/event-stream")


ThreadingHTTPServer(("127.0.0.1", A.port), H).serve_forever()
