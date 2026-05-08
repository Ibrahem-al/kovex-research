// Batch MERGE Function nodes.  Called with { nodes: FunctionNode[] }
UNWIND $nodes AS n
MERGE (f:Function {id: n.id})
SET f.name        = n.name,
    f.file        = n.file,
    f.signature   = n.signature,
    f.return_type = n.return_type,
    f.language    = n.language,
    f.body_hash   = n.body_hash
