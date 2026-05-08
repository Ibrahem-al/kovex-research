// CLI entry: npx ts-node indexer/index.ts <repo_path>
// Runs the full indexing pipeline:
//   1. Find all Python files under <repo_path>
//   2. Parse each file with tree-sitter
//   3. Phase 1 – MERGE nodes + DEFINES edges per file
//   4. Phase 2 – MERGE cross-file edges (IMPORTS, INHERITS, CALLS, USES_TYPE)
//   5. Print node/edge summary

import * as fs   from 'fs';
import * as path from 'path';

import { initParser, parseFile, ParsedFile } from './parse';
import { ingestFile, ingestCrossFileEdges }  from './ingest';
import { driver, verifyConnectivity }         from '../server/neo4j';

// ── File discovery ────────────────────────────────────────────────────────

const SKIP_DIRS = new Set(['__pycache__', '.git', '.tox', 'venv', '.venv', 'env', 'node_modules', '.mypy_cache']);

function findPythonFiles(dir: string): string[] {
  const out: string[] = [];
  function walk(cur: string) {
    let entries: fs.Dirent[];
    try { entries = fs.readdirSync(cur, { withFileTypes: true }); }
    catch { return; }
    for (const e of entries) {
      if (e.isDirectory() && !SKIP_DIRS.has(e.name)) {
        walk(path.join(cur, e.name));
      } else if (e.isFile() && e.name.endsWith('.py')) {
        out.push(path.join(cur, e.name));
      }
    }
  }
  walk(dir);
  return out;
}

// ── Neo4j summary query ───────────────────────────────────────────────────

async function printSummary(): Promise<void> {
  const { getSession } = await import('../server/neo4j');
  const session = getSession();
  try {
    const nodeRes = await session.run(
      `MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY label`,
    );
    const edgeRes = await session.run(
      `MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS cnt ORDER BY type`,
    );

    console.log('\n── Nodes ─────────────────────────────');
    for (const row of nodeRes.records) {
      console.log(`  ${String(row.get('label')).padEnd(12)} ${row.get('cnt')}`);
    }
    console.log('── Edges ─────────────────────────────');
    for (const row of edgeRes.records) {
      console.log(`  ${String(row.get('type')).padEnd(12)} ${row.get('cnt')}`);
    }
  } finally {
    await session.close();
  }
}

// ── Main ──────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const repoRoot = path.resolve(process.argv[2] ?? './httpx');

  if (!fs.existsSync(repoRoot)) {
    console.error(`Error: path not found: ${repoRoot}`);
    process.exit(1);
  }

  console.log(`Target repo : ${repoRoot}`);

  // Verify Neo4j is reachable before doing any work
  await verifyConnectivity();

  // ── Discover files ───────────────────────────────────────────────────
  const files = findPythonFiles(repoRoot);
  console.log(`Python files: ${files.length}`);

  if (files.length === 0) {
    console.error('No Python files found — check the path.');
    process.exit(1);
  }

  // ── Parse ────────────────────────────────────────────────────────────
  console.log('\nInitializing parser...');
  await initParser();

  const allParsed: ParsedFile[] = [];
  let   parseErrors = 0;

  process.stdout.write('Parsing    ');
  for (let i = 0; i < files.length; i++) {
    try {
      allParsed.push(parseFile(files[i], repoRoot));
    } catch (err) {
      parseErrors++;
      process.stderr.write(`\nParse error ${files[i]}: ${err}\n`);
    }
    if ((i + 1) % 10 === 0 || i + 1 === files.length) {
      process.stdout.write(`\rParsing    ${i + 1}/${files.length}`);
    }
  }
  console.log(`  (${parseErrors} errors)`);

  // ── Phase 1: nodes + DEFINES ──────────────────────────────────────────
  let ingestErrors = 0;

  process.stdout.write('Phase 1    ');
  for (let i = 0; i < allParsed.length; i++) {
    try {
      await ingestFile(allParsed[i]);
    } catch (err) {
      ingestErrors++;
      process.stderr.write(`\nIngest error ${allParsed[i].module.path}: ${err}\n`);
    }
    if ((i + 1) % 10 === 0 || i + 1 === allParsed.length) {
      process.stdout.write(`\rPhase 1    ${i + 1}/${allParsed.length}`);
    }
  }
  console.log(`  (${ingestErrors} errors)`);

  // ── Phase 2: cross-file edges ─────────────────────────────────────────
  process.stdout.write('Phase 2    cross-file edges...');
  await ingestCrossFileEdges(allParsed);
  console.log(' done');

  // ── Summary ───────────────────────────────────────────────────────────
  await printSummary();

  // Totals
  const totalFns  = allParsed.reduce((s, p) => s + p.functions.length, 0);
  const totalTys  = allParsed.reduce((s, p) => s + p.types.length, 0);
  const totalSyms = allParsed.reduce((s, p) => s + p.symbols.length, 0);
  console.log(`\nParsed     ${allParsed.length} modules, ${totalFns} functions, ${totalTys} types, ${totalSyms} symbols`);
  console.log('Neo4j      http://localhost:7474  (neo4j / kovexpassword)\n');
}

main()
  .catch(err => { console.error(err); process.exit(1); })
  .finally(() => driver.close());
