"""Shared helpers for the stack linker: graph index, path normalisation, file walking."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

SKIP_DIRS = {
    "node_modules", ".git", ".next", "build", "dist", "target", ".dart_tool",
    "venv", ".venv", "__pycache__", ".claude", ".playwright-mcp", "graphify-out",
    ".gradle", "out", "coverage",
}


def walk(root: Path, suffixes: tuple[str, ...]):
    """Yield source files under root with one of the suffixes, skipping build dirs."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.is_symlink():
                    stack.append(p)
            elif p.suffix in suffixes:
                yield p


def rel(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


_VAR = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\{[^}]*\}|(?<=/):[A-Za-z_][A-Za-z0-9_]*")


def normalize_path(path: str) -> str:
    """Collapse every path variable to ``{}``; drop query string / trailing slash."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = _VAR.sub("{}", path)
    path = re.sub(r"/{2,}", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def endpoint_id(method: str, path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", path.lower().replace("{}", "p")).strip("_")
    return f"endpoint_{method.lower()}_{slug}"


class GraphIndex:
    """Lookup helpers over the raw graph.json dict, plus a buffer of additions."""

    def __init__(self, data: dict):
        self.data = data
        self.nodes: list[dict] = data["nodes"]
        self.links_key = "links" if "links" in data else "edges"
        self.links: list[dict] = data.setdefault(self.links_key, [])
        self.ids = {n["id"] for n in self.nodes}
        self.by_file: dict[str, list[dict]] = defaultdict(list)
        self.classes: dict[str, list[dict]] = defaultdict(list)
        for n in self.nodes:
            sf = n.get("source_file")
            if sf:
                self.by_file[sf].append(n)
            if n.get("_callable_class") and sf and sf.endswith((".java", ".ts", ".tsx", ".dart")):
                self.classes[n.get("label", "")].append(n)
        self._edge_keys = {(l["source"], l["target"], l.get("relation")) for l in self.links}
        self.added_nodes = 0
        self.added: dict[str, int] = defaultdict(int)

    @staticmethod
    def lineno(node: dict) -> int:
        m = re.match(r"L(\d+)", str(node.get("source_location") or ""))
        return int(m.group(1)) if m else 0

    def file_node(self, sf: str) -> dict | None:
        cands = [n for n in self.by_file.get(sf, []) if str(n.get("source_location") or "") in ("L1", "")
                 and n.get("label") == Path(sf).name]
        if not cands:
            cands = [n for n in self.by_file.get(sf, []) if n.get("label") == Path(sf).name]
        return cands[0] if cands else None

    def callables(self, sf: str) -> list[dict]:
        return sorted((n for n in self.by_file.get(sf, []) if n.get("_callable") and self.lineno(n)),
                      key=self.lineno)

    def enclosing(self, sf: str, line: int) -> dict | None:
        """Innermost-starting callable node at or before ``line`` (file node fallback)."""
        best = None
        for n in self.callables(sf):
            if n.get("_callable_class"):
                continue
            if self.lineno(n) <= line:
                best = n
            else:
                break
        return best or self.file_node(sf)

    def method_at(self, sf: str, name: str, line: int) -> dict | None:
        """Method node named ``name`` in file; nearest start line at/after ``line`` wins."""
        cands = [n for n in self.by_file.get(sf, [])
                 if n.get("_callable") and not n.get("_callable_class")
                 and n.get("label") in (f".{name}()", f"{name}()")]
        if not cands:
            return None
        after = [n for n in cands if self.lineno(n) >= line]
        return min(after, key=self.lineno) if after else max(cands, key=self.lineno)

    def add_node(self, node: dict) -> None:
        if node["id"] in self.ids:
            return
        node.setdefault("file_type", "code")
        node.setdefault("_origin", "stacklink")
        node.setdefault("norm_label", node.get("label", "").casefold())
        self.nodes.append(node)
        self.ids.add(node["id"])
        self.added_nodes += 1

    def add_edge(self, src: str, tgt: str, relation: str, *, source_file: str | None = None,
                 line: int | None = None, confidence: str = "EXTRACTED") -> bool:
        key = (src, tgt, relation)
        if src == tgt or key in self._edge_keys or src not in self.ids or tgt not in self.ids:
            return False
        e = {"source": src, "target": tgt, "relation": relation, "_origin": "stacklink",
             "confidence": confidence, "confidence_score": 1.0 if confidence == "EXTRACTED" else 0.75,
             "weight": 1.0}
        if source_file:
            e["source_file"] = source_file
        if line:
            e["source_location"] = f"L{line}"
        self.links.append(e)
        self._edge_keys.add(key)
        self.added[relation] += 1
        return True

    def unique_class(self, name: str, suffix: str) -> dict | None:
        c = [n for n in self.classes.get(name, []) if n["source_file"].endswith(suffix)]
        return c[0] if len(c) == 1 else None
