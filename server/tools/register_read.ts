import { registerRead } from '../registry';

export const registerReadTool = {
  name: 'register_read' as const,
  description: 'Register agent interest in a node. The agent will receive a directive if that node or any node within k hops is written to.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      agentId:  { type: 'string', description: 'Unique agent identifier' },
      nodeId:   { type: 'string', description: 'Graph node id (sha256)' },
      bodyHash: { type: 'string', description: 'body_hash of the node at read time' },
    },
    required: ['agentId', 'nodeId', 'bodyHash'],
  },
  async handler(input: { agentId: string; nodeId: string; bodyHash: string }) {
    const timestamp = new Date().toISOString();
    registerRead({ agentId: input.agentId, nodeId: input.nodeId, timestamp, bodyHash: input.bodyHash });
    return { success: true, timestamp };
  },
};
