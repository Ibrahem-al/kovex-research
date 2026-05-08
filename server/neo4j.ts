import neo4j, { Driver, Session } from 'neo4j-driver';

const NEO4J_URI  = process.env.NEO4J_URI  ?? 'bolt://localhost:7687';
const NEO4J_USER = process.env.NEO4J_USER ?? 'neo4j';
const NEO4J_PASS = process.env.NEO4J_PASS ?? 'kovexpassword';

export const driver: Driver = neo4j.driver(
  NEO4J_URI,
  neo4j.auth.basic(NEO4J_USER, NEO4J_PASS)
);

export function getSession(): Session {
  return driver.session({ database: 'neo4j' });
}

export async function verifyConnectivity(): Promise<void> {
  await driver.verifyConnectivity();
  process.stderr.write(`OK  Connected to Neo4j at ${NEO4J_URI}\n`);
}

// Allow running directly: npx ts-node server/neo4j.ts
if (require.main === module) {
  verifyConnectivity()
    .then(() => process.exit(0))
    .catch((err: Error) => {
      console.error('FAIL', err.message);
      process.exit(1);
    });
}
