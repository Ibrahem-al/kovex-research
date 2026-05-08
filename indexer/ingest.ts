import { getSession } from '../server/neo4j';
import { ParsedFile } from './parse';
import { dottedToPath } from './resolve';

const BATCH = 500;

function chunk<T>(arr: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

// ── Phase 1: MERGE all nodes + DEFINES edges for one file ────────────────

export async function ingestFile(parsed: ParsedFile): Promise<void> {
  const session = getSession();
  try {
    // Module
    await session.run(
      `MERGE (m:Module {id: $id})
       SET m.path = $path, m.language = $language, m.exports = $exports`,
      parsed.module,
    );

    // Functions (batched)
    for (const batch of chunk(parsed.functions, BATCH)) {
      await session.run(
        `UNWIND $nodes AS n
         MERGE (f:Function {id: n.id})
         SET f.name = n.name, f.file = n.file, f.signature = n.signature,
             f.return_type = n.return_type, f.language = n.language,
             f.body_hash = n.body_hash`,
        { nodes: batch },
      );
    }

    // Types (batched)
    for (const batch of chunk(parsed.types, BATCH)) {
      await session.run(
        `UNWIND $nodes AS n
         MERGE (t:Type {id: n.id})
         SET t.name = n.name, t.file = n.file, t.kind = n.kind,
             t.fields_hash = n.fields_hash, t.language = n.language`,
        { nodes: batch },
      );
    }

    // Symbols (batched)
    for (const batch of chunk(parsed.symbols, BATCH)) {
      await session.run(
        `UNWIND $nodes AS n
         MERGE (s:Symbol {id: n.id})
         SET s.name = n.name, s.file = n.file,
             s.type_annotation = n.type_annotation, s.kind = n.kind`,
        { nodes: batch },
      );
    }

    // DEFINES: Module → Function
    const modId = parsed.module.id;
    for (const batch of chunk(parsed.functions.map(f => f.id), BATCH)) {
      await session.run(
        `UNWIND $ids AS id
         MATCH (m:Module {id: $modId}), (f:Function {id: id})
         MERGE (m)-[:DEFINES]->(f)`,
        { modId, ids: batch },
      );
    }

    // DEFINES: Module → Type
    for (const batch of chunk(parsed.types.map(t => t.id), BATCH)) {
      await session.run(
        `UNWIND $ids AS id
         MATCH (m:Module {id: $modId}), (t:Type {id: id})
         MERGE (m)-[:DEFINES]->(t)`,
        { modId, ids: batch },
      );
    }

    // DEFINES: Module → Symbol
    for (const batch of chunk(parsed.symbols.map(s => s.id), BATCH)) {
      await session.run(
        `UNWIND $ids AS id
         MATCH (m:Module {id: $modId}), (s:Symbol {id: id})
         MERGE (m)-[:DEFINES]->(s)`,
        { modId, ids: batch },
      );
    }
  } finally {
    await session.close();
  }
}

// ── Phase 2: MERGE cross-file edges after all nodes are ingested ──────────

export async function ingestCrossFileEdges(allParsed: ParsedFile[]): Promise<void> {
  const session = getSession();
  try {
    // IMPORTS: Module → Module
    const importPairs: { fromId: string; toPath: string; altPath: string; symbols: string }[] = [];
    for (const parsed of allParsed) {
      for (const imp of parsed.imports) {
        if (!imp.fromModule) continue;
        const toPath    = dottedToPath(imp.fromModule, parsed.module.path);
        const altPath   = toPath.replace(/\.py$/, '/__init__.py'); // also try package init
        importPairs.push({
          fromId:  parsed.module.id,
          toPath,
          altPath,
          symbols: JSON.stringify(imp.symbols),
        });
      }
    }
    for (const batch of chunk(importPairs, BATCH)) {
      await session.run(
        `UNWIND $pairs AS p
         MATCH (from:Module {id: p.fromId})
         MATCH (to:Module) WHERE to.path = p.toPath OR to.path = p.altPath
         MERGE (from)-[r:IMPORTS]->(to)
         SET r.symbols = p.symbols`,
        { pairs: batch },
      );
    }

    // INHERITS: Type → Type  (match parent by name anywhere in graph)
    const inheritPairs: { childFile: string; childName: string; parentName: string }[] = [];
    for (const parsed of allParsed) {
      for (const inh of parsed.inherits) {
        inheritPairs.push({
          childFile:  parsed.module.path,
          childName:  inh.childName,
          parentName: inh.parentName,
        });
      }
    }
    for (const batch of chunk(inheritPairs, BATCH)) {
      await session.run(
        `UNWIND $pairs AS p
         MATCH (child:Type  {name: p.childName,  file: p.childFile}),
               (parent:Type {name: p.parentName})
         MERGE (child)-[:INHERITS]->(parent)`,
        { pairs: batch },
      );
    }

    // CALLS: Function → Function  (best-effort by name; ambiguous names get multiple edges)
    const callPairs: { callerFile: string; callerName: string; calleeName: string; line: number }[] = [];
    for (const parsed of allParsed) {
      for (const call of parsed.calls) {
        callPairs.push({
          callerFile: parsed.module.path,
          callerName: call.callerName,
          calleeName: call.calleeName,
          line:       call.line,
        });
      }
    }
    for (const batch of chunk(callPairs, BATCH)) {
      await session.run(
        `UNWIND $pairs AS p
         MATCH (caller:Function {name: p.callerName, file: p.callerFile}),
               (callee:Function {name: p.calleeName})
         MERGE (caller)-[r:CALLS]->(callee)
         ON CREATE SET r.call_site_line = p.line`,
        { pairs: batch },
      );
    }

    // USES_TYPE: Function → Type  (best-effort by name)
    const usesPairs: { fnFile: string; fnName: string; typeName: string; role: string }[] = [];
    for (const parsed of allParsed) {
      for (const use of parsed.typeUses) {
        usesPairs.push({
          fnFile:   parsed.module.path,
          fnName:   use.functionName,
          typeName: use.typeName,
          role:     use.role,
        });
      }
    }
    for (const batch of chunk(usesPairs, BATCH)) {
      await session.run(
        `UNWIND $pairs AS p
         MATCH (f:Function {name: p.fnName, file: p.fnFile}),
               (t:Type     {name: p.typeName})
         MERGE (f)-[r:USES_TYPE]->(t)
         ON CREATE SET r.role = p.role`,
        { pairs: batch },
      );
    }
  } finally {
    await session.close();
  }
}
