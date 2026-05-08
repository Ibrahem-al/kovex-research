// Kovex MCP Server — entry point.
// Registers all 8 tools with the MCP SDK and connects via stdio transport.
// Start with: npx ts-node server/index.ts

import { Server }               from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';

import { verifyConnectivity, driver } from './neo4j';

import { registerReadTool }    from './tools/register_read';
import { deregisterReadTool }  from './tools/deregister_read';
import { listReadsTool }       from './tools/list_reads';
import { commitWriteTool }     from './tools/commit_write';
import { getNodeTool }         from './tools/get_node';
import { getDependentsTool }   from './tools/get_dependents';
import { queryGraphTool }      from './tools/query_graph';
import { pollDirectivesTool }  from './tools/poll_directives';

// ── Tool registry ─────────────────────────────────────────────────────────

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const TOOLS: Array<{ name: string; description: string; inputSchema: any; handler: (i: any) => Promise<any> }> = [
  registerReadTool,
  deregisterReadTool,
  listReadsTool,
  commitWriteTool,
  getNodeTool,
  getDependentsTool,
  queryGraphTool,
  pollDirectivesTool,
];

const TOOL_MAP = new Map(TOOLS.map(t => [t.name, t]));

// ── Server setup ──────────────────────────────────────────────────────────

const server = new Server(
  { name: 'kovex', version: '0.1.0' },
  { capabilities: { tools: {} } },
);

// tools/list — return all tool definitions
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: TOOLS.map(t => ({
    name:        t.name,
    description: t.description,
    inputSchema: t.inputSchema,
  })),
}));

// tools/call — dispatch to the right handler
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const tool = TOOL_MAP.get(name);
  if (!tool) {
    return {
      content: [{ type: 'text' as const, text: `Unknown tool: ${name}` }],
      isError: true,
    };
  }
  try {
    const result = await tool.handler(args ?? {});
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(result, null, 2) }],
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return {
      content: [{ type: 'text' as const, text: `Error: ${message}` }],
      isError: true,
    };
  }
});

// ── Start ─────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  await verifyConnectivity();

  const transport = new StdioServerTransport();
  await server.connect(transport);

  // Log to stderr so it doesn't corrupt the stdio MCP stream
  process.stderr.write('Kovex MCP server running (stdio)\n');
  process.stderr.write(`Tools: ${TOOLS.map(t => t.name).join(', ')}\n`);
}

main().catch(err => {
  process.stderr.write(`Fatal: ${err}\n`);
  driver.close().finally(() => process.exit(1));
});
