import json
import re

import networkx as nx

from graphify.exporters.html3d import FORCE_GRAPH_URL, to_html3d


def _graph(n=3, label="a"):
    G = nx.DiGraph()
    for i in range(n):
        G.add_node(f"n{i}", label=label if i == 0 else f"f{i}", source_file=f"core/x{i}.java",
                   source_location=f"L{i}", file_type="code")
    for i in range(1, n):
        G.add_edge(f"n{i}", "n0", relation="calls", confidence="EXTRACTED")
    return G


def _data(html):
    m = re.search(r'<script type="application/json" id="gdata">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_basic(tmp_path):
    G = _graph()
    out = tmp_path / "o.html"
    to_html3d(G, {0: ["n0", "n1"], 1: ["n2"]}, str(out), community_labels={0: "Alpha"})
    h = out.read_text()
    assert "3d-force-graph@1.73.4" in h and FORCE_GRAPH_URL in h
    d = _data(h)
    assert len(d["nodes"]) == 3 and len(d["links"]) == 2
    assert d["comms"][0]["n"] == "Alpha"
    assert d["layers"] == ["core"]
    assert d["links"][0][:2] == [1, 0]  # dependent -> dependency
    assert d["communityView"] is False


def test_malicious_label(tmp_path):
    bad = "</script><img src=x onerror=alert(1)>"
    G = _graph(label=bad)
    out = tmp_path / "o.html"
    to_html3d(G, {0: list(G.nodes)}, str(out))
    h = out.read_text()
    assert "</script><img" not in h
    assert h.count("</script>") == h.count("<script")
    assert _data(h)["nodes"][0]["l"] == bad


def test_community_view_threshold(tmp_path):
    G = _graph(10)
    out = tmp_path / "o.html"
    to_html3d(G, {0: list(G.nodes)}, str(out), community_view_threshold=5)
    assert _data(out.read_text())["communityView"] is True
