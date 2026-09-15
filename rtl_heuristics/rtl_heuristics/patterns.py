"""Structural pattern recognition: register roles and module archetypes.

This is the "find the most relevant known design" step: every module instance is
matched against a catalogue of archetypes that RTL engineers recognise on sight
(reset synchroniser, binary clock divider, toggle handshake mailbox, fill
counter with terminal flag, shift-register window + rolling accumulator, linear
adder reduction, ...).  Each archetype carries a playbook of the standard
timing fixes for that structure.
"""
from __future__ import annotations
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from .netgraph import Design, Register, Cell
from .delay_model import ARITH, CMP_EQ, CMP_MAG


def _const_of(bits):
    if bits and all(isinstance(b, str) for b in bits):
        try:
            return int("".join(reversed(bits)).replace("x", "0"), 2)
        except ValueError:
            return None
    return None


def classify_registers(design: Design):
    """Set reg.role for every register."""
    d = design
    qbit_reg = d.reg_of_qbit
    for reg in d.regs.values():
        roles = set()
        d_drivers = [d.driver.get(b) for b in reg.d_bits if isinstance(b, int)]
        cells = [x[1] for x in d_drivers if x and x[0] == "cell"]
        types = Counter(c.type for c in cells)
        qset = set(reg.q_bits)
        # shift stage: D bits come straight from another register's Q (no logic)
        src_regs = set()
        direct = 0
        for b in reg.d_bits:
            if isinstance(b, int) and b in qbit_reg:
                direct += 1; src_regs.add(qbit_reg[b].name)
        if reg.d_bits and direct == len(reg.d_bits) and src_regs and reg.name not in src_regs:
            roles.add("shift_stage")
            src = next(iter(src_regs))
            if d.regs[src].family != reg.family or d.regs[src].clock != reg.clock:
                roles.add("cross_clock_sample")
                if reg.width == 1:
                    roles.add("sync_stage")
        # vector shift register {q[W-2:0], din}: synchroniser idiom when din crosses clocks
        if reg.width >= 2 and all(reg.d_bits[i] == reg.q_bits[i - 1] for i in range(1, reg.width)):
            roles.add("shift_vector")
            din = reg.d_bits[0]
            if isinstance(din, int) and din in qbit_reg and qbit_reg[din].clock != reg.clock:
                roles.add("sync_stage")
            elif isinstance(din, int) and din not in qbit_reg and d.driver.get(din, ("port",))[0] == "port":
                roles.add("sync_stage")
        # constant load (e.g. reset synchroniser shifting in 1'b1)
        if reg.d_bits and all(isinstance(b, str) or b in qset for b in reg.d_bits) and any(isinstance(b, str) for b in reg.d_bits):
            roles.add("const_shift")
        # loop-carried arithmetic / counters / toggles (look a few cells deep through arithmetic and muxes)
        cone = list(cells)
        frontier = list(cells)
        for _ in range(3):
            nxt = []
            for c in frontier:
                if c.type in ARITH or c.type in ("$mux", "$pmux", "$and", "$or", "$xor", "$not"):
                    for b in c.in_bits():
                        dc = d.driver_cell(b)
                        if dc is not None and not dc.is_seq and dc not in cone:
                            cone.append(dc); nxt.append(dc)
            frontier = nxt
        for c in cone:
            ins = set(c.in_bits())
            if c.type in ARITH and ins & qset:
                other = [b for b in c.conns.get("B", []) + c.conns.get("A", []) if not (isinstance(b, int) and b in qset)]
                if _const_of(other) == 1 or _const_of(c.conns.get("B", [])) == 1:
                    roles.add("counter")
                else:
                    roles.add("accumulator")
            if c.type in ("$not",) and ins & qset and reg.width == 1:
                roles.add("toggle")
            if c.type == "$mux" and reg.width == 1:
                a, b_ = c.conns.get("A", []), c.conns.get("B", [])
                nb = d.driver_cell(b_[0]) if b_ and isinstance(b_[0], int) else None
                if (nb and nb.type == "$not" and set(nb.in_bits()) & qset) or (a and a[0] in qset):
                    roles.add("toggle")
        # FSM-like: Q feeds equality compares against constants whose results steer muxes back into D
        cmp_consts = set()
        for b in reg.q_bits:
            for cell, port, idx in d.loads.get(b, []):
                if cell.type in CMP_EQ:
                    other = cell.conns["B"] if port == "A" else cell.conns["A"]
                    cv = _const_of(other)
                    if cv is not None:
                        cmp_consts.add(cv)
        if cmp_consts and ("counter" not in roles) and reg.width <= 8 and len(cmp_consts) >= 2:
            roles.add("fsm_state")
        if cmp_consts and "counter" in roles:
            roles.add("counter_with_compare")
        # clock source
        if any(any(port == "CLK" for _, port, _ in d.loads.get(b, [])) for b in reg.q_bits):
            roles.add("clock_source")
        if any(any(port in ("ARST", "SET", "CLR", "ALOAD") for _, port, _ in d.loads.get(b, [])) for b in reg.q_bits):
            roles.add("reset_source")
        if not roles:
            roles.add("pipeline" if direct else "plain")
        reg.role = ",".join(sorted(roles))
        reg.attrs = dict(reg.attrs)
        reg.attrs["src_regs"] = sorted(src_regs)


# --------------------------------------------------------------------- modules
ARCHETYPES = {
    "reset_synchronizer": dict(
        desc="Async-assert / sync-deassert reset synchroniser (constant shifted through 2+ flops)",
        risks=["reset output is a very high-fanout net: recovery/removal checks fail pre-layout",
               "if the synchroniser is in a different clock family from its consumers it is a reset-domain crossing"],
        fixes=["replicate the synchroniser (or its last stage) per block so each copy drives <= ~64 flops",
               "let the datapath registers (history/pipeline) run without reset when the function allows",
               "constrain: set_false_path on reset *assertion* only; recovery of deassertion stays timed"]),
    "two_flop_synchronizer": dict(
        desc="Generic 2-flop (or deeper) single-bit synchroniser",
        risks=["combinational logic before the first stage", "no ASYNC_REG placement attribute", "very high fanout when used as a reset"],
        fixes=["set_false_path / set_max_delay to the first stage only", "add async_reg attributes", "replicate when the output fans out to hundreds of loads"]),
    "clock_divider_counter": dict(
        desc="Synchronous binary counter whose bits are used as generated clocks",
        risks=["divided clock taken from a flop output: fine (glitch free) but must be declared with create_generated_clock",
               "any logic between the counter bit and the CK pins makes the clock glitchy"],
        fixes=["keep the divider synchronous (all bits on the master clock), never a ripple counter",
               "declare -divide_by / -edges generated clocks on the flop outputs"]),
    "toggle_handshake_mailbox": dict(
        desc="Bundled-data mailbox: req/ack toggle bits through 2-flop synchronisers, payload held stable",
        risks=["payload bus is a multi-bit crossing; only safe because the handshake guarantees stability",
               "recovery checks on the destination-side async reset"],
        fixes=["keep set_false_path only on req/ack to the first synchroniser stage",
               "bound payload transport with set_max_delay (bundled-data constraint)",
               "if throughput matters, replace with an async FIFO (gray-coded pointers)"]),
    "fill_counter_with_flag": dict(
        desc="Startup/fill counter whose terminal-count compare produces a broadcast control flag",
        risks=["the compare output is combinational and fans out to the whole datapath: a late, high-fanout control net",
               "flag is recomputed every cycle although it only changes once"],
        fixes=["register the flag (compute it from the *next* count value) so it launches from a flop",
               "make the flag sticky once set and replicate it per consumer block",
               "use a one-hot / down-counter zero-detect instead of a wide equality compare"]),
    "shift_window_accumulator": dict(
        desc="Shift-register history (tapped delay line) feeding a loop-carried rolling accumulator",
        risks=["3-operand add (sum + new - old) mapped as two ripple adders in series",
               "control gating (valid/full) placed inside the accumulator feedback path",
               "output masking mux after the accumulator adds a level and blocks retiming"],
        fixes=["carry-save (3:2) compression of sum/new/old followed by ONE carry-propagate adder",
               "move the gating onto the outgoing history word (zero the subtrahend at the last tap register) instead of muxing after the adder",
               "explicit parallel-prefix adder for the final CPA if the mapper produces ripple chains",
               "for area: replace the delay line with an SRAM ring buffer if latency contract allows"]),
    "linear_adder_reduction": dict(
        desc="Sequential for-loop accumulation of N operands into one register (N-1 adders in series)",
        risks=["depth grows as (W + N*c) even with carry-chain overlap; with prefix adders it is N*log(W)",
               "single register at the end: no pipelining inside the tree"],
        fixes=["balanced binary adder tree (depth ceil(log2 N))",
               "carry-save (Wallace/Dadda) compression to 2 vectors + one CPA",
               "pipeline the tree (register after each CSA layer) if one extra cycle of latency is acceptable",
               "apply any valid/mask once at the tree output instead of on every operand"]),
    "wrapper": dict(desc="Wiring-only module (no local cells)", risks=[], fixes=[]),
    "top_multi_clock": dict(
        desc="Top level with several asynchronous master clocks, dividers, and crossings",
        risks=["cross-family paths not covered by exceptions are timed at the shortest period",
               "reset synchronisers per domain multiply recovery endpoints"],
        fixes=["review set_clock_groups / max_delay exceptions per crossing", "keep one reset synchroniser per family"]),
    "fsm": dict(
        desc="Finite state machine (state register compared against constants, next-state muxes)",
        risks=["binary-encoded state decode on high-fanout control outputs", "output logic combinational from state"],
        fixes=["one-hot encoding when >4 states and state bits drive wide datapath control",
               "register FSM outputs (Moore/registered-Mealy)", "split large next-state case into parallel_case"]),
    "generic_datapath": dict(desc="Registers + arithmetic without a recognised archetype", risks=[], fixes=[]),
    "pipeline_regs": dict(desc="Only pass-through/pipeline registers", risks=[], fixes=[]),
}


@dataclass
class ModuleProfile:
    module: str
    instances: int
    archetype: str
    confidence: float
    evidence: list = field(default_factory=list)
    local_regs: int = 0
    local_reg_bits: int = 0
    comb_cells: Counter = field(default_factory=Counter)
    roles: Counter = field(default_factory=Counter)


def classify_modules(design: Design) -> dict[str, ModuleProfile]:
    d = design
    by_mod_inst: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))   # module -> inst -> regs
    by_mod_cells: dict[str, Counter] = defaultdict(Counter)
    inst_count = d.instance_counts()
    for reg in d.regs.values():
        by_mod_inst[reg.module][reg.inst_path].append(reg)
    for c in d.comb_cells():
        by_mod_cells[c.module][c.type] += 1
    profiles = {}
    all_mods = set(inst_count) | set(by_mod_inst) | set(by_mod_cells)
    for mod in all_mods:
        insts = by_mod_inst.get(mod, {})
        n_inst = inst_count.get(mod, len(insts) or 1)
        # representative instance = the one with the most registers; roles are voted over all instances
        rep = max(insts.values(), key=len, default=[])
        roles = Counter()
        for regs in insts.values():
            for r in regs:
                for role in r.role.split(","):
                    roles[role] += 1
        roles = Counter({k: max(1, round(v / max(len(insts), 1))) for k, v in roles.items()})
        cc = by_mod_cells.get(mod, Counter())
        # per-instance combinational cell counts
        cc_inst = Counter({k: v // max(n_inst, 1) for k, v in cc.items()})
        arche, conf, ev = _match_archetype(d, mod, rep, roles, cc_inst)
        profiles[mod] = ModuleProfile(mod, n_inst, arche, conf, ev, len(rep), sum(r.width for r in rep), cc_inst, roles)
    return profiles


def _match_archetype(d: Design, mod: str, regs: list, roles: Counter, cc: Counter):
    n_add = cc.get("$add", 0) + cc.get("$sub", 0) + cc.get("$alu", 0)
    if not regs and sum(cc.values()) == 0:
        return "wrapper", 1.0, ["no local registers or logic"]
    if mod == d.top:
        fams = {r.family for r in d.regs.values() if r.family}
        if len(fams) >= 2:
            return "top_multi_clock", 0.9, [f"{len(fams)} master clock families"]
    if roles.get("const_shift") and all(r.width <= 4 for r in regs) and len(regs) <= 2 and roles.get("reset_source"):
        return "reset_synchronizer", 0.95, ["constant shifted through a short async-reset register, output used as reset"]
    if (roles.get("shift_vector") or roles.get("sync_stage")) and all(r.width <= 4 for r in regs) and len(regs) <= 2:
        return "two_flop_synchronizer", 0.9, ["short shift register whose input comes from another clock (used as reset and/or data synchroniser)"]
    if roles.get("clock_source") and (roles.get("counter") or roles.get("toggle")) and len(regs) <= 2:
        return "clock_divider_counter", 0.95, ["counter/toggle register bits drive CK pins"]
    if roles.get("sync_stage", 0) >= 2 and roles.get("toggle", 0) >= 1:
        return "toggle_handshake_mailbox", 0.9, ["toggle req/ack registers plus 2-flop synchronisers and enabled payload"]
    if roles.get("counter_with_compare") and n_add <= 2 and cc.get("$eq", 0) + cc.get("$ne", 0) >= 1 and len(regs) <= 2:
        return "fill_counter_with_flag", 0.9, ["counter compared against a constant, compare result exported"]
    if roles.get("shift_stage", 0) >= 4 and roles.get("accumulator"):
        return "shift_window_accumulator", 0.95, [f"{roles['shift_stage']} shift stages + loop-carried adder/subtractor"]
    if n_add >= 3 and len(regs) <= 2 and not roles.get("accumulator"):
        return "linear_adder_reduction", 0.85 if _is_chain(d, mod) else 0.6, [f"{n_add} adders feeding one register"]
    if roles.get("fsm_state"):
        return "fsm", 0.7, ["narrow register compared against >=2 constants feeding its own next state"]
    if regs and all(("shift_stage" in r.role or "pipeline" in r.role) for r in regs):
        return "pipeline_regs", 0.8, ["pass-through registers only"]
    return "generic_datapath", 0.4, []


def _is_chain(d: Design, mod: str) -> bool:
    """True if the adders of the module form a dependency chain (Y of one feeds A/B of the next)."""
    adders = [c for c in d.comb_cells() if c.module == mod and c.type in ARITH]
    if len(adders) < 2:
        return False
    outs = {b: c for c in adders for b in c.out_bits()}
    chained = 0
    for c in adders:
        if any(b in outs for b in c.in_bits()):
            chained += 1
    return chained >= len(adders) // 2


def adder_chains(d: Design):
    """Find maximal chains of arithmetic cells (output of one feeds the next).

    Returns list of (module, inst_path, [cells...]) for chains of length >= 2.
    """
    arith = [c for c in d.comb_cells() if c.type in ARITH]
    out_owner = {b: c for c in arith for b in c.out_bits()}
    succ = defaultdict(list); pred = defaultdict(list)
    for c in arith:
        for b in c.in_bits():
            if b in out_owner and out_owner[b] is not c:
                succ[out_owner[b].name].append(c); pred[c.name].append(out_owner[b])
    chains = []
    seen = set()
    for c in arith:
        if pred[c.name] or c.name in seen:
            continue
        # walk longest path greedily
        chain = [c]; cur = c
        while succ[cur.name]:
            nxt = max(succ[cur.name], key=lambda x: len(succ[x.name]))
            if nxt.name in seen:
                break
            chain.append(nxt); cur = nxt
        for x in chain:
            seen.add(x.name)
        if len(chain) >= 2:
            chains.append((c.module, c.inst_path, chain))
    return chains
