import { listReads } from '../registry';

export const listReadsTool = {
  name: 'list_reads' as const,
  description: 'Return all nodes currently in the agent\'s read set.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      agentId: { type: 'string' },
    },
    required: ['agentId'],
  },
  async handler(input: { agentId: string }) {
    return { reads: listReads(input.agentId) };
  },
};
