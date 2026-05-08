# Kovex — Engineering Spec for Claude Code

Kovex is a graph-aware coordination substrate for multi-agent LLM coding systems.
It indexes a codebase into a Neo4j property graph, exposes it to agents via an
MCP server, and prevents semantic conflicts by notifying agents when a node they
depend on is written to by another agent.

This is a **research prototype** targeting an arXiv paper + NeurIPS/ICLR workshop
submission. Every design decision should be justifiable in a paper. Prefer
correctness and clarity over performance optimizations.

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| MCP Server | TypeScript (Node.js 20+) | Official Anthropic MCP SDK is TS-first |
| Graph DB | Neo4j 5.x (Docker) | Native property graph, Cypher BFS support |
| Code Parser | Tree-sitter (via `web-tree-sitter` or `tree-sitter` npm) | Language-agnostic, ACER-validated |
| Eval Harness | Python 3.11+ | Standard ML/SE research tooling |
| Target Codebase | `httpx` (Python HTTP client) | Small (~150 files), rich cross-file types, clean deps |
| Logging | JSON-L (append-only) | Easy to parse in eval scripts |
| Container | Docker Compose | Neo4j + app in one command |

---

## Repo Structure to Build

```
kovex/
├── CLAUDE.md                    ← this file
├── docker-compose.yml           ← Neo4j + app services
├── package.json                 ← root TS project
├── tsconfig.json
│
├── db/
│   ├── schema.cypher            ← node constraints + indexes
│   └── queries/
│       ├── bfs_notify.cypher    ← k-hop BFS notification query
│       ├── merge_function.cypher
│       ├── merge_type.cypher
│       ├── merge_module.cypher
│       └── merge_edge.cypher
│
├── indexer/
│   ├── parse.ts                 ← Tree-sitter parser → AST extraction
│   ├── resolve.ts               ← cross-file import resolution
│   ├── ingest.ts                ← emit Cypher MERGE statements to Neo4j
│   └── index.ts                 ← CLI entry: `npx ts-node indexer/index.ts <repo_path>`
│
├── server/
│   ├── index.ts                 ← MCP server entry point
│   ├── registry.ts              ← Read Registry (Map<agentId, Set<nodeId>>)
│   ├── writelog.ts              ← Write Log (append JSON-L to logs/)
│   ├── notify.ts                ← Algorithm 1: k-hop BFS notification
│   ├── neo4j.ts                 ← Neo4j driver singleton
│   └── tools/
│       ├── register_read.ts
│       ├── deregister_read.ts
│       ├── list_reads.ts
│       ├── commit_write.ts      ← main coordination tool; calls notify.ts
│       ├── get_node.ts
│       ├── get_dependents.ts
│       ├── query_graph.ts
│       └── poll_directives.ts
│
├── eval/
│   ├── harness.py               ← two-agent test harness (Agent A writes, B notified)
│   ├── conflict_check.py        ← Definition 2 operationalization: compile C_B post-write
│   ├── baseline_harness.py      ← same harness, no Kovex coordination
│   ├── tasks/
│   │   └── httpx_tasks.json     ← 10-20 multi-agent coding tasks on httpx
│   └── results/
│       └── .gitkeep
│
└── logs/
    └── write_events.jsonl       ← runtime write log (gitignored)
```

---

## Graph Schema

### Node Labels and Properties

```cypher
// Function node
(:Function {
  id: string,           // sha256(name + file)
  name: string,
  file: string,         // relative path from repo root
  signature: string,    // full param list as string
  return_type: string,
  language: string,     // "python" | "typescript"
  body_hash: string     // sha256 of contract surface only (sig + return)
})

// Type node (class, interface, struct, enum)
(:Type {
  id: string,
  name: string,
  file: string,
  kind: string,         // "class" | "interface" | "struct" | "enum"
  fields_hash: string,  // sha256 of sorted field names+types
  language: string
})

// Module node (one per source file)
(:Module {
  id: string,
  path: string,         // relative file path
  language: string,
  exports: string       // JSON array of exported symbol names
})

// Symbol node (module-level variable or constant)
(:Symbol {
  id: string,
  name: string,
  file: string,
  type_annotation: string,
  kind: string          // "variable" | "constant"
})
```

### Edge Types

```cypher
(:Function)-[:CALLS {call_site_line: int}]->(:Function)
(:Function)-[:USES_TYPE {role: string}]->(:Type)      // role: param|return|local
(:Module)-[:DEFINES]->(:Function)
(:Module)-[:DEFINES]->(:Type)
(:Module)-[:DEFINES]->(:Symbol)
(:Module)-[:IMPORTS {symbols: string}]->(:Module)     // symbols: JSON array
(:Type)-[:INHERITS]->(:Type)
(:Type)-[:IMPLEMENTS]->(:Type)
(:Function)-[:OVERRIDES]->(:Function)
```

### Constraints and Indexes (db/schema.cypher)

```cypher
CREATE CONSTRAINT fn_id  IF NOT EXISTS FOR (n:Function) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT ty_id  IF NOT EXISTS FOR (n:Type)     REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT mod_id IF NOT EXISTS FOR (n:Module)   REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT sym_id IF NOT EXISTS FOR (n:Symbol)   REQUIRE n.id IS UNIQUE;

CREATE INDEX fn_name  IF NOT EXISTS FOR (n:Function) ON (n.name);
CREATE INDEX fn_file  IF NOT EXISTS FOR (n:Function) ON (n.file);
CREATE INDEX ty_name  IF NOT EXISTS FOR (n:Type)     ON (n.name);
CREATE INDEX mod_path IF NOT EXISTS FOR (n:Module)   ON (n.path);
```

---

## The Notification Algorithm (server/notify.ts)

Implement exactly as specified in Algorithm 1 of the Phase 2 paper.
The Cypher equivalent (use this in bfs_notify.cypher):

```cypher
MATCH path = (start {id: $nodeId})-[*1..$k]-(neighbor)
WHERE neighbor.id <> $nodeId
RETURN DISTINCT neighbor.id AS nodeId,
       min(length(path))    AS distance
ORDER BY distance ASC
```

Then in TypeScript: for each returned nodeId, look up which agents have it in
their ReadSet (from the in-memory registry) and enqueue a directive for each.

---

## MCP Tools (server/tools/)

Each tool is a function that takes validated JSON input and returns JSON output.
Register all tools with the MCP SDK in server/index.ts.

| Tool | Key Logic |
|---|---|
| `register_read` | Add `(agentId, nodeId, timestamp, bodyHash)` to registry |
| `deregister_read` | Remove from registry |
| `list_reads` | Return all nodeIds in RS(agentId) |
| `commit_write` | Apply patch → update Neo4j → run notify algorithm → log WriteEvent → return notify_set |
| `get_node` | Cypher MATCH by id, return node props. No registry side effect. |
| `get_dependents` | Run BFS query, return nodes + distances |
| `query_graph` | Execute read-only Cypher from agent, return results |
| `poll_directives` | Return and clear agent's directive queue |

### WriteEvent log record format (logs/write_events.jsonl)
One JSON object per line:
```json
{
  "write_id": "uuid",
  "agent_writer": "agent_A",
  "node_id": "abc123",
  "node_label": "Function",
  "node_name": "send_request",
  "h_pre": "sha256...",
  "h_post": "sha256...",
  "t_w": "2026-01-01T00:00:00Z",
  "k": 2,
  "notify_set": [
    {"agent": "agent_B", "trigger_node": "def456", "distance": 1}
  ]
}
```

---

## Semantic Conflict Definition (for eval/conflict_check.py)

A semantic conflict between Agent A (writer) and Agent B (reader) exists if:
1. Agent A committed an observable write to node N (body_hash changed)
2. Agent B had a registered read on N or a node within k hops of N before the write
3. Agent B generated code C_B after its read registration
4. `Valid(C_B, pre_write_state) == True` AND `Valid(C_B, post_write_state) == False`

Operationalize condition 4 by:
- Running `mypy` or `pyright` on C_B against the pre-write httpx codebase → must pass
- Running `mypy` or `pyright` on C_B against the post-write codebase → must fail
- The failure must reference a name/type that traces to node N or its k-hop neighbors

---

## docker-compose.yml

```yaml
version: "3.8"
services:
  neo4j:
    image: neo4j:5.18
    ports:
      - "7474:7474"   # browser
      - "7687:7687"   # bolt
    environment:
      NEO4J_AUTH: neo4j/kovexpassword
    volumes:
      - neo4j_data:/data

  kovex:
    build: .
    depends_on:
      - neo4j
    environment:
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USER: neo4j
      NEO4J_PASS: kovexpassword
      HOP_RADIUS: "2"
    volumes:
      - ./logs:/app/logs

volumes:
  neo4j_data:
```

---

## Environment Variables

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASS=kovexpassword
HOP_RADIUS=2          # k value for notification algorithm
TARGET_REPO=./httpx   # path to cloned httpx repo
LOG_PATH=./logs/write_events.jsonl
```

---

## Build Order for Phase 3

Build in this exact order — each step depends on the previous:

1. `docker-compose.yml` + `db/schema.cypher` → get Neo4j running with constraints
2. `server/neo4j.ts` → verify connection
3. `indexer/parse.ts` → Tree-sitter extraction for Python
4. `indexer/ingest.ts` → MERGE nodes/edges into Neo4j
5. `indexer/index.ts` → run on `httpx`, verify graph in Neo4j browser
6. `server/registry.ts` + `server/writelog.ts` → in-memory state
7. `server/notify.ts` → Algorithm 1, unit-test with a mock graph
8. `server/tools/*.ts` → implement each tool, test individually
9. `server/index.ts` → register all tools with MCP SDK, verify with MCP inspector
10. `eval/harness.py` → two-agent harness (Agent A writes `send_request` signature, Agent B depends on it)
11. `eval/baseline_harness.py` → same harness, skip MCP coordination
12. `eval/conflict_check.py` → compile-check Agent B's code pre/post write

---

## What "Phase 3 Done" Looks Like

- [ ] `docker compose up` starts Neo4j and the MCP server with no errors
- [ ] `npx ts-node indexer/index.ts ./httpx` indexes the repo; Neo4j browser shows nodes and edges
- [ ] All 8 MCP tools respond correctly in the MCP inspector
- [ ] Two-agent harness runs: Agent A writes a function signature change, Agent B receives a directive within 500ms
- [ ] Baseline harness runs: same scenario, no directive, Agent B produces broken code
- [ ] `write_events.jsonl` captures the full event with correct notify_set
- [ ] `conflict_check.py` correctly classifies the baseline run as a semantic conflict

---

## Paper References

- Phase 1 (Related Work + Problem Statement): `kovex_phase1.pdf`
- Phase 2 (System Design): `kovex_phase2.pdf`
- Both PDFs are in the repo root for reference during build.

Do not deviate from the schema, algorithm, or tool interface defined in Phase 2
without noting the deviation and the reason — evaluation validity depends on the
implementation matching the paper spec exactly.
