// Algorithm 1 — k-hop BFS neighborhood query.
// Returns all nodes reachable from $nodeId within $k hops (undirected) along
// CALLS edges, together with their minimum hop distance.
// Used by server/notify.ts after a commit_write to find agents to notify.

// NOTE: Neo4j 5.x rejects $k as a path-length parameter.
// notify.ts builds this query with k inlined (e.g. [:CALLS*1..2]).
// This file shows the canonical form from the paper spec.
//
// Traversal is constrained to CALLS edges. DEFINES (module membership) and
// USES_TYPE (shared parameter/return types) edges create 2-hop paths between
// every pair of co-located functions and every pair sharing a common type,
// which is too permissive for signature-change notifications. Restricting to
// CALLS makes the algorithm fire on actual caller/callee dependencies.
MATCH path = (start {id: $nodeId})-[:CALLS*1..$k]-(neighbor)
WHERE neighbor.id <> $nodeId
RETURN DISTINCT neighbor.id AS nodeId,
       min(length(path))    AS distance
ORDER BY distance ASC
