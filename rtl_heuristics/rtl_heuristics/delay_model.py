"""Technology-agnostic delay model in *gate levels* (NAND2-equivalent stages).

Two mapping styles are modelled because the same RTL adder can be mapped as a
ripple chain (area-oriented ABC scripts) or as a parallel-prefix structure
(timing-driven mapping).  Levels are converted to ns with `ns_per_level`
(default 0.045 ns ~ a loaded NAND2 in Nangate45 typical; calibrate with
--ns-per-level or automatically from an OpenSTA report).
"""
from __future__ import annotations
import math
from dataclasses import dataclass

ARITH = {"$add", "$sub", "$alu", "$neg", "$macc"}
CMP_MAG = {"$lt", "$le", "$gt", "$ge"}
CMP_EQ = {"$eq", "$ne", "$eqx", "$nex"}
BITWISE = {"$and", "$or", "$xor", "$xnor", "$not", "$buf", "$pos", "$bweqx", "$bwmux"}
REDUCE = {"$reduce_and", "$reduce_or", "$reduce_xor", "$reduce_xnor", "$reduce_bool",
          "$logic_not", "$logic_and", "$logic_or"}
SHIFT = {"$shl", "$shr", "$sshl", "$sshr", "$shift", "$shiftx"}
MULDIV = {"$mul", "$div", "$mod", "$divfloor", "$modfloor", "$pow"}


def clog2(n):
    return max(0, math.ceil(math.log2(n))) if n > 1 else 0


@dataclass
class DelayModel:
    ns_per_level: float = 0.045
    clk_to_q_ns: float = 0.10
    setup_ns: float = 0.035
    recovery_ns: float = 0.24
    uncertainty_ns: float = 0.04
    style: str = "ripple"          # 'ripple' (pessimistic, area-mapped) or 'prefix'
    fanout_free: int = 4           # loads that cost nothing extra
    fanout_branch: int = 4         # buffer-tree branching factor
    unbuffered_ns_per_load: float = 0.0095   # measured on Nangate45 X1 drivers

    # ---- fanout ----------------------------------------------------------
    def fanout_penalty_ns(self, fanout: int) -> float:
        """Delay added by a buffered fanout tree (what a good synthesis run gives)."""
        if fanout <= self.fanout_free:
            return 0.0
        levels = math.ceil(math.log(fanout / self.fanout_free, self.fanout_branch))
        return levels * self.ns_per_level

    def fanout_unbuffered_ns(self, fanout: int) -> float:
        """Delay if the net is left unbuffered (what happens with area-only mapping)."""
        if fanout <= self.fanout_free:
            return 0.0
        return (fanout - self.fanout_free) * self.unbuffered_ns_per_load

    # ---- per-cell arithmetic bit model -----------------------------------
    def adder_bit_cost(self, width: int):
        """(per-bit ripple increment, constant) for a `width`-bit adder."""
        if self.style == "prefix":
            return 0.0, 2 * clog2(max(width, 2)) + 2
        return 1.0, 2

    def cell_levels(self, ctype: str, width: int, n_inputs: int = 2, sel_width: int = 1) -> float:
        """Flat (non bit-level) level estimate for a whole cell, worst output bit."""
        w = max(width, 1)
        if ctype in ARITH:
            inc, c = self.adder_bit_cost(w)
            return inc * (w - 1) + c
        if ctype in CMP_EQ:
            return clog2(w) + 1
        if ctype in CMP_MAG:
            return (w + 1) if self.style == "ripple" else (2 * clog2(w) + 2)
        if ctype in BITWISE:
            return 1
        if ctype in REDUCE:
            return max(1, clog2(w))
        if ctype == "$mux":
            return 1
        if ctype == "$pmux":
            return clog2(max(sel_width, 2)) + 1
        if ctype in SHIFT:
            return max(1, clog2(w))
        if ctype == "$mul":
            return (2 * w) if self.style == "ripple" else (clog2(w) * 2 + 2 * clog2(2 * w))
        if ctype in MULDIV:
            return w * clog2(w) * 2
        if ctype in {"$lut", "$sop"}:
            return 2
        if ctype in {"$tribuf", "$concat", "$slice"}:
            return 0
        return 1

    def ns(self, levels: float) -> float:
        return levels * self.ns_per_level
