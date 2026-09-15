"""Clock-domain tracing and CDC detection on the abstract graph.

A *root* is a top-level input port bit that a register's clock pin traces back
to, through combinational clock logic (gates, muxes) and through clock-
generating registers (dividers).  Roots are named `port` (scalar port) or
`port[i]` (bit of a vector port).  Two roots are asynchronous unless the SDC
places their clocks in the same `set_clock_groups` group or a `--sync` override
says so.  Without any SDC, every root is its own asynchronous domain.

Clock *names* are decoration: master clock names from create_clock on ports,
generated-clock names attached to divider outputs or register clock pins.
Port data bits get a domain from set_input_delay/set_output_delay (parsed from
the SDC text) or from the OpenSTA endpoint data (launch / capture clocks).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .abstract import AbstractGraph, Node, _bits_of


@dataclass
class ClockDef:
    name: str
    period: Optional[float] = None
    is_generated: bool = False
    master: Optional[str] = None
    sources: List[str] = field(default_factory=list)   # netlist port / pin names
    source_obj: Optional[str] = None                   # -source of a generated clock


@dataclass
class SdcInfo:
    clocks: Dict[str, ClockDef] = field(default_factory=dict)
    groups: List[List[str]] = field(default_factory=list)      # set_clock_groups (async/exclusive)
    port_clock: Dict[str, str] = field(default_factory=dict)   # 'port' or 'port[i]' -> clock (io delays)


# ---------------------------------------------------------------------------
# SDC (plain-text subset) parser -- best effort; Tcl loops/variables are skipped
# ---------------------------------------------------------------------------
_GETOBJ = re.compile(r"\[\s*(?:get_ports|get_pins|get_clocks|get_cells|get_nets)\s+((?:-\w+\s+)*)(\{[^}]*\}|\S+?)\s*\]")


def _has_tcl(s: str) -> bool:
    return "$" in s or "[expr" in s or "[format" in s or "[lindex" in s


def _objs(s: str) -> List[str]:
    out = []
    for m in _GETOBJ.finditer(s):
        t = m.group(2).strip("{}")
        out.extend(x for x in t.split() if not _has_tcl(x))
    if not out:
        t = s.strip().strip("{}")
        out.extend(x for x in t.split() if not x.startswith("-") and not x.startswith("[") and not _has_tcl(x))
    return out


def _arg_after(line: str, flag: str) -> Optional[str]:
    """Value following `flag`: a {..} group, a bracket-balanced [..] command, or a word."""
    m = re.search(re.escape(flag) + r"\s+", line)
    if not m:
        return None
    i = m.end()
    if i >= len(line):
        return None
    if line[i] == "{":
        j = line.find("}", i)
        return line[i:j + 1] if j > 0 else line[i:]
    if line[i] == "[":
        depth = 0
        for j in range(i, len(line)):
            if line[j] == "[":
                depth += 1
            elif line[j] == "]":
                depth -= 1
                if depth == 0:
                    return line[i:j + 1]
        return line[i:]
    m2 = re.match(r"\S+", line[i:])
    return m2.group(0) if m2 else None


def _opt(line: str, flag: str, objs: bool = False) -> Optional[str]:
    v = _arg_after(line, flag)
    if v is None:
        return None
    if objs:
        o = _objs(v)
        return o[0] if o else None
    v = v.strip("{}")
    return None if _has_tcl(v) else v


def _strip_opts(line: str, flags: Iterable[str]) -> str:
    for f in flags:
        while True:
            v = _arg_after(line, f)
            m = re.search(re.escape(f) + r"\s+", line)
            if v is None or m is None:
                break
            line = line[:m.start()] + " " + line[m.end() + len(v):]
        line = line.replace(f, " ")
    return " ".join(line.split()[1:])


def parse_sdc_text(path: str) -> SdcInfo:
    info = SdcInfo()
    try:
        text = open(path).read()
    except OSError:
        return info
    text = re.sub(r"\\\r?\n", " ", text)
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        cmd = toks[0]
        if cmd == "create_clock":
            name = _opt(line, "-name")
            period = _opt(line, "-period")
            targets = _objs(_strip_opts(line, ["-name", "-period", "-waveform", "-add"]))
            if not name:
                name = targets[0] if targets else None
            if name:
                cd = info.clocks.setdefault(name, ClockDef(name))
                try:
                    cd.period = float(period) if period else cd.period
                except ValueError:
                    pass
                cd.sources = targets
        elif cmd == "create_generated_clock":
            name = _opt(line, "-name")
            src = _opt(line, "-source", objs=True)
            master = _opt(line, "-master_clock")
            targets = _objs(_strip_opts(line, ["-name", "-source", "-master_clock", "-divide_by",
                                                 "-multiply_by", "-edges", "-edge_shift", "-duty_cycle",
                                                 "-invert", "-add", "-combinational"]))
            if name:
                cd = info.clocks.setdefault(name, ClockDef(name, is_generated=True))
                cd.is_generated = True
                cd.sources = targets
                cd.master = master or cd.master
                cd.source_obj = src
        elif cmd == "set_clock_groups" and any(t in toks for t in ("-asynchronous", "-logically_exclusive",
                                                                    "-physically_exclusive")):
            parsed = []
            for g in re.findall(r"-group\s+(\{[^}]*\}|\[[^\]]*\]|\S+)", line):
                names = _objs(g) if g.startswith("[") else [x for x in g.strip("{}").split() if not _has_tcl(x)]
                if names:
                    parsed.append(names)
            info.groups.extend(parsed)
        elif cmd in ("set_input_delay", "set_output_delay"):
            clk = _opt(line, "-clock", objs=True)
            targets = _objs(_strip_opts(line, ["-clock", "-max", "-min", "-add_delay", "-clock_fall",
                                                 "-rise", "-fall", "-reference_pin", "-source_latency_included",
                                                 "-network_latency_included"]))
            if clk:
                for t in targets:
                    info.port_clock[t] = clk
    # resolve generated-clock masters from their -source port
    port_clock = {}
    for cd in info.clocks.values():
        if not cd.is_generated:
            for s in cd.sources:
                port_clock[s] = cd.name
    for cd in info.clocks.values():
        if cd.is_generated and not cd.master and cd.source_obj in port_clock:
            cd.master = port_clock[cd.source_obj]
    return info


# ---------------------------------------------------------------------------
# Domain tracing
# ---------------------------------------------------------------------------
_SELECT_PINS = {"S", "EN", "E", "SEL", "CE"}
_GATING_TYPES = {"$and", "$or", "$logic_and", "$logic_or", "$nand", "$nor", "$_AND_", "$_OR_", "$_NAND_", "$_NOR_",
                 "$_ANDNOT_", "$_ORNOT_", "$reduce_and", "$reduce_or"}


class ClockAnalysis:
    def __init__(self, graph: AbstractGraph, sdc: Optional[SdcInfo] = None,
                 sta_clocks: Optional[Dict[str, ClockDef]] = None,
                 name_to_node: Optional[Dict[str, str]] = None,
                 port_clock_hints: Optional[Dict[str, str]] = None, cdc_async_pins: bool = False):
        """name_to_node: netlist object (instance pin / net) -> abstract node id, from the
        STA mapping, used to place generated clocks.  port_clock_hints: 'port[i]' -> clock."""
        self.g = graph
        self.d = graph.design
        self.sdc = sdc or SdcInfo()
        self.clocks: Dict[str, ClockDef] = {k: ClockDef(**vars(v)) for k, v in self.sdc.clocks.items()}
        for k, v in (sta_clocks or {}).items():
            c = self.clocks.get(k)
            if c is None:
                self.clocks[k] = ClockDef(**vars(v))
            else:
                c.period = v.period if v.period else c.period
                c.is_generated = c.is_generated or v.is_generated
                c.master = c.master or v.master
                c.sources = c.sources or v.sources
        self.name_to_node = name_to_node or {}
        self.cdc_async_pins = cdc_async_pins        # count crossings into ARST/SET/CLR pins as CDC?
        self.port_clock_hints: Dict[str, str] = dict(self.sdc.port_clock)
        self.port_clock_hints.update(port_clock_hints or {})
        # port bit -> root key
        self.bit_root_key: Dict[int, str] = {}
        for p in self.d.ports.values():
            if p.direction in ("input", "inout"):
                for i, b in enumerate(p.bits):
                    if b is not None:
                        self.bit_root_key[b] = self.port_key(p.name, i)
        self.roots: List[str] = []
        self.root_index: Dict[str, int] = {}
        self.node_roots: Dict[str, int] = {}              # seq node id -> root mask
        self.port_bit_roots: Dict[int, int] = {}          # data port bit -> root mask (from io delays)
        self.node_clock_names: Dict[str, Set[str]] = defaultdict(set)
        self.generator_nodes: Dict[str, Set[str]] = defaultdict(set)
        self.root_clock: Dict[str, str] = {}
        self.root_group: Dict[str, int] = {}
        self.async_mask: List[int] = []
        self.unresolved: List[str] = []
        self._trace_memo: Dict[int, Set[Tuple[str, str]]] = {}
        self._node_roots_memo: Dict[str, Set[str]] = {}
        self.declared_roots: Set[str] = set()
        for cd in self.clocks.values():
            if not cd.is_generated:
                for src in cd.sources:
                    self.declared_roots.update(self.port_keys_of_obj(src))
        self.undeclared_clock_ports: Dict[str, Set[str]] = defaultdict(set)   # node -> port keys
        # generated clocks defined on internal wires / hierarchical pins cut the backward trace
        self.bit_clock_def: Dict[int, str] = {}
        for cd in self.clocks.values():
            if not cd.is_generated:
                continue
            for src in cd.sources:
                src = src.strip("{}")
                if "/" not in src and self.port_keys_of_obj(src):
                    continue
                if re.search(r"/(CK|CLK|C|CP|CKN|CLKB|G|E|EN)$", src):
                    continue                      # defined on a register clock pin: handled per node
                bits = self.d.lookup_name(src.replace("/", "."))
                for b in bits or []:
                    if b is not None:
                        self.bit_clock_def.setdefault(b, cd.name)
        self._clockdef_roots_memo: Dict[str, Set[str]] = {}
        self._gen_clock_nodes: Dict[str, List[str]] = {}
        self.node_clock_override: Dict[str, Set[str]] = {}

    # -- keys ------------------------------------------------------------------
    def port_key(self, name: str, idx: int) -> str:
        p = self.d.ports.get(name)
        if p is not None and len(p.bits) == 1:
            return name
        return f"{name}[{idx}]"

    def port_keys_of_obj(self, obj: str) -> List[str]:
        """'clk[0]' -> ['clk[0]'] ; 'clk' (vector) -> all bits ; 'rst' -> ['rst']"""
        obj = obj.strip("{}")
        m = re.match(r"^([^/\[\]]+)(?:\[(\d+)\])?$", obj)
        if not m:
            return []
        name, idx = m.group(1), m.group(2)
        p = self.d.ports.get(name)
        if p is None:
            return []
        if idx is not None:
            return [self.port_key(name, int(idx))]
        return [self.port_key(name, i) for i in range(len(p.bits))]

    def port_bits_of_obj(self, obj: str) -> List[int]:
        obj = obj.strip("{}")
        m = re.match(r"^([^/\[\]]+)(?:\[(\d+)\])?$", obj)
        if not m:
            return []
        p = self.d.ports.get(m.group(1))
        if p is None:
            return []
        if m.group(2) is not None:
            i = int(m.group(2))
            return [p.bits[i]] if 0 <= i < len(p.bits) and p.bits[i] is not None else []
        return [b for b in p.bits if b is not None]

    # -- tracing ---------------------------------------------------------------
    def _trace_bit(self, bit: int, guard: Set[int], cut_defs: bool = True) -> Set[Tuple[str, str]]:
        """Clock sources of a bit: ('root', key) | ('clockdef', clock) | ('node', seq node id) |
        ('undeclared', port key).  cut_defs=False ignores a clock definition on `bit` itself."""
        if bit in self._trace_memo and cut_defs:
            return self._trace_memo[bit]
        if bit in guard:
            return set()
        guard.add(bit)
        out: Set[Tuple[str, str]] = set()
        if bit in self.bit_root_key:
            key = self.bit_root_key[bit]
            if not self.declared_roots or key in self.declared_roots:
                out.add(("root", key))
            else:
                out.add(("undeclared", key))
        elif bit in self.bit_clock_def and not cut_defs is False:
            out.add(("clockdef", self.bit_clock_def[bit]))
        else:
            src = self.g.bit_source_node.get(bit)
            if src is not None:
                out.add(("node", src))
            else:
                drv = self.d.bit_driver.get(bit)
                if drv is not None and drv[0] >= 0:
                    cell = self.d.cells[drv[0]]
                    per_input = []
                    for pname, bits in cell.inputs.items():
                        if pname in _SELECT_PINS:          # mux selects / enables are not clocks
                            continue
                        sub: Set[Tuple[str, str]] = set()
                        for b in bits:
                            if b is not None:
                                sub |= self._trace_bit(b, guard)
                        per_input.append(sub)
                    if cell.type in _GATING_TYPES:
                        # clock gating (AND/OR of a clock with an enable): keep the input(s) that
                        # reach a clock root; otherwise the input(s) that reach a clock generator
                        def clocklike(src):
                            for k, n in src:
                                if k in ("root", "clockdef"):
                                    return True
                                if k == "node" and self.g.nodes[n].kind != "latch":
                                    return True
                            return False
                        keep = [s for s in per_input if clocklike(s)]
                        for s in (keep or per_input):
                            out |= s
                    else:
                        for s in per_input:
                            out |= s
        guard.discard(bit)
        if cut_defs:
            self._trace_memo[bit] = out
        return out

    def _clockdef_roots(self, clock: str) -> Set[str]:
        """Root port keys behind a generated clock defined on an internal wire."""
        if clock in self._clockdef_roots_memo:
            return self._clockdef_roots_memo[clock]
        self._clockdef_roots_memo[clock] = set()
        roots: Set[str] = set()
        cd = self.clocks.get(clock)
        if cd is not None and cd.master:
            m = self.clocks.get(cd.master)
            if m is not None:
                for src in m.sources:
                    roots.update(k for k in self.port_keys_of_obj(src) if not self.declared_roots or k in self.declared_roots)
        if not roots:
            for b, name in self.bit_clock_def.items():
                if name != clock:
                    continue
                for kind, n in self._trace_bit(b, set(), cut_defs=False):
                    if kind == "root":
                        roots.add(n)
                    elif kind == "clockdef":
                        roots |= self._clockdef_roots(n)
                    elif kind == "node":
                        self.generator_nodes.setdefault(n, set()).add(clock)
                        roots |= self._node_root_names(n, set())
        self._clockdef_roots_memo[clock] = roots
        return roots

    def _node_root_names(self, nid: str, guard: Set[str]) -> Set[str]:
        if nid in self._node_roots_memo:
            return self._node_roots_memo[nid]
        if nid in guard:
            return set()
        guard.add(nid)
        node = self.g.nodes[nid]
        roots: Set[str] = set()
        for b in node.clk_bits:
            for kind, name in self._trace_bit(b, set()):
                if kind == "root":
                    roots.add(name)
                elif kind == "clockdef":
                    roots |= self._clockdef_roots(name)
                elif kind == "undeclared":
                    self.undeclared_clock_ports[nid].add(name)
                else:
                    self.generator_nodes.setdefault(name, set())
                    roots |= self._node_root_names(name, guard)
        guard.discard(nid)
        self._node_roots_memo[nid] = roots
        return roots

    def run(self, sync_roots: Optional[List[List[str]]] = None):
        g = self.g
        node_root_names: Dict[str, Set[str]] = {}
        for nid, node in g.nodes.items():
            if node.is_seq and node.clk_bits:
                node_root_names[nid] = self._node_root_names(nid, set())
        allroots: Set[str] = set()
        for r in node_root_names.values():
            allroots |= r
        # master clocks declared on ports are roots even if nothing traced to them
        for cd in self.clocks.values():
            if not cd.is_generated:
                for s in cd.sources:
                    for k in self.port_keys_of_obj(s):
                        allroots.add(k)
                        self.root_clock[k] = cd.name
        self.roots = sorted(allroots, key=_natkey)
        self.root_index = {r: i for i, r in enumerate(self.roots)}
        for r in self.roots:
            self.root_clock.setdefault(r, r)
        for nid, names in node_root_names.items():
            m = 0
            for r in names:
                m |= 1 << self.root_index[r]
            self.node_roots[nid] = m
            if not names:
                self.unresolved.append(nid)
        self._assign_clock_names()
        self._compute_async(sync_roots)
        self._assign_port_domains()
        return self

    # -- clock names -----------------------------------------------------------
    def _resolve_obj_to_nodes(self, obj: str) -> List[str]:
        """SDC/STA object (port, hierarchical pin, instance pin, net) -> sequential/port node ids.
        A wire driven by combinational clock logic (gates, muxes) is traced back to the
        registers / ports that generate it."""
        obj = obj.strip("{}")
        if obj in self.name_to_node:
            return [self.name_to_node[obj]]
        if "/" not in obj and self.port_keys_of_obj(obj):
            return ["P:" + re.sub(r"\[\d+\]$", "", obj)]
        wire = obj.replace("/", ".")
        bits = self.d.lookup_name(wire)
        if bits is None:
            m = re.match(r"^(.*)/([\w\[\]]+)$", obj)
            if m:
                bits = self.d.lookup_name(m.group(1).replace("/", "."))
        out: List[str] = []
        for b in bits or []:
            if b is None:
                continue
            src = self.g.bit_source_node.get(b)
            if src:
                out.append(src)
                continue
            drv = self.d.bit_driver.get(b)
            if drv is not None and drv[0] >= 0:
                for kind, name in self._trace_bit(b, set(), cut_defs=False):
                    if kind in ("undeclared", "clockdef"):
                        continue
                    out.append(("P:" + re.sub(r"\[\d+\]$", "", name)) if kind == "root" else name)
                continue
            for (rc, pn) in self.d.bit_readers.get(b, ()):
                if rc >= 0 and rc in self.g.cell_node:
                    out.append(self.g.cell_node[rc])
        return list(dict.fromkeys(out))

    def _assign_clock_names(self):
        g = self.g
        gen_at_sink: Dict[str, Set[str]] = defaultdict(set)
        for cd in self.clocks.values():
            if not cd.is_generated:
                continue
            for s in cd.sources:
                for nid in self._resolve_obj_to_nodes(s):
                    if nid not in self.g.nodes or self.g.nodes[nid].kind == "comb":
                        continue
                    if nid in self.generator_nodes:
                        self.generator_nodes[nid].add(cd.name)
                    elif re.search(r"/(CK|CLK|C|CP|CKN|CLKB|G|E|EN)$", s) and nid in self.node_roots:
                        gen_at_sink[nid].add(cd.name)
                    elif self.g.nodes[nid].is_seq:
                        self.generator_nodes[nid].add(cd.name)
            self._gen_clock_nodes[cd.name] = [n for s in cd.sources for n in self._resolve_obj_to_nodes(s)]
        for nid, node in g.nodes.items():
            if not (node.is_seq and node.clk_bits):
                continue
            names = set(gen_at_sink.get(nid, ()))
            if not names:
                names = self._clock_names_for_node(nid, set())
            self.node_clock_names[nid] = names
        for nid, names in self.node_clock_override.items():
            if names:
                self.node_clock_names[nid] = set(names)
        for nid, names in self.generator_nodes.items():
            if nid in g.nodes:
                g.nodes[nid].attrs["clock_generator"] = sorted(names) if names else ["<undeclared>"]

    def _clock_names_for_node(self, nid: str, guard: Set[str]) -> Set[str]:
        if nid in guard:
            return set()
        guard.add(nid)
        names: Set[str] = set()
        for b in self.g.nodes[nid].clk_bits:
            for kind, name in self._trace_bit(b, set()):
                if kind == "undeclared":
                    continue
                if kind == "root":
                    names.add(self.root_clock.get(name, name))
                elif kind == "clockdef":
                    names.add(name)
                else:
                    gen = self.generator_nodes.get(name)
                    if gen:
                        names |= gen
                    else:
                        sub = self._clock_names_for_node(name, guard)
                        names |= {f"{s}/gen@{self.g.nodes[name].name}" for s in sub}
        guard.discard(nid)
        return names

    # -- async relation ----------------------------------------------------------
    def _compute_async(self, sync_roots: Optional[List[List[str]]]):
        clock_group: Dict[str, int] = {}
        for gi, grp in enumerate(self.sdc.groups):
            for c in grp:
                clock_group[c] = gi
        # generated clocks listed in a group without their master -> master joins
        for cd in self.clocks.values():
            if cd.is_generated and cd.master and cd.name in clock_group and cd.master not in clock_group:
                clock_group[cd.master] = clock_group[cd.name]
        next_gid = len(self.sdc.groups)
        for r in self.roots:
            c = self.root_clock.get(r, r)
            if c in clock_group:
                self.root_group[r] = clock_group[c]
            else:
                self.root_group[r] = next_gid
                next_gid += 1
        for grp in sync_roots or []:
            keys = [k for r in grp for k in (self.port_keys_of_obj(r) or [r]) if k in self.root_group]
            if keys:
                gid = min(self.root_group[k] for k in keys)
                for k in keys:
                    self.root_group[k] = gid
        n = len(self.roots)
        self.async_mask = [0] * n
        for i, ri in enumerate(self.roots):
            m = 0
            for j, rj in enumerate(self.roots):
                if self.root_group[ri] != self.root_group[rj]:
                    m |= 1 << j
            self.async_mask[i] = m

    def clock_root_mask(self, clock_name: str) -> int:
        """Root mask of a clock name (master or generated)."""
        cd = self.clocks.get(clock_name)
        master = clock_name
        seen = set()
        while cd is not None and cd.is_generated and cd.master and cd.master not in seen:
            seen.add(cd.master)
            master = cd.master
            cd = self.clocks.get(master)
        m = 0
        for r, c in self.root_clock.items():
            if c == master and r in self.root_index:
                m |= 1 << self.root_index[r]
        if m == 0:
            for nid in self._gen_clock_nodes.get(clock_name, []):
                m |= self.node_roots.get(nid, 0)
        if m == 0 and clock_name in self.root_index:
            m = 1 << self.root_index[clock_name]
        return m

    def _assign_port_domains(self):
        for obj, clk in self.port_clock_hints.items():
            m = self.clock_root_mask(clk)
            if not m:
                continue
            for b in self.port_bits_of_obj(obj):
                self.port_bit_roots[b] = self.port_bit_roots.get(b, 0) | m
            pname = re.sub(r"\[\d+\]$", "", obj.strip("{}"))
            nid = f"P:{pname}"
            if nid in self.g.nodes:
                self.node_roots[nid] = self.node_roots.get(nid, 0) | m
                self.node_clock_names[nid].add(clk)

    # -- queries -----------------------------------------------------------------
    def is_async(self, mask_a: int, mask_b: int) -> bool:
        for i in _bits_of(mask_a):
            if mask_b & self.async_mask[i]:
                return True
        return False

    def root_names(self, mask: int) -> List[str]:
        return [self.roots[i] for i in _bits_of(mask)]

    def bit_roots(self, bit: int, node_id: Optional[str]) -> int:
        """Root mask for a specific bit driven by / consumed by node_id."""
        if node_id is not None and self.g.nodes[node_id].is_port:
            return self.port_bit_roots.get(bit, 0)
        return self.node_roots.get(node_id, 0) if node_id else 0

    # -- apply to graph ------------------------------------------------------------
    def apply(self):
        g = self.g
        for nid, node in g.nodes.items():
            if node.is_seq or node.is_port:
                m = self.node_roots.get(nid, 0)
                if m or nid in self.node_clock_names:
                    node.attrs["clock_roots"] = self.root_names(m)
                    node.attrs["clocks"] = sorted(self.node_clock_names.get(nid, ()))
                if nid in self.undeclared_clock_ports:
                    node.attrs["unconstrained_clock_ports"] = sorted(self.undeclared_clock_ports[nid])
        self._mark_cdc()
        return self

    def _mark_cdc(self):
        g = self.g
        d = self.d
        nscc = len(g.scc_order)
        scc_pred: List[Set[int]] = [set() for _ in range(nscc)]
        for si, fo in enumerate(g.scc_fanout):
            for sw in fo:
                scc_pred[sw].add(si)
        direct_src = [0] * nscc
        direct_sink = [0] * nscc
        async_bits: Dict[str, Set[int]] = {}
        if not self.cdc_async_pins:
            for nid, node in g.nodes.items():
                if node.is_seq:
                    ab = g._async_bits(node)
                    if ab:
                        async_bits[nid] = ab
        def is_async_pin(b: int, t: str) -> bool:
            return b in async_bits.get(t, ())
        for i, ci in enumerate(g.comb_cells):
            cell = d.cells[ci]
            si = g.scc_of[i]
            m = 0
            for b in cell.all_input_bits():
                if b is None:
                    continue
                s = g.bit_source_node.get(b)
                if s is not None:
                    m |= self.bit_roots(b, s)
            direct_src[si] |= m
            m = 0
            for b in cell.all_output_bits():
                if b is None:
                    continue
                for t in g.bit_sink_nodes.get(b, ()):
                    if not is_async_pin(b, t):
                        m |= self.bit_roots(b, t)
            direct_sink[si] |= m
        src_mask = [0] * nscc
        for si in range(nscc - 1, -1, -1):          # forward topological
            m = direct_src[si]
            for sp in scc_pred[si]:
                m |= src_mask[sp]
            src_mask[si] = m
        sink_mask = [0] * nscc
        for si in range(nscc):                       # reverse topological
            m = direct_sink[si]
            for sw in g.scc_fanout[si]:
                m |= sink_mask[sw]
            sink_mask[si] = m
        cdc_cells = 0
        cdc_pairs: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
        for i, ci in enumerate(g.comb_cells):
            si = g.scc_of[i]
            sm, tm = src_mask[si], sink_mask[si]
            if not (sm and tm and self.is_async(sm, tm)):
                continue
            cdc_cells += 1
            nid = g.cell_node.get(ci)
            if nid is None:
                continue
            node = g.nodes[nid]
            node.labels.add("CDC")
            for a in _bits_of(sm):
                for b in _bits_of(tm & self.async_mask[a]):
                    cdc_pairs[nid].add((self.roots[a], self.roots[b]))
            cell = d.cells[ci]
            for b in cell.all_input_bits():
                if b is None:
                    continue
                s = g.bit_source_node.get(b)
                if s is not None and self.is_async(self.bit_roots(b, s), tm):
                    g.nodes[s].labels.add("CDC")
                    e = g.edges.get((s, nid))
                    if e is not None:
                        e.attrs["cdc"] = True
            for b in cell.all_output_bits():
                if b is None:
                    continue
                for t in g.bit_sink_nodes.get(b, ()):
                    if not is_async_pin(b, t) and self.is_async(sm, self.bit_roots(b, t)):
                        g.nodes[t].labels.add("CDC")
                        e = g.edges.get((nid, t))
                        if e is not None:
                            e.attrs["cdc"] = True
        for nid, pairs in cdc_pairs.items():
            g.nodes[nid].attrs["cdc_crossings"] = sorted(f"{a}->{b}" for a, b in pairs)
        # direct element -> element connections
        for (s, t), e in g.edges.items():
            ns, nt = g.nodes[s], g.nodes[t]
            if ns.kind == "comb" or nt.kind == "comb":
                continue
            # bit-accurate check for ports
            cross = False
            for b in nt.bits_in:
                if is_async_pin(b, t):
                    continue
                if g.bit_source_node.get(b) == s and self.is_async(self.bit_roots(b, s), self.bit_roots(b, t)):
                    cross = True
                    break
            if cross:
                e.attrs["cdc"] = True
                ns.labels.add("CDC")
                nt.labels.add("CDC")
        g.stats["cdc_comb_cells"] = cdc_cells
        g.stats["cdc_nodes"] = sum(1 for n in g.nodes.values() if "CDC" in n.labels)
        g.stats["cdc_edges"] = sum(1 for e in g.edges.values() if e.attrs.get("cdc"))

    def summary(self) -> dict:
        return {
            "roots": self.roots,
            "root_clock": self.root_clock,
            "root_group": self.root_group,
            "clocks": {k: {"period": v.period, "generated": v.is_generated, "master": v.master,
                           "sources": v.sources[:6]} for k, v in self.clocks.items()},
            "clock_generators": {k: sorted(v) for k, v in self.generator_nodes.items()},
            "unresolved_clock_nodes": self.unresolved[:50],
            "unresolved_clock_node_count": len(self.unresolved),
            "undeclared_clock_ports": sorted({p for ps in self.undeclared_clock_ports.values() for p in ps}),
        }


def _natkey(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
