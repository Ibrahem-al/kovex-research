// Append-only JSON-L write log.
// One WriteEvent object per line in logs/write_events.jsonl.

import * as fs   from 'fs';
import * as path from 'path';

// ── Types (match spec §WriteEvent log record format exactly) ──────────────

export interface NotifyEntry {
  agent:        string;
  trigger_node: string;
  distance:     number;
}

export interface WriteEvent {
  write_id:     string; // UUID v4
  agent_writer: string;
  node_id:      string;
  node_label:   string; // "Function" | "Type" | "Module" | "Symbol"
  node_name:    string;
  h_pre:        string; // body_hash before write
  h_post:       string; // body_hash after write
  t_w:          string; // ISO-8601 timestamp
  k:            number; // hop radius used
  notify_set:   NotifyEntry[];
}

// ── Log path ──────────────────────────────────────────────────────────────

function resolveLogPath(): string {
  return process.env.LOG_PATH
    ?? path.join(process.cwd(), 'logs', 'write_events.jsonl');
}

// ── Public API ─────────────────────────────────────────────────────────────

export function appendWriteEvent(event: WriteEvent): void {
  const logPath = resolveLogPath();
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  fs.appendFileSync(logPath, JSON.stringify(event) + '\n', 'utf8');
}

// Read all events back (for eval scripts / debugging).
export function readAllEvents(): WriteEvent[] {
  const logPath = resolveLogPath();
  if (!fs.existsSync(logPath)) return [];
  return fs
    .readFileSync(logPath, 'utf8')
    .split('\n')
    .filter(l => l.trim())
    .map(l => JSON.parse(l) as WriteEvent);
}
