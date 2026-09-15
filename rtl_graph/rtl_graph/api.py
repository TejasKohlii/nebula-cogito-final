"""Programmatic access to a produced graph.json.

    from rtl_graph.api import Graph
    g = Graph.load("out/graph.json")
    g.node("R:domain[0].engine.reducer.score")      # dict
    g.preds(nid), g.succs(nid), g.edge(a, b)        # topology
    g.critical_paths()                              # list of dicts, with resolved node dicts
    g.crossings(), g.fsms()                         # interactable sets
    g.interactable()                                # node ids that are on a critical path / crossing / FSM
    g.walk_through(path)                            # [(node, entry, exit, through)] along a critical path
    g.slim()                                        # small dict for a first render
    g.subgraph(ids)                                 # nodes+edges restricted to ids
    g.by_domain_pair()                              # crossings grouped by (src root, dst root)
    g.dom_id(nid)                                   # DOM/CSS-safe id

No third-party dependencies.  Every method works on the plain dict too: `g.doc`.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

LABELS = ("CDC", "fsm_reg", "violating")
SEQ_KINDS = ("seq", "latch", "memory", "blackbox")
PORT_KINDS = ("port_in", "port_out", "port_inout")


class Graph:
    def __init__(self, doc: dict):
        self.doc = doc
        self.meta: dict = doc["meta"]
        self.stats: dict = doc.get("stats", {})
        self.nodes: Dict[str, dict] = {n["id"]: n for n in doc["nodes"]}
        self.edges: List[dict] = doc["edges"]
        self._out: Dict[str, List[dict]] = defaultdict(list)
        self._in: Dict[str, List[dict]] = defaultdict(list)
        self._edge: Dict[Tuple[str, str], dict] = {}
        for e in self.edges:
            self._out[e["src"]].append(e)
            self._in[e["dst"]].append(e)
            self._edge[(e["src"], e["dst"])] = e
        self._by_key: Dict[str, str] = {n.get("key", i): i for i, n in self.nodes.items()}

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str) -> "Graph":
        with open(path) as f:
            return cls(json.load(f))

    # ------------------------------------------------------------------ nodes
    def node(self, nid: str) -> dict:
        return self.nodes[nid]

    def by_key(self, key: str) -> Optional[dict]:
        """Look a node up by its run-stable key (comb nodes are renumbered between runs)."""
        nid = self._by_key.get(key)
        return self.nodes.get(nid) if nid else None

    def registers(self) -> List[dict]:
        return [n for n in self.nodes.values() if n["kind"] in SEQ_KINDS]

    def combs(self) -> List[dict]:
        return [n for n in self.nodes.values() if n["kind"] == "comb"]

    def ports(self) -> List[dict]:
        return [n for n in self.nodes.values() if n["kind"] in PORT_KINDS]

    def with_label(self, label: str) -> List[dict]:
        return [n for n in self.nodes.values() if label in n["labels"]]

    def on_critical_path(self, number: Optional[int] = None) -> List[dict]:
        """Nodes with critical_path != 0, or only those numbered `number` (1 = worst path)."""
        return [n for n in self.nodes.values()
                if n.get("critical_path") and (number is None or n["critical_path"] == number)]

    def find(self, regex: str) -> List[dict]:
        rx = re.compile(regex)
        return [n for n in self.nodes.values() if rx.search(n["name"]) or rx.search(n["id"])]

    def by_hier(self) -> Dict[str, List[str]]:
        """Owning instance -> node ids; multi-owner nodes appear under every owner."""
        out: Dict[str, List[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            owners = n.get("modules") or [{"hier": n.get("hier") or ""}]
            for hier in sorted({owner.get("hier") or "" for owner in owners}):
                out[hier].append(nid)
        return dict(out)

    # ------------------------------------------------------------------ edges
    def edge(self, src: str, dst: str) -> Optional[dict]:
        return self._edge.get((src, dst))

    def out_edges(self, nid: str) -> List[dict]:
        return self._out.get(nid, [])

    def in_edges(self, nid: str) -> List[dict]:
        return self._in.get(nid, [])

    def succs(self, nid: str) -> List[str]:
        return [e["dst"] for e in self._out.get(nid, [])]

    def preds(self, nid: str) -> List[str]:
        return [e["src"] for e in self._in.get(nid, [])]

    def neighbourhood(self, ids: Iterable[str], hops: int = 1) -> Set[str]:
        seen = set(ids)
        frontier = set(ids)
        for _ in range(hops):
            nxt = set()
            for i in frontier:
                nxt.update(self.succs(i)); nxt.update(self.preds(i))
            nxt -= seen
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        return seen

    # ------------------------------------------------------------------ interactable sets
    def timing_endpoints(self, violating_only: bool = False) -> List[dict]:
        """All distinct STA endpoints, preserving bit/pin identity and unmapped pins."""
        return [ep for ep in self.meta.get("timing_endpoints", [])
                if not violating_only or ep["violating"]]

    def critical_paths(self) -> List[dict]:
        """meta.critical_paths with `node_dicts` resolved (ordered as on the path)."""
        out = []
        for cp in self.meta.get("critical_paths", []):
            c = dict(cp)
            c["node_dicts"] = [self.nodes[i] for i in cp["nodes"] if i in self.nodes]
            out.append(c)
        return out

    def paths_of(self, nid: str) -> List[dict]:
        idx = self.meta.get("node_paths", {}).get(nid, [])
        cps = self.meta.get("critical_paths", [])
        return [cps[i] for i in idx if i < len(cps)]

    def walk_through(self, cp: dict) -> List[Tuple[str, Optional[float], Optional[float], Optional[float], bool]]:
        """[(node, entry, exit, through, merged)] for a critical path; falls back to the node
        list (no times) when the path was not attributed exactly."""
        if cp.get("segments"):
            return [(s["node"], s["entry"], s["exit"], s["through"], bool(s.get("merged"))) for s in cp["segments"]]
        return [(n, None, None, None, False) for n in cp.get("nodes", [])]

    def crossings(self) -> List[dict]:
        return self.meta.get("crossings", [])

    def crossings_of(self, nid: str) -> List[dict]:
        idx = self.nodes[nid].get("crossings") or []
        xs = self.crossings()
        return [xs[i] for i in idx if i < len(xs)]

    def by_domain_pair(self) -> Dict[Tuple[str, str], List[dict]]:
        out: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
        for x in self.crossings():
            for a in x.get("src_roots") or ["?"]:
                for b in x.get("dst_roots") or ["?"]:
                    if a != b:
                        out[(a, b)].append(x)
        return dict(out)

    def fsms(self) -> List[dict]:
        return self.meta.get("fsms", [])

    def fsm_area(self, fsm: dict) -> Set[str]:
        """Node ids that make up one FSM: state register, next-state and output logic."""
        return {fsm["state_reg"], *fsm.get("next_state_nodes", []), *fsm.get("output_nodes", [])}

    def crossing_area(self, x: dict) -> Set[str]:
        return {i for i in (x["src"], x["dst"], x.get("via")) if i}

    def interactable(self) -> Dict[str, Set[str]]:
        """{'critical': ids, 'cdc': ids, 'fsm': ids, 'all': ids}"""
        crit: Set[str] = set()
        for cp in self.meta.get("critical_paths", []):
            crit.update(cp.get("nodes", []))
        cdc: Set[str] = set()
        for x in self.crossings():
            cdc |= self.crossing_area(x)
        fsm: Set[str] = set()
        for f in self.fsms():
            fsm |= self.fsm_area(f)
        return {"critical": crit, "cdc": cdc, "fsm": fsm, "all": crit | cdc | fsm}

    # ------------------------------------------------------------------ ranking helpers
    def worst_registers(self, n: int = 20, setup_only: bool = False) -> List[dict]:
        key = "worst_slack_setup" if setup_only else "worst_slack"
        regs = [r for r in self.registers() + self.ports() if r.get(key) is not None]
        return sorted(regs, key=lambda r: r[key])[:n]

    def worst_through(self, n: int = 20, exact_only: bool = False) -> List[dict]:
        cs = [c for c in self.combs() if c.get("through_delay") is not None
              and (c.get("through_exact") or not exact_only)]
        return sorted(cs, key=lambda c: (c["through_slack"] if c["through_slack"] is not None else 0,
                                         -c["through_delay"]))[:n]

    def fanout_hubs(self, min_fanout: int = 64) -> List[dict]:
        return sorted((n for n in self.nodes.values() if (n.get("fanout") or 0) >= min_fanout),
                      key=lambda n: -n["fanout"])

    # ------------------------------------------------------------------ export helpers
    def subgraph(self, ids: Iterable[str]) -> dict:
        ids = set(ids)
        return {"nodes": [self.nodes[i] for i in ids if i in self.nodes],
                "edges": [e for e in self.edges if e["src"] in ids and e["dst"] in ids]}

    def slim(self, ids: Optional[Iterable[str]] = None) -> dict:
        """Topology + the few numbers a first render needs (about 1/20 of graph.json)."""
        keep = set(ids) if ids is not None else set(self.nodes)
        nodes = []
        for i in keep:
            n = self.nodes.get(i)
            if n is None:
                continue
            nodes.append({k: n.get(k) for k in ("id", "kind", "name", "width", "labels", "key", "hier", "module", "modules",
                                                 "delay", "delay_exact", "through_slack", "asap", "combinational_loop",
                                                 "worst_slack", "clocks", "fanout", "critical_path", "instances",
                                                 "split_of", "bit_range", "violating_bits", "crossings", "fsm", "fsm_of")})
        edges = [{k: e.get(k) for k in ("src", "dst", "width", "cdc", "async_width", "arrival", "slack", "asap")}
                 for e in self.edges if e["src"] in keep and e["dst"] in keep]
        return {"design": self.meta.get("design"), "stats": self.stats, "nodes": nodes, "edges": edges,
                "timing_summary": self.meta.get("timing_summary"),
                "clocks": self.meta.get("clocks", {}).get("root_clock", {})}

    def to_cytoscape(self, ids: Optional[Iterable[str]] = None) -> List[dict]:
        sg = self.subgraph(ids) if ids is not None else {"nodes": list(self.nodes.values()), "edges": self.edges}
        els = [{"data": {"id": n["id"], "label": n["name"], **{k: n.get(k) for k in ("kind", "labels", "width",
                "delay", "through_slack", "worst_slack", "hier", "module", "modules", "critical_path", "instances", "key", "asap")}}} for n in sg["nodes"]]
        els += [{"data": {"id": f"{e['src']}->{e['dst']}", "source": e["src"], "target": e["dst"],
                          **{k: e.get(k) for k in ("width", "cdc", "arrival", "slack", "asap")}}} for e in sg["edges"]]
        return els

    @staticmethod
    def dom_id(nid: str) -> str:
        """Stable DOM/CSS-safe identifier for a node id (reversible via a lookup table)."""
        return "n_" + re.sub(r"[^A-Za-z0-9_]", lambda m: "_%02x" % ord(m.group(0)), nid)


def load(path: str) -> Graph:
    return Graph.load(path)
