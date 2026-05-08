import { initParser, parseFile } from './parse';
import * as path from 'path';

async function main() {
  const repoRoot = path.resolve(__dirname, '..', 'httpx');
  const target   = path.join(repoRoot, 'httpx', '_client.py');

  console.log('Initializing parser...');
  await initParser();
  console.log('OK');

  console.log(`Parsing ${target} ...`);
  const result = parseFile(target, repoRoot);

  console.log(`\nModule:    ${result.module.path}`);
  console.log(`Functions: ${result.functions.length}`);
  console.log(`Types:     ${result.types.length}`);
  console.log(`Symbols:   ${result.symbols.length}`);
  console.log(`Imports:   ${result.imports.length}`);
  console.log(`Calls:     ${result.calls.length}`);
  console.log(`TypeUses:  ${result.typeUses.length}`);
  console.log(`Inherits:  ${result.inherits.length}`);

  console.log('\nSample functions:');
  result.functions.slice(0, 5).forEach(f =>
    console.log(`  ${f.name}${f.signature} ${f.return_type ? '-> ' + f.return_type : ''}`)
  );

  console.log('\nSample types:');
  result.types.forEach(t => console.log(`  class ${t.name}`));

  console.log('\nSample inherits:');
  result.inherits.forEach(i => console.log(`  ${i.childName} -> ${i.parentName}`));
}

main().catch(err => { console.error(err); process.exit(1); });
