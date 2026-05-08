// Main coordination tool.
// 1. Fetch current node from Neo4j → h_pre
// 2. Apply patch properties → recompute body_hash / fields_hash → h_post
// 3. Persist updated node
// 4. Run BFS notification algorithm
// 5. Append WriteEvent to log
// 6. Return { write_id, notify_set }

import * as crypto from 'crypto';
import { getSession } from '../neo4j';
import { runNotify, HOP_RADIUS } from '../notify';
import { appendWriteEvent } from '../writelog';

function sha256(s: string): string {
  return crypto.createHash('sha256').update(s, 'utf8').digest('hex');
}

type Patch = Record<string, string | number | boolean>;

// Allowed property keys per label — guards against arbitrary key injection.
const ALLOWED_PROPS: Record<string, Set<string>> = {
  Function: new Set(['name', 'signature', 'return_type', 'language', 'body_hash']),
  Type:     new Set(['name', 'kind', 'fields_hash', 'language']),
  Module:   new Set(['path', 'language', 'exports']),
  Symbol:   new Set(['name', 'type_annotation', 'kind']),
};

export const commitWriteTool = {
  name: 'commit_write' as const,
  description: 'Apply a property patch to a node, trigger BFS notifications, log the write event, and return the notify_set.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      agentId: { type: 'string' },
      nodeId:  { type: 'string' },
      patch: {
        type: 'object',
        description: 'Property updates. Function: signature/return_type. Type: fields_hash. Module: exports.',
      },
    },
    required: ['agentId', 'nodeId', 'patch'],
  },

  async handler(input: { agentId: string; nodeId: string; patch: Patch }) {
    const { agentId, nodeId, patch } = input;
    const session = getSession();

    try {
      // ── 1. Fetch current node ──────────────────────────────────────────
      const fetchRes = await session.run(
        'MATCH (n {id: $nodeId}) RETURN n, labels(n)[0] AS label LIMIT 1',
        { nodeId },
      );
      if (fetchRes.records.length === 0) {
        throw new Error(`Node not found: ${nodeId}`);
      }
      const rec      = fetchRes.records[0];
      const label    = rec.get('label') as string;
      const current  = rec.get('n').properties as Record<string, string>;
      const nodeName = current.name ?? nodeId;
      const hPre     = current.body_hash ?? current.fields_hash ?? '';

      // ── 2. Validate patch keys ─────────────────────────────────────────
      const allowed = ALLOWED_PROPS[label] ?? new Set<string>();
      for (const key of Object.keys(patch)) {
        if (!allowed.has(key)) {
          throw new Error(`Patch key '${key}' is not allowed for ${label} nodes.`);
        }
      }

      // ── 3. Merge patch and recompute contract hash ─────────────────────
      const updated: Record<string, string | number | boolean> = { ...current, ...patch };
      let hPost = hPre;

      if (label === 'Function') {
        const sig = String(updated.signature   ?? '');
        const ret = String(updated.return_type ?? '');
        hPost = sha256(sig + '\x00' + ret);
        updated.body_hash = hPost;
      } else if (label === 'Type' && patch.fields_hash) {
        hPost = String(patch.fields_hash);
      }

      // ── 4. Persist to Neo4j ────────────────────────────────────────────
      // Build SET clause from allowed keys only (excludes id, file, language)
      const patchKeys = Object.keys(patch);
      const extraKeys = label === 'Function' ? ['body_hash'] : [];
      const setKeys   = [...new Set([...patchKeys, ...extraKeys])];
      const setClauses = setKeys.map(k => `n.${k} = $${k}`).join(', ');
      const setParams  = Object.fromEntries(setKeys.map(k => [k, updated[k]]));

      await session.run(
        `MATCH (n {id: $nodeId}) SET ${setClauses}`,
        { nodeId, ...setParams },
      );

      // ── 5. Notify ──────────────────────────────────────────────────────
      const writeId   = crypto.randomUUID();
      const timestamp = new Date().toISOString();
      const notifySet = await runNotify({ writtenNodeId: nodeId, writeId, writerAgent: agentId, timestamp });

      // ── 6. Log ────────────────────────────────────────────────────────
      appendWriteEvent({
        write_id:     writeId,
        agent_writer: agentId,
        node_id:      nodeId,
        node_label:   label,
        node_name:    nodeName,
        h_pre:        hPre,
        h_post:       hPost,
        t_w:          timestamp,
        k:            HOP_RADIUS,
        notify_set:   notifySet,
      });

      return { write_id: writeId, h_pre: hPre, h_post: hPost, notify_set: notifySet };
    } finally {
      await session.close();
    }
  },
};
