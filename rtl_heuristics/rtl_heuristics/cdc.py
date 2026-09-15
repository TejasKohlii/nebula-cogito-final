"""Clock-domain-crossing and reset-domain-crossing structural checks."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from .netgraph import Design, Register
from .clocks import trace_to_sources


@dataclass
class Crossing:
    src_reg: str
    src_clock: str
    dst_reg: str
    dst_clock: str
    width: int
    kind: str                 # 'data' | 'reset'
    through_logic: bool
    sync_depth: int           # number of back-to-back 1-bit flops at the destination (0 = none)
    classification: str = ""  # 'synchronizer' | 'handshake_payload' | 'unsynchronized' | 'bus_unsynchronized' | 'reset_domain_crossing'
    notes: list = field(default_factory=list)
    module: str = ""
    inst_path: str = ""


def _sync_depth(d: Design, reg: Register) -> int:
    """Length of the chain reg -> next reg -> ... where every stage is a 1-bit direct copy on the same clock."""
    depth = 1
    cur = reg
    for _ in range(6):
        nxt = None
        for b in cur.q_bits:
            for cell, port, idx in d.loads.get(b, []):
                if cell.is_seq and port == "D" and cell.name in d.reg_of_cell:
                    r2 = d.reg_of_cell[cell.name]
                    if r2 and r2.width == 1 and r2.clock == cur.clock and all(x in cur.q_bits for x in r2.d_bits):
                        nxt = r2
        if nxt is None or cur.width != 1:
            break
        depth += 1; cur = nxt
    return depth if reg.width == 1 else 0


def find_crossings(d: Design, timing) -> list[Crossing]:
    crossings = []
    for reg in d.regs.values():
        # data crossings: any source register of a different family in the D/EN cone
        src_by_fam = defaultdict(set)
        through_logic = False
        for b in reg.d_bits + list(reg.en_bits) + list(reg.srst_bits):
            if not isinstance(b, int):
                continue
            drv = d.driver_cell(b)
            if drv is not None and not drv.is_seq:
                for clk in timing.src_clocks.get(drv.name, set()):
                    src_by_fam[clk].add(b)
                through_logic = True
            elif b in d.reg_of_qbit:
                src_by_fam[d.reg_of_qbit[b].clock].add(b)
            elif drv is None and b in timing.launch and timing.launch[b]:
                src_by_fam[timing.launch[b]].add(b)
        for clk, bits in src_by_fam.items():
            if not clk or clk == reg.clock:
                continue
            if clk.startswith("port:") and clk not in timing.domains:
                continue   # un-clocked input port (no set_input_delay): not a clock domain
            fam_src = timing.family_of(clk); fam_dst = timing.family_of(reg.clock)
            if fam_src and fam_src == fam_dst:
                continue   # related clocks: timed normally
            # which source register?
            src_names = set()
            for b in [x for x in reg.d_bits if isinstance(x, int)][:8]:
                for kind, obj, sb in trace_to_sources(d, b):
                    if kind == "reg" and sb in d.reg_of_qbit and d.reg_of_qbit[sb].clock == clk:
                        src_names.add(d.reg_of_qbit[sb].name)
                    elif kind == "port":
                        src_names.add(f"port:{d.port_bit_name.get(sb, d.bit_name(sb))}")
            depth = _sync_depth(d, reg)
            vector_sync = "shift_vector" in reg.role and len(bits) == 1 and reg.d_bits and reg.d_bits[0] in bits
            eff_width = 1 if vector_sync else reg.width
            if vector_sync:
                depth = reg.width
            cr = Crossing(",".join(sorted(src_names)) or clk, clk, reg.name, reg.clock, eff_width, "data",
                          through_logic, depth, module=reg.module, inst_path=reg.inst_path)
            if eff_width == 1 and depth >= 2 and not through_logic:
                cr.classification = "synchronizer"
                if "async_reg" not in str(reg.cell.params) and not reg.cell.params.get("async_reg"):
                    cr.notes.append("no async_reg/ASYNC_REG attribute visible after elaboration (Yosys drops attributes on generated cells); verify placement constraint")
            elif eff_width == 1 and depth >= 2 and through_logic:
                cr.classification = "logic_before_synchronizer"
                cr.notes.append("combinational logic between the source register and the first synchroniser flop: glitches can be captured")
            elif reg.en_bits and eff_width > 1:
                cr.classification = "handshake_payload"
                cr.notes.append("multi-bit bus captured under an enable: safe only if the enable is a synchronised handshake and the source holds the data (bundled-data). Needs set_max_delay on the payload.")
            elif eff_width > 1:
                cr.classification = "bus_unsynchronized"
                cr.notes.append("multi-bit bus crosses clock families with no synchronised enable: data coherency is not guaranteed")
            else:
                cr.classification = "unsynchronized"
            crossings.append(cr)
        # reset crossings: async reset pin driven from another family's register
        if isinstance(reg.arst_bit, int):
            srcs = trace_to_sources(d, reg.arst_bit)
            for kind, obj, sb in srcs:
                if kind == "reg" and sb in d.reg_of_qbit:
                    rs = d.reg_of_qbit[sb]
                    if timing.family_of(rs.clock) != timing.family_of(reg.clock) or rs.clock != reg.clock:
                        cr = Crossing(rs.name, rs.clock, reg.name, reg.clock, 1, "reset", False, 0,
                                      classification="reset_domain_crossing", module=reg.module, inst_path=reg.inst_path)
                        same_fam = timing.family_of(rs.clock) == timing.family_of(reg.clock)
                        cr.notes.append("reset released by a synchroniser in a related clock (recovery timed at the fast period)" if same_fam
                                        else "reset released by a synchroniser of an asynchronous clock family: recovery check across domains")
                        crossings.append(cr)
    return crossings


def reconvergence(d: Design, crossings: list[Crossing]):
    """Synchronised single bits from the same source domain that meet in one combinational cone."""
    syncs = [c for c in crossings if c.classification == "synchronizer"]
    by_dst = defaultdict(list)
    for c in syncs:
        by_dst[(c.src_clock, c.dst_clock, c.inst_path)].append(c)
    issues = []
    for key, lst in by_dst.items():
        if len(lst) < 2:
            continue
        # do the sync outputs converge on the same downstream cell?
        end_regs = {}
        for c in lst:
            reg = d.regs[c.dst_reg]
            for b in reg.q_bits:
                for cell, port, idx in d.loads.get(b, []):
                    end_regs.setdefault(cell.name, set()).add(c.dst_reg)
        conv = [(cell, regs) for cell, regs in end_regs.items() if len(regs) >= 2]
        if conv:
            issues.append((key, [r for _, regs in conv for r in regs]))
    return issues
