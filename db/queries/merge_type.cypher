// Batch MERGE Type nodes.  Called with { nodes: TypeNode[] }
UNWIND $nodes AS n
MERGE (t:Type {id: n.id})
SET t.name        = n.name,
    t.file        = n.file,
    t.kind        = n.kind,
    t.fields_hash = n.fields_hash,
    t.language    = n.language
