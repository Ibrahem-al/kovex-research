// Batch MERGE Module nodes.  Called with { nodes: ModuleNode[] }
UNWIND $nodes AS n
MERGE (m:Module {id: n.id})
SET m.path     = n.path,
    m.language = n.language,
    m.exports  = n.exports
