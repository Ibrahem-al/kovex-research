import neo4j from 'neo4j-driver';
import { getSession } from '../neo4j';
import { HOP_RADIUS } from '../notify';

export const getDependentsTool = {
  name: 'get_dependents' as const,
  description: 'Return all nodes within k hops of the given node, with distances and properties. Uses the same BFS as the notification algorithm.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      nodeId: { type: 'string' },
      k:      { type: 'number', description: `Hop radius (default: ${HOP_RADIUS})` },
    },
    required: ['nodeId'],
  },
  async handler(input: { nodeId: string; k?: number }) {
    const k = input.k ?? HOP_RADIUS;
    const session = getSession();
    try {
      const result = await session.run(
        `MATCH path = (start {id: $nodeId})-[*1..${k}]-(neighbor)
         WHERE neighbor.id <> $nodeId
         WITH neighbor, min(length(path)) AS distance
         RETURN neighbor.id AS nodeId, distance, properties(neighbor) AS props
         ORDER BY distance ASC`,
        { nodeId: input.nodeId },
      );
      return {
        dependents: result.records.map(r => ({
          nodeId:     r.get('nodeId') as string,
          distance:   neo4j.integer.toNumber(r.get('distance')),
          properties: r.get('props') as Record<string, unknown>,
        })),
      };
    } finally {
      await session.close();
    }
  },
};
