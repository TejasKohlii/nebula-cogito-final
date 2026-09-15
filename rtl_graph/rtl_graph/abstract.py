"""Build the abstract graph: ports, sequential nodes (registers / latches /
memories / black boxes) and combinational nodes.

Combinational partition
-----------------------
Every combinational cell is tagged with three things:

* the set of *sink* elements (registers, memories, black boxes, output ports)
  it can reach without passing through any sequential element,
* the set of *source* elements (registers, input ports) that can reach it the
  same way,
* the instance path (module) it was flattened out of.

Serial stretches that agree on all three form one combinational node.
Forks, joins, and module boundaries break stretches. Consequences:

* logic that only feeds register R from R's own inputs -> one node "C -> R";
* logic shared by several branches (sink set {R1, R2}) is its own node;
* where a signal leaves the middle of one branch's cone and enters the middle
  of another's, the gates in between have a source set of their own and become
  their own node. Acyclic cell graphs remain acyclic after partitioning;
* logic from two modules is never merged into one node, even with identical
  sink and source sets.

All edges are plain (solid) edges: element -> comb, comb -> comb, comb ->
element, element -> element.

Register splitting
------------------
`split_regs` = {register name: [(lo, hi), ...]} breaks a register vector into
one node per index range (`R:x[5]`, `R:x[0:4]`), so the cone feeding one
violating flip-flop can be seen apart from its neighbours.  The annotator
computes the plan from per-bit slack on critical paths and the graph is built
a second time with it.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .netlist import Design, Cell


@dataclass
class Node:
    id: str
    kind: str                 # port_in | port_out | port_inout | seq | latch | memory | blackbox | comb
    name: str
    width: int = 0
    bits_out: List[int] = field(default_factory=list)   # bits this element drives
    bits_in: List[int] = field(default_factory=list)    # data bits this element consumes
    clk_bits: List[int] = field(default_factory=list)
    cells: List[int] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    labels: Set[str] = field(default_factory=set)

    @property
    def is_seq(self) -> bool:
        return self.kind in ("seq", "latch", "memory", "blackbox")

    @property
    def is_port(self) -> bool:
        return self.kind.startswith("port")

    @property
    def is_sink(self) -> bool:
        return self.is_seq or self.kind in ("port_out", "port_inout")

    @property
    def is_source(self) -> bool:
        return self.is_seq or self.kind in ("port_in", "port_inout")


@dataclass
class Edge:
    src: str
    dst: str
    width: int = 0            # number of bits carried
    async_width: int = 0      # bits landing on async pins (ARST/SET/CLR/...)
    attrs: dict = field(default_factory=dict)


_META_TYPES = {"$scopeinfo", "$print", "$check", "$assert", "$assume", "$cover", "$live", "$fair", "$initstate"}
_SUFFIX_BONUS = re.compile(r"(_q|_reg|_r|_ff|_d1|_s|_sync)(\[\d+\])?$", re.I)
SplitPlan = Dict[str, List[Tuple[int, int]]]         # register name -> [(lo, hi)] inclusive index ranges


class AbstractGraph:
    def __init__(self, design: Design, collapse_arrays: bool = True, split_regs: Optional[SplitPlan] = None):
        self.design = design
        self.collapse_arrays = collapse_arrays
        self.split_regs: SplitPlan = split_regs or {}
        self.nodes: Dict[str, Node] = {}
        self.edges: Dict[Tuple[str, str], Edge] = {}
        self.bit_source_node: Dict[int, str] = {}     # bit -> node id that drives it (non-comb)
        self.bit_sink_nodes: Dict[int, List[str]] = defaultdict(list)
        self.cell_node: Dict[int, str] = {}           # cell idx -> node id (seq + comb)
        self.module_of: Dict[str, str] = {}           # instance path -> module type ($scopeinfo)
        # comb machinery (kept for later passes)
        self.comb_cells: List[int] = []
        self.comb_local: Dict[int, int] = {}          # cell idx -> local index
        self.scc_of: List[int] = []                   # local index -> scc id
        self.scc_order: List[List[int]] = []          # sccs in reverse topological order (sinks first)
        self.scc_fanout: List[Set[int]] = []
        self.scc_sinkmask: List[int] = []
        self.scc_srcmask: List[int] = []
        self.sink_ids: List[str] = []                 # sink node id by mask bit
        self.sink_index: Dict[str, int] = {}
        self.source_ids: List[str] = []               # source node id by mask bit
        self.source_index: Dict[str, int] = {}
        self.stats: Dict[str, int] = {}

    # ------------------------------------------------------------- building
    def build(self):
        d = self.design
        self._build_ports()
        self._build_sequential()
        self._index_bits()
        self._partition_comb()
        self._build_edges()
        self.stats.update({
            "ports": sum(1 for n in self.nodes.values() if n.is_port),
            "seq_nodes": sum(1 for n in self.nodes.values() if n.is_seq),
            "comb_nodes": sum(1 for n in self.nodes.values() if n.kind == "comb"),
            "edges": len(self.edges),
            "cells_total": len(d.cells),
        })
        return self

    def _build_ports(self):
        for p in self.design.ports.values():
            kind = {"input": "port_in", "output": "port_out", "inout": "port_inout"}[p.direction]
            bits = [b for b in p.bits if b is not None]
            n = Node(id=f"P:{p.name}", kind=kind, name=p.name, width=len(p.bits))
            if kind in ("port_in", "port_inout"):
                n.bits_out = list(bits)
            if kind in ("port_out", "port_inout"):
                n.bits_in = list(bits)
            self.nodes[n.id] = n

    # -- register naming -----------------------------------------------------
    @staticmethod
    def _cell_prefix(cell_name: str) -> str:
        """Instance-path prefix of a flattened cell name ('a.b.$procdff$3' -> 'a.b.')."""
        name = cell_name
        if name.startswith("$flatten"):
            name = name[len("$flatten"):]
        name = name.replace("\\", "")
        parts = name.split(".")
        out = []
        for p in parts:
            if p.startswith("$"):
                break
            out.append(p)
        if len(out) == len(parts):          # explicitly named cell: drop its own name
            out = out[:-1]
        return ".".join(out) + ("." if out else "")

    def _score_name(self, name: str, width: int, prefix: str) -> tuple:
        d = self.design
        hidden = d.net_hidden.get(name, True)
        inside = name.startswith(prefix)
        depth = name.count(".")
        rel_depth = -(depth - prefix.count(".")) if inside else -depth
        exact = len(d.netnames.get(name, [])) == width
        bonus = bool(_SUFFIX_BONUS.search(name))
        return (not hidden, not name.startswith("$"), inside, exact, rel_depth, bonus, -len(name), name)

    def _best_name_for_bits(self, bits: List[int], width: int, prefix: str) -> Optional[str]:
        d = self.design
        cands = None
        for b in bits:
            names = {n for n, _ in d.names_of_bit(b)}
            cands = names if cands is None else (cands & names)
            if not cands:
                return None
        if not cands:
            return None
        return max(cands, key=lambda n: self._score_name(n, width, prefix))

    def _reg_name(self, cell: Cell, qbits: List[int]) -> str:
        prefix = self._cell_prefix(cell.name)
        name = self._best_name_for_bits(qbits, len(qbits), prefix)
        if name is None:
            names = self.design.names_of_bit(qbits[0])
            if names:
                name = max((n for n, _ in names), key=lambda n: self._score_name(n, len(qbits), prefix))
            else:
                name = cell.name
        if self.collapse_arrays:
            # mem2reg'd arrays: 'a.b.history[3]' where 'a.b.history' is not itself a wire
            m = re.match(r"^(.*)\[(\d+)\]$", name)
            if m and m.group(1) not in self.design.netnames:
                return m.group(1) + "[*]"
        return name

    def _vec_index(self, name: str, bit: int) -> Optional[int]:
        """Index of `bit` inside the RTL vector `name`, if the bit carries that name."""
        for n, i in self.design.names_of_bit(bit):
            if n == name:
                return i
        return None

    def _split_cell(self, cell: Cell, name: str, ranges: Optional[List[Tuple[int, int]]]):
        """Distribute one flip-flop cell's bits over the sub-registers of a split plan.

        Yields (sub_name, {"q", "out", "in", "range"}).  Pins as wide as Q are split
        positionally (D[i] belongs with Q[i]); narrower pins (EN, ARST, ...) go to every
        sub-register.  Bits whose index is unknown or outside every range stay on the
        unsplit remainder `name`."""
        q = cell.outputs.get("Q", [])
        parts: Dict[Optional[Tuple[int, int]], dict] = {}

        def part(key):
            if key not in parts:
                parts[key] = {"q": [], "out": [], "in": [], "range": key}
            return parts[key]

        if not ranges:
            p = part(None)
            p["q"] = [b for b in q if b is not None]
            p["out"] = [b for pn, bits in cell.outputs.items() if pn != "Q" for b in bits if b is not None]
            p["in"] = [b for bits in cell.inputs.values() for b in bits if b is not None]
            yield name, p
            return
        key_of_pos: List[Optional[Tuple[int, int]]] = []
        for b in q:
            key = None
            if b is not None:
                idx = self._vec_index(name, b)
                if idx is not None:
                    key = next((r for r in ranges if r[0] <= idx <= r[1]), None)
            key_of_pos.append(key)
            if b is not None:
                part(key)["q"].append(b)
        for pn, bits in list(cell.outputs.items()) + list(cell.inputs.items()):
            if pn == "Q":
                continue
            slot = "out" if pn in cell.outputs else "in"
            if len(bits) == len(q) and len(q) > 1:
                for b, key in zip(bits, key_of_pos):
                    if b is not None:
                        part(key)[slot].append(b)
            else:
                for p in parts.values():
                    p[slot].extend(b for b in bits if b is not None)
        for key, p in parts.items():
            if key is None:
                yield name, p
            else:
                lo, hi = key
                yield (f"{name}[{lo}]" if lo == hi else f"{name}[{lo}:{hi}]"), p

    def _build_sequential(self):
        d = self.design
        groups: Dict[str, Node] = {}
        for cell in d.cells:
            if cell.kind in ("ff", "latch"):
                qbits = [b for b in cell.outputs.get("Q", []) if b is not None]
                if not qbits:
                    continue
                name = self._reg_name(cell, qbits)
                ranges = self.split_regs.get(name) if not name.endswith("[*]") else None
                for sub_name, sub in self._split_cell(cell, name, ranges):
                    nid = f"R:{sub_name}"
                    node = groups.get(nid)
                    if node is None:
                        node = Node(id=nid, kind="seq" if cell.kind == "ff" else "latch", name=sub_name)
                        node.attrs["cell_types"] = Counter()
                        node.attrs["_qbits"] = set()
                        if sub_name != name:
                            node.attrs["split_of"] = f"R:{name}"
                            node.attrs["split_base"] = name
                            node.attrs["bit_range"] = list(sub["range"])
                        groups[nid] = node
                    node.cells.append(cell.idx)
                    node.attrs["_qbits"].update(sub["q"])
                    node.bits_out.extend(sub["q"])
                    node.bits_out.extend(sub["out"])
                    node.bits_in.extend(sub["in"])
                    for pname, bits in cell.clock_pins.items():
                        node.clk_bits.extend(b for b in bits if b is not None)
                    node.attrs["cell_types"][cell.type] += 1
                    if cell.async_pins:
                        node.attrs["async_pins"] = sorted(set(node.attrs.get("async_pins", [])) | cell.async_pins)
                    self.cell_node.setdefault(cell.idx, nid)
            elif cell.kind == "mem":
                memid = d.param_str(cell, "MEMID", cell.name).lstrip("\\")
                nid = f"M:{memid}"
                node = groups.get(nid)
                if node is None:
                    node = Node(id=nid, kind="memory", name=memid)
                    node.attrs["cell_types"] = Counter()
                    groups[nid] = node
                node.cells.append(cell.idx)
                node.attrs["cell_types"][cell.type] += 1
                w = d.param_int(cell, "WIDTH", 0)
                if w:
                    node.attrs["word_width"] = max(node.attrs.get("word_width", 0), w)
                sz = d.param_int(cell, "SIZE", 0)
                if sz:
                    node.attrs["depth"] = sz
                for pname, bits in cell.outputs.items():
                    node.bits_out.extend(b for b in bits if b is not None)
                for pname, bits in cell.inputs.items():
                    node.bits_in.extend(b for b in bits if b is not None)
                for pname, bits in cell.clock_pins.items():
                    node.clk_bits.extend(b for b in bits if b is not None)
                self.cell_node[cell.idx] = nid
            elif cell.kind == "blackbox":
                nid = f"B:{cell.name}"
                node = Node(id=nid, kind="blackbox", name=cell.name)
                node.attrs["cell_types"] = Counter({cell.type: 1})
                node.cells.append(cell.idx)
                for pname, bits in cell.outputs.items():
                    node.bits_out.extend(b for b in bits if b is not None)
                for pname, bits in cell.inputs.items():
                    node.bits_in.extend(b for b in bits if b is not None)
                for pname, bits in cell.clock_pins.items():
                    node.clk_bits.extend(b for b in bits if b is not None)
                groups[nid] = node
                self.cell_node[cell.idx] = nid
        for nid, node in groups.items():
            node.bits_out = sorted(set(node.bits_out))
            node.bits_in = sorted(set(node.bits_in))
            node.clk_bits = sorted(set(node.clk_bits))
            if node.kind == "memory":
                node.width = node.attrs.get("word_width", len(node.bits_out))
            elif node.kind == "blackbox":
                node.width = len(node.bits_out)
            else:
                # width of the RTL vector: distinct Q bits of this (sub-)register; also record declared width
                qbits = node.attrs.pop("_qbits", set())
                node.width = len(qbits)
                base = node.attrs.get("split_base", node.name)
                if node.name.endswith("[*]"):
                    # collapsed array: width of one word, depth = number of words
                    words = set()
                    for ci in node.cells:
                        for b in d.cells[ci].outputs.get("Q", []):
                            if b is None:
                                continue
                            for nm, _i in d.names_of_bit(b):
                                if nm.startswith(node.name[:-3]) and re.match(r"^\[\d+\]$", nm[len(node.name) - 3:]):
                                    words.add(nm)
                    if words:
                        node.attrs["depth"] = len(words)
                        node.width = max(len(d.netnames[w]) for w in words)
                        node.attrs["array"] = True
                decl = d.netnames.get(base)
                if decl is not None:
                    node.attrs["declared_width"] = len(decl)
                    if len(decl) != node.width and "split_of" not in node.attrs:
                        node.attrs["partial_vector"] = True
            node.attrs["cell_types"] = dict(node.attrs["cell_types"])
            node.attrs["cell_count"] = len(node.cells)
            self.nodes[nid] = node

    def _index_bits(self):
        for n in self.nodes.values():
            if n.kind == "comb":
                continue
            if n.is_source:
                for b in n.bits_out:
                    self.bit_source_node.setdefault(b, n.id)
            if n.is_sink:
                for b in n.bits_in:
                    self.bit_sink_nodes[b].append(n.id)
        # sinks and sources get mask indices
        self.sink_ids = sorted(n.id for n in self.nodes.values() if n.is_sink)
        self.sink_index = {nid: i for i, nid in enumerate(self.sink_ids)}
        self.source_ids = sorted(n.id for n in self.nodes.values() if n.is_source)
        self.source_index = {nid: i for i, nid in enumerate(self.source_ids)}
        # instance path -> module type, from the $scopeinfo cells `flatten` leaves behind
        for cell in self.design.cells:
            if cell.type == "$scopeinfo":
                self.module_of[cell.name.replace("\\", "")] = str(cell.attrs.get("module", ""))

    def cell_hier(self, cell: Cell) -> str:
        """Instance path a flattened cell came from ('' at top level)."""
        return self._cell_prefix(cell.name).rstrip(".")

    def module_type(self, hier: str) -> str:
        return _pretty_module(self.module_of.get(hier, self.design.top if not hier else ""))

    def set_module_ownership(self, node: Node):
        """Record every owning instance from actual cell membership.

        `modules` is authoritative, including when a later pass combines cells
        from multiple instances. Legacy `hier` is their common enclosing scope
        and `module` is a display string; single-owner values stay unchanged.
        """
        counts = Counter(self.cell_hier(self.design.cells[ci]) for ci in set(node.cells))
        owners = [{"hier": hier, "module": self.module_type(hier), "cell_count": count}
                  for hier, count in sorted(counts.items())]
        node.attrs["modules"] = owners
        if not owners:
            return
        scopes = [owner["hier"].split(".") if owner["hier"] else [] for owner in owners]
        common = []
        for parts in zip(*scopes):
            if len(set(parts)) != 1:
                break
            common.append(parts[0])
        node.attrs["hier"] = ".".join(common)
        node.attrs["module"] = " + ".join(sorted({owner["module"] for owner in owners if owner["module"]}))

    def bit_vec_index(self, nid: str, bit: int) -> Optional[int]:
        """Index of `bit` in the RTL vector behind node `nid` (the base vector for split registers)."""
        node = self.nodes.get(nid)
        if node is None:
            return None
        if node.is_port:
            for n, i in self.design.names_of_bit(bit):
                if n == node.name:
                    return i
            return None
        return self._vec_index(node.attrs.get("split_base", node.name), bit)

    def vec_indices(self, nid: str) -> List[int]:
        """Sorted vector indices of the Q bits a register node holds."""
        node = self.nodes[nid]
        out = {self.bit_vec_index(nid, b) for b in node.bits_out}
        return sorted(i for i in out if i is not None)

    # -- combinational partition ------------------------------------------------
    def _partition_comb(self):
        d = self.design
        self.comb_cells = [c.idx for c in d.cells if c.kind == "comb" and c.type not in _META_TYPES]
        self.comb_local = {ci: i for i, ci in enumerate(self.comb_cells)}
        n = len(self.comb_cells)
        # fanout among comb cells, direct sink masks, direct source masks
        fanout: List[List[int]] = [[] for _ in range(n)]
        direct_mask: List[int] = [0] * n
        direct_src: List[int] = [0] * n
        for i, ci in enumerate(self.comb_cells):
            cell = d.cells[ci]
            fo = set()
            m = 0
            for b in cell.all_output_bits():
                if b is None:
                    continue
                for sid in self.bit_sink_nodes.get(b, ()):
                    m |= 1 << self.sink_index[sid]
                for (rc, _p) in d.bit_readers.get(b, ()):
                    if rc >= 0 and rc in self.comb_local:
                        fo.add(self.comb_local[rc])
            fanout[i] = list(fo)
            direct_mask[i] = m
            sm = 0
            for b in cell.all_input_bits():
                if b is None:
                    continue
                src = self.bit_source_node.get(b)
                if src is not None and src in self.source_index:
                    sm |= 1 << self.source_index[src]
            direct_src[i] = sm
        # Tarjan SCC (iterative); emits SCCs in reverse topological order
        index = [-1] * n
        low = [0] * n
        onstack = [False] * n
        stack: List[int] = []
        scc_of = [-1] * n
        sccs: List[List[int]] = []
        counter = 0
        for root in range(n):
            if index[root] != -1:
                continue
            work = [(root, 0)]
            index[root] = low[root] = counter; counter += 1
            stack.append(root); onstack[root] = True
            while work:
                v, pi = work[-1]
                if pi < len(fanout[v]):
                    w = fanout[v][pi]
                    work[-1] = (v, pi + 1)
                    if index[w] == -1:
                        index[w] = low[w] = counter; counter += 1
                        stack.append(w); onstack[w] = True
                        work.append((w, 0))
                    elif onstack[w]:
                        if index[w] < low[v]:
                            low[v] = index[w]
                else:
                    work.pop()
                    if work:
                        u = work[-1][0]
                        if low[v] < low[u]:
                            low[u] = low[v]
                    if low[v] == index[v]:
                        comp = []
                        while True:
                            w = stack.pop(); onstack[w] = False
                            scc_of[w] = len(sccs)
                            comp.append(w)
                            if w == v:
                                break
                        sccs.append(comp)
        self.scc_of = scc_of
        self.scc_order = sccs
        scc_fanout: List[Set[int]] = [set() for _ in sccs]
        for i in range(n):
            si = scc_of[i]
            for w in fanout[i]:
                sw = scc_of[w]
                if sw != si:
                    scc_fanout[si].add(sw)
        self.scc_fanout = scc_fanout
        sinkmask = [0] * len(sccs)
        for si, comp in enumerate(sccs):          # reverse topological: fanouts done first
            m = 0
            for i in comp:
                m |= direct_mask[i]
            for sw in scc_fanout[si]:
                m |= sinkmask[sw]
            sinkmask[si] = m
        self.scc_sinkmask = sinkmask
        # source masks flow the other way (sources first = forward topological order)
        scc_fanin: List[Set[int]] = [set() for _ in sccs]
        for si, fos in enumerate(scc_fanout):
            for sw in fos:
                scc_fanin[sw].add(si)
        srcmask = [0] * len(sccs)
        for si in range(len(sccs) - 1, -1, -1):
            m = 0
            for i in sccs[si]:
                m |= direct_src[i]
            for sp in scc_fanin[si]:
                m |= srcmask[sp]
            srcmask[si] = m
        self.scc_srcmask = srcmask
        # Collapse only serial stretches. Forks, joins and module re-entry must
        # remain boundaries even when their transitive source/sink sets agree.
        region = {}
        signatures = [{(sinkmask[si], srcmask[si], self.cell_hier(d.cells[self.comb_cells[i]]))
                       for i in comp} for si, comp in enumerate(sccs)]
        for si in range(len(sccs) - 1, -1, -1):
            pred = next(iter(scc_fanin[si])) if len(scc_fanin[si]) == 1 else None
            region[si] = (region[pred] if pred is not None and len(scc_fanout[pred]) == 1
                          and signatures[si] == signatures[pred] and len(signatures[si]) == 1
                          else si)
        by_key = defaultdict(list)
        dangling = 0
        for i, ci in enumerate(self.comb_cells):
            m = sinkmask[scc_of[i]]
            if m == 0:
                dangling += 1
                continue
            by_key[(m, srcmask[scc_of[i]], self.cell_hier(d.cells[ci]), region[scc_of[i]])].append(ci)
        self.stats["dangling_comb_cells"] = dangling
        # deterministic numbering: fewer sinks first, then sink names, module, sources
        def group_key(key):
            m, sm, hier, region_id = key
            sinks = [self.sink_ids[k] for k in _bits_of(m)]
            return (len(sinks), sinks, hier, [self.source_ids[k] for k in _bits_of(sm)],
                    sorted(d.cells[ci].name for ci in by_key[key]))
        for k, key in enumerate(sorted(by_key, key=group_key)):
            m, sm, hier, region_id = key
            cells = by_key[key]
            nid = f"C{k}"
            node = Node(id=nid, kind="comb", name=nid, cells=cells)
            sinks = [self.sink_ids[b] for b in _bits_of(m)]
            node.attrs["sinks"] = sinks
            node.attrs["sink_count"] = len(sinks)
            node.attrs["all_sources"] = [self.source_ids[b] for b in _bits_of(sm)]   # transitive
            self.set_module_ownership(node)
            node.attrs["combinational_loop"] = any(
                len(sccs[scc_of[self.comb_local[ci]]]) > 1
                or self.comb_local[ci] in fanout[self.comb_local[ci]] for ci in cells)
            node.attrs["cell_count"] = len(cells)
            types = Counter(d.cells[ci].type for ci in cells)
            node.attrs["cell_types"] = dict(types.most_common(12))
            node.attrs["_mask"] = m
            node.attrs["_srcmask"] = sm
            self.nodes[nid] = node
            for ci in cells:
                self.cell_node[ci] = nid

    # -- edges -----------------------------------------------------------------
    def _add_edge(self, src: str, dst: str, is_async: bool = False):
        e = self.edges.get((src, dst))
        if e is None:
            e = Edge(src, dst)
            self.edges[(src, dst)] = e
        e.width += 1
        if is_async:
            e.async_width += 1

    def _build_edges(self):
        d = self.design
        # comb cell inputs: element -> comb, comb -> comb
        for ci in self.comb_cells:
            nid = self.cell_node.get(ci)
            if nid is None:
                continue
            cell = d.cells[ci]
            for b in cell.all_input_bits():
                if b is None:
                    continue
                src = self.bit_source_node.get(b)
                if src is not None:
                    self._add_edge(src, nid)
                    continue
                drv = d.bit_driver.get(b)
                if drv is None:
                    continue
                dc, _ = drv
                if dc >= 0:
                    dn = self.cell_node.get(dc)
                    if dn is not None and dn != nid:
                        self._add_edge(dn, nid)
        # sink inputs: comb -> sink, element -> sink (direct)
        for node in list(self.nodes.values()):
            if not node.is_sink:
                continue
            async_bits = self._async_bits(node)
            for b in node.bits_in:
                is_async = b in async_bits
                src = self.bit_source_node.get(b)
                if src is not None:
                    if src != node.id or node.kind != "comb":
                        self._add_edge(src, node.id, is_async)
                    continue
                drv = d.bit_driver.get(b)
                if drv is None:
                    continue
                dc, _ = drv
                if dc >= 0:
                    dn = self.cell_node.get(dc)
                    if dn is not None and dn != node.id:
                        self._add_edge(dn, node.id, is_async)
        # direct sources per comb node (for later passes)
        srcs: Dict[str, Set[str]] = defaultdict(set)
        preds: Dict[str, Set[str]] = defaultdict(set)
        for (s, t), e in self.edges.items():
            if self.nodes[t].kind == "comb":
                if self.nodes[s].kind == "comb":
                    preds[t].add(s)
                else:
                    srcs[t].add(s)
        for nid, node in self.nodes.items():
            if node.kind == "comb":
                node.attrs["sources"] = sorted(srcs.get(nid, ()))
                node.attrs["comb_preds"] = sorted(preds.get(nid, ()))

    def _async_bits(self, node: Node) -> Set[int]:
        out = set()
        for ci in node.cells:
            cell = self.design.cells[ci]
            for pname in cell.async_pins:
                out.update(b for b in cell.inputs.get(pname, []) if b is not None)
        return out

    # ------------------------------------------------------------- helpers
    def combs_feeding(self, sink: str) -> List[str]:
        """Comb nodes that list `sink` among their sinks (indexed once, on first use)."""
        idx = self.__dict__.get("_by_sink")
        if idx is None:
            idx = defaultdict(list)
            for nid, n in self.nodes.items():
                if n.kind == "comb":
                    for s in n.attrs.get("sinks", ()):
                        idx[s].append(nid)
            self.__dict__["_by_sink"] = idx
        return idx.get(sink, [])

    def combs_fed_by(self, source: str) -> List[str]:
        """Comb nodes that `source` reaches through comb logic (transitive; indexed once)."""
        idx = self.__dict__.get("_by_source")
        if idx is None:
            idx = defaultdict(list)
            for nid, n in self.nodes.items():
                if n.kind == "comb":
                    for s in n.attrs.get("all_sources", ()):
                        idx[s].append(nid)
            self.__dict__["_by_source"] = idx
        return idx.get(source, [])

    def successors(self, nid: str):
        return [t for (s, t) in self.edges if s == nid]

    def predecessors(self, nid: str):
        return [s for (s, t) in self.edges if t == nid]

    def transitive_sources(self, comb_id: str) -> Set[str]:
        """All non-comb sources feeding comb node `comb_id` (through comb -> comb edges).

        The partition already propagated this set (`all_sources`, from the source masks), so
        this is a lookup; the walk below only serves nodes built without it."""
        n = self.nodes[comb_id]
        if "all_sources" in n.attrs:
            return set(n.attrs["all_sources"])
        seen = set()
        out = set()
        stack = [comb_id]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            n = self.nodes[c]
            out.update(n.attrs.get("sources", ()))
            stack.extend(n.attrs.get("comb_preds", ()))
        return out


def _pretty_module(name: str) -> str:
    r"""Yosys parametrised module name -> 'base(P=40, Q=3)'.

    '$paramod\window_lane\DEPTH=s32'00000000000000000000000000101000' -> 'window_lane(DEPTH=40)'."""
    if not name.startswith("$paramod"):
        return name.replace("\\", "")
    # '$paramod' \ base \ P=v \ P=v ...   (or '$paramod$<hash>' \ base when the list is too long)
    tokens = name.split("\\")[1:]
    if not tokens:
        return name
    base = tokens[0]
    params = []
    for t in tokens[1:]:
        if "=" not in t:
            continue
        k, v = t.split("=", 1)
        m = re.match(r"^s?(\d+)'([01]+)$", v)
        if m:
            v = str(int(m.group(2), 2))
        params.append(f"{k}={v}")
    return base + (f"({', '.join(params)})" if params else "")


def _bits_of(m: int):
    """Yield set-bit positions of int m (ascending)."""
    while m:
        low = m & -m
        yield low.bit_length() - 1
        m ^= low
