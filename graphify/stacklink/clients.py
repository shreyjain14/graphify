"""Frontend (TS) and mobile (Dart) HTTP client calls, gateway rewrites, DTO mirrors."""
from __future__ import annotations

import re
from pathlib import Path

from .common import GraphIndex, line_of, normalize_path, read, rel, walk
from .spring import _balanced

_GENERIC = r"(?:<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>)?"
_VERB_CALL = re.compile(r"\.(get|post|put|delete|patch|getBytes)\s*" + _GENERIC + r"\s*\(\s*(?=['\"`]/)")
_FETCH_API = re.compile(r"\bfetchApi\s*" + _GENERIC + r"\s*\(\s*(?=['\"`]/)")
_FETCH_RAW = re.compile(r"\bfetch\s*\(\s*`\$\{[A-Za-z_]*(?:API|BASE)[A-Za-z_]*\}(?=/)")


def scan_literal(text: str, i: int) -> tuple[str, int]:
    """Read the string/template literal starting at ``text[i]`` (a quote); ``${...}`` nests."""
    q, j, n = text[i], i + 1, len(text)
    out = []
    while j < n:
        c = text[j]
        if c == "\\":
            out.append(text[j:j + 2])
            j += 2
            continue
        if c == q:
            break
        if q == "`" and c == "$" and text[j + 1:j + 2] == "{":
            depth, k = 0, j + 1
            while k < n:
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            out.append("{}")
            j = k + 1
            continue
        if c == "\n" and q != "`":
            break
        out.append(c)
        j += 1
    return "".join(out), j




def gateway_rules(root: Path) -> list[tuple[str, str, bool]]:
    """(from_prefix, to_prefix, has_segment) parsed from Spring Cloud Gateway RewritePath filters."""
    rules: set[tuple[str, str, bool]] = set()
    for p in walk(root, (".yml", ".yaml")):
        if "gateway" not in p.parts[-6:] and "gateway" not in p.name:
            if "gateway" not in rel(root, p):
                continue
        text = read(p)
        for m in re.finditer(r"RewritePath=\s*([^,\s]+)\s*,\s*(\S+)", text):
            src, dst = m.group(1), m.group(2)
            seg = "(?<segment>" in src
            src_p = src.split("(?<segment>")[0]
            dst_p = dst.replace("${segment}", "")
            rules.add((src_p, dst_p, seg))
    return sorted(rules, key=lambda r: -len(r[0]))


def candidates(path: str, rules: list[tuple[str, str, bool]]) -> list[str]:
    """Core-side path candidates for a client-side path (client base is ``/v1``)."""
    base = normalize_path(path)
    # `/x${query}` — a trailing interpolation glued to a literal is a query suffix.
    base = re.sub(r"(?<=[^/])\{\}$", "", base) or "/"
    out = [base]
    full = "/v1" + base
    for src, dst, seg in rules:
        if seg:
            if full.startswith(src):
                rest = full[len(src):]
                if src.endswith("/") or rest == "" or rest.startswith("/"):
                    out.append(normalize_path(dst + rest))
        elif full == src:
            out.append(normalize_path(dst))
    return list(dict.fromkeys(out))


class EndpointTable:
    def __init__(self, idx: GraphIndex):
        self.exact: dict[tuple[str, str], str] = {}
        self.by_verb: dict[str, list[tuple[list[str], str]]] = {}
        for n in idx.nodes:
            if n.get("_endpoint"):
                verb, path = n["label"].split(" ", 1)
                self.exact[(verb, path)] = n["id"]
                self.by_verb.setdefault(verb, []).append((path.strip("/").split("/"), n["id"]))

    def match(self, verb: str, cands: list[str]) -> tuple[str | None, str]:
        for c in cands:
            if (verb, c) in self.exact:
                return self.exact[(verb, c)], "EXTRACTED"
        for c in cands:
            segs = c.strip("/").split("/")
            best, best_score, tie = None, -1, False
            for esegs, eid in self.by_verb.get(verb, []):
                if len(esegs) != len(segs):
                    continue
                score, ok = 0, True
                for a, b in zip(segs, esegs):
                    if a == b and a != "{}":
                        score += 1
                    elif a == "{}" or b == "{}":
                        continue
                    else:
                        ok = False
                        break
                if not ok:
                    continue
                if score > best_score:
                    best, best_score, tie = eid, score, False
                elif score == best_score:
                    tie = True
            if best and not tie:
                return best, "INFERRED"
        return None, ""


def _method_in(text: str, open_i: int) -> str:
    body = text[open_i:_balanced(text, open_i)]
    vm = re.search(r"method\s*:\s*['\"](\w+)['\"]", body)
    return vm.group(1).upper() if vm else "GET"


def _ts_calls(text: str):
    for m in _VERB_CALL.finditer(text):
        verb = m.group(1)
        raw, _ = scan_literal(text, m.end())
        yield ("GET" if verb == "getBytes" else verb.upper()), raw, m.start()
    for m in _FETCH_API.finditer(text):
        raw, _ = scan_literal(text, m.end())
        yield _method_in(text, text.index("(", m.start())), raw, m.start()
    for m in _FETCH_RAW.finditer(text):
        raw, _ = scan_literal(text, text.index("`", m.start()))
        yield _method_in(text, text.index("(", m.start())), raw.replace("{}", "", 1) if raw.startswith("{}") else raw, m.start()


def link_clients(idx: GraphIndex, root: Path, stats: dict) -> None:
    table = EndpointTable(idx)
    if not table.exact:
        return
    rules = gateway_rules(root)
    unmatched: list[str] = []
    total = {"ts": 0, "dart": 0}
    hit = {"ts": 0, "dart": 0}
    for p in walk(root, (".ts", ".tsx", ".dart")):
        sf = rel(root, p)
        parts = sf.split("/")
        if (p.name.endswith((".test.ts", ".test.tsx", ".spec.ts", "_test.dart"))
                or {"test", "tests", "__tests__", "e2e", "integration_test"} & set(parts[:-1])):
            continue
        text = read(p)
        kind = "dart" if p.suffix == ".dart" else "ts"
        if kind == "dart":
            calls = ((("GET" if m.group(1) == "getBytes" else m.group(1).upper()),
                      scan_literal(text, m.end())[0], m.start())
                     for m in _VERB_CALL.finditer(text))
        else:
            calls = _ts_calls(text)
        for verb, raw, pos in calls:
            total[kind] += 1
            eid, conf = table.match(verb, candidates(raw, rules))
            if not eid:
                unmatched.append(f"{verb} {raw} ({sf})")
                continue
            hit[kind] += 1
            line = line_of(text, pos)
            enc = idx.enclosing(sf, line)
            if enc:
                idx.add_edge(enc["id"], eid, "calls_endpoint", source_file=sf, line=line, confidence=conf)
    stats["client_calls"] = total
    stats["client_matched"] = hit
    stats["unmatched_sample"] = unmatched


def _java_targets(idx: GraphIndex) -> dict[str, dict]:
    return {name: nodes[0] for name, nodes in idx.classes.items()
            if len([n for n in nodes if n["source_file"].endswith(".java")]) == 1
            and nodes[0]["source_file"].endswith(".java")}


def _variants(name: str) -> list[str]:
    out = [name]
    for suf in ("Response", "Request", "Dto", "DTO"):
        if name.endswith(suf) and len(name) > len(suf):
            out.append(name[: -len(suf)])
        else:
            out.append(name + suf)
    return out


def link_mirrors(idx: GraphIndex) -> None:
    java = _java_targets(idx)
    for name, nodes in list(idx.classes.items()):
        for n in nodes:
            sf = n["source_file"]
            if sf.endswith(".tsx") or sf.endswith(".java") or "test" in sf.lower().split("/")[-1]:
                continue
            if not sf.endswith((".ts", ".dart")):
                continue
            for v in _variants(name):
                tgt = java.get(v)
                if tgt:
                    idx.add_edge(n["id"], tgt["id"], "mirrors", source_file=sf,
                                 line=idx.lineno(n) or None, confidence="INFERRED")
                    break
