// Unit test for Algorithm 1 using a synthetic 4-node chain in Neo4j.
//
// Graph:  A --CALLS--> B --CALLS--> C --CALLS--> D
//
// Setup:
//   agent_writer  writes A
//   agent_reader  registers reads on B (dist=1) and C (dist=2)
//   agent_far     registers read  on D (dist=3, beyond k=2)
//
// Expected notify_set (k=2):
//   agent_reader notified via B (dist=1) and via C (dist=2)
//   agent_far    NOT notified (D is dist=3)

import { driver, getSession } from './neo4j';
import { registerRead, pollDirectives } from './registry';
import { runNotify } from './notify';

const TEST_PREFIX = '__notify_test__';
const A = `${TEST_PREFIX}_A`;
const B = `${TEST_PREFIX}_B`;
const C = `${TEST_PREFIX}_C`;
const D = `${TEST_PREFIX}_D`;

async function setup(): Promise<void> {
  const session = getSession();
  try {
    await session.run(`
      MERGE (a:Function {id: $A, name: 'A', file: 'test', body_hash: 'h_pre'})
      MERGE (b:Function {id: $B, name: 'B', file: 'test', body_hash: 'hB'})
      MERGE (c:Function {id: $C, name: 'C', file: 'test', body_hash: 'hC'})
      MERGE (d:Function {id: $D, name: 'D', file: 'test', body_hash: 'hD'})
      MERGE (a)-[:CALLS {call_site_line: 1}]->(b)
      MERGE (b)-[:CALLS {call_site_line: 2}]->(c)
      MERGE (c)-[:CALLS {call_site_line: 3}]->(d)
    `, { A, B, C, D });
  } finally {
    await session.close();
  }
}

async function teardown(): Promise<void> {
  const session = getSession();
  try {
    await session.run(
      `MATCH (n) WHERE n.id IN [$A,$B,$C,$D] DETACH DELETE n`,
      { A, B, C, D },
    );
  } finally {
    await session.close();
  }
}

async function run(): Promise<void> {
  console.log('Setting up synthetic graph...');
  await setup();

  // Register reads
  const ts = new Date().toISOString();
  registerRead({ agentId: 'agent_reader', nodeId: B, timestamp: ts, bodyHash: 'hB' });
  registerRead({ agentId: 'agent_reader', nodeId: C, timestamp: ts, bodyHash: 'hC' });
  registerRead({ agentId: 'agent_far',    nodeId: D, timestamp: ts, bodyHash: 'hD' });

  console.log('Running notify (k=2) from node A...');
  const notifySet = await runNotify({
    writtenNodeId: A,
    writeId:       'test-write-001',
    writerAgent:   'agent_writer',
    timestamp:     new Date().toISOString(),
    k:             2,
  });

  console.log('\nNotify set returned:');
  for (const entry of notifySet) {
    console.log(`  agent=${entry.agent}  trigger=${entry.trigger_node}  dist=${entry.distance}`);
  }

  // Assertions
  const readerEntries = notifySet.filter(e => e.agent === 'agent_reader');
  const farEntries    = notifySet.filter(e => e.agent === 'agent_far');

  console.log('\nAssertions:');

  const hasB = readerEntries.some(e => e.trigger_node === B && e.distance === 1);
  const hasC = readerEntries.some(e => e.trigger_node === C && e.distance === 2);
  console.log(`  agent_reader notified via B (dist=1): ${hasB ? 'PASS' : 'FAIL'}`);
  console.log(`  agent_reader notified via C (dist=2): ${hasC ? 'PASS' : 'FAIL'}`);
  console.log(`  agent_far NOT notified (dist=3):      ${farEntries.length === 0 ? 'PASS' : 'FAIL'}`);

  // Check directive queues
  const readerDirectives = pollDirectives('agent_reader');
  const farDirectives    = pollDirectives('agent_far');
  console.log(`  agent_reader queue length = ${readerDirectives.length} (expected 2): ${readerDirectives.length === 2 ? 'PASS' : 'FAIL'}`);
  console.log(`  agent_far    queue length = ${farDirectives.length}  (expected 0): ${farDirectives.length === 0  ? 'PASS' : 'FAIL'}`);

  const allPass = hasB && hasC && farEntries.length === 0
    && readerDirectives.length === 2 && farDirectives.length === 0;

  console.log(`\n${allPass ? '✓ All assertions passed' : '✗ Some assertions FAILED'}`);

  await teardown();
  console.log('Test nodes cleaned up.');
}

run()
  .catch(err => { console.error(err); process.exit(1); })
  .finally(() => driver.close());
