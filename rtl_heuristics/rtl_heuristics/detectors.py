"""Heuristic detectors. Each returns a list of Issue objects.

Every detector is a plain algorithm over the design model / timing estimate.
Issues are grouped by RTL location (module + source line + signal) so that a
structure replicated in 40 instances is reported once with instances=40.
"""
from __future__ import annotations
import math, re
from collections import defaultdict, Counter
from .netgraph import Design, Register, Cell, SEQ_TYPES, LATCH_TYPES, short_src
from .delay_model import DelayModel, ARITH, CMP_EQ, CMP_MAG, MULDIV, clog2
from .timing import TimingEngine, Endpoint
from .patterns import adder_chains, ARCHETYPES
from .issues import Issue, Fix


def _local(d: Design, name: str):
    path, local, mod = d.split_instance(name)
    return path, local, mod


def _sig_key(d: Design, name: str):
    """Instance-independent key for a flattened net/register name."""
    path, local, mod = d.split_instance(name)
    local = re.sub(r"\[\d+\]", "[*]", local)
    return (mod, re.sub(r"\[\*\]$", "", local))


def _net_key(d: Design, b):
    """Instance-independent key for a net; unnamed nets are keyed by their driver."""
    name = d.bit_name(b)
    if re.fullmatch(r"net\d+", name):
        drv = d.driver_cell(b)
        if drv is not None:
            return (drv.module, f"{drv.type}@{drv.short_src}")
    return _sig_key(d, name)


def _fmt(x, nd=3):
    return None if x is None else round(float(x), nd)


class Context:
    def __init__(self, d: Design, te: TimingEngine, domains, sdc, model: DelayModel, profiles, crossings, cfg):
        self.d, self.te, self.domains, self.sdc, self.m = d, te, domains, sdc, model
        self.profiles, self.crossings, self.cfg = profiles, crossings, cfg

    def period(self, clk):
        return self.te.period_of(clk)

    def clock_label(self, clk):
        dom = self.domains.get(clk)
        if not dom:
            return clk
        return f"{dom.sdc_name or dom.id}" + (f" ({dom.period:.3f} ns)" if dom.period else "")


# ============================================================ 1. critical paths
def det_critical_paths(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    groups: dict = {}
    for ep in te.endpoints:
        if ep.kind == "recovery":
            continue
        stages = te.path_of(ep.bit)
        if ep.relation == "async":
            continue
        sig = (ep.kind, _sig_key(d, ep.name), _sig_key(d, ep.start_name) if ep.start_name else None)
        g = groups.get(sig)
        crit = ep.slack if ep.slack is not None else -ep.arrival
        clabel = ctx.clock_label(ep.capture_clock)
        if g is None:
            g = groups[sig] = {"ep": ep, "stages": stages, "crit": crit, "count": 0, "insts": set(), "per_clock": {}}
        elif crit < g["crit"]:
            g.update(ep=ep, stages=stages, crit=crit)
        g["count"] += 1; g["insts"].add(ep.name)
        if ep.slack is not None:
            g["per_clock"][clabel] = min(g["per_clock"].get(clabel, 1e9), round(ep.slack, 4))
    ranked = sorted(groups.values(), key=lambda g: g["crit"])
    issues = []
    budget_frac = ctx.cfg.get("depth_warn_fraction", 0.8)
    for g in ranked[: ctx.cfg.get("max_critical_groups", 25)]:
        ep, stages = g["ep"], g["stages"]
        period = ep.period
        if ep.slack is not None:
            if ep.slack >= 0 and (period is None or ep.arrival < budget_frac * period):
                break
        # composition of the path
        fan_pen = sum(s.fanout_pen for s in stages)
        unbuf_extra = sum(m.fanout_unbuffered_ns(s.fanout_in) - s.fanout_pen for s in stages if s.fanout_in > m.fanout_free)
        arith = sum(s.arr_out - s.arr_in for s in stages if s.type in ARITH)
        mux = sum(s.arr_out - s.arr_in for s in stages if s.type in ("$mux", "$pmux"))
        total = ep.arrival
        cut = _balanced_cut(stages, total)
        sev = "critical" if (ep.slack is not None and ep.slack < -0.25 * (period or 1)) else ("high" if (ep.slack is not None and ep.slack < 0) else "medium")
        epath, elocal, emod = _local(d, ep.name)
        spath, slocal, smod = _local(d, ep.start_name) if ep.start_name else ("", "", "")
        if len(g["insts"]) > 1:
            elocal = _sig_key(d, ep.name)[1]
            slocal = _sig_key(d, ep.start_name)[1] if ep.start_name else slocal
        fixes = []
        if fan_pen > 0.25 * total:
            worst = max(stages, key=lambda s: s.fanout_pen)
            fixes.append(Fix("Break the high-fanout control net on this path", "fanout_replication",
                             f"{worst.fanout_pen:.2f} ns of the {total:.2f} ns arrival is fanout buffering of net '{d.bit_name(worst.in_bit)}' (effective fanout {worst.fanout_in}). See the HIGH_FANOUT_NET issue for that net.",
                             expected_gain_ns=round(worst.fanout_pen * 0.7, 3), latency_change="none", equivalence="sequential (register duplication)"))
        if arith > 0.4 * total:
            fixes.append(Fix("Restructure the arithmetic (carry-save + prefix adder)", "carry_save_restructuring",
                             f"{arith:.2f} ns of the path is inside {sum(1 for s in stages if s.type in ARITH)} chained arithmetic cells. Compress multi-operand additions with 3:2 counters and finish with one parallel-prefix adder.",
                             expected_gain_ns=round(arith - m.ns(2 * clog2(max(stages[0].width, 2)) + 4), 3), latency_change="none", equivalence="combinational (arithmetic normalisation needed for SAT)"))
        fixes.append(Fix("Insert a pipeline register at the balanced cut", "pipeline_insertion",
                         f"Cut after stage {cut['index']} ({cut['type']} at {cut['src']}); left part ≈{cut['left_ns']:.2f} ns, right part ≈{cut['right_ns']:.2f} ns. Adds one cycle of latency on this path; downstream consumers and any valid/handshake must be re-aligned.",
                         steps=[f"declare a register of {cut['width']} bits in module {cut['module']} clocked by {ctx.clock_label(ep.capture_clock)}",
                                f"register the output of the {cut['type']} at {cut['src']}",
                                "delay every sibling signal that merges with this one after the cut by one cycle (or retime with the synthesis tool)",
                                "update the latency contract / valid pipeline and re-run equivalence with a 1-cycle output offset"],
                         expected_gain_ns=round(max(total - max(cut['left_ns'], cut['right_ns']) - m.clk_to_q_ns, 0), 3),
                         latency_change="+1 cycle", equivalence="sequential with latency offset", risk="medium"))
        iss = Issue("DEEP_COMB_PATH", "timing", sev,
                    f"Register-to-{ep.kind} path {slocal or ep.start_name} → {elocal} needs ≈{total:.2f} ns, budget {period if period else '?'} ns on {ctx.clock_label(ep.capture_clock)}",
                    emod, elocal, stages[-1].src if stages else "", len(g["insts"]),
                    sorted(g["insts"])[:5],
                    evidence={"endpoint_bits": g["count"], "est_arrival_ns": _fmt(total), "est_arrival_if_unbuffered_ns": _fmt(total + unbuf_extra),
                              "worst_est_slack_per_clock": g["per_clock"], "est_required_ns": _fmt(ep.required), "est_slack_ns": _fmt(ep.slack),
                              "period_ns": _fmt(period), "capture_clock": ctx.clock_label(ep.capture_clock), "launch_clock": ctx.clock_label(ep.launch_clock),
                              "relation": ep.relation, "start": ep.start_name, "end": ep.name, "end_bit": d.bit_name(ep.bit),
                              "fanout_share_ns": _fmt(fan_pen), "arith_share_ns": _fmt(arith), "mux_share_ns": _fmt(mux),
                              "levels_est": round(total / m.ns_per_level, 1),
                              "stages": [{"type": s.type, "module": s.module, "src": s.src, "width": s.width,
                                          "in": d.bit_name(s.in_bit) if isinstance(s.in_bit, int) else str(s.in_bit),
                                          "arr_in": _fmt(s.arr_in), "arr_out": _fmt(s.arr_out), "fanout_in": s.fanout_in, "fanout_pen": _fmt(s.fanout_pen)} for s in stages],
                              "balanced_cut": cut},
                    fixes=fixes)
        issues.append(iss)
    return issues


def _balanced_cut(stages, total):
    best, bi = None, 0
    acc = 0.0
    for i, s in enumerate(stages):
        acc = s.arr_out
        diff = abs(acc - total / 2)
        if best is None or diff < best:
            best, bi = diff, i
    s = stages[bi] if stages else None
    return {"index": bi, "type": s.type if s else "", "src": s.src if s else "", "module": s.module if s else "",
            "width": s.width if s else 0, "left_ns": _fmt(s.arr_out if s else 0), "right_ns": _fmt(total - (s.arr_out if s else 0)),
            "after_signal": ""}


# ============================================================ 2. high fanout
def det_high_fanout(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    thr = ctx.cfg.get("fanout_threshold", 32)
    groups = defaultdict(list)
    for b in list(d.loads.keys()):
        f = te.fan(b)
        if f >= thr:
            if sum(1 for _, port, _ in d.loads[b] if port == "CLK") > len(d.loads[b]) // 2:
                continue   # clock net: CTS problem, not RTL
            groups[_net_key(d, b)].append((b, f))
    issues = []
    for key, lst in groups.items():
        b, f = max(lst, key=lambda x: x[1])
        mod, local = key
        drv = d.driver.get(b)
        src_kind = "port" if drv and drv[0] == "port" else ("register" if drv and drv[1].is_seq else f"combinational ({drv[1].type})" if drv else "undriven")
        src_cell = drv[1] if drv and drv[0] == "cell" else None
        ctrl_pins = Counter()
        consumers = Counter()
        for cell, port, idx in d.loads.get(b, []):
            ctrl_pins[port] += 1; consumers[cell.module] += 1
        arrival = te.arr.get(b, 0.0)
        remaining = te.remaining.get(b, 0.0)
        pen_buf = m.fanout_penalty_ns(f); pen_unbuf = m.fanout_unbuffered_ns(f)
        # which clock/period?
        clk = te.launch.get(b, ""); period = te.period_of(clk)
        est_slack = (period - m.uncertainty_ns - m.setup_ns - (arrival + pen_buf + remaining)) if period else None
        is_ctrl = any(p in ("S", "EN", "ARST", "SRST", "CLK") for p in ctrl_pins)
        is_reset = "ARST" in ctrl_pins or "SRST" in ctrl_pins
        if is_reset:
            continue  # handled by det_async_reset_fanout
        sev = "high" if f >= 128 else ("medium" if f >= 64 else "low")
        if est_slack is not None and est_slack < 0:
            sev = "critical" if f >= 128 else "high"
        k = max(2, math.ceil(f / ctx.cfg.get("fanout_per_copy", 64)))
        fixes = []
        if src_kind.startswith("combinational"):
            fixes.append(Fix("Register the control signal, then replicate the register", "register_duplication",
                             f"'{local}' is produced by a {src_cell.type} and then fans out to {f} loads. Compute it one cycle early from the next-state value (or make it sticky) so that it launches from a flop, then keep {k} copies (one per consumer block, ≈{math.ceil(f / k)} loads each). Synthesis must not merge the copies (keep / dont_touch / preserve attribute).",
                             steps=[f"derive '{local}_next' from the next-state expression of its source register(s)",
                                    f"add `reg {local}_q [{k - 1}:0]` with (* keep *) and assign each consumer block its own copy",
                                    "replace uses of the combinational signal by the registered copy",
                                    "check the one-cycle earlier evaluation keeps the exact same cycle behaviour (sticky flags do)"],
                             rtl_before=f"assign {local} = <compare/decode of state>;",
                             rtl_after=f"// registered, replicated {k}x\nreg [{k - 1}:0] {local}_q;\nalways @(posedge clk or negedge rst_n)\n  if (!rst_n) {local}_q <= '0;\n  else        {local}_q <= {{{k}{{<compare of NEXT state>}}}};\n// consumer i uses {local}_q[i % {k}]",
                             expected_gain_ns=round(pen_buf - m.fanout_penalty_ns(math.ceil(f / k)) + (te.arr.get(b, 0.0) - m.clk_to_q_ns), 3),
                             expected_gain_note="model: fanout penalty reduction + removal of the source logic from the path",
                             latency_change="none (flag computed from next-state)", equivalence="sequential", risk="low",
                             ppa=f"+{k} flops, +{k} buffers; negligible"))
        else:
            fixes.append(Fix("Duplicate the source register", "register_duplication",
                             f"Keep {k} copies of '{local}' (each driving ≈{math.ceil(f / k)} loads) with a keep attribute; assign consumers round-robin by block.",
                             expected_gain_ns=round(pen_buf - m.fanout_penalty_ns(math.ceil(f / k)), 3),
                             latency_change="none", equivalence="sequential", risk="low", ppa=f"+{(k - 1)} flops per instance"))
        fixes.append(Fix("Let synthesis build a buffer tree (timing-driven mapping)", "fanout_buffering",
                         f"With area-only mapping this net costs ≈{pen_unbuf:.2f} ns unbuffered; a buffer tree of branching 4 costs ≈{pen_buf:.2f} ns. Enable timing-driven ABC (-D <period>) / repair_design in the flow and set_max_fanout.",
                         sdc_change="set_max_fanout 32 [current_design]", expected_gain_ns=round(pen_unbuf - pen_buf, 3),
                         latency_change="none", equivalence="none needed", risk="low", ppa="+buffers"))
        iss = Issue("HIGH_FANOUT_NET", "fanout", sev,
                    f"Net '{local}' in {mod} drives {f} effective loads ({', '.join(f'{p}:{n}' for p, n in ctrl_pins.most_common(3))})",
                    mod, local, src_cell.short_src if src_cell else "", len(lst), [d.bit_name(x[0]) for x in lst[:5]],
                    evidence={"effective_fanout": f, "cell_loads": d.net_fanout_cells(b), "driver": src_kind,
                              "consumer_modules": dict(consumers), "control_pins": dict(ctrl_pins),
                              "est_penalty_buffered_ns": _fmt(pen_buf), "est_penalty_unbuffered_ns": _fmt(pen_unbuf),
                              "arrival_ns": _fmt(arrival), "remaining_ns": _fmt(remaining), "est_slack_ns": _fmt(est_slack),
                              "clock": ctx.clock_label(clk), "is_control": is_ctrl},
                    fixes=fixes)
        issues.append(iss)
    return issues


# ============================================================ 3. late control
def det_late_control(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    groups = {}
    for c in d.comb_cells():
        if c.type != "$mux":
            continue
        S = c.conns.get("S", [])
        if len(S) != 1 or not isinstance(S[0], int):
            continue
        s_arr = te.in_arrival(S[0])
        data_arr = max((te.in_arrival(b) for p in ("A", "B") for b in c.conns.get(p, []) if isinstance(b, int)), default=0.0)
        remaining = max((te.remaining.get(y, 0.0) for y in c.out_bits()), default=0.0)
        sdrv = d.driver_cell(S[0])
        if s_arr > data_arr + m.ns(1) and remaining > m.ns(4) and sdrv is not None and not sdrv.is_seq:
            key = (c.module, c.short_src)
            g = groups.setdefault(key, {"cells": [], "s_arr": s_arr, "data_arr": data_arr, "rem": remaining, "sel": d.bit_name(S[0]), "sdrv": sdrv, "fan": te.fan(S[0]), "w": c.width, "insts": set()})
            g["cells"].append(c); g["insts"].add(c.inst_path)
            g["s_arr"] = max(g["s_arr"], s_arr); g["rem"] = max(g["rem"], remaining)
    issues = []
    for (mod, src), g in groups.items():
        sel_local = _sig_key(d, g["sel"])[1]
        const_zero = any(all(isinstance(b, str) for b in c.conns.get("A", [])) or all(isinstance(b, str) for b in c.conns.get("B", [])) for c in g["cells"])
        fixes = [Fix("Pre-compute the select one cycle early / register it", "control_precompute",
                     f"Select '{sel_local}' arrives at ≈{g['s_arr']:.2f} ns while the data it steers is ready at ≈{g['data_arr']:.2f} ns, and ≈{g['rem']:.2f} ns of logic follows the mux. Generate the select from the next-state of its source register so it launches from a flop (see HIGH_FANOUT_NET / COMPARATOR_FLAG fixes).",
                     expected_gain_ns=round(g["s_arr"] - g["data_arr"], 3), latency_change="none", equivalence="sequential", risk="low"),
                 Fix("Shannon expansion around the late signal", "shannon_expansion",
                     f"Evaluate the downstream logic for both values of '{sel_local}' in parallel and select at the end: the late signal then passes through a single mux level. Costs ≈2x the downstream logic ({g['w']} bits).",
                     expected_gain_ns=round(min(g["rem"], g["s_arr"] - g["data_arr"]), 3), latency_change="none", equivalence="combinational", risk="low", ppa="up to 2x the downstream logic area")]
        if const_zero:
            fixes.insert(1, Fix("Move the zero-masking to the register / operand boundary", "operand_masking_relocation",
                                f"The mux forces a constant when '{sel_local}' is inactive. Apply the mask where the operand is stored (e.g. clear the register, or AND the operand before the arithmetic) so the masking is off the arithmetic path, or apply one mask at the final output register instead of per operand.",
                                latency_change="none", equivalence="sequential (mask timing must match)", risk="low"))
        issues.append(Issue("LATE_CONTROL_SELECT", "timing", "high" if g["fan"] >= 32 else "medium",
                            f"Late-arriving select '{sel_local}' on mux at {src} in {mod} (select {g['s_arr']:.2f} ns vs data {g['data_arr']:.2f} ns, {g['rem']:.2f} ns of logic after it)",
                            mod, sel_local, src, len(g["cells"]), sorted(g["insts"])[:5],
                            evidence={"select_arrival_ns": _fmt(g["s_arr"]), "data_arrival_ns": _fmt(g["data_arr"]), "logic_after_ns": _fmt(g["rem"]),
                                      "select_driver": g["sdrv"].type, "select_fanout": g["fan"], "mux_width": g["w"], "masks_to_constant": const_zero},
                            fixes=fixes))
    return issues


# ============================================================ 4. comparator flag
def det_comparator_flag(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    groups = {}
    for c in d.comb_cells():
        if c.type not in CMP_EQ:
            continue
        A, B = c.conns.get("A", []), c.conns.get("B", [])
        const = None; var = None
        for x, y in ((A, B), (B, A)):
            if x and all(isinstance(b, str) for b in x):
                const, var = x, y
        if const is None:
            continue
        srcs = {d.reg_of_qbit[b].name for b in var if isinstance(b, int) and b in d.reg_of_qbit}
        if not srcs:
            continue
        y = c.out_bits()
        if not y:
            continue
        f = te.fan(y[0])
        if f < ctx.cfg.get("flag_fanout_threshold", 8):
            continue
        key = (c.module, c.short_src)
        g = groups.setdefault(key, {"cell": c, "fan": f, "srcs": srcs, "insts": set(), "n": 0, "w": len(var), "const": int("".join(reversed(const)).replace("x", "0"), 2), "out": d.bit_name(y[0])})
        g["insts"].add(c.inst_path); g["fan"] = max(g["fan"], f); g["n"] += 1
    issues = []
    for (mod, src), g in groups.items():
        reg = d.regs[next(iter(g["srcs"]))]
        rl = _sig_key(d, reg.name)[1]; ol = _sig_key(d, g["out"])[1]
        role = reg.role
        sticky = "counter" in role
        fixes = [Fix("Register the flag from the next-state value", "registered_flag",
                     f"'{ol}' = ({rl} == {g['const']}) is recomputed combinationally every cycle and fans out to {g['fan']} loads. Compute it from the value {rl} will take next cycle and store it in a flop; the compare leaves the datapath path entirely.",
                     steps=[f"add `reg {ol}_q;`",
                            f"in the always block that updates {rl}: `{ol}_q <= ({rl}_next == {g['const']});` where {rl}_next is the expression assigned to {rl}",
                            f"assign {ol} = {ol}_q;" + (" (sticky: once the counter stops at the terminal count the flag never drops, so `{ol}_q <= {ol}_q | (...)` is equivalent)" if sticky else "")],
                     rtl_before=f"assign {ol} = ({rl} == {g['const']});",
                     rtl_after=f"reg {ol}_q;\nalways @(posedge clk or negedge rst_n)\n  if (!rst_n) {ol}_q <= 1'b0;\n  else if (!{ol}_q) {ol}_q <= ({rl} == {g['const'] - 1}); // one cycle early == next-state compare\nassign {ol} = {ol}_q;" if sticky else "",
                     expected_gain_ns=round(m.ns(clog2(g["w"]) + 1), 3), expected_gain_note="compare levels removed from the path; fanout penalty unchanged unless replicated",
                     latency_change="none", equivalence="sequential", risk="low", ppa="+1 flop per instance")]
        if sticky:
            fixes.append(Fix("Down-counter with zero detect / one-hot terminal", "counter_restructure",
                             "Load DEPTH and count down; the terminal condition becomes a NOR of the count bits (or the borrow-out), which is shallower than an equality compare against an arbitrary constant.",
                             latency_change="none", equivalence="sequential", risk="low"))
        issues.append(Issue("COMPARATOR_FLAG_BROADCAST", "control", "high" if g["fan"] >= 64 else "medium",
                            f"Combinational compare '{ol}' ({rl} == {g['const']}) drives {g['fan']} loads from {mod}",
                            mod, ol, src, g["n"], sorted(g["insts"])[:5],
                            evidence={"compare_width": g["w"], "constant": g["const"], "effective_fanout": g["fan"], "source_register": rl, "source_role": role},
                            fixes=fixes))
    return issues


# ============================================================ 5. arithmetic chains
def det_arith_chains(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    groups = {}
    for mod, inst, chain in adder_chains(d):
        key = (mod, tuple(c.short_src for c in chain))
        g = groups.setdefault(key, {"chain": chain, "insts": set(), "n": 0})
        g["insts"].add(inst); g["n"] += 1
    issues = []
    for (mod, srcs), g in groups.items():
        chain = g["chain"]
        W = max(c.width for c in chain)
        n_ops = len(chain) + 1
        first_in = min((te.in_arrival(b) for c in chain[:1] for b in c.in_bits()), default=0.0)
        last_out = max((te.arr.get(b, 0.0) for b in chain[-1].out_bits()), default=0.0)
        depth = last_out - max(first_in, 0.0)
        loop = any(b in d.reg_of_qbit and any(x in d.reg_of_qbit[b].d_bits for x in chain[-1].out_bits()) for c in chain for b in c.in_bits())
        # simple loop detection: does the chain output go to a register whose Q feeds the chain?
        out_regs = set()
        for b in chain[-1].out_bits():
            for cell, port, idx in d.loads.get(b, []):
                if cell.is_seq and port == "D" and cell.name in d.reg_of_cell:
                    out_regs.add(d.reg_of_cell[cell.name].name)
        in_regs = {d.reg_of_qbit[b].name for c in chain for b in c.in_bits() if b in d.reg_of_qbit}
        loop = bool(out_regs & in_regs)
        # target: CSA tree then one prefix CPA
        csa_levels = max(1, math.ceil(math.log(n_ops / 2, 1.5))) if n_ops > 2 else 1
        new_depth = m.ns(csa_levels + 2 * clog2(max(W, 2)) + 2)
        types = "".join("+" if c.type == "$add" else "-" if c.type == "$sub" else "?" for c in chain)
        if loop:
            title = f"Loop-carried {n_ops}-operand accumulator ({types}) of {W} bits in {mod}: two adders in series inside the register feedback"
            fix = Fix("Carry-save accumulator: 3:2 compress, single CPA", "carry_save_restructuring",
                      f"Replace `acc <= acc + a - b` (two {W}-bit adders in series, ≈{depth:.2f} ns) with one 3:2 compressor layer feeding a single {W}-bit adder (≈{new_depth:.2f} ns). Subtraction becomes addition of ~b with a carry-in of 1. If the mapper still builds ripple carries, instantiate an explicit Kogge-Stone/Brent-Kung adder.",
                      rtl_before=f"sum <= sum + sample - old;",
                      rtl_after=("wire [W-1:0] nb = ~old;              // -old = ~old + 1\n"
                                 "wire [W-1:0] s  = sum ^ sample ^ nb;  // 3:2 compressor (sum bits)\n"
                                 "wire [W-1:0] c  = (sum & sample) | (sum & nb) | (sample & nb);\n"
                                 "sum <= s + {c[W-2:0], 1'b1};          // one carry-propagate adder, +1 completes the two's complement"),
                      expected_gain_ns=round(max(depth - new_depth, 0), 3), latency_change="none",
                      equivalence="combinational (use alumacc/techmap normalisation before SAT)", risk="low", ppa="≈+W XOR/MAJ cells, -1 adder")
            fix2 = Fix("Keep the accumulator in redundant (carry-save) form", "carry_save_accumulator",
                       "Store the accumulator as two vectors (sum, carry) and only resolve with a CPA where the value is consumed; the feedback loop is then one 3:2 layer deep. Increases register count 2x for the accumulator.",
                       latency_change="none if the CPA is kept combinational at the output; +1 if registered", equivalence="sequential", risk="medium", ppa="+W flops")
            fixes = [fix, fix2]
        else:
            title = f"Linear chain of {len(chain)} {W}-bit adders ({n_ops} operands) at {srcs[0]} in {mod}"
            fixes = [Fix("Balanced adder tree / carry-save (Wallace) tree", "adder_tree_balancing",
                         f"The for-loop accumulation builds {len(chain)} adders in series (≈{depth:.2f} ns). Add operands pairwise as a balanced tree (depth ceil(log2 {n_ops}) = {clog2(n_ops)} adders) or compress all {n_ops} operands with 3:2 counters to two vectors and use one CPA (≈{new_depth:.2f} ns).",
                         rtl_before="for (k=0;k<N;k++) acc = acc + x[k];",
                         rtl_after=("// balanced tree (N=8)\n"
                                    "wire [W-1:0] l1_0 = x[0]+x[1], l1_1 = x[2]+x[3], l1_2 = x[4]+x[5], l1_3 = x[6]+x[7];\n"
                                    "wire [W-1:0] l2_0 = l1_0+l1_1, l2_1 = l1_2+l1_3;\n"
                                    "assign acc = l2_0 + l2_1;\n"
                                    "// or: csa_tree #(.N(8),.W(W)) u (.x(x), .s(s), .c(c)); assign acc = s + c;"),
                         expected_gain_ns=round(max(depth - new_depth, 0), 3), latency_change="none",
                         equivalence="combinational (arithmetic normalisation)", risk="low", ppa="balanced tree: same adder count; CSA: fewer carry chains"),
                     Fix("Pipeline the reduction tree", "pipeline_insertion",
                         f"Register after the first tree level ({clog2(n_ops)} levels total). +1 cycle latency on the reduced value.",
                         latency_change="+1 cycle", equivalence="sequential with latency offset", risk="medium")]
        sig_name = sorted(_sig_key(d, r)[1] for r in out_regs)[0] if out_regs else (_local(d, d.bit_name(chain[-1].out_bits()[0]))[1] if chain[-1].out_bits() else "")
        issues.append(Issue("ADDER_CHAIN", "arithmetic", "high" if depth > 0.5 * (ctx.period(te.launch.get(chain[0].in_bits()[0], "")) or 1e9) else "medium",
                            title, mod, sig_name, srcs[0],
                            g["n"], sorted(g["insts"])[:5],
                            evidence={"operands": n_ops, "width": W, "chain_ops": types, "loop_carried": loop,
                                      "est_chain_depth_ns": _fmt(depth), "est_after_csa_ns": _fmt(new_depth),
                                      "srcs": list(srcs), "output_registers": sorted(_local(d, r)[1] for r in out_regs)},
                            fixes=fixes))
    return issues


# ============================================================ 6. wide arithmetic
def det_wide_arith(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    groups = {}
    thr = ctx.cfg.get("wide_arith_bits", 24)
    for c in d.comb_cells():
        if c.type not in ARITH | CMP_MAG | MULDIV or c.width < thr:
            continue
        outs = c.out_bits()
        arr = max((te.arr.get(b, 0.0) for b in outs), default=0.0)
        rem = max((te.remaining.get(b, 0.0) for b in outs), default=0.0)
        clk = te.launch.get(outs[0], "") if outs else ""
        period = te.period_of(clk)
        if period is None or arr + rem < 0.7 * period:
            continue
        key = (c.module, c.short_src, c.type)
        g = groups.setdefault(key, {"c": c, "insts": set(), "arr": arr, "rem": rem, "period": period})
        g["insts"].add(c.inst_path)
    issues = []
    for (mod, src, t), g in groups.items():
        c = g["c"]; W = c.width
        ripple = m.ns(W + 2); prefix = m.ns(2 * clog2(W) + 2)
        if t in MULDIV:
            fixes = [Fix("Pipeline / sequentialise the multiplier-divider", "multiplier_pipelining",
                         f"{t} of {W} bits on a path that consumes ≈{(g['arr'] + g['rem']) / g['period'] * 100:.0f}% of the period. Pipeline the partial-product tree, or if one operand is constant replace with shift-add (strength reduction); dividers/modulo should be multi-cycle or replaced by power-of-two shifts.",
                         latency_change="+N cycles", equivalence="sequential with latency offset", risk="medium")]
        elif t in CMP_MAG:
            fixes = [Fix("Prefix comparator / reduce compare width", "comparator_restructure",
                         "Magnitude compare of {W} bits mapped as a ripple borrow chain. Use a tree comparator or compare only the bits that can differ.", latency_change="none", equivalence="combinational")]
        else:
            fixes = [Fix("Force a parallel-prefix adder", "prefix_adder",
                         f"A {W}-bit ripple adder costs ≈{ripple:.2f} ns; a Kogge-Stone/Brent-Kung/Sklansky adder ≈{prefix:.2f} ns. Either run timing-driven mapping (ABC `-D`, `abc -fast` keeps prefix structure; see the local flow notes) or instantiate an explicit prefix adder module.",
                         expected_gain_ns=round(ripple - prefix, 3), latency_change="none", equivalence="combinational", risk="low", ppa="prefix adder ≈1.5-2x adder area"),
                     Fix("Carry-select / split adder with pipelining", "carry_select_split",
                         f"Split the {W}-bit add into two {W // 2}-bit halves; compute the upper half for both carry values (carry-select), or register the lower half's carry-out and finish the upper half next cycle (+1 latency on the upper bits).",
                         latency_change="none (carry-select) / +1 (split)", equivalence="combinational / sequential", risk="low")]
        oname = _local(d, d.bit_name(c.out_bits()[0]))[1]
        if re.fullmatch(r"net\d+", oname):
            oname = f"{t}@{src}"
        issues.append(Issue("WIDE_ARITH_ON_CRITICAL", "arithmetic", "medium",
                            f"{W}-bit {t} at {src} in {mod} sits on a path using ≈{(g['arr'] + g['rem']) / g['period'] * 100:.0f}% of the {g['period']:.3f} ns period",
                            mod, oname, src, len(g["insts"]), sorted(g["insts"])[:5],
                            evidence={"width": W, "type": t, "arrival_ns": _fmt(g["arr"]), "remaining_ns": _fmt(g["rem"]), "period_ns": _fmt(g["period"]),
                                      "est_ripple_ns": _fmt(ripple), "est_prefix_ns": _fmt(prefix)},
                            fixes=fixes))
    return issues


# ============================================================ 7. hierarchy boundaries
def det_unregistered_outputs(ctx: Context):
    """Module outputs driven by combinational logic that depends on module inputs."""
    d = ctx.d
    from .netgraph import strip_module_name
    issues = []
    for mname, mod in d.hier_mods.items():
        base = strip_module_name(mname)
        if base == d.top:
            continue
        ports = mod.get("ports", {})
        cells = mod.get("cells", {})
        driver = {}
        loads = defaultdict(list)
        in_bits = set()
        for pn, p in ports.items():
            if p["direction"] == "input":
                in_bits |= {b for b in p["bits"] if isinstance(b, int)}
        for cn, c in cells.items():
            if c["type"] in ("$scopeinfo",):
                continue
            dirs = c.get("port_directions", {})
            for pn, bits in c["connections"].items():
                for b in (bits if isinstance(bits, list) else [bits]):
                    if not isinstance(b, int):
                        continue
                    if dirs.get(pn) == "output":
                        driver[b] = (cn, c)
                    else:
                        loads[b].append((cn, c))
        n_inst = d.instance_counts().get(base, 1)
        comb_out = defaultdict(list)     # port -> bits driven by comb
        through = defaultdict(list)      # port -> bits combinationally reachable from inputs
        for pn, p in ports.items():
            if p["direction"] != "output":
                continue
            for i, b in enumerate(p["bits"]):
                if not isinstance(b, int):
                    continue
                if b in in_bits:
                    through[pn].append(i); continue
                drv = driver.get(b)
                if drv is None:
                    continue
                cn, c = drv
                if c["type"] in SEQ_TYPES or c["type"] in d.hier_mods:
                    continue   # register, or a sub-module instance (analysed at its own level)
                comb_out[pn].append(i)
                # reach inputs?
                stack, seen, hit = [b], set(), False
                while stack:
                    x = stack.pop()
                    if x in seen:
                        continue
                    seen.add(x)
                    if x in in_bits:
                        hit = True; break
                    dd = driver.get(x)
                    if dd and dd[1]["type"] not in SEQ_TYPES and dd[1]["type"] != "$scopeinfo":
                        for pn2, bits in dd[1]["connections"].items():
                            if dd[1].get("port_directions", {}).get(pn2) != "output":
                                stack += [y for y in (bits if isinstance(bits, list) else [bits]) if isinstance(y, int)]
                if hit:
                    through[pn].append(i)
        for pn, idxs in comb_out.items():
            ttype = driver[ports[pn]["bits"][idxs[0]]][1]["type"]
            src = short_src(driver[ports[pn]["bits"][idxs[0]]][1].get("attributes", {}).get("src", ""))
            is_through = bool(through.get(pn))
            issues.append(Issue("UNREGISTERED_MODULE_OUTPUT", "hierarchy", "medium" if is_through else "low",
                                f"Output '{pn}' of {base} is combinational ({ttype}){' and depends on module inputs: the timing path crosses the hierarchy boundary' if is_through else ''}",
                                base, pn, src, n_inst, [],
                                evidence={"bits": len(idxs), "driver_type": ttype, "input_to_output_combinational": is_through,
                                          "note": "hierarchical synthesis (ABC per module) cannot optimise across this boundary; the consumer sees a late-arriving input"},
                                fixes=[Fix("Register the module output (or move the logic to the consumer)", "boundary_registering",
                                           f"Drive '{pn}' from a flop; if the logic is a mask/mux of a register, apply it inside the consumer at the register boundary instead so the boundary net is a clean flop output.",
                                           latency_change="+1 cycle if a real register is added; none if the mask moves", equivalence="sequential", risk="low"),
                                       Fix("Flatten the hierarchy for synthesis", "hierarchy_flattening",
                                           "Run `flatten` (or ungroup) before ABC so cross-boundary logic is mapped together; keep hierarchy only where constraint names need it.",
                                           latency_change="none", equivalence="none needed", risk="low")]))
    return issues


# ============================================================ 8. async reset fanout
def det_async_reset_fanout(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    by_src = defaultdict(lambda: {"regs": 0, "bits": 0, "mods": Counter(), "clocks": set()})
    for reg in d.regs.values():
        if isinstance(reg.arst_bit, int):
            key = reg.arst_bit
            g = by_src[key]; g["regs"] += 1; g["bits"] += reg.width; g["mods"][reg.module] += reg.width; g["clocks"].add(reg.clock)
    groups = {}
    thr = ctx.cfg.get("reset_fanout_threshold", 256)
    for b, g in by_src.items():
        if g["bits"] < thr:
            continue
        key = _sig_key(d, d.bit_name(b))
        gg = groups.setdefault(key, {"bits": 0, "regs": 0, "insts": set(), "mods": Counter(), "clocks": set(), "b": b})
        gg["bits"] = max(gg["bits"], g["bits"]); gg["regs"] = max(gg["regs"], g["regs"]); gg["mods"] |= g["mods"]; gg["clocks"] |= g["clocks"]
        gg["insts"].add(d.bit_name(b))
    issues = []
    for (mod, local), g in groups.items():
        b = g["b"]
        drv = d.driver.get(b)
        src_reg = d.reg_of_qbit.get(b)
        pen_unbuf = m.fanout_unbuffered_ns(g["bits"]); pen_buf = m.fanout_penalty_ns(g["bits"])
        fastest = min((te.period_of(c) or 1e9 for c in g["clocks"]), default=None)
        est_slack = (fastest - m.uncertainty_ns - m.recovery_ns - (m.clk_to_q_ns + pen_buf)) if fastest and fastest < 1e9 else None
        k = math.ceil(g["bits"] / ctx.cfg.get("reset_fanout_per_copy", 512))
        no_reset_cands = [(mname, n) for mname, n in g["mods"].most_common(3)]
        issues.append(Issue("ASYNC_RESET_FANOUT", "reset", "high" if (est_slack is not None and est_slack < 0) or g["bits"] > 2048 else "medium",
                            f"Asynchronous reset '{local}' releases {g['bits']} flop bits ({g['regs']} registers) across {len(g['mods'])} module types; recovery check at the {fastest:.3f} ns clock" if fastest else f"Asynchronous reset '{local}' releases {g['bits']} flop bits",
                            mod, local, src_reg.cell.short_src if src_reg else "", len(g["insts"]), sorted(g["insts"])[:5],
                            evidence={"reset_bits": g["bits"], "registers": g["regs"], "by_module": dict(g["mods"]), "source": src_reg.name if src_reg else d.bit_name(b),
                                      "source_role": src_reg.role if src_reg else "port", "est_penalty_buffered_ns": _fmt(pen_buf), "est_penalty_unbuffered_ns": _fmt(pen_unbuf),
                                      "fastest_capture_period_ns": _fmt(fastest), "est_recovery_slack_ns": _fmt(est_slack)},
                            fixes=[Fix("Replicate the reset synchroniser's last stage per block", "reset_tree_replication",
                                       f"Keep {k} copies of the synchronised reset (one per engine/lane group, ≤512 bits each) with keep attributes; the deassertion edge stays synchronous within the family so recovery is still timed but the tree depth drops from ≈{pen_buf:.2f} ns to ≈{m.fanout_penalty_ns(math.ceil(g['bits'] / k)):.2f} ns.",
                                       expected_gain_ns=round(pen_buf - m.fanout_penalty_ns(math.ceil(g["bits"] / k)), 3), latency_change="none", equivalence="sequential (reset behaviour identical)", risk="low", ppa=f"+{k} flops"),
                                   Fix("Drop the reset on datapath registers that are qualified by a valid flag", "reset_removal",
                                       f"Registers whose contents are ignored until a valid/fill flag is set (typically the largest groups here: {', '.join(f'{mn}:{n} bits' for mn, n in no_reset_cands)}) do not need an asynchronous reset; removing it removes them from the recovery check and from the reset tree entirely.",
                                       latency_change="none", equivalence="sequential with reset-state don't-care (needs formal with initial-value abstraction)", risk="medium", ppa="smaller flops (no reset pin)"),
                                   Fix("Let the physical flow buffer the reset; constrain recovery only", "fanout_buffering",
                                       "Pre-layout the reset net is a single driver. `set_max_fanout` + `repair_design`/buffer insertion will build the tree; recovery/removal remain checked. This does not remove the RTL fanout, it only bounds the delay.",
                                       sdc_change="set_max_fanout 32 [current_design]", latency_change="none", equivalence="none", risk="low")]))
    return issues


# ============================================================ 9. CDC
def det_cdc(ctx: Context):
    d = ctx.d
    issues = []
    groups = defaultdict(list)
    for cr in ctx.crossings:
        groups[(cr.classification, cr.module, _sig_key(d, cr.dst_reg)[1])].append(cr)
    has_max_delay = bool(getattr(ctx.sdc, "max_delays", []))
    for (cls, mod, local), lst in groups.items():
        cr = lst[0]
        insts = sorted({c.inst_path for c in lst})[:5]
        src_l = _local(d, cr.src_reg.split(",")[0])[1] if cr.src_reg and not cr.src_reg.startswith("port:") else cr.src_reg
        ev = {"src": cr.src_reg, "src_clock": ctx.clock_label(cr.src_clock), "dst_clock": ctx.clock_label(cr.dst_clock),
              "width": cr.width, "sync_depth": cr.sync_depth, "through_logic": cr.through_logic, "notes": cr.notes}
        if cls == "synchronizer":
            issues.append(Issue("CDC_SYNCHRONIZER", "cdc", "info", f"2-flop synchroniser '{local}' in {mod} ({src_l} → {ctx.clock_label(cr.dst_clock)})",
                                mod, local, cr.dst_reg and d.regs[cr.dst_reg].cell.short_src, len(lst), insts, evidence=ev,
                                fixes=[Fix("Keep: constrain and mark", "cdc_synchronizer",
                                           "set_false_path (or set_max_delay one destination period) from the source register to the first stage only; add ASYNC_REG/async_reg attributes so placement keeps the stages adjacent.",
                                           sdc_change=f"set_false_path -from [get_cells <{src_l}>] -to [get_pins <{local}_meta/D>]", latency_change="none", equivalence="none", risk="low")]))
        elif cls == "handshake_payload":
            issues.append(Issue("CDC_BUNDLED_DATA_BUS", "cdc", "low" if has_max_delay else "medium",
                                f"{cr.width}-bit bus '{local}' in {mod} captured from {ctx.clock_label(cr.src_clock)} under a synchronised enable",
                                mod, local, d.regs[cr.dst_reg].cell.short_src, len(lst), insts, evidence=dict(ev, sdc_has_max_delay=has_max_delay),
                                fixes=[Fix("Bundled-data constraint", "cdc_bundled_data",
                                           "Keep the payload stable from request launch until acknowledge (already the mailbox protocol). Constrain payload transport with set_max_delay ≤ one destination period (ideally ≤ 80 %) and never blanket-false-path the crossing; the request must be launched at least one source cycle after the payload is stable (or use a delayed request).",
                                           sdc_change="set_max_delay -from [payload regs] -to [dst_data regs] <0.8*T_dst>", latency_change="none", equivalence="none", risk="low"),
                                       Fix("Replace mailbox with an asynchronous FIFO for throughput", "async_fifo",
                                           "If skipped snapshots are not acceptable, a 2-deep gray-pointer FIFO gives one transfer per destination cycle.", latency_change="protocol change", equivalence="not equivalent (throughput)", risk="high")]))
        elif cls in ("unsynchronized", "bus_unsynchronized", "logic_before_synchronizer"):
            sev = "critical" if cls != "logic_before_synchronizer" else "high"
            issues.append(Issue("CDC_UNSAFE", "cdc", sev, f"{cls.replace('_', ' ')}: '{local}' in {mod} captures {src_l} from {ctx.clock_label(cr.src_clock)}",
                                mod, local, d.regs[cr.dst_reg].cell.short_src, len(lst), insts, evidence=ev,
                                fixes=[Fix("Add a proper synchroniser / handshake", "cdc_synchronizer",
                                           "Single bit: register in the source domain then 2 flops in the destination. Multi-bit: gray code (counters), toggle/req-ack handshake with held payload, or an async FIFO. No combinational logic before the first synchroniser flop.",
                                           latency_change="+2 destination cycles", equivalence="not combinational; protocol-level", risk="medium")]))
        elif cls == "reset_domain_crossing":
            issues.append(Issue("RESET_DOMAIN_CROSSING", "reset", "medium", f"Reset of '{local}' in {mod} is released by {src_l} clocked on {ctx.clock_label(cr.src_clock)} while '{local}' runs on {ctx.clock_label(cr.dst_clock)}",
                                mod, local, d.regs[cr.dst_reg].cell.short_src, len(lst), insts, evidence=ev,
                                fixes=[Fix("Use a reset synchroniser in the destination clock", "reset_synchronizer",
                                           "Each clock domain (including generated clocks used with different edges) should release reset through its own synchroniser; the recovery check then stays inside one clock. If the same family is intended, keep it but expect the recovery check at the fast clock period.",
                                           latency_change="none", equivalence="reset-sequence only", risk="low")]))
    return issues


# ============================================================ 10. clocks / constraints
def det_clocks(ctx: Context):
    issues = []
    for dom in ctx.domains.values():
        if dom.registers == 0:
            continue
        if dom.kind == "logic":
            issues.append(Issue("CLOCK_THROUGH_LOGIC", "clocking", "high", f"Clock '{dom.id}' passes through combinational logic (gated / glitch-prone)",
                                ctx.d.top, dom.id, "", 1, [], evidence={"notes": dom.notes, "registers": dom.registers},
                                fixes=[Fix("Use an integrated clock-gating cell or a flop-based divider", "clock_gating_cell", "Never derive clocks with AND/OR/MUX in RTL; use a latch-based ICG or take the clock from a flop output and declare it as a generated clock.", risk="high")]))
        if dom.kind == "generated" and dom.period_source != "sdc":
            issues.append(Issue("GENERATED_CLOCK_UNDECLARED", "constraints", "high", f"Generated clock from register '{dom.source_reg}' (ratio {dom.ratio}) has no matching create_generated_clock",
                                ctx.d.top, dom.source_reg, "", 1, [], evidence={"ratio": dom.ratio, "period_source": dom.period_source, "registers": dom.registers, "notes": dom.notes},
                                fixes=[Fix("Declare the generated clock", "sdc_generated_clock", "create_generated_clock -name <g> -source <master port> -divide_by <N> [get_pins <divider flop>/Q]", sdc_change=f"create_generated_clock -source <master> -divide_by {int(dom.ratio) if dom.ratio else 'N'} [get_pins <{dom.source_reg}>]", risk="low")]))
        if dom.kind == "master" and dom.period_source in ("default", "unknown"):
            issues.append(Issue("UNCONSTRAINED_CLOCK", "constraints", "high", f"Clock port '{dom.root_port}' drives {dom.registers} registers but has no create_clock",
                                ctx.d.top, dom.root_port, "", 1, [], evidence={"registers": dom.registers},
                                fixes=[Fix("Add create_clock", "sdc_create_clock", "create_clock -name <n> -period <T> [get_ports <port>]", risk="low")]))
        if dom.notes and dom.kind == "generated" and any("ratio" in n for n in dom.notes):
            issues.append(Issue("DIVIDER_UNRECOGNISED", "clocking", "low", f"Divider register '{dom.source_reg}' is not a simple +1 counter / toggle: ratio unknown to the analyser",
                                ctx.d.top, dom.source_reg, "", 1, [], evidence={"notes": dom.notes}, fixes=[]))
    # related-clock paths: multicycle candidates
    te = ctx.te; d = ctx.d
    groups = {}
    for ep in te.endpoints:
        if ep.relation != "related" or ep.slack is None or ep.slack > 0.3 * (ep.period or 1):
            continue
        pl, pc = te.period_of(ep.launch_clock), te.period_of(ep.capture_clock)
        if pl and pc and pl > pc * 1.5:
            key = (_sig_key(d, ep.name), ep.launch_clock, ep.capture_clock)
            g = groups.setdefault(key, {"ep": ep, "n": 0}); g["n"] += 1
    for (key, lc, cc), g in groups.items():
        ep = g["ep"]
        ratio = int(round(te.period_of(lc) / te.period_of(cc)))
        issues.append(Issue("MULTICYCLE_CANDIDATE", "constraints", "low", f"Path from {ctx.clock_label(lc)} to {ctx.clock_label(cc)} into '{key[1]}' is timed at the fast period but the source only changes every {ratio} fast cycles",
                            key[0], key[1], "", g["n"], [], evidence={"launch": ctx.clock_label(lc), "capture": ctx.clock_label(cc), "ratio": ratio, "est_slack_ns": _fmt(ep.slack)},
                            fixes=[Fix("set_multicycle_path", "multicycle_constraint", f"If the destination truly samples only every {ratio} cycles (phase-aligned generated clock), set_multicycle_path -setup {ratio} -end and -hold {ratio - 1}. Verify with simulation; this is a constraint, not an RTL change.",
                                       sdc_change=f"set_multicycle_path -setup {ratio} -from [get_clocks {ctx.domains[lc].sdc_name}] -to [get_clocks {ctx.domains[cc].sdc_name}]", risk="medium")]))
    return issues


# ============================================================ 11. retiming balance
def det_retiming_imbalance(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    groups = {}
    for reg in d.regs.values():
        period = te.period_of(reg.clock)
        if not period or reg.width < 4:
            continue
        din = te.reg_depth_in(reg)
        if din < 0.6 * period:
            continue
        # predecessor registers and their own input depth
        preds = {}
        for b in reg.d_bits:
            if not isinstance(b, int):
                continue
            sb = te.start_bit.get(b)
            if sb in d.reg_of_qbit:
                pr = d.reg_of_qbit[sb]
                if _enters_as_control(d, te, b):
                    continue   # the critical start is a control signal (mux select / enable): not a retiming candidate
                preds[pr.name] = te.reg_depth_in(pr)
        shallow = {n: v for n, v in preds.items() if v < 0.25 * period and n != reg.name}
        if not shallow:
            continue
        key = (reg.module, _sig_key(d, reg.name)[1])
        g = groups.setdefault(key, {"reg": reg, "din": din, "dout": te.reg_depth_out(reg), "shallow": shallow, "period": period, "insts": set(), "loop": reg.name in preds})
        g["insts"].add(reg.inst_path)
    issues = []
    for (mod, local), g in groups.items():
        sh = {_local(d, n)[1]: _fmt(v) for n, v in list(g["shallow"].items())[:6]}
        issues.append(Issue("RETIMING_IMBALANCE", "timing", "medium",
                            f"Register '{local}' in {mod} has ≈{g['din']:.2f} ns of logic before it while its predecessor registers ({', '.join(list(sh)[:3])}) have almost none: candidate for backward retiming / pre-computation",
                            mod, local, g["reg"].cell.short_src, len(g["insts"]), sorted(g["insts"])[:5],
                            evidence={"depth_in_ns": _fmt(g["din"]), "depth_out_ns": _fmt(g["dout"]), "period_ns": _fmt(g["period"]), "shallow_predecessors_depth_ns": sh, "loop_carried": g["loop"]},
                            fixes=[Fix("Backward retiming: move part of the logic across the shallow predecessor registers", "retiming",
                                       ("Operands that come from a shift/pipeline register can be pre-processed one stage earlier (e.g. negate/mask the outgoing word at the previous tap, pre-add operands that are both available a cycle early). " +
                                        ("The loop-carried operand itself cannot move; only the non-recursive part of the expression can. " if g["loop"] else "") +
                                        "Synthesis retiming (`yosys` `dfflegalize`+ABC `-dret`, or vendor retime) can do this automatically if registers have no reset/enable differences."),
                                       latency_change="none", equivalence="sequential (retiming-equivalent; use induction-based SEC)", risk="medium")]))
    return issues


def _enters_as_control(d: Design, te: TimingEngine, bit) -> bool:
    for st in te.path_of(bit):
        cell = d.cells.get(st.cell)
        if cell is None or not isinstance(st.in_bit, int):
            continue
        for port in ("S", "EN"):
            if st.in_bit in cell.conns.get(port, []):
                return True
        if cell.type in CMP_EQ | CMP_MAG:
            return True
    return False


# ============================================================ 12. misc structural
def det_misc(ctx: Context):
    d, te, m = ctx.d, ctx.te, ctx.m
    issues = []
    # combinational loops
    if te.loop_cells:
        cells = [d.cells[n] for n in te.loop_cells[:10]]
        issues.append(Issue("COMBINATIONAL_LOOP", "structure", "critical", f"{len(te.loop_cells)} cells are in combinational loops", cells[0].module if cells else d.top, "", cells[0].short_src if cells else "", 1,
                            [c.name for c in cells[:5]], evidence={"cells": [(c.type, c.short_src) for c in cells]},
                            fixes=[Fix("Break the loop with a register or restructure", "loop_breaking", "STA cannot time loops; usually an incomplete if/else in a combinational block or a feedback through a mux.", risk="high")]))
    # latches
    latches = [r for r in d.regs.values() if r.cell.type in LATCH_TYPES]
    if latches:
        groups = defaultdict(list)
        for r in latches:
            groups[(r.module, _sig_key(d, r.name)[1])].append(r)
        for (mod, local), lst in groups.items():
            issues.append(Issue("LATCH_INFERRED", "structure", "high", f"Latch inferred for '{local}' in {mod}", mod, local, lst[0].cell.short_src, len(lst), [r.inst_path for r in lst[:5]],
                                fixes=[Fix("Complete the assignment / use a flop", "latch_removal", "Assign the signal in every branch of the combinational block, or make it a clocked register.", risk="low")]))
    # priority mux chains
    chains = _mux_chains(d)
    for (mod, src), (length, insts, w) in chains.items():
        if length >= ctx.cfg.get("mux_chain_threshold", 4):
            issues.append(Issue("PRIORITY_MUX_CHAIN", "logic", "medium" if length >= 6 else "low", f"if/else-if chain of {length} {w}-bit muxes in series at {src} in {mod}", mod, "", src, len(insts), sorted(insts)[:5],
                                evidence={"chain_length": length, "width": w, "est_levels": length},
                                fixes=[Fix("Parallel case / one-hot select", "mux_restructure", "If the conditions are mutually exclusive use a parallel (one-hot AND-OR) mux: depth log2(N) instead of N. Otherwise move the most timing-critical condition to the last (closest to output) mux.", latency_change="none", equivalence="combinational", risk="low")]))
    # multipliers / dividers (grouped by source line)
    groups = {}
    for c in d.comb_cells():
        if c.type in MULDIV:
            g = groups.setdefault((c.module, c.short_src, c.type), {"c": c, "insts": set(), "const": False})
            g["insts"].add(c.inst_path)
            g["const"] |= any(all(isinstance(b, str) for b in c.conns.get(p, [])) for p in ("A", "B"))
    for (mod, src, t), g in groups.items():
        c = g["c"]
        issues.append(Issue("EXPENSIVE_OPERATOR", "arithmetic", "medium", f"{t} of {c.width} bits at {src} in {mod}" + (" (constant operand)" if g["const"] else ""), mod, f"{t}@{src}", src, len(g["insts"]), sorted(g["insts"])[:5],
                            evidence={"width": c.width, "constant_operand": g["const"]},
                            fixes=[Fix("Strength reduction / pipelining", "multiplier_pipelining", "Constant operand → shift-add (canonical signed digit); variable → pipelined Booth/Wallace multiplier; division/modulo → multi-cycle sequential divider or power-of-two shift.", latency_change="depends", equivalence="sequential", risk="medium")]))
    # FSM state registers
    fsms = [r for r in d.regs.values() if "fsm_state" in r.role]
    groups = defaultdict(list)
    for r in fsms:
        groups[(r.module, _sig_key(d, r.name)[1])].append(r)
    for (mod, local), lst in groups.items():
        r = lst[0]
        fan = max(te.fan(b) for b in r.q_bits)
        issues.append(Issue("FSM_ENCODING", "control", "low" if fan < 32 else "medium", f"FSM state register '{local}' ({r.width} bits) in {mod}, decode fanout {fan}", mod, local, r.cell.short_src, len(lst), [x.inst_path for x in lst[:5]],
                            evidence={"state_bits": r.width, "decode_fanout": fan},
                            fixes=[Fix("One-hot encoding + registered outputs", "fsm_recoding", "One-hot removes the state decode (each state bit is the decoded condition); register Mealy outputs or convert to Moore so outputs launch from flops.", latency_change="none (Moore may add 1)", equivalence="sequential", risk="low")]))
    # large register arrays (mem2reg)
    arrays = defaultdict(list)
    for r in d.regs.values():
        mm = re.match(r"(.*)\[(\d+)\]$", _local(d, r.name)[1])
        if mm:
            arrays[(r.module, mm.group(1))].append(r)
    for (mod, base), lst in arrays.items():
        n_inst = len({r.inst_path for r in lst})
        words = len(lst) // max(n_inst, 1)
        bits = sum(r.width for r in lst) // max(n_inst, 1)
        if bits < 256:
            continue
        shift = all("shift_stage" in r.role or "plain" in r.role or "pipeline" in r.role for r in lst)
        issues.append(Issue("LARGE_REGISTER_ARRAY", "area", "info", f"Array '{base}' in {mod}: {words} words, {bits} flop bits per instance ({'shift register / delay line' if shift else 'random access'})", mod, base, lst[0].cell.short_src, n_inst, sorted({r.inst_path for r in lst})[:5],
                            evidence={"words": words, "bits_per_instance": bits, "total_bits": bits * n_inst, "access": "shift" if shift else "indexed", "has_reset": any(r.arst_bit is not None for r in lst)},
                            fixes=[Fix("SRAM / register-file ring buffer (area)", "memory_mapping", "A delay line of N words can be an SRAM ring buffer with a write pointer and read pointer = write-N (1R1W or 2-port), cutting flop count by ~N×W. Changes cell count and needs a memory macro; timing of the datapath is unchanged.", latency_change="none if read is registered identically", equivalence="sequential", risk="medium", ppa="large area/power reduction")] if shift else
                                  [Fix("Map to SRAM", "memory_mapping", "Indexed arrays of this size read through a mux tree; an SRAM macro removes the mux tree and the flops.", risk="medium")]))
    # deep enable cones on wide registers
    groups = {}
    for r in d.regs.values():
        if not r.en_bits or r.width < 8:
            continue
        en = r.en_bits[0]
        if not isinstance(en, int):
            continue
        arr = te.in_arrival(en); period = te.period_of(r.clock)
        if period and arr > 0.5 * period:
            key = (r.module, _sig_key(d, r.name)[1])
            g = groups.setdefault(key, {"r": r, "arr": arr, "period": period, "insts": set()}); g["insts"].add(r.inst_path)
    for (mod, local), g in groups.items():
        issues.append(Issue("DEEP_ENABLE_CONE", "control", "medium", f"Enable of {g['r'].width}-bit register '{local}' in {mod} arrives at ≈{g['arr']:.2f} ns of a {g['period']:.3f} ns period", mod, local, g["r"].cell.short_src, len(g["insts"]), sorted(g["insts"])[:5],
                            evidence={"enable_arrival_ns": _fmt(g["arr"]), "period_ns": _fmt(g["period"]), "width": g["r"].width},
                            fixes=[Fix("Register / pre-compute the enable", "control_precompute", "Compute the enable condition one cycle early from next-state values so the wide register's enable mux is driven by a flop.", latency_change="none", equivalence="sequential", risk="low")]))
    # I/O paths
    io = defaultdict(list)
    for ep in te.endpoints:
        if ep.slack is None or ep.slack >= 0:
            continue
        if ep.kind == "output":
            io[(ep.kind, _sig_key(d, ep.name))].append(ep)
        elif ep.start_bit is not None and ep.period and te.arr.get(ep.start_bit, 0.0) > 0.4 * ep.period:
            io[("input", _sig_key(d, ep.name))].append(ep)   # external input delay eats the budget
    for (kind, key), lst in io.items():
        ep = min(lst, key=lambda e: e.slack)
        issues.append(Issue("IO_PATH_VIOLATION", "timing", "medium", f"{'Output' if kind == 'output' else 'Input'} path {ep.start_name} → {ep.name} est. slack {ep.slack:.2f} ns", key[0], key[1], "", len(lst), [],
                            evidence={"est_slack_ns": _fmt(ep.slack), "arrival_ns": _fmt(ep.arrival), "period_ns": _fmt(ep.period)},
                            fixes=[Fix("Register at the boundary", "boundary_registering", "Drive outputs straight from flops / register inputs before use; the external delay budget in the SDC then only covers the flop.", latency_change="+1 cycle", equivalence="sequential with latency offset", risk="medium")]))
    return issues


def _mux_chains(d: Design):
    muxes = [c for c in d.comb_cells() if c.type == "$mux"]
    out_owner = {b: c for c in muxes for b in c.out_bits()}
    pred = {}
    for c in muxes:
        for p in ("A", "B"):
            for b in c.conns.get(p, []):
                if b in out_owner and out_owner[b] is not c:
                    pred[c.name] = out_owner[b]
    length = {}
    def L(c):
        if c.name in length:
            return length[c.name]
        length[c.name] = 1 + (L(pred[c.name]) if c.name in pred else 0)
        return length[c.name]
    res = {}
    has_succ = {p.name for p in pred.values()}
    for c in muxes:
        if c.name in has_succ:
            continue
        l = L(c)
        if l >= 2:
            key = (c.module, c.short_src)
            cur = res.get(key, (0, set(), c.width))
            res[key] = (max(cur[0], l), cur[1] | {c.inst_path}, c.width)
    return res


# ============================================================ 13. resource sharing (area)
def det_resource_sharing(ctx: Context):
    d = ctx.d
    issues = []
    seen = set()
    for c in d.comb_cells():
        if c.type != "$mux":
            continue
        da = d.driver_cell(c.conns["A"][0]) if c.conns.get("A") and isinstance(c.conns["A"][0], int) else None
        db = d.driver_cell(c.conns["B"][0]) if c.conns.get("B") and isinstance(c.conns["B"][0], int) else None
        if da and db and da is not db and da.type == db.type and da.type in ARITH | MULDIV and da.width == db.width and da.width >= 8:
            key = (c.module, c.short_src)
            if key in seen:
                continue
            seen.add(key)
            issues.append(Issue("RESOURCE_SHARING", "area", "info", f"Mux at {c.short_src} in {c.module} selects between two {da.width}-bit {da.type} results: operator can be shared", c.module, "", c.short_src, 1, [c.inst_path],
                                fixes=[Fix("Mux the operands, share one operator", "resource_sharing", "Select the operands first and use a single adder/multiplier; reduces area, adds one mux level before the operator (check timing).", risk="low", ppa="-1 operator, +operand muxes")]))
    return issues


ALL_DETECTORS = [det_critical_paths, det_high_fanout, det_late_control, det_comparator_flag, det_arith_chains,
                 det_wide_arith, det_unregistered_outputs, det_async_reset_fanout, det_cdc, det_clocks,
                 det_retiming_imbalance, det_misc, det_resource_sharing]


def run_all(ctx: Context):
    issues = []
    for det in ALL_DETECTORS:
        issues += det(ctx)
    return issues
