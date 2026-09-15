"""Bit-level static depth/delay estimation on the word-level netlist.

Forward pass computes arrival time per bit (ns) with predecessor pointers, so
critical paths can be reconstructed.  Reverse pass computes remaining delay
(for retiming-balance heuristics).  Arithmetic cells use a per-bit ripple or
prefix model; everything else uses a per-cell level count.
"""
from __future__ import annotations
import fnmatch, math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from .netgraph import Design, Register, Cell, param_int
from .delay_model import DelayModel, ARITH, CMP_EQ, CMP_MAG, BITWISE, REDUCE, SHIFT, MULDIV, clog2


@dataclass
class Endpoint:
    kind: str            # 'setup' | 'recovery' | 'output'
    name: str            # register name or port bit
    bit: object
    reg: Register | None
    capture_clock: str
    launch_clock: str
    arrival: float
    required: float | None
    slack: float | None
    period: float | None
    relation: str        # 'same' | 'related' | 'async' | 'unknown'
    start_bit: object = None
    start_name: str = ""


@dataclass
class PathStage:
    cell: str
    type: str
    module: str
    inst_path: str
    src: str
    width: int
    in_bit: object
    out_bit: object
    arr_in: float
    arr_out: float
    fanout_in: int
    fanout_pen: float


class _DelayTable:
    """set_input/output_delay lookup with glob support ('a[*]', 'samples*', '*all_inputs*')."""
    def __init__(self, entries):
        self.exact = {}
        self.globs = []
        for clk, kind, val, ports in entries:
            if kind == "min" or val is None:
                continue
            for p in ports:
                p = p.strip()
                if any(ch in p for ch in "*?["):
                    if "*" in p or "?" in p:
                        self.globs.append((p, clk, val)); continue
                self.exact[p] = (clk, max(val, self.exact.get(p, ("", 0))[1]))

    def get(self, key, default=("", 0.0)):
        if key in self.exact:
            return self.exact[key]
        base = key.split("[")[0]
        best = None
        for pat, clk, val in self.globs:
            if pat == "*all_inputs*" or pat == "*all_outputs*" or fnmatch.fnmatchcase(key, pat) or fnmatch.fnmatchcase(base, pat):
                if best is None or val > best[1]:
                    best = (clk, val)
        return best or default


class TimingEngine:
    def __init__(self, design: Design, domains: dict, model: DelayModel, sdc):
        self.d = design
        self.domains = domains
        self.m = model
        self.sdc = sdc
        self.arr: dict = {}          # bit -> arrival ns (at the driver output, before fanout penalty)
        self.pred: dict = {}         # out bit -> (cell, in bit)
        self.launch: dict = {}       # bit -> clock id of the critical source
        self.src_clocks: dict = defaultdict(set)   # cell name -> set of clock ids reaching it
        self.start_bit: dict = {}    # bit -> source bit of critical path
        self.cell_order: list = []
        self.loop_cells: list = []
        self.eff_fanout: dict = {}
        self.endpoints: list[Endpoint] = []
        self.remaining: dict = {}    # bit -> longest ns from this bit to any endpoint
        self.cell_through: dict = {} # cell -> ns through
        self.input_delay = self._input_delays()
        self.output_delay = self._output_delays()

    # ------------------------------------------------------------- helpers
    def _input_delays(self):
        return _DelayTable(getattr(self.sdc, "input_delays", []))

    def _output_delays(self):
        return _DelayTable(getattr(self.sdc, "output_delays", []))

    def sdc_clock_to_domain(self, sdc_name):
        for dom in self.domains.values():
            if dom.sdc_name == sdc_name:
                return dom.id
        return ""

    def period_of(self, dom_id):
        dom = self.domains.get(dom_id)
        return dom.period if dom else None

    def family_of(self, dom_id):
        dom = self.domains.get(dom_id)
        return dom.family if dom else ""

    def fan(self, b):
        if b not in self.eff_fanout:
            self.eff_fanout[b] = self.d.effective_fanout(b)
        return self.eff_fanout[b]

    def in_arrival(self, b):
        """Arrival of a bit as seen by a consumer (driver arrival + fanout penalty)."""
        if not isinstance(b, int):
            return -1.0
        return self.arr.get(b, 0.0) + self.m.fanout_penalty_ns(self.fan(b))

    # ------------------------------------------------------------ topo sort
    def _topo(self):
        d = self.d
        comb = [c for c in d.cells.values() if not c.is_seq]
        indeg = {c.name: 0 for c in comb}
        succ = defaultdict(list)
        for c in comb:
            for b in c.in_bits():
                drv = d.driver_cell(b)
                if drv is not None and not drv.is_seq:
                    indeg[c.name] += 1
                    succ[drv.name].append(c.name)
        q = deque([n for n, k in indeg.items() if k == 0])
        order = []
        while q:
            n = q.popleft(); order.append(n)
            for s in succ[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    q.append(s)
        self.loop_cells = [n for n, k in indeg.items() if k > 0]
        self.cell_order = order

    # -------------------------------------------------------------- forward
    def run(self):
        d, m = self.d, self.m
        self._topo()
        # sources
        for reg in d.regs.values():
            for b in reg.q_bits:
                self.arr[b] = m.clk_to_q_ns
                self.launch[b] = reg.clock
                self.start_bit[b] = b
        for pname, p in d.ports.items():
            if p["dir"] in ("input", "inout"):
                for i, b in enumerate(p["bits"]):
                    if isinstance(b, int):
                        bn = d.port_bit_name.get(b, d.bit_name(b))
                        clk, val = self.input_delay.get(bn, self.input_delay.get(pname, ("", 0.0)))
                        self.arr[b] = val
                        self.launch[b] = self.sdc_clock_to_domain(clk) if clk else f"port:{bn}"
                        self.start_bit[b] = b
        for cname in self.cell_order:
            self._eval_cell(d.cells[cname])
        self._collect_endpoints()
        self._reverse()
        return self

    def _eval_cell(self, c: Cell):
        t, m = c.type, self.m
        ins = [(p, bits) for p, bits in c.inputs()]
        outs = [(p, bits) for p, bits in c.outputs()]
        # source clocks reaching this cell
        sc = set()
        for p, bits in ins:
            for b in bits:
                if isinstance(b, int):
                    drv = self.d.driver_cell(b)
                    if drv is not None and not drv.is_seq:
                        sc |= self.src_clocks.get(drv.name, set())
                    elif b in self.launch:
                        sc.add(self.launch[b])
        self.src_clocks[c.name] = sc
        max_in_arr = -1.0
        for p, bits in ins:
            for b in bits:
                max_in_arr = max(max_in_arr, self.in_arrival(b))
        if t in ARITH and "Y" in c.conns:
            self._eval_arith(c)
        elif t == "$mux" or t == "$bwmux":
            A, B, S, Y = c.conns["A"], c.conns["B"], c.conns["S"], c.conns["Y"]
            s_arr = max(self.in_arrival(s) for s in S) if S else -1
            s_bit = S[0] if S else None
            for i, y in enumerate(Y):
                if not isinstance(y, int):
                    continue
                cands = [(self.in_arrival(A[i]) if i < len(A) else -1, A[i] if i < len(A) else None),
                         (self.in_arrival(B[i]) if i < len(B) else -1, B[i] if i < len(B) else None),
                         (s_arr, s_bit)]
                a, pb = max(cands, key=lambda x: x[0])
                self._set(y, a + m.ns(1), c, pb)
        elif t in BITWISE and len(outs) == 1 and all(len(bits) == len(outs[0][1]) for _, bits in ins):
            Y = outs[0][1]
            for i, y in enumerate(Y):
                if not isinstance(y, int):
                    continue
                cands = [(self.in_arrival(bits[i]), bits[i]) for _, bits in ins if isinstance(bits[i], int)]
                if not cands:
                    self._set(y, m.ns(1), c, None); continue
                a, pb = max(cands, key=lambda x: x[0])
                self._set(y, a + m.ns(1), c, pb)
        elif t in CMP_MAG and self.m.style == "ripple":
            # ripple comparator: MSB decided last, bit j costs (W-j)
            A, B = c.conns.get("A", []), c.conns.get("B", [])
            W = max(len(A), len(B))
            best, pb = -1.0, None
            for j in range(W):
                for src in (A, B):
                    if j < len(src) and isinstance(src[j], int):
                        v = self.in_arrival(src[j]) + m.ns(W - j + 1)
                        if v > best:
                            best, pb = v, src[j]
            for y in outs[0][1]:
                if isinstance(y, int):
                    self._set(y, best, c, pb)
        else:
            # generic: worst input + flat cell levels
            sel_w = len(c.conns.get("S", [])) if t == "$pmux" else 1
            n_in = sum(len(b) for _, b in ins)
            lv = m.cell_levels(t, c.width, n_in, sel_w)
            best, pb = -1.0, None
            for p, bits in ins:
                for b in bits:
                    if isinstance(b, int):
                        v = self.in_arrival(b)
                        if v > best:
                            best, pb = v, b
            for p, bits in outs:
                for y in bits:
                    if isinstance(y, int):
                        self._set(y, max(best, 0.0) + m.ns(lv), c, pb)
        # through delay for reverse pass
        out_max = max((self.arr.get(y, 0.0) for _, bits in outs for y in bits if isinstance(y, int)), default=0.0)
        self.cell_through[c.name] = max(out_max - max(max_in_arr, 0.0), 0.0)

    def _eval_arith(self, c: Cell):
        m = self.m
        A, B, Y = c.conns.get("A", []), c.conns.get("B", []), c.conns["Y"]
        W = len(Y)
        inc, const = m.adder_bit_cost(W)
        run, run_bit = -1.0, None      # running max of (arrival[j] + ripple from j)
        for i in range(W):
            cands = []
            for src in (A, B):
                if i < len(src) and isinstance(src[i], int):
                    cands.append((self.in_arrival(src[i]), src[i]))
            if cands:
                a, pb = max(cands, key=lambda x: x[0])
                if a >= run + m.ns(inc):
                    run, run_bit = a, pb
                else:
                    run = run + m.ns(inc)
            else:
                run = run + m.ns(inc) if run >= 0 else run
            y = Y[i]
            if isinstance(y, int):
                if m.style == "prefix":
                    # prefix: arrival = max over all lower input bits + log cost
                    self._set(y, max(run, 0.0) + m.ns(const + 2 * clog2(max(i + 1, 2))) - (m.ns(inc) * 0), c, run_bit)
                else:
                    self._set(y, max(run, 0.0) + m.ns(const), c, run_bit)

    def _set(self, y, arrival, cell, pred_bit):
        self.arr[y] = arrival
        self.pred[y] = (cell, pred_bit)
        if isinstance(pred_bit, int):
            self.launch[y] = self.launch.get(pred_bit, "")
            self.start_bit[y] = self.start_bit.get(pred_bit, pred_bit)
        else:
            self.launch[y] = ""
            self.start_bit[y] = None

    # ----------------------------------------------------------- endpoints
    def _relation(self, launch, capture):
        if not launch or not capture:
            return "unknown"
        if launch == capture:
            return "same"
        fl, fc = self.family_of(launch), self.family_of(capture)
        if fl and fc and fl == fc:
            return "related"
        if launch.startswith("port:") and not launch.startswith("port:clk"):
            return "unknown"
        return "async"

    def _make_ep(self, kind, name, b, reg, capture, launch):
        arr = self.in_arrival(b)
        rel = self._relation(launch, capture)
        pc, pl = self.period_of(capture), self.period_of(launch)
        period = None
        if rel in ("same", "related", "unknown"):
            cands = [p for p in (pc, pl) if p]
            period = min(cands) if cands else None
        req = slack = None
        unc = self.m.uncertainty_ns
        dom = self.domains.get(capture)
        if dom and dom.sdc_name and dom.sdc_name in getattr(self.sdc, "uncertainty", {}):
            unc = self.sdc.uncertainty[dom.sdc_name] or unc
        if period is not None and rel != "async":
            if kind == "recovery":
                req = period - unc - self.m.recovery_ns
            elif kind == "output":
                req = period - unc - self.output_delay.get(name, ("", 0.0))[1]
            else:
                req = period - unc - self.m.setup_ns
            slack = req - arr
        sb = self.start_bit.get(b)
        return Endpoint(kind, name, b, reg, capture, launch, arr, req, slack, period, rel,
                        start_bit=sb, start_name=self.d.bit_name(sb) if sb is not None else "")

    def _collect_endpoints(self):
        d = self.d
        eps = []
        for reg in d.regs.values():
            cap = reg.clock
            for b in reg.d_bits:
                if isinstance(b, int):
                    eps.append(self._make_ep("setup", reg.name, b, reg, cap, self.launch.get(b, "")))
            for b in reg.en_bits + reg.srst_bits:
                if isinstance(b, int):
                    eps.append(self._make_ep("setup", reg.name, b, reg, cap, self.launch.get(b, "")))
            if isinstance(reg.arst_bit, int):
                eps.append(self._make_ep("recovery", reg.name, reg.arst_bit, reg, cap, self.launch.get(reg.arst_bit, "")))
        for pname, p in d.ports.items():
            if p["dir"] == "output":
                for i, b in enumerate(p["bits"]):
                    if not isinstance(b, int):
                        continue
                    bn = d.port_bit_name.get(b, d.bit_name(b))
                    clk = self.output_delay.get(bn, self.output_delay.get(pname, ("", 0)))[0]
                    cap = self.sdc_clock_to_domain(clk) if clk else self.launch.get(b, "")
                    eps.append(self._make_ep("output", bn, b, None, cap, self.launch.get(b, "")))
        self.endpoints = eps

    # ------------------------------------------------------------ reverse
    def _reverse(self):
        rem = defaultdict(float)
        for ep in self.endpoints:
            if isinstance(ep.bit, int):
                rem[ep.bit] = max(rem[ep.bit], 0.0)
        for cname in reversed(self.cell_order):
            c = self.d.cells[cname]
            out_rem = max((rem.get(y, -1.0) for y in c.out_bits()), default=-1.0)
            if out_rem < 0:
                continue
            thr = self.cell_through.get(cname, 0.0)
            for b in c.in_bits():
                v = out_rem + thr + self.m.fanout_penalty_ns(self.fan(b))
                if v > rem.get(b, -1.0):
                    rem[b] = v
        self.remaining = rem

    # --------------------------------------------------------------- paths
    def path_of(self, bit) -> list[PathStage]:
        stages = []
        b = bit
        guard = 0
        while isinstance(b, int) and b in self.pred and guard < 100000:
            guard += 1
            cell, pb = self.pred[b]
            st = PathStage(cell.name, cell.type, cell.module, cell.inst_path, cell.short_src, cell.width,
                           pb, b, self.in_arrival(pb) if isinstance(pb, int) else 0.0, self.arr.get(b, 0.0),
                           self.fan(pb) if isinstance(pb, int) else 0,
                           self.m.fanout_penalty_ns(self.fan(pb)) if isinstance(pb, int) else 0.0)
            if stages and stages[-1].cell == cell.name:
                stages[-1].in_bit, stages[-1].arr_in = pb, st.arr_in
                stages[-1].fanout_in, stages[-1].fanout_pen = st.fanout_in, st.fanout_pen
            else:
                stages.append(st)
            b = pb
        stages.reverse()
        return stages

    def reg_depth_in(self, reg: Register):
        return max((self.in_arrival(b) for b in reg.d_bits if isinstance(b, int)), default=0.0)

    def reg_depth_out(self, reg: Register):
        return max((self.remaining.get(b, 0.0) + self.m.fanout_penalty_ns(self.fan(b)) for b in reg.q_bits), default=0.0)
