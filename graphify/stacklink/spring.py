"""Spring MVC endpoints, DTO usage, JPA entities/repositories (regex heuristics)."""
from __future__ import annotations

import re
from pathlib import Path

from .common import GraphIndex, endpoint_id, line_of, normalize_path, read, rel, snake, walk

_MAPPING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b")
_ANNOT = re.compile(r"@[A-Za-z_][\w.]*")
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_IDENT = re.compile(r"[A-Z][A-Za-z0-9_]*")
_JAVA_SKIP = {"String", "Object", "List", "Map", "Set", "Page", "ResponseEntity", "Optional",
              "Integer", "Long", "Boolean", "Double", "UUID", "Void", "Collection", "Iterable"}


def _balanced(text: str, i: int) -> int:
    """Index just past the ``(...)`` group that starts at ``text[i] == '('``."""
    depth, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_annotations(text: str, i: int) -> int:
    n = len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        m = _ANNOT.match(text, i)
        if not m:
            return i
        i = m.end()
        j = i
        while j < n and text[j] in " \t":
            j += 1
        if j < n and text[j] == "(":
            i = _balanced(text, j)


def _parse_args(args: str) -> tuple[list[str], list[str]]:
    """Return (paths, http_methods) from a mapping annotation's argument text."""
    methods = re.findall(r"RequestMethod\.([A-Z]+)", args)
    pm = re.search(r"\b(?:value|path)\s*=\s*(\{[^}]*\}|\"(?:[^\"\\]|\\.)*\")", args)
    if pm:
        paths = _STR.findall(pm.group(1))
    else:
        head = args.split("method", 1)[0]
        paths = _STR.findall(head)
    return paths, methods


def _join(prefix: str, path: str) -> str:
    return normalize_path((prefix.rstrip("/") + "/" + path.lstrip("/")) if (prefix or path) else "/")


def _type_idents(t: str) -> list[str]:
    return [x for x in _IDENT.findall(t) if x not in _JAVA_SKIP]


def _class_prefixes(text: str) -> tuple[list[str], int]:
    """Class-level @RequestMapping prefixes and the offset where the class body starts."""
    for m in _MAPPING.finditer(text):
        end = m.end()
        args = ""
        if end < len(text) and text[end:end + 1] == "(":
            close = _balanced(text, end)
            args, end = text[end + 1:close - 1], close
        after = _skip_annotations(text, end)
        if re.match(r"(?:public\s+|abstract\s+|final\s+)*(?:class|interface)\b", text[after:after + 60]):
            return (_parse_args(args)[0] or [""]), after
    return [""], 0


def link_controllers(idx: GraphIndex, root: Path) -> None:
    for p in walk(root, (".java",)):
        text = read(p)
        if "Mapping" not in text or not re.search(r"@(Rest)?Controller\b", text):
            continue
        sf = rel(root, p)
        prefixes, body = _class_prefixes(text)
        for m in _MAPPING.finditer(text, body + 1 if body else 0):
            kind = m.group(1)
            end = m.end()
            args = ""
            if text[end:end + 1] == "(":
                close = _balanced(text, end)
                args, end = text[end + 1:close - 1], close
            sig = _skip_annotations(text, end)
            sm = re.compile(r"(?:(?:public|protected|private|static|final|default)\s+)*"
                            r"([\w.<>\[\],?\s]+?)\s+(\w+)\s*\(").match(text, sig)
            if not sm:
                continue
            ret, name = sm.group(1).strip(), sm.group(2)
            paths, methods = _parse_args(args)
            if kind == "Request":
                verbs = methods or ["GET"]
            else:
                verbs = [kind.upper()]
            paths = paths or [""]
            line = line_of(text, sig)
            mnode = idx.method_at(sf, name, line)
            if mnode is None:
                continue
            pclose = _balanced(text, sm.end() - 1)
            params = text[sm.end():pclose - 1]
            for pre in prefixes:
                for pa in paths:
                    full = _join(pre, pa)
                    for verb in verbs:
                        eid = endpoint_id(verb, full)
                        idx.add_node({"id": eid, "label": f"{verb} {full}", "source_file": sf,
                                      "source_location": f"L{line}", "_endpoint": True})
                        idx.add_edge(eid, mnode["id"], "handled_by", source_file=sf, line=line)
            types = _type_idents(ret)
            for rb in re.finditer(r"@RequestBody\s+(?:@\w+(?:\([^)]*\))?\s+)*([\w.<>?,\s]+?)\s+\w+\s*(?:,|$)", params):
                types += _type_idents(rb.group(1))
            for t in dict.fromkeys(types):
                cls = idx.unique_class(t, ".java")
                if cls:
                    idx.add_edge(mnode["id"], cls["id"], "uses_dto", source_file=sf, line=line)


_TABLE_NAME = re.compile(r"@Table\s*\(([^)]*)\)")
_CREATE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:\w+\.)?\"?(\w+)\"?", re.I)


def link_jpa(idx: GraphIndex, root: Path) -> None:
    tables: dict[str, list[dict]] = {}
    for n in idx.nodes:
        sf = n.get("source_file") or ""
        if sf.endswith(".sql") and n.get("label") and not n["label"].endswith(".sql"):
            tables.setdefault(n["label"].lower(), []).append(n)
    created: dict[str, tuple[str, int]] = {}
    for p in walk(root, (".sql",)):
        t = read(p)
        for m in _CREATE.finditer(t):
            created.setdefault(m.group(1).lower(), (rel(root, p), line_of(t, m.start())))

    def table_nodes(name: str) -> list[dict]:
        name = name.lower()
        hit = tables.get(name, [])
        if not hit and name in created:
            sf, ln = created[name]
            idx.add_node({"id": f"table_{name}", "label": name, "source_file": sf,
                          "source_location": f"L{ln}", "_table": True})
            hit = [n for n in idx.nodes if n["id"] == f"table_{name}"]
        return hit

    entities: dict[str, dict] = {}
    for p in walk(root, (".java",)):
        text = read(p)
        sf = rel(root, p)
        if re.search(r"@Entity\b", text):
            for cm in re.finditer(r"@Entity\b", text):
                after = _skip_annotations(text, cm.end())
                km = re.match(r"(?:public\s+|abstract\s+|final\s+)*class\s+(\w+)", text[after:after + 120])
                if not km:
                    continue
                cname = km.group(1)
                cnode = next((n for n in idx.classes.get(cname, []) if n["source_file"] == sf), None)
                if not cnode:
                    continue
                entities[cname] = cnode
                head = text[max(0, cm.start() - 400):after]
                tm = _TABLE_NAME.search(head)
                nm = re.search(r"name\s*=\s*\"([^\"]+)\"", tm.group(1)) if tm else None
                base = nm.group(1) if nm else snake(cname)
                cands = [base] if nm else [base, base + "s", base + "es", re.sub(r"y$", "ies", base)]
                for c in cands:
                    nodes = table_nodes(c)
                    if nodes:
                        for tn in nodes:
                            idx.add_edge(cnode["id"], tn["id"], "maps_to", source_file=sf,
                                         line=line_of(text, cm.start()),
                                         confidence="EXTRACTED" if nm else "INFERRED")
                        break
    for p in walk(root, (".java",)):
        text = read(p)
        sf = rel(root, p)
        for m in re.finditer(r"interface\s+(\w+)(?:<[^>]*>)?\s+extends\s+([^{]+)\{", text):
            em = re.search(r"\b(?:Jpa|Crud|PagingAndSorting|ListCrud|Reactive\w*)Repository\s*<\s*(\w+)", m.group(2))
            if not em:
                continue
            repo = next((n for n in idx.classes.get(m.group(1), []) if n["source_file"] == sf), None)
            ent = idx.unique_class(em.group(1), ".java")
            if repo and ent:
                idx.add_edge(repo["id"], ent["id"], "repository_of", source_file=sf,
                             line=line_of(text, m.start()))
