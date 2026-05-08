import { pollDirectives } from '../registry';

export const pollDirectivesTool = {
  name: 'poll_directives' as const,
  description: 'Return and clear the agent\'s directive queue. Each directive means a node the agent depends on was written to. Destructive read — the queue is empty after this call.',
  inputSchema: {
    type: 'object' as const,
    properties: {
      agentId: { type: 'string' },
    },
    required: ['agentId'],
  },
  async handler(input: { agentId: string }) {
    return { directives: pollDirectives(input.agentId) };
  },
};
