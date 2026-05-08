// Reference patterns for all edge types.
// ingest.ts runs these as UNWIND batches; these single-pair forms are
// provided for manual inspection / ad-hoc repair in the Neo4j browser.

// ── Module → Function / Type / Symbol ─────────────────────────────────────
MATCH (m:Module {id: $modId}), (f:Function {id: $fnId})
MERGE (m)-[:DEFINES]->(f);

MATCH (m:Module {id: $modId}), (t:Type {id: $tyId})
MERGE (m)-[:DEFINES]->(t);

MATCH (m:Module {id: $modId}), (s:Symbol {id: $symId})
MERGE (m)-[:DEFINES]->(s);

// ── Module → Module ────────────────────────────────────────────────────────
MATCH (a:Module {id: $fromId}), (b:Module {path: $toPath})
MERGE (a)-[r:IMPORTS]->(b)
SET r.symbols = $symbols;

// ── Type → Type ────────────────────────────────────────────────────────────
MATCH (child:Type {name: $childName, file: $childFile}),
      (parent:Type {name: $parentName})
MERGE (child)-[:INHERITS]->(parent);

// ── Function → Function ────────────────────────────────────────────────────
MATCH (caller:Function {name: $callerName, file: $callerFile}),
      (callee:Function {name: $calleeName})
MERGE (caller)-[r:CALLS]->(callee)
ON CREATE SET r.call_site_line = $line;

// ── Function → Type ────────────────────────────────────────────────────────
MATCH (f:Function {name: $fnName, file: $fnFile}),
      (t:Type {name: $typeName})
MERGE (f)-[r:USES_TYPE]->(t)
ON CREATE SET r.role = $role;
