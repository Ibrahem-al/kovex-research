// Kovex Graph Schema — Node Constraints and Indexes
// Apply once after Neo4j starts: docker exec <neo4j-container> cypher-shell -u neo4j -p kovexpassword -f /var/lib/neo4j/import/db/schema.cypher
// Or let the neo4j-init service in docker-compose.yml handle it automatically.

// ── Uniqueness Constraints ─────────────────────────────────────────────────
CREATE CONSTRAINT fn_id  IF NOT EXISTS FOR (n:Function) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT ty_id  IF NOT EXISTS FOR (n:Type)     REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT mod_id IF NOT EXISTS FOR (n:Module)   REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT sym_id IF NOT EXISTS FOR (n:Symbol)   REQUIRE n.id IS UNIQUE;

// ── Lookup Indexes ─────────────────────────────────────────────────────────
CREATE INDEX fn_name  IF NOT EXISTS FOR (n:Function) ON (n.name);
CREATE INDEX fn_file  IF NOT EXISTS FOR (n:Function) ON (n.file);
CREATE INDEX ty_name  IF NOT EXISTS FOR (n:Type)     ON (n.name);
CREATE INDEX mod_path IF NOT EXISTS FOR (n:Module)   ON (n.path);
