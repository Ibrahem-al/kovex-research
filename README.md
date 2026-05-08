# Kovex: Graph-Aware Coordination for Multi-Agent LLM Coding Systems

Kovex is a graph-aware coordination substrate for multi-agent LLM coding systems. It indexes a target codebase into a Neo4j property graph, exposes that graph to agents through an MCP server, and prevents semantic conflicts by notifying any agent whose registered read-set intersects the k-hop neighborhood of a node another agent writes. Notifications fire on actual call dependencies (CALLS edges only), so a function-signature change reaches its callers and callees without flooding agents that merely share a module or parameter type.

## Prerequisites

- Node.js 20+
- Python 3.11+
- Docker (for Neo4j)

## Quickstart

```bash
# 1. Clone the repo (httpx is a pinned submodule — pull it too)
git clone --recurse-submodules https://github.com/Ibrahem-al/kovex-research.git
cd kovex-research

# 2. Install dependencies
npm install
pip install -r requirements.txt

# 3. Start Neo4j (also applies the schema via the neo4j-init service)
docker compose up -d

# 4. Index httpx into the graph
npx ts-node indexer/index.ts ./httpx/httpx

# 5. Start the MCP server (speaks JSON-RPC over stdio)
npx ts-node server/index.ts
```

Neo4j browser is available at <http://localhost:7474> (`neo4j` / `kovexpassword`).

## Reproducing the Evaluation

The Phase 4 harness runs all 14 tasks from `eval/tasks/httpx_tasks.json` as both Kovex-coordinated and baseline scenarios, and writes per-task results plus a summary to `eval/results/`.

```bash
python eval/harness.py
```

Single-task runs: `python eval/harness.py --task T01`.

## Paper

[arXiv link coming soon]

## License

MIT — see [LICENSE](LICENSE).
