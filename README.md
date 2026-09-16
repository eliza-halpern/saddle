# Saddle

Break your local model to harness.

Saddle is a deterministic execution harness that turns a non-deterministic local LLM into a dependable coding tool: constrained DAG planning, parallel proof-gated workers, machine-checked gates. No retries on unverified foundations.

**Status:** pre-implementation. The architecture is specified in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the next step is the §6 vertical slice.

## Layout

- `docs/ARCHITECTURE.md` — full architecture specification.
- Engine, scheduler, and gates land at the top level as the vertical slice is built.

## Quickstart

See "§6 Adoption Strategy" in the architecture doc. First milestone: guided DAG emission → Kahn scheduler → one Tier-1-gated node → proof journal entry.

## Requirements (planned)

- Python 3.12+
- Self-operated vLLM ≥ 0.28 server with guided decoding (XGrammar) + KV offloading —
  the proven setup is [qwen38-27b-rtx3090](https://github.com/syv-ai/qwen38-27b-rtx3090)
- pytest, coverage.py, mutmut (Tier-2 sampled)
