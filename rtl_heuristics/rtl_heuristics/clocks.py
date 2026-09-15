"""Clock-domain inference from netlist structure, then reconciliation with the SDC."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from .netgraph import Design, Register, param_int
from .sdc_parse import SDC


@dataclass
class ClockDomain:
    id: str                 # internal id: 'port:clk[1]' / 'gen:domain[1].divider.count[0]' / 'logic:<net>'
    root_port: str          # master port bit name (family key)
    family: str
    kind: str               # 'master' | 'generated' | 'logic' | 'unknown'
    ratio: float | None = None   # period multiplier vs master (generated)
    source_reg: str = ""
    sdc_name: str = ""
    period: float | None = None
    period_source: str = ""      # 'sdc' | 'derived' | 'default' | 'unknown'
    registers: int = 0
    reg_bits: int = 0
    notes: list = field(default_factory=list)


def _const_value(bits):
    if all(isinstance(b, str) for b in bits):
        try:
            return int("".join(reversed([b for b in bits])), 2)
        except ValueError:
            return None
    return None


def divider_ratio(design: Design, reg: Register, bit_index: int):
    """Ratio of a clock taken from bit `bit_index` of register `reg`.

    counter (D = Q + 1)  -> 2**(bit+1);  toggle (D = ~Q) -> 2; otherwise None.
    """
    if not reg.d_bits:
        return None
    d0 = reg.d_bits[0]
    drv = design.driver_cell(d0) if isinstance(d0, int) else None
    if drv is None:
        return None
    if drv.type in ("$add", "$sub", "$alu"):
        a, b = drv.conns.get("A", []), drv.conns.get("B", [])
        qset = set(reg.q_bits)
        if all((x in qset) for x in a if isinstance(x, int)) and _const_value(b) == 1:
            return 2 ** (bit_index + 1)
        if all((x in qset) for x in b if isinstance(x, int)) and _const_value(a) == 1:
            return 2 ** (bit_index + 1)
    if drv.type in ("$not", "$logic_not") and reg.width == 1:
        a = drv.conns.get("A", [])
        if a and a[0] in reg.q_bits:
            return 2
    if drv.type == "$mux":
        # toggle with enable: D = en ? ~Q : Q  -> unknown ratio (programmable)
        return None
    return None


def trace_to_sources(design: Design, bit, limit=200):
    """Walk back through combinational cells to register Q bits / ports."""
    seen, stack, out = set(), [bit], []
    while stack and len(seen) < limit:
        b = stack.pop()
        if b in seen or not isinstance(b, int):
            continue
        seen.add(b)
        d = design.driver.get(b)
        if d is None:
            continue
        if d[0] == "port":
            out.append(("port", d[1], b)); continue
        cell = d[1]
        if cell.is_seq:
            out.append(("reg", cell, b)); continue
        stack.extend(cell.in_bits())
    return out


def infer_clocks(design: Design, sdc: SDC, default_period: float | None, overrides: dict):
    """Assign design.regs[*].clock / .family and return {id: ClockDomain}."""
    domains: dict[str, ClockDomain] = {}
    memo: dict = {}

    def domain_of_bit(b, depth=0):
        if b in memo:
            return memo[b]
        if not isinstance(b, int) or depth > 8:
            dom = ClockDomain(id=f"const:{b}", root_port="", family="", kind="unknown")
        else:
            d = design.driver.get(b)
            if d is None:
                dom = ClockDomain(id=f"undriven:{design.bit_name(b)}", root_port="", family="", kind="unknown")
            elif d[0] == "port":
                name = design.port_bit_name.get(b, design.bit_name(b))
                dom = ClockDomain(id=f"port:{name}", root_port=name, family=name, kind="master", ratio=1.0)
            else:
                cell = d[1]
                if cell.is_seq and b in design.reg_of_qbit:
                    reg = design.reg_of_qbit[b]
                    idx = reg.q_bits.index(b)
                    parent = domain_of_bit(reg.clk_bit, depth + 1)
                    r = divider_ratio(design, reg, idx)
                    ratio = (parent.ratio * r) if (r and parent.ratio) else None
                    dom = ClockDomain(id=f"gen:{design.bit_name(b)}", root_port=parent.root_port,
                                      family=parent.family, kind="generated", ratio=ratio,
                                      source_reg=reg.name)
                    if r is None:
                        dom.notes.append("divider ratio not recognised structurally (not a +1 counter / toggle flop)")
                    if reg.cell.type in ("$dff", "$dffe") and reg.arst_bit is None:
                        dom.notes.append("divider register has no reset")
                else:
                    srcs = trace_to_sources(design, b)
                    fam = ""
                    for kind, obj, sb in srcs:
                        pd = domain_of_bit(sb if kind == "port" else sb, depth + 1)
                        if pd.family:
                            fam = pd.family; break
                    dom = ClockDomain(id=f"logic:{design.bit_name(b)}", root_port=fam, family=fam,
                                      kind="logic", ratio=None)
                    dom.notes.append(f"clock passes through combinational cell {cell.type} ({cell.short_src})")
        memo[b] = domains.setdefault(dom.id, dom)
        return memo[b]

    for reg in design.regs.values():
        if reg.clk_bit is None:
            reg.clock, reg.family = "latch", ""
            continue
        dom = domain_of_bit(reg.clk_bit)
        reg.clock, reg.family = dom.id, dom.family
        dom.registers += 1
        dom.reg_bits += reg.width

    # --- periods: overrides > SDC > derived > default
    for dom in domains.values():
        ov = overrides.get(dom.id) or overrides.get(dom.root_port) if dom.kind == "master" else overrides.get(dom.id)
        sdc_clk = None
        if dom.kind == "master":
            sdc_clk = sdc.clock_by_port(dom.root_port)
        elif dom.kind == "generated" and dom.ratio:
            sdc_clk = sdc.generated_for(dom.root_port, dom.ratio)
        elif dom.kind == "generated":
            sdc_clk = sdc.generated_for(dom.root_port, -1)
        master = sdc.clock_by_port(dom.root_port) if dom.root_port else None
        if ov is not None:
            dom.period, dom.period_source = float(ov), "override"
            dom.sdc_name = sdc_clk.name if sdc_clk else ""
        elif sdc_clk and sdc_clk.period:
            dom.period, dom.period_source, dom.sdc_name = sdc_clk.period, "sdc", sdc_clk.name
        elif master and master.period and dom.ratio:
            dom.period, dom.period_source = master.period * dom.ratio, "derived"
            dom.notes.append("no matching create_generated_clock in SDC; period derived from divider ratio")
        elif default_period:
            dom.period, dom.period_source = default_period, "default"
        else:
            dom.period_source = "unknown"
    return domains
