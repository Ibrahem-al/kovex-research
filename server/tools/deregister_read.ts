import { deregisterRead } from '../registry';

export const deregisterReadTool = {
  name: 'deregister_read' as const,
  description: 'Remove a node from the agent\'s read set. The agent will no longer receive directives triggered by that node.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      agentId: { type: 'string' },
      nodeId:  { type: 'string' },
    },
    required: ['agentId', 'nodeId'],
  },
  async handler(input: { agentId: string; nodeId: string }) {
    const removed = deregisterRead(input.agentId, input.nodeId);
    return { success: removed };
  },
};
