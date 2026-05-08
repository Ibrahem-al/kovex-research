// Cross-file import resolution helpers.
// Converts Python dotted-module names to relative file paths so ingest.ts
// can look up the target Module node in Neo4j by its `path` property.

import * as path from 'path';

// "httpx._types"   → "httpx/_types.py"
// "httpx"          → "httpx/__init__.py"
// ".utils"         → resolved relative to callerFile
export function dottedToPath(
  moduleName: string,
  callerFile?: string,
): string {
  if (moduleName.startsWith('.')) {
    // Relative import: count leading dots
    const dots  = moduleName.match(/^\.+/)?.[0].length ?? 1;
    const rest  = moduleName.slice(dots);
    const base  = callerFile
      ? path.dirname(callerFile).replace(/\\/g, '/')
      : '';
    const up    = Array(dots - 1).fill('..').join('/');
    const prefix = up ? `${base}/${up}` : base;
    return rest
      ? `${prefix}/${rest.replace(/\./g, '/')}.py`
      : `${prefix}/__init__.py`;
  }

  // Absolute import
  const parts = moduleName.split('.');
  // If last segment looks like a symbol (starts uppercase), treat parent as module
  const isSymbol = /^[A-Z]/.test(parts[parts.length - 1]);
  const modParts = isSymbol ? parts.slice(0, -1) : parts;
  if (modParts.length === 0) return '';

  const base = modParts.join('/');
  // Prefer package (__init__.py) over bare module if name has no extension hint
  // ingest.ts uses MATCH on Module.path so both forms are tried
  return `${base}.py`;
}

// Build a map from dotted module name → Module.id (sha256 of path)
// so ingest.ts can resolve imports without extra DB reads.
import * as crypto from 'crypto';
import { ModuleNode } from './parse';

export function buildModuleIndex(modules: ModuleNode[]): Map<string, string> {
  const idx = new Map<string, string>();
  for (const m of modules) {
    idx.set(m.path, m.id);
    // Also index by dotted name: "httpx/_types.py" → "httpx._types"
    const dotted = m.path.replace(/\//g, '.').replace(/\.py$/, '');
    idx.set(dotted, m.id);
  }
  return idx;
}
