// In-memory Read Registry and per-agent Directive Queue.
// Single module-level singleton — shared across all MCP tool handlers.

export interface ReadEntry {
  agentId:   string;
  nodeId:    string;
  timestamp: string; // ISO-8601
  bodyHash:  string; // body_hash of the node at registration time
}

export interface Directive {
  writeId:     string; // UUID of the triggering WriteEvent
  writerAgent: string;
  writtenNode: string; // node that was actually written
  triggerNode: string; // node in RS(agent) that is within k hops
  distance:    number;
  timestamp:   string; // ISO-8601
}

// ── Registry ──────────────────────────────────────────────────────────────

// agentId → nodeId → ReadEntry
const _reads = new Map<string, Map<string, ReadEntry>>();

// agentId → Directive[]
const _directives = new Map<string, Directive[]>();

export function registerRead(entry: ReadEntry): void {
  if (!_reads.has(entry.agentId)) _reads.set(entry.agentId, new Map());
  _reads.get(entry.agentId)!.set(entry.nodeId, entry);
}

export function deregisterRead(agentId: string, nodeId: string): boolean {
  return _reads.get(agentId)?.delete(nodeId) ?? false;
}

export function listReads(agentId: string): ReadEntry[] {
  return [...(_reads.get(agentId)?.values() ?? [])];
}

// Return every agentId whose ReadSet contains nodeId.
// Called by notify.ts after a commit_write.
export function agentsReadingNode(nodeId: string): string[] {
  const out: string[] = [];
  for (const [agentId, nodeMap] of _reads) {
    if (nodeMap.has(nodeId)) out.push(agentId);
  }
  return out;
}

// ── Directive queue ────────────────────────────────────────────────────────

export function enqueueDirective(agentId: string, directive: Directive): void {
  if (!_directives.has(agentId)) _directives.set(agentId, []);
  _directives.get(agentId)!.push(directive);
}

// Returns and clears the queue (destructive read — matches poll semantics).
export function pollDirectives(agentId: string): Directive[] {
  const queue = _directives.get(agentId) ?? [];
  _directives.delete(agentId);
  return queue;
}

// ── Introspection (for tests / MCP inspector) ──────────────────────────────

export function snapshot(): { reads: Record<string, string[]>; queueLengths: Record<string, number> } {
  const reads: Record<string, string[]> = {};
  for (const [agentId, nodeMap] of _reads) reads[agentId] = [...nodeMap.keys()];

  const queueLengths: Record<string, number> = {};
  for (const [agentId, q] of _directives) queueLengths[agentId] = q.length;

  return { reads, queueLengths };
}
