"""html3d - interactive 3D node map (3d-force-graph) as ONE self-contained HTML file.

Usage:
    python -m graphify.exporters.html3d <graph.json> <out.html>
or
    to_html3d(G, communities, "out.html", community_labels={...})

Edge direction: the exporter reads direction from ``_src``/``_tgt`` markers
(see paths.load_node_link_graph) and falls back to ``(u, v)`` for DiGraphs.
Convention: an edge A->B means "A depends on B" (dependent -> dependency).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import networkx as nx

from graphify.exporters.base import COMMUNITY_COLORS
from graphify.paths import write_text_atomic
from graphify.security import sanitize_label

FORCE_GRAPH_VERSION = "1.73.4"
FORCE_GRAPH_URL = f"https://cdn.jsdelivr.net/npm/3d-force-graph@{FORCE_GRAPH_VERSION}/dist/3d-force-graph.min.js"
COMMUNITY_VIEW_THRESHOLD = 5_000


def _safe_json(obj) -> str:
    """JSON safe inside <script>: ensure_ascii escapes U+2028/9, '<' -> \\u003c
    (blocks </script> and <!-- breakouts, same approach as html.py)."""
    return json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c")


def _layer_of(source_file) -> str:
    if not source_file:
        return "(none)"
    parts = str(source_file).replace("\\", "/").lstrip("./").split("/")
    return parts[0] if len(parts) > 1 and parts[0] else "(root)"


def _edge_dir(u, v, d):
    return d.get("_src", u), d.get("_tgt", v)


def build_payload(G, communities, community_labels=None) -> dict:
    node_comm = {n: cid for cid, ms in communities.items() for n in ms}
    ids = list(G.nodes())
    idx = {n: i for i, n in enumerate(ids)}
    deg = dict(G.degree())
    labels = dict(community_labels or {})
    comm_ids = sorted({node_comm.get(n, -1) for n in ids})
    cpos = {c: i for i, c in enumerate(comm_ids)}
    cnames, ccolors = [], []
    for c in comm_ids:
        name = labels.get(c)
        if not name:
            for n in communities.get(c, []):
                if n in G and G.nodes[n].get("community_name"):
                    name = G.nodes[n]["community_name"]
                    break
        cnames.append(sanitize_label(name or ("Unassigned" if c == -1 else f"Community {c}")))
        ccolors.append(COMMUNITY_COLORS[c % len(COMMUNITY_COLORS)] if c >= 0 else "#888888")
    layers, lpos = [], {}
    nodes = []
    for n in ids:
        d = G.nodes[n]
        sf = d.get("source_file") or ""
        ly = _layer_of(sf)
        if ly not in lpos:
            lpos[ly] = len(layers)
            layers.append(ly)
        nodes.append({
            "id": sanitize_label(str(n)),
            "l": sanitize_label(d.get("label", n)),
            "k": sanitize_label(d.get("kind") or d.get("file_type") or ""),
            "f": sanitize_label(sf),
            "s": sanitize_label(d.get("source_location") or ""),
            "c": cpos[node_comm.get(n, -1)],
            "y": lpos[ly],
            "d": deg.get(n, 0),
        })
    rels, rpos, confs, cfpos, links = [], {}, [], {}, []
    for u, v, d in G.edges(data=True):
        a, b = _edge_dir(u, v, d)
        if a not in idx or b not in idx:
            continue
        r = sanitize_label(d.get("relation") or "")
        cf = sanitize_label(d.get("confidence") or "")
        if r not in rpos:
            rpos[r] = len(rels); rels.append(r)
        if cf not in cfpos:
            cfpos[cf] = len(confs); confs.append(cf)
        links.append([idx[a], idx[b], rpos[r], cfpos[cf]])
    return {"nodes": nodes, "links": links, "rels": rels, "confs": confs,
            "comms": [{"n": n, "c": c} for n, c in zip(cnames, ccolors)],
            "layers": layers}


def to_html3d(G, communities, output_path, community_labels=None,
              community_view_threshold: int = COMMUNITY_VIEW_THRESHOLD,
              title: str | None = None) -> bool:
    payload = build_payload(G, communities, community_labels)
    payload["communityView"] = len(payload["nodes"]) > community_view_threshold
    page = (_TEMPLATE
            .replace("__TITLE__", _esc(title or Path(str(output_path)).name))
            .replace("__CDN__", FORCE_GRAPH_URL)
            .replace("__DATA__", _safe_json(payload)))
    write_text_atomic(Path(output_path), page)
    return True


def _esc(s: str) -> str:
    import html
    return html.escape(s, quote=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python -m graphify.exporters.html3d <graph.json> <out.html>", file=sys.stderr)
        return 2
    from graphify.paths import load_node_link_graph
    G = load_node_link_graph(argv[0])
    communities: dict = {}
    labels: dict = {}
    for n, d in G.nodes(data=True):
        c = d.get("community")
        if c is None:
            continue
        try:
            c = int(c)
        except (TypeError, ValueError):
            continue
        communities.setdefault(c, []).append(n)
        if d.get("community_name"):
            labels.setdefault(c, d["community_name"])
    to_html3d(G, communities, argv[1], community_labels=labels)
    print(f"wrote {argv[1]} ({G.number_of_nodes()} nodes, {G.number_of_edges()} edges)")
    return 0


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>graphify 3D - __TITLE__</title>
<style>
:root{--bg:#0b0e14;--panel:#141922;--line:#262d3a;--tx:#e6e9ef;--tx2:#9aa4b5;--ac:#6ea8fe}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--tx);font:13px/1.4 system-ui,sans-serif;overflow:hidden}
#g{position:absolute;inset:0}
.side{position:absolute;top:0;bottom:0;width:300px;background:var(--panel);border:1px solid var(--line);padding:10px;overflow:auto;z-index:5}
#left{left:0;border-width:0 1px 0 0}#right{right:0;border-width:0 0 0 1px;display:none}
h3{margin:12px 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--tx2)}
input[type=text]{width:100%;padding:7px 9px;background:var(--bg);color:var(--tx);border:1px solid var(--line);border-radius:6px}
button{background:#1e2633;color:var(--tx);border:1px solid var(--line);border-radius:6px;padding:5px 9px;cursor:pointer}
button:hover,button.on{background:var(--ac);color:#04101f}
.row{display:flex;gap:6px;align-items:center;margin:4px 0;flex-wrap:wrap}
.item{display:flex;align-items:center;gap:6px;padding:2px 0;cursor:pointer;user-select:none}
.item.off{opacity:.35}.dot{width:10px;height:10px;border-radius:50%;flex:none}
.item .cnt{margin-left:auto;color:var(--tx2)}
#results div,.nb{padding:3px 6px;border-radius:4px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#results div:hover,#results div.sel,.nb:hover{background:#222b3a}
.sub{color:var(--tx2);font-size:11px}
.lbl{position:absolute;z-index:4;pointer-events:none;transform:translate(-50%,-130%);background:rgba(10,12,18,.88);border:1px solid var(--line);padding:1px 6px;border-radius:4px;white-space:nowrap;font-size:12px}
#legend{max-height:34vh;overflow:auto}
</style></head><body>
<div id="g"></div>
<div id="left" class="side">
 <input id="q" type="text" placeholder="Search label / file  ( / )" autocomplete="off">
 <div id="results"></div>
 <h3>Color by</h3>
 <div class="row"><button id="bc" class="on">community</button><button id="bl">layer</button></div>
 <h3>View</h3>
 <div class="row"><button id="bv">community view</button><label><input type="checkbox" id="pt"> particles</label></div>
 <div class="sub" id="stats"></div>
 <h3>Legend <span class="sub">(click to hide)</span></h3>
 <div id="legend"></div>
</div>
<div id="right" class="side"></div>
<div id="hl" class="lbl" style="display:none"></div>
<div id="sl" class="lbl" style="display:none"></div>
<script type="application/json" id="gdata">__DATA__</script>
<script src="__CDN__"></script>
<script>
"use strict";
// Relations that mean "source depends on target" (dependent -> dependency).
// Impact of B = every A that can reach B following these edges forward.
const IMPACT_RELATIONS = new Set(["calls","imports","imports_from","uses","references","inherits","implements","extends","calls_endpoint","handled_by","uses_dto","mirrors","maps_to","repository_of","fed_by","consumes","renders"]);
const D = JSON.parse(document.getElementById("gdata").textContent);
const N = D.nodes, NN = N.length;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LAYER_COL = i => "hsl(" + ((i*137.5)%360) + ",65%,60%)";
// adjacency
const outA = Array.from({length:NN},()=>[]), inA = Array.from({length:NN},()=>[]), rev = Array.from({length:NN},()=>[]);
for (const [s,t,r] of D.links) { outA[s].push([t,r]); inA[t].push([s,r]); if (IMPACT_RELATIONS.has(D.rels[r])) rev[t].push(s); }
const commMembers = D.comms.map(()=>[]); N.forEach((n,i)=>commMembers[n.c].push(i));
const commCount = D.comms.map((_,i)=>commMembers[i].length);
const layerCount = D.layers.map(()=>0); N.forEach(n=>layerCount[n.y]++);
const maxDeg = Math.max(1, ...N.map(n=>n.d));

let colorMode = "community", communityView = !!D.communityView;
const expanded = new Set(), hiddenC = new Set(), hiddenL = new Set();
let selected = null, impactSet = null, hover = null, impactDepth = 3, impactOn = false;
const realObj = new Array(NN), superObj = new Array(D.comms.length);

function realNode(i){ if(!realObj[i]){const n=N[i]; realObj[i]={key:"n"+i,i,sup:false,val:1+4*Math.sqrt(n.d/maxDeg)*2};} return realObj[i]; }
function superNode(c){ if(!superObj[c]){superObj[c]={key:"c"+c,c,sup:true,val:3+Math.sqrt(commCount[c])*1.5};} return superObj[c]; }
function nodeOf(i){ return (communityView && !expanded.has(N[i].c)) ? superNode(N[i].c) : realNode(i); }

function buildData(){
  const nodes=[], seen=new Set();
  if (!communityView) { for(let i=0;i<NN;i++) nodes.push(realNode(i)); }
  else {
    D.comms.forEach((_,c)=>{
      if (expanded.has(c)) { const s=superObj[c]; for(const i of commMembers[c]){ const o=realNode(i); if(o.x===undefined&&s&&s.x!==undefined){o.x=s.x+(Math.random()-.5)*10;o.y=s.y+(Math.random()-.5)*10;o.z=s.z+(Math.random()-.5)*10;} nodes.push(o);} }
      else nodes.push(superNode(c));
    });
  }
  const links=[], agg=new Map();
  for (const [s,t,r,cf] of D.links) {
    const a=nodeOf(s), b=nodeOf(t);
    if (a===b) continue;
    if (!a.sup && !b.sup) links.push({source:a,target:b,r,cf,w:1});
    else { const k=a.key+">"+b.key; const e=agg.get(k); if(e) e.w++; else agg.set(k,{source:a,target:b,r:-1,cf:-1,w:1}); }
  }
  for (const e of agg.values()) links.push(e);
  return {nodes,links};
}

function ncol(o){
  if (o.sup) {
    if (impactOn && impactSet && !commMembers[o.c].some(i=>impactSet.has(i))) return "#2a3040";
    return D.comms[o.c].c;
  }
  const n=N[o.i];
  if (impactOn && impactSet && !impactSet.has(o.i) && o.i!==selected) return "#2a3040";
  if (o.i===selected) return "#ffffff";
  return colorMode==="community" ? D.comms[n.c].c : LAYER_COL(n.y);
}
function nvis(o){
  if (o.sup) return !hiddenC.has(o.c);
  const n=N[o.i]; return !hiddenC.has(n.c) && !hiddenL.has(n.y);
}
function lvis(l){ return nvis(l.source) && nvis(l.target); }
function lcol(l){
  if (impactOn && impactSet) { const a=l.source,b=l.target; if(!a.sup&&!b.sup&&impactSet.has(a.i)&&(impactSet.has(b.i)||b.i===selected)) return "#ffb454"; return "#1b2130"; }
  return "#8896ab";
}

const big = NN>2000;
const Graph = ForceGraph3D()($("g"))
  .backgroundColor("#0b0e14").showNavInfo(false)
  .nodeId("key").nodeVal(o=>o.val).nodeRelSize(3).nodeResolution(big?3:8)
  .nodeColor(ncol).nodeVisibility(nvis).nodeOpacity(0.92)
  .nodeLabel(null)
  .linkVisibility(lvis).linkColor(lcol).linkOpacity(0.18).linkWidth(0)
  .linkDirectionalParticles(0).linkDirectionalParticleWidth(1.4)
  .warmupTicks(big?60:0).cooldownTicks(big?150:300).cooldownTime(big?12000:15000)
  .d3AlphaDecay(big?0.05:0.0228).d3VelocityDecay(0.35)
  .enableNodeDrag(!big)
  .onNodeHover(o=>{ hover=o||null; $("g").style.cursor=o?"pointer":""; })
  .onNodeClick(o=>onClickNode(o))
  .onBackgroundClick(()=>clearSel());
try { Graph.d3Force("link").distance(l=>(l.source.sup||l.target.sup)?60:30); } catch(e){}
function refreshStyle(){ Graph.nodeColor(ncol).nodeVisibility(nvis).linkVisibility(lvis).linkColor(lcol); }
function setData(){ Graph.graphData(buildData()); updateStats(); }

function updateStats(){
  const g=Graph.graphData();
  $("stats").textContent = NN+" nodes / "+D.links.length+" edges; showing "+g.nodes.length+" / "+g.links.length+(communityView?" (community view)":"");
  $("bv").classList.toggle("on",communityView);
}

// labels (HTML overlay sprites, only hover + selection)
function labelText(o){ return o.sup ? D.comms[o.c].n+" ("+commCount[o.c]+")" : N[o.i].l; }
function place(el,o){
  if(!o||o.x===undefined||!nvis(o)){el.style.display="none";return;}
  const p=Graph.graph2ScreenCoords(o.x,o.y,o.z);
  el.style.display="block"; el.style.left=p.x+"px"; el.style.top=p.y+"px";
  const t=labelText(o); if(el.textContent!==t) el.textContent=t;
}
(function loop(){ place($("hl"),hover&&hover!==selObj()?hover:null); place($("sl"),selObj()); requestAnimationFrame(loop); })();
function selObj(){ return selected===null?null:(selSuper!==null?superNode(selSuper):nodeOf(selected)); }
let selSuper=null;

// selection & panel
function focusObj(o){
  if(!o||o.x===undefined) return;
  const d=Math.hypot(o.x,o.y,o.z)||1, r=90, k=1+r/d;
  Graph.cameraPosition({x:o.x*k,y:o.y*k,z:o.z*k},o,900);
}
function flyTo(i){
  const c=N[i].c;
  if (communityView && !expanded.has(c)) { expanded.add(c); setData(); setTimeout(()=>{select(i);focusObj(realNode(i));},700); }
  else { select(i); focusObj(realNode(i)); }
}
function onClickNode(o){
  if (o.sup) { selSuper=o.c; selected=null; impactOn=false; impactSet=null; showSuper(o.c); refreshStyle(); return; }
  select(o.i);
}
function clearSel(){ selected=null; selSuper=null; impactOn=false; impactSet=null; $("right").style.display="none"; refreshStyle(); }
function select(i){ selected=i; selSuper=null; impactOn=false; impactSet=null; showPanel(i); refreshStyle(); }
function showSuper(c){
  const p=$("right"); p.style.display="block";
  p.innerHTML='<b>'+esc(D.comms[c].n)+'</b><div class="sub">'+commCount[c]+' nodes</div><div class="row"><button id="ex">Expand community</button></div>';
  $("ex").onclick=()=>{ expanded.add(c); selSuper=null; clearSel(); setData(); };
}
function groupNb(list, dirLabel){
  const by=new Map();
  for(const [j,r] of list){ const k=D.rels[r]||"(unlabeled)"; if(!by.has(k)) by.set(k,[]); by.get(k).push(j); }
  let h="";
  for(const [k,arr] of [...by].sort((a,b)=>b[1].length-a[1].length)){
    h+='<div class="sub">'+dirLabel+' '+esc(k)+' ('+arr.length+')</div>';
    for(const j of arr.slice(0,100)) h+='<div class="nb" data-j="'+j+'" title="'+esc(N[j].f)+'">'+esc(N[j].l)+'</div>';
    if(arr.length>100) h+='<div class="sub">+'+(arr.length-100)+' more</div>';
  }
  return h;
}
function showPanel(i){
  const n=N[i], p=$("right"); p.style.display="block";
  let h='<b>'+esc(n.l)+'</b><div class="sub">'+esc(n.k||"node")+'</div>';
  h+='<div>'+esc(n.f)+(n.s?':'+esc(n.s):'')+'</div>';
  h+='<div><span class="dot" style="display:inline-block;background:'+D.comms[n.c].c+'"></span> '+esc(D.comms[n.c].n)+'</div>';
  h+='<div class="sub">layer: '+esc(D.layers[n.y])+' | degree '+n.d+'</div>';
  h+='<h3>Impact (what depends on this)</h3><div class="row"><button id="imp">Impact mode</button><input type="range" id="dep" min="1" max="8" value="'+impactDepth+'"><span id="depv">'+impactDepth+'</span></div><div id="impres" class="sub"></div>';
  h+='<h3>Outgoing (depends on)</h3>'+(groupNb(outA[i],"-&gt;")||'<div class="sub">none</div>');
  h+='<h3>Incoming (dependents)</h3>'+(groupNb(inA[i],"&lt;-")||'<div class="sub">none</div>');
  p.innerHTML=h;
  p.querySelectorAll(".nb").forEach(e=>e.onclick=()=>flyTo(+e.dataset.j));
  $("imp").onclick=()=>{ impactOn=!impactOn; $("imp").classList.toggle("on",impactOn); runImpact(); };
  $("dep").oninput=e=>{ impactDepth=+e.target.value; $("depv").textContent=impactDepth; if(impactOn) runImpact(); };
  if(impactOn){ $("imp").classList.add("on"); }
}
function runImpact(){
  if(selected===null) return;
  if(!impactOn){ impactSet=null; $("impres").textContent=""; refreshStyle(); return; }
  const dist=new Map([[selected,0]]); let fr=[selected];
  for(let d=1; d<=impactDepth && fr.length; d++){
    const nx=[]; for(const b of fr) for(const a of rev[b]) if(!dist.has(a)){dist.set(a,d);nx.push(a);} fr=nx;
  }
  impactSet=new Set(dist.keys()); impactSet.delete(selected);
  const per=new Map(); for(const a of impactSet){const y=D.layers[N[a].y]; per.set(y,(per.get(y)||0)+1);}
  let t=impactSet.size+" dependent nodes within depth "+impactDepth+":<br>";
  for(const [y,c] of [...per].sort((a,b)=>b[1]-a[1])) t+=esc(y)+": "+c+"<br>";
  $("impres").innerHTML=t;
  // in community view, expand nothing automatically; collapsed supers light up if they contain impacted nodes
  refreshStyle();
}

// legend
function renderLegend(){
  const el=$("legend"); el.innerHTML="";
  const items = colorMode==="community"
    ? D.comms.map((c,i)=>({i,name:c.n,col:c.c,cnt:commCount[i],hid:hiddenC.has(i),set:hiddenC}))
    : D.layers.map((n,i)=>({i,name:n,col:LAYER_COL(i),cnt:layerCount[i],hid:hiddenL.has(i),set:hiddenL}));
  items.sort((a,b)=>b.cnt-a.cnt);
  for(const it of items.slice(0,300)){
    const d=document.createElement("div"); d.className="item"+(it.hid?" off":"");
    d.innerHTML='<span class="dot" style="background:'+it.col+'"></span><span>'+esc(it.name)+'</span><span class="cnt">'+it.cnt+'</span>';
    d.onclick=()=>{ it.set.has(it.i)?it.set.delete(it.i):it.set.add(it.i); renderLegend(); refreshStyle(); };
    el.appendChild(d);
  }
}
$("bc").onclick=()=>{colorMode="community";$("bc").classList.add("on");$("bl").classList.remove("on");renderLegend();refreshStyle();};
$("bl").onclick=()=>{colorMode="layer";$("bl").classList.add("on");$("bc").classList.remove("on");renderLegend();refreshStyle();};
$("bv").onclick=()=>{ communityView=!communityView; expanded.clear(); clearSel(); setData(); };
$("pt").onchange=e=>Graph.linkDirectionalParticles(e.target.checked?2:0);

// search
function score(q,toks,n){
  const l=n.l.toLowerCase(), f=n.f.toLowerCase(); let s=0;
  for(const t of toks){
    let x=l.indexOf(t);
    if(x>=0) s+=100-Math.min(x,50)+(l===t?100:0);
    else if(f.indexOf(t)>=0) s+=40;
    else { let k=0; for(const ch of l){ if(ch===t[k]) k++; if(k===t.length) break; } if(k===t.length) s+=10; else return 0; }
  }
  return s;
}
let hits=[], hsel=0, qt=null;
function doSearch(){
  const q=$("q").value.trim().toLowerCase(); hits=[]; hsel=0;
  if(q){ const toks=q.split(/\s+/); const arr=[];
    for(let i=0;i<NN;i++){ const s=score(q,toks,N[i]); if(s>0) arr.push([s+Math.log(1+N[i].d),i]); }
    arr.sort((a,b)=>b[0]-a[0]); hits=arr.slice(0,20).map(x=>x[1]); }
  const r=$("results"); r.innerHTML="";
  hits.forEach((i,k)=>{ const d=document.createElement("div"); d.className=k===hsel?"sel":""; d.innerHTML=esc(N[i].l)+' <span class="sub">'+esc(N[i].f)+'</span>'; d.onclick=()=>flyTo(i); r.appendChild(d); });
}
$("q").addEventListener("input",()=>{clearTimeout(qt);qt=setTimeout(doSearch,120);});
$("q").addEventListener("keydown",e=>{
  if(e.key==="Enter"&&hits.length){ flyTo(hits[hsel]); }
  else if(e.key==="ArrowDown"||e.key==="ArrowUp"){ e.preventDefault(); hsel=Math.max(0,Math.min(hits.length-1,hsel+(e.key==="ArrowDown"?1:-1))); [...$("results").children].forEach((c,k)=>c.className=k===hsel?"sel":""); }
});
document.addEventListener("keydown",e=>{
  const typing=document.activeElement&&document.activeElement.tagName==="INPUT"&&document.activeElement.type==="text";
  if(e.key==="/"&&!typing){e.preventDefault();$("q").focus();$("q").select();}
  else if(e.key==="Escape"){ clearSel(); $("q").blur(); }
});
function fit(){ Graph.width(window.innerWidth).height(window.innerHeight); }
window.addEventListener("resize",fit); fit();
renderLegend(); setData();
window.__graphify3d = {Graph, flyTo, select, runImpact, state:()=>({communityView,selected,impact:impactSet&&impactSet.size})};
</script></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
