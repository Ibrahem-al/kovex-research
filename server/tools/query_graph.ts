import { getSession } from '../neo4j';

// Simple prefix guard — prevents mutation queries from agents.
// Production would use a read-only transaction; this is sufficient for the prototype.
const READ_ONLY = /^\s*(MATCH|RETURN|WITH|CALL|SHOW|EXPLAIN|PROFILE)/i;

export const queryGraphTool = {
  name: 'query_graph' as const,
  description: 'Execute a read-only Cypher query against the graph and return results as plain objects.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      cypher: { type: 'string', description: 'Read-only Cypher (MATCH/RETURN/WITH/CALL/SHOW)' },
      params: { type: 'object', description: 'Optional query parameters' },
    },
    required: ['cypher'],
  },
  async handler(input: { cypher: string; params?: Record<string, unknown> }) {
    if (!READ_ONLY.test(input.cypher)) {
      throw new Error('query_graph only accepts read-only Cypher (MATCH, RETURN, WITH, CALL, SHOW).');
    }
    const session = getSession();
    try {
      const result = await session.run(input.cypher, input.params ?? {});
      return {
        records: result.records.map(r => {
          const obj: Record<string, unknown> = {};
          for (const key of r.keys as string[]) {
            const val = r.get(key);
            // Unwrap Neo4j Node/Relationship objects to plain properties
            obj[key] = (val !== null && typeof val === 'object' && 'properties' in val)
              ? (val as { properties: unknown }).properties
              : val;
          }
          return obj;
        }),
      };
    } finally {
      await session.close();
    }
  },
};
