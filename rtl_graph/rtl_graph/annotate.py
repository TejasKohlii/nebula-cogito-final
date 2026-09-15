"""Map OpenSTA objects onto abstract-graph nodes and annotate the graph with
slack, delay, and the critical / violating / fsm_reg labels."""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .abstract import AbstractGraph, Node, SplitPlan
from .sta_report import StaData, PathRecord
from .fsm_info import parse_fsm_info

_GENBLK = re.compile(r"(?:^|(?<=\.))genblk\d+\.")
_CLK_LEAF = re.compile(r"^(CK|CLK|C|CP|G|E|EN|CKN|CLKB|GN|RD_CLK|WR_CLK)$", re.I)


def _norm_netlist_name(name: str) -> str:
    """OpenSTA hierarchical name -> flattened Yosys wire name."""
    n = name.strip()
    n = n.replace("\\", "")
    n = re.sub(r"\s+\[", "[", n)          # '\foo[2] [15]' -> 'foo[2][15]'
    n = n.replace(" ", "")
    n = n.replace("/", ".")
    return n


class Mapper:
    """Netlist instance / pin / net names -> abstract node ids."""

    def __init__(self, graph: AbstractGraph, sta: Optional[StaData] = None):
        self.g = graph
        self.d = graph.design
        self.sta = sta
        self.inst_node: Dict[str, str] = {}
        self.unmapped_instances: List[str] = []
        self.inst_clocks: Dict[str, List[str]] = {}
        self._pin_cache: Dict[str, Optional[str]] = {}
        self._norm_index: Optional[Dict[str, str]] = None
        if sta is not None:
            for inst, info in sta.registers.items():
                nid = None
                for pin, net in info["q_nets"].items():
                    nid = self.map_net(net)
                    if nid:
                        break
                if nid is None:
                    nid = self.guess_from_instance(inst)
                if nid is None:
                    self.unmapped_instances.append(inst)
                else:
                    self.inst_node[inst] = nid
                if info.get("clocks"):
                    self.inst_clocks[inst] = info["clocks"]

    # -- nets ------------------------------------------------------------------
    def map_net(self, net: str) -> Optional[str]:
        r = self.map_net_bit(net)
        return r[0] if r else None

    def map_net_bit(self, net: str) -> Optional[Tuple[str, Optional[int]]]:
        """(node id, design bit) for a netlist net; bit is None when only the node is known."""
        flat = _norm_netlist_name(net)
        cand = [flat]
        m = re.match(r"^(.*)\[\d+\]$", flat)
        if m:
            cand.append(m.group(1))
            m2 = re.match(r"^(.*)\[\d+\]$", m.group(1))
            if m2:
                cand.append(m2.group(1))
        for c in cand:
            bits = self.d.lookup_name(c)
            if not bits:
                continue
            for b in bits:
                if b is None:
                    continue
                src = self.g.bit_source_node.get(b)
                if src is not None:
                    return src, b
                drv = self.d.bit_driver.get(b)
                if drv and drv[0] >= 0 and drv[0] in self.g.cell_node:
                    return self.g.cell_node[drv[0]], b
        # memory node by name
        for c in cand:
            if f"M:{c}" in self.g.nodes:
                return f"M:{c}", None
        # unnamed generate blocks are named differently by different front-ends
        # ('genblk1.' segments): retry with those segments removed on both sides
        if _GENBLK.search(flat) or "genblk" in flat:
            if self._norm_index is None:
                self._norm_index = {}
                for name in self.d.netnames:
                    self._norm_index.setdefault(_GENBLK.sub("", name), name)
            for c in cand:
                orig = self._norm_index.get(_GENBLK.sub("", c))
                if orig is not None:
                    suffix = flat[len(c):] if flat.startswith(c) else ""
                    r = self.map_net_bit(orig + suffix) if suffix else self.map_net_bit(orig)
                    if r:
                        return r
        return None

    def map_pin_bit(self, pin: str, prefer_sink: bool = False) -> Tuple[Optional[str], Optional[int]]:
        """(node id, vector index) for an STA start/end pin; the index is the bit's position in
        the RTL vector (register Q bit, or port bit) and None when it cannot be told."""
        key = ("bit", pin, prefer_sink)
        if key not in self._pin_cache:
            self._pin_cache[key] = self._map_pin_bit(pin.strip(), prefer_sink)
        return self._pin_cache[key]

    def _map_pin_bit(self, pin: str, prefer_sink: bool) -> Tuple[Optional[str], Optional[int]]:
        inst = pin.rsplit("/", 1)[0] if "/" in pin else None
        info = self.sta.registers.get(inst) if (self.sta is not None and inst) else None
        if info:
            for _p, net in info["q_nets"].items():
                r = self.map_net_bit(net)
                if r and r[1] is not None:
                    return r[0], self.g.bit_vec_index(r[0], r[1])
        nid = self.map_pin(pin, prefer_sink)
        if nid is None:
            return None, None
        node = self.g.nodes[nid]
        if node.is_port:
            m = re.search(r"\[(\d+)\]$", pin.strip("\\ "))
            return nid, (int(m.group(1)) if m else (0 if node.width == 1 else None))
        # register named without an STA record: take the index from the pin / instance name
        flat = _norm_netlist_name(inst or pin)
        m = re.search(r"(?:\[(\d+)\]|_(\d+)_)$", flat)
        if m:
            return nid, int(m.group(1) or m.group(2))
        r = self.map_net_bit(flat)
        if r and r[0] == nid and r[1] is not None:
            return nid, self.g.bit_vec_index(nid, r[1])
        return nid, (0 if node.width == 1 else None)

    def map_net_sink(self, net: str) -> Optional[str]:
        """Node that *reads* a net (used for D-side nets)."""
        flat = _norm_netlist_name(net)
        bits = self.d.lookup_name(flat)
        for b in bits or []:
            if b is None:
                continue
            sinks = self.g.bit_sink_nodes.get(b)
            if sinks:
                return sinks[0]
        return None

    # -- instances / pins ---------------------------------------------------------
    def guess_from_instance(self, inst: str) -> Optional[str]:
        """Instance names of the form a/b/name_reg[3], a/b/name_reg_3_, a/b/name[3]."""
        flat = _norm_netlist_name(inst)
        cands = [flat]
        m = re.match(r"^(.*?)_reg(?:\[(\d+)\]|_(\d+)_)?$", flat)
        if m:
            base = m.group(1)
            idx = m.group(2) or m.group(3)
            cands.append(f"{base}[{idx}]" if idx else base)
        cands.append(re.sub(r"_reg$", "", flat))
        for c in cands:
            bits = self.d.lookup_name(c)
            for b in bits or []:
                if b is None:
                    continue
                src = self.g.bit_source_node.get(b)
                if src:
                    return src
        return None

    def map_pin(self, pin: str, prefer_sink: bool = False) -> Optional[str]:
        key = (pin, prefer_sink)
        if key in self._pin_cache:
            return self._pin_cache[key]
        nid = self._map_pin(pin, prefer_sink)
        self._pin_cache[key] = nid
        return nid

    def _map_pin(self, pin: str, prefer_sink: bool) -> Optional[str]:
        pin = pin.strip()
        # text reports name the *instance* (or port) on Startpoint/Endpoint lines
        if pin in self.inst_node:
            return self.inst_node[pin]
        if "/" not in pin:
            # top-level port (bit) or a top-level register instance
            base = re.sub(r"\[\d+\]$", "", pin.strip("\\ "))
            nid = f"P:{base}"
            if nid in self.g.nodes:
                return nid
            return self.guess_from_instance(pin)
        inst = pin.rsplit("/", 1)[0]
        if inst in self.inst_node:
            return self.inst_node[inst]
        nid = self.guess_from_instance(pin) or self.guess_from_instance(inst)
        if nid:
            return nid
        # hierarchical pin of a module instance:  a/b/c/port  -> wire a.b.c.port
        flat = _norm_netlist_name(pin)
        if prefer_sink:
            nid = self.map_net_sink(flat)
            if nid:
                return nid
        return self.map_net(flat)

    def map_path_ends(self, p: PathRecord) -> Tuple[Optional[str], Optional[str]]:
        s = self.map_pin(p.startpoint) if p.startpoint else None
        if s is None and p.points and p.points[0].net:
            s = self.map_net(p.points[0].net)
        t = self.map_pin(p.endpoint, prefer_sink=True)
        return s, t

    def name_to_node_map(self, objs) -> Dict[str, str]:
        out = {}
        for o in objs:
            nid = self.map_pin(o) if "/" in o else None
            if nid is None:
                nid = self.map_net(o)
            if nid:
                out[o] = nid
        return out


# ---------------------------------------------------------------------------
def port_clock_hints(sta: StaData, graph: AbstractGraph) -> Dict[str, str]:
    """'port[i]' -> clock name, from STA startpoints / endpoints that are ports."""
    hints: Dict[str, str] = {}
    for p in sta.all_paths():
        if p.startpoint and "/" not in p.startpoint and p.launch_clock:
            hints.setdefault(p.startpoint.strip("\\ "), p.launch_clock)
        if p.endpoint and "/" not in p.endpoint and p.capture_clock:
            hints.setdefault(p.endpoint.strip("\\ "), p.capture_clock)
    return hints


def node_clock_overrides(mapper: Mapper) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = defaultdict(set)
    for inst, clks in mapper.inst_clocks.items():
        nid = mapper.inst_node.get(inst)
        if nid:
            out[nid].update(clks)
    return out


# ---------------------------------------------------------------------------
class Annotator:
    def __init__(self, graph: AbstractGraph, sta: Optional[StaData], mapper: Optional[Mapper],
                 slack_checks: str = "all", critical_per_group: int = 1, critical_overall: bool = False,
                 fsm_mode: str = "both", fsm_name_regex: str = r"(^|[._])(state|fsm)([._\[]|$)|_st(ate)?_?[qd]?$",
                 clock_analysis=None, fsm_info_path: Optional[str] = None):
        self.ca = clock_analysis
        self.fsm_info_path = fsm_info_path
        self.attributed: Dict[str, tuple] = {}      # endpoint pin -> (PathRecord, segments)
        self.split_plan: SplitPlan = {}             # registers on critical paths worth expanding
        self.g = graph
        self.sta = sta
        self.mapper = mapper
        self.slack_checks = slack_checks
        self.critical_per_group = critical_per_group
        self.critical_overall = critical_overall
        self.fsm_mode = fsm_mode
        self.fsm_re = re.compile(fsm_name_regex, re.I)
        self.meta: dict = {"critical_paths": [], "unmapped_endpoints": 0, "unmapped_startpoints": 0,
                           "mapped_paths": 0, "node_paths": {}, "crossings": [], "fsms": []}

    # -- FSM ---------------------------------------------------------------------
    def label_fsm(self):
        d = self.g.design
        fsm_wires = set()
        if self.fsm_mode in ("detect", "both"):
            for name, attrs in d.net_attrs.items():
                enc = attrs.get("fsm_encoding")
                if enc is not None and str(enc).strip("\x00 ") not in ("none", ""):
                    fsm_wires.add(name)
        for nid, node in self.g.nodes.items():
            if node.kind not in ("seq", "latch"):
                continue
            src = None
            if fsm_wires:
                names = set()
                for b in node.bits_out:
                    names.update(n for n, _ in d.names_of_bit(b))
                if names & fsm_wires:
                    src = "fsm_detect"
            leaf = node.name.rsplit(".", 1)[-1]
            if src is None and self.fsm_mode in ("names", "both") and self.fsm_re.search(leaf):
                src = "name"
            if src:
                node.labels.add("fsm_reg")
                node.attrs["fsm_source"] = src
        self.g.stats["fsm_regs"] = sum(1 for n in self.g.nodes.values() if "fsm_reg" in n.labels)

    # -- timing --------------------------------------------------------------------
    def apply_timing(self):
        if self.sta is None or self.mapper is None:
            return
        g = self.g
        m = self.mapper
        ep_stats: Dict[str, dict] = defaultdict(lambda: {"worst_slack": None, "worst_slack_setup": None,
                                                         "n": 0, "worst_delay": None, "worst_delay_path": None,
                                                         "clocks": set(), "worst_slack_path": None})
        sp_clocks: Dict[str, Set[str]] = defaultdict(set)
        bit_slack: Dict[str, Dict[int, float]] = defaultdict(dict)       # sink node -> {vector index: worst slack}
        launch_viol: Dict[str, Set[int]] = defaultdict(set)              # source node -> indices launching a violation
        unmapped_ep = unmapped_sp = mapped = 0
        seen_endpoints: Set[str] = set()
        for p in self.sta.all_paths():
            s, t = m.map_path_ends(p)
            if t is None:
                unmapped_ep += 1
                continue
            if p.startpoint and s is None:
                unmapped_sp += 1
            mapped += 1
            st = ep_stats[t]
            if p.endpoint not in seen_endpoints:
                seen_endpoints.add(p.endpoint)
                st["n"] += 1
            if p.slack is not None:
                _t, ti = m.map_pin_bit(p.endpoint, prefer_sink=True)
                if ti is not None:
                    bs = bit_slack[t]
                    if ti not in bs or p.slack < bs[ti]:
                        bs[ti] = p.slack
                if p.slack < 0 and s is not None and p.startpoint:
                    _s, si = m.map_pin_bit(p.startpoint)
                    if si is not None:
                        launch_viol[s].add(si)
                if st["worst_slack"] is None or p.slack < st["worst_slack"]:
                    st["worst_slack"] = p.slack
                    st["worst_slack_path"] = {"startpoint": p.startpoint, "endpoint": p.endpoint,
                                              "start_node": s, "launch_clock": p.launch_clock,
                                              "capture_clock": p.capture_clock, "slack": p.slack,
                                              "check": p.check, "arrival": p.arrival, "required": p.required,
                                              "launch_time": p.launch_time}
                if p.check in ("setup", "output", "max_delay"):
                    if st["worst_slack_setup"] is None or p.slack < st["worst_slack_setup"]:
                        st["worst_slack_setup"] = p.slack
            cd = p.comb_delay
            if cd is not None and (st["worst_delay"] is None or cd > st["worst_delay"]):
                st["worst_delay"] = cd
                st["worst_delay_path"] = {"startpoint": p.startpoint, "endpoint": p.endpoint, "start_node": s,
                                          "comb_delay": cd, "data_delay": p.data_delay,
                                          "arrival": p.arrival, "launch_clock": p.launch_clock,
                                          "capture_clock": p.capture_clock, "slack": p.slack}
            if p.capture_clock:
                st["clocks"].add(p.capture_clock)
            if s is not None and p.launch_clock:
                sp_clocks[s].add(p.launch_clock)
        self.meta.update({"unmapped_endpoints": unmapped_ep, "unmapped_startpoints": unmapped_sp,
                          "mapped_paths": mapped,
                          "unmapped_register_instances": len(m.unmapped_instances),
                          "unmapped_register_examples": m.unmapped_instances[:20]})
        # endpoint (sink) nodes
        for nid, st in ep_stats.items():
            node = g.nodes[nid]
            node.attrs["worst_slack"] = st["worst_slack"]
            node.attrs["worst_slack_setup"] = st["worst_slack_setup"]
            node.attrs["endpoints_timed"] = st["n"]
            node.attrs["worst_slack_path"] = st["worst_slack_path"]
            node.attrs["worst_arrival_delay"] = st["worst_delay"]
            wp = st["worst_slack_path"] or {}
            node.attrs["arrival"] = wp.get("arrival")
            node.attrs["required"] = wp.get("required")
            if st["clocks"]:
                node.attrs["sta_capture_clocks"] = sorted(st["clocks"])
            key = "worst_slack_setup" if self.slack_checks == "data" else "worst_slack"
            v = node.attrs.get(key)
            node.attrs["violated"] = bool(v is not None and v < 0)
        for nid, clks in sp_clocks.items():
            g.nodes[nid].attrs["sta_launch_clocks"] = sorted(clks)
        # per-bit view of the same data, for expanding registers on critical paths
        for nid, bs in bit_slack.items():
            g.nodes[nid].attrs["bit_slack"] = [[i, round(v, 4)] for i, v in sorted(bs.items())]
            g.nodes[nid].attrs["violating_bits"] = sorted(i for i, v in bs.items() if v < 0)
        for nid, idx in launch_viol.items():
            g.nodes[nid].attrs["launch_violating_bits"] = sorted(idx)
        # combinational nodes: worst delay over their sinks
        for nid, node in g.nodes.items():
            if node.kind != "comb":
                continue
            best = None
            for t in node.attrs["sinks"]:
                st = ep_stats.get(t)
                if st and st["worst_delay"] is not None and (best is None or st["worst_delay"] > best["comb_delay"]):
                    best = st["worst_delay_path"]
            if best is not None:
                node.attrs["worst_delay"] = best["comb_delay"]
                node.attrs["worst_delay_path"] = best
            # violating: feeds (solid edge) a sink with negative worst slack
            for t in node.attrs["sinks"]:
                e = g.edges.get((nid, t))
                if e is None:
                    continue
                if g.nodes[t].attrs.get("violated"):
                    node.labels.add("violating")
                    node.attrs.setdefault("violating_sinks", []).append(t)
        g.stats["violating_comb_nodes"] = sum(1 for n in g.nodes.values() if "violating" in n.labels)
        g.stats["violated_sinks"] = sum(1 for n in g.nodes.values() if n.attrs.get("violated"))
        self.attribute_paths()
        self._label_critical()

    # -- critical paths ------------------------------------------------------------
    def _label_critical(self):
        g = self.g
        m = self.mapper
        for n in g.nodes.values():
            n.attrs["critical_path"] = 0
        recs = [p for p in (self.sta.critical or self.sta.paths) if p.slack is not None]
        chosen: List[PathRecord] = []
        if self.critical_overall:
            chosen = sorted(recs, key=lambda p: p.slack)[: self.critical_per_group]
        else:
            by_group: Dict[str, List[PathRecord]] = defaultdict(list)
            for p in recs:
                by_group[p.path_group or p.capture_clock or "?"].append(p)
            for grp, ps in by_group.items():
                chosen.extend(sorted(ps, key=lambda p: p.slack)[: self.critical_per_group])
        # Keep every endpoint (including unmapped ones) for inspection and TNS.
        # Prefer detailed records when their slack ties the endpoint summary.
        endpoints = {}
        for p in self.sta.all_paths():
            if p.slack is None:
                continue
            old = endpoints.get(p.endpoint)
            if old is None or (p.slack, -len(p.points)) < (old.slack, -len(old.points)):
                endpoints[p.endpoint] = p
        # TSV endpoints retain more precision than report_checks' JSON output.
        # Their slack is authoritative; taking min(TSV, rounded JSON) biases TNS.
        precise = {}
        for p in self.sta.paths:
            if p.source != "endpoints.tsv" or p.slack is None:
                continue
            old = precise.get(p.endpoint)
            if old is None or p.slack < old.slack:
                precise[p.endpoint] = p
        detailed = {p.endpoint: p for p in self.sta.point_level_paths()}
        for ep, p in precise.items():
            detail = detailed.get(ep)
            if detail and detail.startpoint == p.startpoint and detail.slack is not None and abs(detail.slack - p.slack) < 0.002:
                endpoints[ep] = replace(detail, slack=p.slack, arrival=p.arrival, required=p.required)
            else:
                endpoints[ep] = p
        selected = {p.endpoint: p for p in chosen}
        selected.update({ep: p for ep, p in endpoints.items() if p.slack < 0})
        chosen = list(selected.values())
        self.meta["timing_endpoints"] = [
            {"endpoint": ep, "startpoint": p.startpoint, "slack": p.slack,
             "arrival": p.arrival, "required": p.required, "check": p.check,
             "launch_clock": p.launch_clock, "capture_clock": p.capture_clock,
             "node": m.map_path_ends(p)[1], "violating": p.slack < 0}
            for ep, p in sorted(endpoints.items())]
        negative = [p.slack for p in endpoints.values() if p.slack < 0]
        self.meta["timing_summary"] = {
            "endpoint_count": len(endpoints), "violating_endpoint_count": len(negative),
            "tns_ns": sum(negative), "wns_ns": min(negative, default=0.0),
            "critical_selection": "all violating endpoints plus per-group representatives"}
        # number the paths worst-slack first, so critical_path=1 is the worst path in the design
        chosen.sort(key=lambda p: (p.slack, p.endpoint, p.startpoint))
        for p in chosen:
            s, t = m.map_path_ends(p)
            nodes_on_path: List[str] = []
            att = self.attributed.get(p.endpoint)
            exact = bool(att and att[0].startpoint == p.startpoint and len(att[1]) >= 2)
            if exact:
                nodes_on_path = [seg["node"] for seg in att[1]]
            if s and not exact:
                nodes_on_path.append(s)
            between = []
            if t and not exact:
                for nid, node in g.nodes.items():
                    if node.kind != "comb" or t not in node.attrs["sinks"]:
                        continue
                    if s is None or s in g.transitive_sources(nid):
                        between.append(nid)
                # keep only nodes that can actually reach t on a path from s: those with an
                # edge to t, plus their comb predecessors that also contain s
                nodes_on_path.extend(sorted(between))
                nodes_on_path.append(t)
            idx = len(self.meta["critical_paths"])
            self.meta["critical_paths"].append({
                "index": idx,
                "path_group": p.path_group, "slack": p.slack, "startpoint": p.startpoint,
                "endpoint": p.endpoint, "launch_clock": p.launch_clock, "capture_clock": p.capture_clock,
                "check": p.check, "arrival": p.arrival, "required": p.required,
                "launch_time": p.launch_time, "comb_delay": p.comb_delay, "data_delay": p.data_delay,
                "start_node": s, "end_node": t, "nodes": nodes_on_path, "nodes_exact": exact,
                "segments": (att[1] if exact else None),
                "points": [{"pin": pt.pin, "cell": pt.cell, "net": pt.net, "arrival": pt.arrival,
                            "slew": pt.slew, "cap": pt.cap, "node": (self._net_node(pt.net) if pt.net else None)}
                           for pt in p.points],
            })
            for nid in nodes_on_path:
                self.meta["node_paths"].setdefault(nid, []).append(idx)
                # a node shared by several paths keeps the lowest number = the worst path
                cur = g.nodes[nid].attrs["critical_path"]
                g.nodes[nid].attrs["critical_path"] = idx + 1 if cur == 0 else min(cur, idx + 1)
        g.stats["critical_nodes"] = sum(1 for n in g.nodes.values() if n.attrs.get("critical_path"))
        self._plan_register_splits()

    def _plan_register_splits(self):
        """Registers at either end of a critical path whose bits are only partly violating are
        expanded: each violating bit becomes its own node, runs of clean bits stay compact
        ([32:0] with bits 5 and 21 violating -> 0:4, 5, 6:20, 21, 22:31).  The graph is rebuilt
        with the plan; a register that is already split is left alone."""
        g = self.g
        plan: SplitPlan = {}
        for cp in self.meta["critical_paths"]:
            for nid in (cp.get("start_node"), cp.get("end_node")):
                node = g.nodes.get(nid) if nid else None
                if node is None or node.kind not in ("seq", "latch") or node.width < 2:
                    continue
                if node.attrs.get("array") or node.attrs.get("split_of") or node.name in plan:
                    continue
                idx = g.vec_indices(nid)
                bad = set(node.attrs.get("violating_bits", [])) | set(node.attrs.get("launch_violating_bits", []))
                bad &= set(idx)
                if not bad or len(bad) == len(idx):
                    continue
                ranges: List[Tuple[int, int]] = []
                run: Optional[List[int]] = None
                for i in idx:
                    if i in bad:
                        if run:
                            ranges.append((run[0], run[1])); run = None
                        ranges.append((i, i))
                    elif run and i == run[1] + 1:
                        run[1] = i
                    else:
                        if run:
                            ranges.append((run[0], run[1]))
                        run = [i, i]
                if run:
                    ranges.append((run[0], run[1]))
                plan[node.name] = ranges
        self.split_plan = plan
        self.meta["register_splits"] = {k: [list(r) for r in v] for k, v in plan.items()}


    # -- path attribution: through-delay / through-slack ----------------------------
    def _net_node(self, net: str) -> Optional[str]:
        if self.mapper is None:
            return None
        return self.mapper.map_net(net)

    def _attribute_path(self, p: PathRecord) -> List[dict]:
        """Split a point-level path into per-node segments using the boundary nets that
        survived synthesis.  Returns [{node, entry, exit, through, net, slew, cap, gap}]."""
        m = self.mapper
        start, end = m.map_path_ends(p)
        if start is None or end is None or not p.points:
            return []
        segs: List[dict] = []
        launch = p.launch_time if p.launch_time is not None else (p.q_arrival if p.q_arrival is not None else 0.0)
        cur = {"node": start, "entry": launch, "exit": p.q_arrival if p.q_arrival is not None else launch,
               "net": p.points[0].net, "slew": p.points[0].slew, "cap": p.points[0].cap, "gap": False}
        for pt in p.points[1:]:
            if not pt.net or pt.arrival is None:
                continue
            nid = m.map_net(pt.net)
            if nid is None or nid == end:
                continue
            if nid == cur["node"]:
                cur["exit"] = pt.arrival
                cur["net"] = pt.net
                cur["slew"] = pt.slew if pt.slew is not None else cur["slew"]
                cur["cap"] = pt.cap if pt.cap is not None else cur["cap"]
                continue
            segs.append(cur)
            gap = (cur["node"], nid) not in self.g.edges
            cur = {"node": nid, "entry": cur["exit"], "exit": pt.arrival, "net": pt.net,
                   "slew": pt.slew, "cap": pt.cap, "gap": gap}
        segs.append(cur)
        last_exit = cur["exit"]
        segs.append({"node": end, "entry": p.arrival if p.arrival is not None else last_exit,
                     "exit": p.arrival if p.arrival is not None else last_exit,
                     "net": p.points[-1].net, "slew": None, "cap": None,
                     "gap": (cur["node"], end) not in self.g.edges})
        # gaps: boundary nets between two attributed nodes vanished in synthesis.  Insert the
        # abstract nodes that lie between them as one merged segment sharing the gap's delay.
        filled: List[dict] = []
        for a, b in zip(segs, segs[1:]):
            filled.append(a)
            if b["gap"]:
                between = self._between(a["node"], b["node"])
                if between:
                    entry = a["exit"]
                    exit_ = b["entry"] if b["node"] == end else b["entry"]
                    for nid in between:
                        filled.append({"node": nid, "entry": entry, "exit": exit_, "net": None, "slew": None,
                                       "cap": None, "gap": False, "merged": between})
                    b["gap"] = False
                    b["entry"] = exit_
        filled.append(segs[-1])
        for sg in filled:
            sg["through"] = (sg["exit"] - sg["entry"]) if (sg["exit"] is not None and sg["entry"] is not None) else None
            # OpenSTA extrapolates negative cell delays when the input slew is outside the Liberty
            # table (max-slew violation upstream); keep the value but flag it for the GUI
            sg["negative_delay"] = bool(sg["through"] is not None and sg["through"] < -1e-6)
        return filled

    def _between(self, a: str, b: str) -> List[str]:
        """Combinational nodes on abstract paths from a to b, in dependency order."""
        g = self.g
        if b not in g.nodes:
            return []
        pool = list(g.combs_feeding(b)) + ([b] if g.nodes[b].kind == "comb" else [])
        cand = []
        for nid in pool:
            node = g.nodes[nid]
            if nid == a or a in node.attrs.get("all_sources", ()) or a in node.attrs.get("comb_preds", ()):
                cand.append(nid)
        cand = [c for c in cand if c not in (a, b)]
        # order: nodes with no candidate predecessor first
        order = []
        remaining = set(cand)
        while remaining:
            ready = sorted(n for n in remaining if not (set(g.nodes[n].attrs.get("comb_preds", [])) & remaining))
            if not ready:
                ready = sorted(remaining)
            order.extend(ready)
            remaining -= set(ready)
        return order

    def attribute_paths(self):
        if self.sta is None or self.mapper is None:
            return
        g = self.g
        nstat: Dict[str, dict] = defaultdict(lambda: {"through_slack": None, "through_delay": None, "n": 0,
                                                     "through_path": None, "worst_slew": None, "worst_cap": None})
        estat: Dict[Tuple[str, str], dict] = defaultdict(lambda: {"slack": None, "arrival": None, "n": 0})
        for p in self.sta.point_level_paths():
            segs = self._attribute_path(p)
            if len(segs) < 2:
                continue
            self.attributed[p.endpoint] = (p, segs)
            for sg in segs:
                st = nstat[sg["node"]]
                st["n"] += 1
                if p.slack is not None and (st["through_slack"] is None or p.slack < st["through_slack"]):
                    st["through_slack"] = p.slack
                    st["through_delay"] = sg["through"]
                    st["through_path"] = {"startpoint": p.startpoint, "endpoint": p.endpoint, "slack": p.slack,
                                          "entry": sg["entry"], "exit": sg["exit"], "boundary_net": sg["net"],
                                          "merged_with": sg.get("merged"), "launch_clock": p.launch_clock,
                                          "capture_clock": p.capture_clock}
                if sg["slew"] is not None and (st["worst_slew"] is None or sg["slew"] > st["worst_slew"]):
                    st["worst_slew"] = sg["slew"]
                if sg["cap"] is not None and (st["worst_cap"] is None or sg["cap"] > st["worst_cap"]):
                    st["worst_cap"] = sg["cap"]
            for a, b in zip(segs, segs[1:]):
                es = estat[(a["node"], b["node"])]
                es["n"] += 1
                if p.slack is not None and (es["slack"] is None or p.slack < es["slack"]):
                    es["slack"] = p.slack
                    es["arrival"] = a["exit"]
        d = g.design
        for nid, st in nstat.items():
            node = g.nodes[nid]
            node.attrs["through_delay"] = st["through_delay"]
            node.attrs["through_slack"] = st["through_slack"]
            node.attrs["through_paths"] = st["n"]
            node.attrs["through_path"] = st["through_path"]
            node.attrs["through_exact"] = not bool((st["through_path"] or {}).get("merged_with"))
            node.attrs["negative_delay"] = bool(st["through_delay"] is not None and st["through_delay"] < -1e-6)
            node.attrs["worst_slew"] = st["worst_slew"]
            node.attrs["worst_cap"] = st["worst_cap"]
            if node.kind == "comb":
                # Preserve the sampled local delay from the worst-slack path.
                # schedule.py separately propagates available, unmerged values.
                node.attrs["delay"] = st["through_delay"] if node.attrs["through_exact"] else None
                node.attrs["delay_exact"] = node.attrs["through_exact"]
                if not node.attrs["through_exact"]:
                    node.attrs["through_delay"] = None
        for (a, b), es in estat.items():
            e = g.edges.get((a, b))
            if e is not None:
                e.attrs["arrival"] = es["arrival"]
                e.attrs["slack"] = es["slack"]
                e.attrs["paths"] = es["n"]
        # RTL-level fanout of every node's outputs (max readers of one output bit)
        for nid, node in g.nodes.items():
            if node.kind == "comb":
                fo = 0
                for ci in node.cells:
                    for b in d.cells[ci].all_output_bits():
                        if b is not None:
                            fo = max(fo, len(d.bit_readers.get(b, ())))
                node.attrs["fanout"] = fo
            elif node.is_seq:
                fo = 0
                for b in node.bits_out:
                    fo = max(fo, len(d.bit_readers.get(b, ())))
                node.attrs["fanout"] = fo
        g.stats["attributed_paths"] = len(self.attributed)
        g.stats["nodes_with_through_delay"] = len(nstat)

    # -- GUI records -----------------------------------------------------------------
    def build_crossings(self):
        """One record per (source element, destination element) clock-domain crossing."""
        g, ca = self.g, self.ca
        if ca is None:
            return
        d = g.design
        seen: Set[Tuple[str, str, Optional[str]]] = set()
        recs: List[dict] = []

        def verdict(t: str) -> str:
            node = g.nodes[t]
            if node.kind == "memory":
                return "fifo_storage"
            if node.kind != "seq":
                return "n/a"
            names = set()
            for b in node.bits_out:
                names.update(n for n, _ in d.names_of_bit(b))
            for n in names:
                a = d.net_attrs.get(n, {})
                if any(k.lower() in ("async_reg", "ASYNC_REG".lower()) for k in a):
                    return "async_reg_attribute"
            if node.width >= 2:
                # shift chain: all but one D bit come from the register's own Q bits
                own = 0
                dbits = []
                for ci in node.cells:
                    dbits.extend(b for b in d.cells[ci].inputs.get("D", []) if b is not None)
                for b in dbits:
                    if g.bit_source_node.get(b) == t:
                        own += 1
                if dbits and own >= len(dbits) - 1 and own >= 1:
                    return "shift_register_sync"
            if node.width == 1:
                readers = set()
                for b in node.bits_out:
                    for (rc, pn) in d.bit_readers.get(b, ()):
                        readers.add(rc)
                if readers and all(rc >= 0 and d.cells[rc].kind == "ff" for rc in readers):
                    same = all(ca.node_roots.get(g.cell_node.get(rc), 0) == ca.node_roots.get(t, 0) for rc in readers)
                    if same:
                        return "two_flop_chain"
                return "single_bit"
            return "multi_bit_unknown"

        def add(sn: str, tn: str, via: Optional[str], width: int, async_bits: int):
            key = (sn, tn, via)
            if key in seen:
                return
            seen.add(key)
            recs.append({"src": sn, "dst": tn, "via": via, "width": width, "async_reset_bits": async_bits,
                         "src_clocks": g.nodes[sn].attrs.get("clocks"), "dst_clocks": g.nodes[tn].attrs.get("clocks"),
                         "src_roots": g.nodes[sn].attrs.get("clock_roots"), "dst_roots": g.nodes[tn].attrs.get("clock_roots"),
                         "dst_sync": verdict(tn), "dst_worst_slack": g.nodes[tn].attrs.get("worst_slack")})
        for (sn, tn), e in g.edges.items():
            if not e.attrs.get("cdc"):
                continue
            ns, nt = g.nodes[sn], g.nodes[tn]
            if ns.kind != "comb" and nt.kind != "comb":
                add(sn, tn, None, e.width, e.async_width)
            elif ns.kind == "comb" and nt.kind != "comb":
                for x in sorted(g.transitive_sources(sn)):        # sorted: stable record numbering across runs
                    if ca.is_async(ca.node_roots.get(x, 0), ca.node_roots.get(tn, 0)):
                        add(x, tn, sn, e.width, e.async_width)
        by_node: Dict[str, List[int]] = defaultdict(list)
        for i, r in enumerate(recs):
            for nid in {r["src"], r["dst"], r["via"]}:
                if nid:
                    by_node[nid].append(i)
        for nid, node_recs in by_node.items():
            g.nodes[nid].attrs["crossings"] = node_recs
        self.meta["crossings"] = recs
        g.stats["crossings"] = len(recs)

    def build_fsms(self):
        g = self.g
        d = g.design
        recs = parse_fsm_info(self.fsm_info_path) if self.fsm_info_path else []
        by_wire: Dict[str, dict] = {r["state_wire"]: r for r in recs}
        out: List[dict] = []
        for nid, node in g.nodes.items():
            if "fsm_reg" not in node.labels:
                continue
            names = set()
            for b in node.bits_out:
                names.update(n for n, _ in d.names_of_bit(b))
            info = None
            for n in names:
                if n in by_wire:
                    info = by_wire[n]
                    break
            next_state = list(g.combs_feeding(nid))
            outputs = [c for c in g.combs_fed_by(nid) if nid not in g.nodes[c].attrs.get("sinks", ())]
            rec = {"index": len(out), "state_reg": nid, "state_bits": node.width, "fsm_source": node.attrs.get("fsm_source"),
                   "next_state_nodes": sorted(next_state), "output_nodes": sorted(outputs),
                   "states": info["states"] if info else None, "reset_state": info["reset_state"] if info else None,
                   "transitions": info["transitions"] if info else None,
                   "inputs": info["inputs"] if info else None,
                   "clocks": node.attrs.get("clocks"), "worst_slack": node.attrs.get("worst_slack")}
            node.attrs["fsm"] = rec["index"]
            for c in next_state + outputs:
                g.nodes[c].attrs.setdefault("fsm_of", []).append(nid)
            out.append(rec)
        self.meta["fsms"] = out
        g.stats["fsms_with_state_table"] = sum(1 for r in out if r["states"])

    def build_gui_fields(self):
        """Source locations, hierarchy path and stable keys for every node."""
        g = self.g
        d = g.design
        for nid, node in g.nodes.items():
            if node.kind == "comb":
                g.set_module_ownership(node)
                srcs = []
                for ci in node.cells:
                    sv = d.cells[ci].attrs.get("src")
                    if sv:
                        srcs.append(str(sv).split("|")[0])
                node.attrs["src"] = sorted(set(srcs))[:30]
                if "hier" not in node.attrs:
                    names = list(node.attrs.get("sinks", [])) + list(node.attrs.get("sources", []))
                    node.attrs["hier"] = _common_hier([g.nodes[x].name for x in names if x in g.nodes])
                ident = "|".join(node.attrs.get("sinks", [])) + "#" + "|".join(node.attrs.get("all_sources", [])) \
                    + "#" + node.attrs.get("hier", "") + "#" + "|".join(sorted(d.cells[ci].name for ci in node.cells))
                node.attrs["key"] = "C_" + hashlib.sha1(ident.encode()).hexdigest()[:12]
            elif node.is_seq:
                srcs = []
                for ci in node.cells:
                    sv = d.cells[ci].attrs.get("src")
                    if sv:
                        srcs.append(str(sv).split("|")[0])
                node.attrs["src"] = sorted(set(srcs))[:5]
                node.attrs["hier"] = node.name.rsplit(".", 1)[0] if "." in node.name else ""
                node.attrs["key"] = nid
            else:
                node.attrs["hier"] = ""
                node.attrs["key"] = nid


def _common_hier(names: List[str]) -> str:
    if not names:
        return ""
    parts = [n.split(".")[:-1] for n in names]
    pref = parts[0]
    for p in parts[1:]:
        k = 0
        while k < len(pref) and k < len(p) and pref[k] == p[k]:
            k += 1
        pref = pref[:k]
        if not pref:
            break
    return ".".join(pref)
