import { getSession } from '../neo4j';

export const getNodeTool = {
  name: 'get_node' as const,
  description: 'Fetch a node\'s properties by id. No registry side-effect.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      nodeId: { type: 'string' },
    },
    required: ['nodeId'],
  },
  async handler(input: { nodeId: string }) {
    const session = getSession();
    try {
      const result = await session.run(
        'MATCH (n {id: $nodeId}) RETURN n, labels(n)[0] AS label LIMIT 1',
        { nodeId: input.nodeId },
      );
      if (result.records.length === 0) return { node: null };
      const rec = result.records[0];
      return {
        node: {
          label: rec.get('label') as string,
          ...(rec.get('n').properties as Record<string, unknown>),
        },
      };
    } finally {
      await session.close();
    }
  },
};
