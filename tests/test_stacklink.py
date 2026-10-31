"""Stack linker: Spring endpoints, TS/Dart clients, JPA <-> SQL, RabbitMQ."""
from __future__ import annotations

import json
from pathlib import Path

from graphify.stacklink import link_graph_data, link_graph_file
from graphify.stacklink.clients import candidates
from graphify.stacklink.common import normalize_path


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _node(rel, label, line, **kw):
    nid = (rel.replace("/", "_").replace(".", "_") + "_" + label).lower().replace("(", "").replace(")", "")
    return {"id": nid, "label": label, "source_file": rel, "source_location": f"L{line}",
            "file_type": "code", "_callable": True, **kw}


CONTROLLER = '''package a;
@RestController
@RequestMapping("/sale/orders")
public class OrderController {
    @GetMapping("/{id}")
    public ResponseEntity<OrderResponse> get(@PathVariable UUID id) { return null; }

    @PostMapping
    public ResponseEntity<?> create(@RequestBody OrderRequest req) { return null; }

    @RequestMapping(value = "/x/", method = RequestMethod.PUT)
    public void upd() {}
}
'''
ENTITY = '''package a;
@Entity
@Table(name = "customer_contacts")
public class CustomerContact { }
'''
REPO = '''package a;
public interface CustomerContactRepository extends JpaRepository<CustomerContact, UUID> { }
'''
PUBLISHER = '''package a;
public class Sender {
    @Value("${rabbitmq.out-queue:out_default}")
    private String outQueue;
    public void send() {
        rabbitTemplate.convertAndSend(outQueue, payload);
    }
}
'''
TS = '''export const getOrder = (id: string) =>
  client.get<Order>(`/sale/orders/${id}`);
export const create = (d: any) => client.post<Order>('/sale/orders', d);
export async function upd() {
  return fetchApi<void>('/sale/orders/x', { method: 'PUT' });
}
export const missing = () => client.get('/nope/${a}');
export interface OrderResponse { id: string }
'''
DART = '''class Repo {
  Future<void> load(String id) {
    return api.get<Order>('/sale/orders/$id', decode: (j) => j);
  }
}
class OrderRequest {}
'''
PY = '''import os
OUT = os.getenv("OUT_QUEUE", "out_default")
def on_msg(ch, method, props, body):
    pass
def start():
    channel.queue_declare(queue=OUT)
    channel.basic_consume(queue=OUT, on_message_callback=on_msg)
'''
SQL = "CREATE TABLE customer_contacts (id uuid primary key);\n"


def _build(tmp_path):
    root = tmp_path
    _w(root, "core/src/main/java/a/OrderController.java", CONTROLLER)
    _w(root, "core/src/main/java/a/CustomerContact.java", ENTITY)
    _w(root, "core/src/main/java/a/CustomerContactRepository.java", REPO)
    _w(root, "core/src/main/java/a/Sender.java", PUBLISHER)
    _w(root, "core/src/main/resources/application.yml", "rabbitmq:\n  out-queue: ${OUT_Q:out_default}\n")
    _w(root, "core/src/main/resources/db/migration/V1__c.sql", SQL)
    _w(root, "frontend/src/lib/api/orders.ts", TS)
    _w(root, "mobile/lib/repo.dart", DART)
    _w(root, "worker/app/consumer.py", PY)
    j = "core/src/main/java/a/"
    cls = lambda r, n, line: _node(r, n, line, _callable_class=True)
    nodes = [
        cls(j + "OrderController.java", "OrderController", 3),
        _node(j + "OrderController.java", ".get()", 6),
        _node(j + "OrderController.java", ".create()", 9),
        _node(j + "OrderController.java", ".upd()", 12),
        cls(j + "CustomerContact.java", "CustomerContact", 4),
        cls(j + "CustomerContactRepository.java", "CustomerContactRepository", 2),
        cls(j + "OrderResponse.java", "OrderResponse", 1),
        cls(j + "OrderRequest.java", "OrderRequest", 1),
        _node(j + "Sender.java", ".send()", 5),
        {"id": "sql_t", "label": "customer_contacts", "source_file": "core/src/main/resources/db/migration/V1__c.sql",
         "source_location": "L1", "file_type": "code"},
        _node("frontend/src/lib/api/orders.ts", "getOrder()", 1),
        _node("frontend/src/lib/api/orders.ts", "create()", 3),
        _node("frontend/src/lib/api/orders.ts", "upd()", 4),
        cls("frontend/src/lib/api/orders.ts", "OrderResponse", 8),
        _node("mobile/lib/repo.dart", ".load()", 2),
        cls("mobile/lib/repo.dart", "OrderRequest", 6),
        _node("worker/app/consumer.py", "on_msg()", 3),
        _node("worker/app/consumer.py", "start()", 5),
    ]
    return {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}, root


def _edges(data, rel):
    return {(l["source"], l["target"]) for l in data["links"] if l["relation"] == rel}


def test_normalize_path():
    assert normalize_path("/a/${x}/b/$id/:z/{id}/?q=1") == "/a/{}/b/{}/{}/{}"
    assert normalize_path("/a/b/") == "/a/b"


def test_gateway_rewrite_candidates():
    rules = [("/v1/auth/users", "/users", True)]
    assert "/users/{}" in candidates("/auth/users/${id}", rules)


def test_full_pass(tmp_path):
    data, root = _build(tmp_path)
    stats = link_graph_data(data, root)
    labels = {n["label"]: n["id"] for n in data["nodes"]}
    assert {"GET /sale/orders/{}", "POST /sale/orders", "PUT /sale/orders/x"} <= set(labels)
    get_ep = labels["GET /sale/orders/{}"]
    assert any(t.endswith("orders_get") or t.endswith("ordercontroller.java_.get") or "get" in t
               for _, t in _edges(data, "handled_by") if _ == get_ep)
    callers = {s for s, t in _edges(data, "calls_endpoint") if t == get_ep}
    assert any("getorder" in c for c in callers) and any(".load" in c for c in callers)
    put_callers = {s for s, t in _edges(data, "calls_endpoint") if t == labels["PUT /sale/orders/x"]}
    assert any("upd" in c for c in put_callers)
    assert stats["client_calls"]["ts"] == 4 and stats["client_matched"]["ts"] == 3
    # DTOs and mirrors
    dto_targets = {t for _, t in _edges(data, "uses_dto")}
    assert any("orderresponse" in t for t in dto_targets) and any("orderrequest" in t for t in dto_targets)
    mirror_src = {s for s, _ in _edges(data, "mirrors")}
    assert any(s.startswith("frontend") for s in mirror_src) and any(s.startswith("mobile") for s in mirror_src)
    # JPA
    assert ("core_src_main_java_a_customercontact_java_customercontact", "sql_t") in _edges(data, "maps_to")
    assert len(_edges(data, "repository_of")) == 1
    # queues
    assert "out_default" in labels
    q = labels["out_default"]
    assert any(t == q for _, t in _edges(data, "consumes"))
    assert any(s == q for s, _ in _edges(data, "fed_by"))


def test_file_roundtrip_idempotent(tmp_path):
    data, root = _build(tmp_path)
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(data))
    a = link_graph_file(gp, root)
    n1 = len(json.loads(gp.read_text())["links"])
    link_graph_file(gp, root)
    assert len(json.loads(gp.read_text())["links"]) == n1
    assert a["relations"]["handled_by"] == 3
    from graphify.affected import DEFAULT_AFFECTED_RELATIONS, affected_nodes, load_graph
    g = load_graph(gp)
    hits = {h.node_id for h in affected_nodes(g, "core_src_main_java_a_customercontact_java_customercontact",
                                              relations=DEFAULT_AFFECTED_RELATIONS, depth=3)}
    assert any("repository" in h for h in hits)
