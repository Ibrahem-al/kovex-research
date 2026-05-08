// Algorithm 1 — k-hop BFS notification.
// After a commit_write, run BFS from the written node, find all agents whose
// ReadSet intersects the k-hop neighborhood, and enqueue a Directive for each.

import neo4j from 'neo4j-driver';

import { getSession }                       from './neo4j';
import { agentsReadingNode, enqueueDirective } from './registry';
import type { NotifyEntry }                 from './writelog';

// ── Config ────────────────────────────────────────────────────────────────

export const HOP_RADIUS = parseInt(process.env.HOP_RADIUS ?? '2', 10);

// Neo4j 5.x does not allow parameters as variable-length path bounds ([*1..$k]).
// We build the query string with k inlined. k comes from trusted config, not user input.
//
// BFS is constrained to CALLS edges only. The graph also has DEFINES (module
// membership) and USES_TYPE (shared parameter/return types) edges, but those
// connect every pair of co-located functions and every pair sharing a common
// type within 2 hops — making the notification fire on shared module membership
// or shared types rather than actual call dependencies. A signature change
// should reach callers and callees, not every function that happens to live in
// the same file or take a Request as a parameter.
function buildBfsQuery(k: number): string {
  return `
MATCH path = (start {id: $nodeId})-[:CALLS*1..${k}]-(neighbor)
WHERE neighbor.id <> $nodeId
RETURN DISTINCT neighbor.id AS nodeId,
       min(length(path))    AS distance
ORDER BY distance ASC
  `.trim();
}

// ── Types ─────────────────────────────────────────────────────────────────

export interface NotifyInput {
  writtenNodeId: string;
  writeId:       string; // UUID of the WriteEvent
  writerAgent:   string;
  timestamp:     string; // ISO-8601
  k?:            number; // override HOP_RADIUS (tests / future use)
}

// ── Algorithm 1 ───────────────────────────────────────────────────────────

export async function runNotify(input: NotifyInput): Promise<NotifyEntry[]> {
  const { writtenNodeId, writeId, writerAgent, timestamp } = input;
  const k = input.k ?? HOP_RADIUS;

  const notifySet: NotifyEntry[] = [];

  // ── Distance 0: agents that registered on the written node directly ──────
  for (const agentId of agentsReadingNode(writtenNodeId)) {
    if (agentId === writerAgent) continue; // writer doesn't notify itself
    enqueueDirective(agentId, {
      writeId,
      writerAgent,
      writtenNode: writtenNodeId,
      triggerNode: writtenNodeId,
      distance:    0,
      timestamp,
    });
    notifySet.push({ agent: agentId, trigger_node: writtenNodeId, distance: 0 });
  }

  // ── Distance 1..k: BFS neighbors ─────────────────────────────────────────
  const session = getSession();
  try {
    const result = await session.run(buildBfsQuery(k), { nodeId: writtenNodeId });

    for (const record of result.records) {
      const neighborId: string = record.get('nodeId');
      const distance: number   = neo4j.integer.toNumber(record.get('distance'));

      for (const agentId of agentsReadingNode(neighborId)) {
        if (agentId === writerAgent) continue;
        enqueueDirective(agentId, {
          writeId,
          writerAgent,
          writtenNode: writtenNodeId,
          triggerNode: neighborId,
          distance,
          timestamp,
        });
        notifySet.push({ agent: agentId, trigger_node: neighborId, distance });
      }
    }
  } finally {
    await session.close();
  }

  return notifySet;
}
