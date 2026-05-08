// Deviation: web-tree-sitter (WASM) used instead of native tree-sitter.
// Reason: native tree-sitter fails to compile on Node.js v24 (requires C++20
// but the binding uses C++17). web-tree-sitter is explicitly allowed by spec.

import * as TreeSitter from 'web-tree-sitter';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';

// ── Exported IR types ─────────────────────────────────────────────────────

export interface FunctionNode {
  id: string;
  name: string;
  file: string;
  signature: string;
  return_type: string;
  language: 'python';
  body_hash: string;
}

export interface TypeNode {
  id: string;
  name: string;
  file: string;
  kind: 'class';
  fields_hash: string;
  language: 'python';
}

export interface ModuleNode {
  id: string;
  path: string;
  language: 'python';
  exports: string; // JSON array of exported names
}

export interface SymbolNode {
  id: string;
  name: string;
  file: string;
  type_annotation: string;
  kind: 'variable' | 'constant';
}

export interface ImportInfo {
  fromModule: string | null;
  symbols: string[];
}

export interface CallInfo {
  callerName: string;
  calleeName: string;
  line: number;
}

export interface TypeUseInfo {
  functionName: string;
  typeName: string;
  role: 'param' | 'return' | 'local';
}

export interface InheritInfo {
  childName: string;
  parentName: string;
}

export interface ParsedFile {
  module: ModuleNode;
  functions: FunctionNode[];
  types: TypeNode[];
  symbols: SymbolNode[];
  imports: ImportInfo[];
  calls: CallInfo[];
  typeUses: TypeUseInfo[];
  inherits: InheritInfo[];
}

// ── Parser singleton ──────────────────────────────────────────────────────

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type N = any; // web-tree-sitter Node — typed as any to avoid d.ts version skew

let _parser: TreeSitter.Parser | null = null;

export async function initParser(): Promise<void> {
  if (_parser) return;

  await TreeSitter.Parser.init({
    locateFile(scriptName: string) {
      return path.join(__dirname, '..', 'node_modules', 'web-tree-sitter', scriptName);
    },
  });

  const Python = await TreeSitter.Language.load(
    path.join(__dirname, '..', 'node_modules', 'tree-sitter-python', 'tree-sitter-python.wasm'),
  );

  _parser = new TreeSitter.Parser();
  _parser.setLanguage(Python);
}

// ── Helpers ───────────────────────────────────────────────────────────────

function sha256(s: string): string {
  return crypto.createHash('sha256').update(s, 'utf8').digest('hex');
}

function mkId(name: string, file: string): string {
  return sha256(name + '\x00' + file);
}

const BUILTIN_TYPES = new Set([
  'int', 'str', 'float', 'bool', 'bytes', 'list', 'dict', 'set', 'tuple',
  'None', 'Any', 'Optional', 'Union', 'List', 'Dict', 'Set', 'Tuple',
  'Callable', 'Iterator', 'Generator', 'Type', 'ClassVar',
]);

function isTypeName(name: string): boolean {
  return /^[A-Z]/.test(name) || BUILTIN_TYPES.has(name);
}

function collectTypeNames(node: N): string[] {
  if (!node) return [];
  const out: string[] = [];
  function walk(n: N) {
    if (n.type === 'identifier' && isTypeName(n.text)) out.push(n.text as string);
    for (const c of n.namedChildren as N[]) walk(c);
  }
  walk(node);
  return [...new Set(out)];
}

function collectCalls(body: N, callerName: string, className: string | null, out: CallInfo[]): void {
  function walk(n: N) {
    if (n.type === 'call') {
      const fn = n.childForFieldName('function') as N | null;
      if (fn) {
        let callee: string | null = null;
        if (fn.type === 'attribute') {
          const obj  = fn.childForFieldName('object')    as N | null;
          const attr = fn.childForFieldName('attribute') as N | null;
          if (attr) {
            // self.foo() inside a class method → resolve to ClassName.foo
            if (className && (obj?.text as string | undefined) === 'self') {
              callee = `${className}.${attr.text as string}`;
            } else {
              // non-self attribute call: record only the method name (best-effort)
              callee = attr.text as string;
            }
          }
        } else if (fn.type === 'identifier') {
          callee = fn.text as string;
        }
        // Allow plain names and ClassName.method patterns; skip operators / builtins
        if (callee && /^[A-Za-z_][A-Za-z0-9_.]*$/.test(callee)) {
          out.push({ callerName, calleeName: callee, line: (n.startPosition.row as number) + 1 });
        }
      }
    }
    for (const c of n.namedChildren as N[]) walk(c);
  }
  walk(body);
}

function extractSelfFields(initBody: N): { name: string; type: string }[] {
  const fields: { name: string; type: string }[] = [];
  function walk(n: N) {
    if (n.type === 'annotated_assignment') {
      const lhs = n.namedChildren[0] as N;
      const ann = n.namedChildren[1] as N | undefined;
      if (lhs?.type === 'attribute') {
        const obj  = lhs.childForFieldName('object') as N | null;
        const attr = lhs.childForFieldName('attribute') as N | null;
        if ((obj?.text as string) === 'self' && attr) {
          fields.push({ name: attr.text as string, type: ann?.text ?? 'Any' });
        }
      }
    }
    if (n.type === 'assignment') {
      const lhs = n.childForFieldName('left') as N | null;
      if (lhs?.type === 'attribute') {
        const obj  = lhs.childForFieldName('object') as N | null;
        const attr = lhs.childForFieldName('attribute') as N | null;
        if ((obj?.text as string) === 'self' && attr && !fields.some(f => f.name === (attr!.text as string))) {
          fields.push({ name: attr.text as string, type: 'Any' });
        }
      }
    }
    for (const c of n.namedChildren as N[]) walk(c);
  }
  walk(initBody);
  return fields;
}

function extractClassFields(classBody: N): { name: string; type: string }[] {
  const fields: { name: string; type: string }[] = [];
  for (const child of classBody.namedChildren as N[]) {
    if (child.type === 'annotated_assignment') {
      const lhs = child.namedChildren[0] as N;
      const ann = child.namedChildren[1] as N | undefined;
      if (lhs?.type === 'identifier') {
        fields.push({ name: lhs.text as string, type: ann?.text ?? 'Any' });
      }
    }
    if (child.type === 'function_definition') {
      const mname = (child.childForFieldName('name') as N | null)?.text as string | undefined;
      if (mname === '__init__') {
        const body = child.childForFieldName('body') as N | null;
        if (body) fields.push(...extractSelfFields(body));
      }
    }
  }
  return fields;
}

function extractFunction(
  node: N,
  file: string,
  namePrefix: string,
  calls: CallInfo[],
  typeUses: TypeUseInfo[],
): FunctionNode {
  const rawName    = (node.childForFieldName('name') as N | null)?.text as string ?? '';
  const name       = namePrefix ? `${namePrefix}.${rawName}` : rawName;
  const paramsNode = node.childForFieldName('parameters') as N | null;
  const retNode    = node.childForFieldName('return_type') as N | null;
  const body       = node.childForFieldName('body') as N | null;

  const signature  = (paramsNode?.text as string | undefined) ?? '()';
  const returnType = (retNode?.text as string | undefined) ?? '';
  const bodyHash   = sha256(signature + '\x00' + returnType);

  if (body) collectCalls(body, name, namePrefix || null, calls);

  if (paramsNode) {
    for (const t of collectTypeNames(paramsNode)) {
      typeUses.push({ functionName: name, typeName: t, role: 'param' });
    }
  }
  if (retNode) {
    for (const t of collectTypeNames(retNode)) {
      typeUses.push({ functionName: name, typeName: t, role: 'return' });
    }
  }

  return { id: mkId(name, file), name, file, signature, return_type: returnType, language: 'python', body_hash: bodyHash };
}

// ── Main parse function ───────────────────────────────────────────────────

export function parseFile(absolutePath: string, repoRoot: string): ParsedFile {
  if (!_parser) throw new Error('Parser not initialized — call initParser() first.');

  const source       = fs.readFileSync(absolutePath, 'utf8');
  const relativePath = path.relative(repoRoot, absolutePath).replace(/\\/g, '/');
  const tree         = _parser.parse(source);

  if (!tree) {
    return {
      module: { id: sha256(relativePath), path: relativePath, language: 'python', exports: '[]' },
      functions: [], types: [], symbols: [], imports: [], calls: [], typeUses: [], inherits: [],
    };
  }

  const functions: FunctionNode[] = [];
  const types: TypeNode[]         = [];
  const symbols: SymbolNode[]     = [];
  const imports: ImportInfo[]     = [];
  const calls: CallInfo[]         = [];
  const typeUses: TypeUseInfo[]   = [];
  const inherits: InheritInfo[]   = [];
  const exportedNames: string[]   = [];

  for (const node of (tree.rootNode.namedChildren as N[])) {
    // ── Top-level function ───────────────────────────────────────────────
    if (node.type === 'function_definition') {
      const fn = extractFunction(node, relativePath, '', calls, typeUses);
      functions.push(fn);
      exportedNames.push(fn.name);
    }

    // ── Top-level class ──────────────────────────────────────────────────
    if (node.type === 'class_definition') {
      const className = (node.childForFieldName('name') as N | null)?.text as string ?? '';
      const body      = node.childForFieldName('body') as N | null;
      const argList: N | null =
        node.childForFieldName('argument_list') as N | null ??
        (node.namedChildren as N[]).find((c: N) => c.type === 'argument_list') ?? null;

      const fields     = body ? extractClassFields(body) : [];
      const sorted     = [...fields].sort((a, b) => a.name.localeCompare(b.name));
      const fieldsHash = sha256(sorted.map(f => `${f.name}:${f.type}`).join(','));

      types.push({
        id: mkId(className, relativePath),
        name: className,
        file: relativePath,
        kind: 'class',
        fields_hash: fieldsHash,
        language: 'python',
      });
      exportedNames.push(className);

      if (argList) {
        for (const base of argList.namedChildren as N[]) {
          const baseName: string | null =
            base.type === 'identifier'     ? base.text as string
            : base.type === 'attribute'    ? ((base.childForFieldName('attribute') as N | null)?.text as string ?? null)
            : null;
          if (baseName && baseName !== 'object') {
            inherits.push({ childName: className, parentName: baseName });
          }
        }
      }

      if (body) {
        for (const child of body.namedChildren as N[]) {
          if (child.type === 'function_definition') {
            functions.push(extractFunction(child, relativePath, className, calls, typeUses));
          }
        }
      }
    }

    // ── import X ─────────────────────────────────────────────────────────
    if (node.type === 'import_statement') {
      for (const child of node.namedChildren as N[]) {
        const modName: string =
          child.type === 'aliased_import'
            ? ((child.namedChildren as N[])[0]?.text as string) ?? child.text as string
            : child.text as string;
        imports.push({ fromModule: null, symbols: [modName] });
      }
    }

    // ── from X import Y, Z ───────────────────────────────────────────────
    if (node.type === 'import_from_statement') {
      const modNode = node.childForFieldName('module_name') as N | null;
      const fromMod = (modNode?.text as string | undefined) ?? null;
      const syms: string[] = [];
      for (const child of node.namedChildren as N[]) {
        if (child === modNode) continue;
        if (child.type === 'wildcard_import') { syms.push('*'); continue; }
        const sym: string | null =
          child.type === 'aliased_import'
            ? ((child.namedChildren as N[])[0]?.text as string | null) ?? null
            : (child.type === 'identifier' || child.type === 'dotted_name')
            ? child.type as string
            : null;
        if (sym) syms.push(child.type === 'identifier' || child.type === 'dotted_name'
          ? child.text as string : sym);
      }
      if (syms.length > 0) imports.push({ fromModule: fromMod, symbols: syms });
    }

    // ── Module-level annotated assignment: x: Type = value ───────────────
    if (node.type === 'annotated_assignment') {
      const target = (node.namedChildren as N[])[0];
      const ann    = (node.namedChildren as N[])[1];
      if (target?.type === 'identifier') {
        const name    = target.text as string;
        const isConst = /^[A-Z_][A-Z0-9_]*$/.test(name);
        symbols.push({
          id: mkId(name, relativePath), name, file: relativePath,
          type_annotation: (ann?.text as string | undefined) ?? '',
          kind: isConst ? 'constant' : 'variable',
        });
        exportedNames.push(name);
      }
    }

    // ── Module-level plain assignment: X = value ─────────────────────────
    if (node.type === 'expression_statement') {
      const expr = (node.namedChildren as N[])[0];
      if (expr?.type === 'assignment') {
        const lhs = expr.childForFieldName('left') as N | null;
        if (lhs?.type === 'identifier') {
          const name    = lhs.text as string;
          const isConst = /^[A-Z_][A-Z0-9_]*$/.test(name);
          symbols.push({
            id: mkId(name, relativePath), name, file: relativePath,
            type_annotation: '',
            kind: isConst ? 'constant' : 'variable',
          });
          exportedNames.push(name);
        }
      }
    }
  }

  return {
    module: {
      id: sha256(relativePath),
      path: relativePath,
      language: 'python',
      exports: JSON.stringify(exportedNames),
    },
    functions, types, symbols, imports, calls, typeUses, inherits,
  };
}
