"""Stack linker: cross-layer edges (endpoint, client, DTO, JPA, queue) for generic Spring/Next/Flutter/RabbitMQ stacks.

Edges point dependent -> dependency, like ``calls``, so ``graphify affected`` on the
dependency lists the dependents across layers.
"""
from __future__ import annotations

import json
from pathlib import Path

from .clients import link_clients, link_mirrors
from .common import GraphIndex
from .rabbit import link_java_queues, link_python_queues
from .spring import link_controllers, link_jpa

STACK_RELATIONS = (
    "handled_by", "calls_endpoint", "uses_dto", "mirrors", "maps_to",
    "repository_of", "fed_by", "consumes", "renders",
)


def link_graph_data(data: dict, root: Path) -> dict:
    """Add stack-link nodes/edges to a raw graph.json dict in place; return stats."""
    root = Path(root)
    idx = GraphIndex(data)
    stats: dict = {}
    link_controllers(idx, root)
    link_jpa(idx, root)
    link_clients(idx, root, stats)
    link_mirrors(idx)
    link_java_queues(idx, root)
    link_python_queues(idx, root)
    stats["relations"] = {r: idx.added.get(r, 0) for r in STACK_RELATIONS}
    stats["nodes_added"] = idx.added_nodes
    return stats


def link_graph_file(graph_path: Path, root: Path) -> dict:
    """Load graph.json, run the pass, write it back (arc order preserved: source -> target)."""
    graph_path = Path(graph_path)
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    if "nodes" not in data:
        raise ValueError(f"{graph_path} is not a graph.json")
    # Drop edges from a previous run so re-linking is idempotent.
    key = "links" if "links" in data else "edges"
    keep = [l for l in data.get(key, []) if l.get("_origin") != "stacklink"]
    data[key] = keep
    data["nodes"] = [n for n in data["nodes"] if n.get("_origin") != "stacklink"]
    stats = link_graph_data(data, root)
    tmp = graph_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(graph_path)
    return stats
