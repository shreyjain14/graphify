"""RabbitMQ queue wiring (Spring AMQP publishers/listeners, Python pika consumers) and templates."""
from __future__ import annotations

import re
from pathlib import Path

from .common import GraphIndex, line_of, read, rel, walk
from .spring import _balanced

_PLACEHOLDER = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def flatten_yaml(text: str) -> dict[str, str]:
    """Flatten simple block-style YAML (``a:\\n  b: v``) into dotted keys. Lists/anchors ignored."""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith(("#", "-")):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(r"\s*([\w.\-]+)\s*:\s*(.*?)\s*$", raw)
        if not m:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key, val = m.group(1), m.group(2)
        if val == "" or val.startswith("#"):
            stack.append((indent, key))
        else:
            val = re.sub(r"\s+#.*$", "", val).strip("'\"")
            out[".".join([k for _, k in stack] + [key])] = val
    return out


def _resolve(val: str, props: dict[str, str], depth: int = 0) -> str:
    def sub(m):
        key, default = m.group(1), m.group(2)
        if key in props and depth < 5:
            return _resolve(props[key], props, depth + 1)
        return default if default is not None else ""
    return _PLACEHOLDER.sub(sub, val)


def load_props(root: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    for p in walk(root, (".yml", ".yaml", ".properties")):
        if p.name.startswith("application") and "resources" in p.parts:
            t = read(p)
            if p.suffix == ".properties":
                for m in re.finditer(r"^\s*([\w.\-]+)\s*=\s*(.*?)\s*$", t, re.M):
                    props.setdefault(m.group(1), m.group(2))
            else:
                for k, v in flatten_yaml(t).items():
                    props.setdefault(k, v)
    return props


def _split_args(s: str) -> list[str]:
    out, depth, cur, in_str = [], 0, "", False
    for c in s:
        if c == '"':
            in_str = not in_str
        if not in_str and c in "([{":
            depth += 1
        elif not in_str and c in ")]}":
            depth -= 1
        if c == "," and depth == 0 and not in_str:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
    if cur.strip():
        out.append(cur.strip())
    return out


def _queue_node(idx: GraphIndex, name: str) -> str:
    qid = "queue_" + re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    idx.add_node({"id": qid, "label": name, "_queue": True, "source_file": None})
    return qid


def link_java_queues(idx: GraphIndex, root: Path) -> None:
    props = load_props(root)
    for p in walk(root, (".java",)):
        text = read(p)
        if "convertAndSend" not in text and "RabbitListener" not in text:
            continue
        sf = rel(root, p)
        names: dict[str, str] = {}
        for m in re.finditer(r"@Value\(\s*\"\$\{([^}:]+)(?::([^}]*))?\}\"\s*\)\s*(?:private\s+|final\s+)*String\s+(\w+)", text):
            key, default, field = m.groups()
            names[field] = _resolve("${%s%s}" % (key, ":" + default if default is not None else ""), props)
        for m in re.finditer(r"static\s+final\s+String\s+(\w+)\s*=\s*\"([^\"]+)\"", text):
            names.setdefault(m.group(1), m.group(2))

        def value(arg: str) -> str | None:
            arg = arg.strip()
            lm = re.fullmatch(r"\"([^\"]*)\"", arg)
            if lm:
                return _resolve(lm.group(1), props)
            return names.get(arg.split(".")[-1]) or None

        for m in re.finditer(r"\bconvertAndSend\s*\(", text):
            op = m.end() - 1
            args = _split_args(text[op + 1:_balanced(text, op) - 1])
            if len(args) < 2:
                continue
            q = value(args[1] if len(args) >= 3 else args[0])
            if not q:
                continue
            line = line_of(text, m.start())
            enc = idx.enclosing(sf, line)
            if enc:
                idx.add_edge(_queue_node(idx, q), enc["id"], "fed_by", source_file=sf, line=line)
        for m in re.finditer(r"@RabbitListener\s*\(([^)]*)\)", text):
            qm = re.search(r"queues\s*=\s*(?:\{\s*)?\"([^\"]+)\"", m.group(1))
            if not qm:
                continue
            q = _resolve(qm.group(1), props)
            line = line_of(text, m.end())
            sig = re.compile(r"(?:\s*@\w+(?:\([^)]*\))?)*\s*(?:public\s+|protected\s+|private\s+)?[\w<>\[\],.?\s]+?\s+(\w+)\s*\(").match(text, m.end())
            node = idx.method_at(sf, sig.group(1), line) if sig else None
            node = node or idx.enclosing(sf, line)
            if node and q:
                idx.add_edge(node["id"], _queue_node(idx, q), "consumes", source_file=sf, line=line)


_PY_CONST = re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*[\w\[\], ]+)?=\s*(?:os\.(?:getenv|environ\.get)\(\s*['\"][^'\"]+['\"]\s*,\s*)?['\"]([^'\"]+)['\"]", re.M)
_PY_ARG = r"((?:os\.(?:getenv|environ\.get)\([^)]*\))|[\w.]+|['\"][^'\"]+['\"])"


def link_python_queues(idx: GraphIndex, root: Path) -> None:
    files = list(walk(root, (".py",)))
    consts: dict[str, dict[str, str]] = {}
    for p in files:
        svc = rel(root, p).split("/")[0]
        for m in _PY_CONST.finditer(read(p)):
            consts.setdefault(svc, {}).setdefault(m.group(1), m.group(2))
    templates: dict[str, dict[str, str]] = {}
    for p in walk(root, (".html", ".j2", ".jinja")):
        templates.setdefault(rel(root, p).split("/")[0], {}).setdefault(p.name, rel(root, p))

    def value(arg: str, svc: str) -> str | None:
        arg = arg.strip()
        lm = re.fullmatch(r"['\"]([^'\"]+)['\"]", arg)
        if lm:
            return lm.group(1)
        gm = re.fullmatch(r"os\.(?:getenv|environ\.get)\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)", arg)
        if gm:
            return gm.group(1)
        return consts.get(svc, {}).get(arg.split(".")[-1])

    for p in files:
        text = read(p)
        if not re.search(r"basic_consume|\.consume\(|basic_publish|get_template|render_template", text):
            continue
        sf = rel(root, p)
        svc = sf.split("/")[0]
        for m in re.finditer(r"\.basic_consume\(([^)]*)\)|\.consume\(\s*" + _PY_ARG, text):
            argtxt = m.group(1) if m.group(1) is not None else m.group(2)
            if m.group(1) is not None:
                qm = re.search(r"queue\s*=\s*" + _PY_ARG, argtxt) or re.match(r"\s*" + _PY_ARG, argtxt)
                q = value(qm.group(1), svc) if qm else None
                cb = re.search(r"on_message_callback\s*=\s*(\w+)", argtxt)
            else:
                q, cb = value(argtxt, svc), None
            if not q:
                continue
            line = line_of(text, m.start())
            node = None
            if cb:
                node = next((n for n in idx.by_file.get(sf, []) if n.get("label") == cb.group(1) + "()"), None)
            node = node or idx.enclosing(sf, line)
            if node:
                idx.add_edge(node["id"], _queue_node(idx, q), "consumes", source_file=sf, line=line)
        for m in re.finditer(r"basic_publish\(([^)]*)\)", text):
            rk = re.search(r"routing_key\s*=\s*" + _PY_ARG, m.group(1))
            q = value(rk.group(1), svc) if rk else None
            if q:
                line = line_of(text, m.start())
                enc = idx.enclosing(sf, line)
                if enc:
                    idx.add_edge(_queue_node(idx, q), enc["id"], "fed_by", source_file=sf, line=line)
        for m in re.finditer(r"(?:get_template|render_template)\(\s*['\"]([^'\"]+)['\"]", text):
            name = m.group(1)
            tpath = templates.get(svc, {}).get(Path(name).name)
            if not tpath:
                continue
            tid = "template_" + re.sub(r"[^a-z0-9]+", "_", tpath.lower()).strip("_")
            idx.add_node({"id": tid, "label": Path(tpath).name, "source_file": tpath, "source_location": "L1"})
            line = line_of(text, m.start())
            enc = idx.enclosing(sf, line)
            if enc:
                idx.add_edge(enc["id"], tid, "renders", source_file=sf, line=line)
