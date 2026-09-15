"""Load a Yosys `write_json` dump of a flattened design into a bit-level netlist.

Every wire bit is an integer id (Yosys numbering).  Constants ("0","1","x","z")
are dropped: they have no driver and create no edges.  Buffers ($buf, $pos,
$_BUF_) are collapsed so that their output bit *is* their input bit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Yosys cell classification
# ---------------------------------------------------------------------------
FF_TYPES = {
    "$dff", "$dffe", "$adff", "$adffe", "$sdff", "$sdffe", "$sdffce",
    "$aldff", "$aldffe", "$dffsr", "$dffsre", "$ff", "$anyinit",
}
LATCH_TYPES = {"$dlatch", "$adlatch", "$dlatchsr", "$sr"}
MEM_TYPES = {"$mem", "$mem_v2", "$memrd", "$memrd_v2", "$memwr", "$memwr_v2",
             "$meminit", "$meminit_v2"}
BUF_TYPES = {"$buf", "$pos", "$_BUF_"}
GATE_FF_RE = re.compile(r"^\$_(DFF|DFFE|SDFF|SDFFE|SDFFCE|ADFF|ADFFE|ALDFF|ALDFFE|DFFSR|DFFSRE|FF)(_|$)")
GATE_LATCH_RE = re.compile(r"^\$_(DLATCH|ADLATCH|DLATCHSR|SR)(_|$)")

# pins that carry a clock (edge- or level-sensitive control) per cell type
CLOCK_PINS = {"CLK", "C", "RD_CLK", "WR_CLK"}
LATCH_CLOCK_PINS = {"EN", "E"}
ASYNC_PINS = {"ARST", "ALOAD", "AD", "SET", "CLR", "R", "S"}
CLOCK_NAME_RE = re.compile(r"(clk|clock|^ck$|^c$|_ck$|^cp$)", re.I)


def cell_kind(ctype: str) -> str:
    """'ff' | 'latch' | 'mem' | 'buf' | 'comb' | 'blackbox'"""
    if ctype in FF_TYPES or GATE_FF_RE.match(ctype):
        return "ff"
    if ctype in LATCH_TYPES or GATE_LATCH_RE.match(ctype):
        return "latch"
    if ctype in MEM_TYPES:
        return "mem"
    if ctype in BUF_TYPES:
        return "buf"
    if ctype.startswith("$"):
        return "comb"
    return "blackbox"


@dataclass
class Cell:
    idx: int
    name: str
    type: str
    kind: str
    inputs: Dict[str, List[int]] = field(default_factory=dict)   # data inputs
    outputs: Dict[str, List[int]] = field(default_factory=dict)
    clock_pins: Dict[str, List[int]] = field(default_factory=dict)
    async_pins: Set[str] = field(default_factory=set)
    params: dict = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)
    hide_name: bool = False

    def all_input_bits(self):
        for bits in self.inputs.values():
            for b in bits:
                yield b

    def all_output_bits(self):
        for bits in self.outputs.values():
            for b in bits:
                yield b


@dataclass
class Port:
    name: str
    direction: str            # input | output | inout
    bits: List[int]


class Design:
    """Bit-level view of one flattened Yosys module."""

    def __init__(self, top: str):
        self.top = top
        self.cells: List[Cell] = []
        self.ports: Dict[str, Port] = {}
        # netnames: public name -> bits ; plus reverse index bit -> [(name, idx)]
        self.netnames: Dict[str, List[int]] = {}
        self.net_attrs: Dict[str, dict] = {}
        self.net_hidden: Dict[str, bool] = {}
        self.bit_names: Dict[int, List[Tuple[str, int]]] = {}
        # connectivity
        self.bit_driver: Dict[int, Tuple[int, str]] = {}     # bit -> (cell idx, port); ports use idx=-1
        self.bit_readers: Dict[int, List[Tuple[int, str]]] = {}  # bit -> [(cell idx, port)]; -1 = top port
        self.alias: Dict[int, int] = {}                       # buffer output bit -> input bit

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str, top: Optional[str] = None) -> "Design":
        with open(path) as f:
            data = json.load(f)
        mods = data["modules"]
        if top is None:
            tops = [m for m, v in mods.items() if v.get("attributes", {}).get("top") in (1, "1", "00000000000000000000000000000001")]
            if len(tops) == 1:
                top = tops[0]
            elif len(mods) == 1:
                top = next(iter(mods))
            else:
                # the module that is not instantiated by any other module
                inst = {c["type"] for m in mods.values() for c in m.get("cells", {}).values()}
                cands = [m for m in mods if m not in inst]
                if len(cands) != 1:
                    raise ValueError(f"cannot determine top module; candidates: {cands}")
                top = cands[0]
        d = cls(top)
        d._build(mods[top])
        return d

    @staticmethod
    def _is_bit(b) -> bool:
        return isinstance(b, int)

    def _build(self, mod: dict):
        # ports
        for pname, p in mod.get("ports", {}).items():
            self.ports[pname] = Port(pname, p["direction"], [b if self._is_bit(b) else None for b in p["bits"]])
        # netnames
        for nname, n in mod.get("netnames", {}).items():
            bits = [b if self._is_bit(b) else None for b in n["bits"]]
            self.netnames[nname] = bits
            self.net_attrs[nname] = n.get("attributes", {})
            self.net_hidden[nname] = bool(n.get("hide_name", 0))
            for i, b in enumerate(bits):
                if b is not None:
                    self.bit_names.setdefault(b, []).append((nname, i))
        # cells
        for cname, c in mod.get("cells", {}).items():
            ctype = c["type"]
            kind = cell_kind(ctype)
            cell = Cell(len(self.cells), cname, ctype, kind,
                        params=c.get("parameters", {}), attrs=c.get("attributes", {}),
                        hide_name=bool(c.get("hide_name", 0)))
            dirs = c.get("port_directions", {})
            for pname, bits in c.get("connections", {}).items():
                bl = [b if self._is_bit(b) else None for b in bits]
                pdir = dirs.get(pname)
                if pdir is None:
                    pdir = "output" if (kind == "blackbox" and self._guess_output(pname)) else "input"
                if pdir == "output":
                    cell.outputs[pname] = bl
                else:
                    # classify control pins
                    if kind in ("ff", "mem") and pname in CLOCK_PINS:
                        cell.clock_pins[pname] = bl
                    elif kind == "latch" and pname in LATCH_CLOCK_PINS:
                        cell.clock_pins[pname] = bl
                    elif kind == "blackbox" and CLOCK_NAME_RE.search(pname):
                        cell.clock_pins[pname] = bl
                    else:
                        cell.inputs[pname] = bl
                        if pname in ASYNC_PINS:
                            cell.async_pins.add(pname)
                    if pdir == "inout":
                        cell.outputs[pname] = bl
            self.cells.append(cell)
        # buffer aliasing
        for cell in self.cells:
            if cell.kind == "buf":
                a = cell.inputs.get("A", [])
                y = cell.outputs.get("Y", [])
                for ai, yi in zip(a, y):
                    if ai is not None and yi is not None and ai != yi:
                        self.alias[yi] = ai
        # resolve alias chains
        def canon(b):
            seen = set()
            while b in self.alias and b not in seen:
                seen.add(b)
                b = self.alias[b]
            return b
        for k in list(self.alias):
            self.alias[k] = canon(k)
        self.alias = {k: v for k, v in self.alias.items() if k != v}
        self._apply_alias()
        self._index_connectivity()

    @staticmethod
    def _guess_output(pname: str) -> bool:
        return bool(re.search(r"(^q$|^qn$|^y$|^z$|^zn$|_o$|_out$|^out)", pname, re.I))

    def canon(self, b):
        return self.alias.get(b, b)

    def _apply_alias(self):
        if not self.alias:
            return
        al = self.alias
        def fix(bits):
            return [al.get(b, b) if b is not None else None for b in bits]
        for cell in self.cells:
            if cell.kind == "buf":
                continue
            for d in (cell.inputs, cell.outputs, cell.clock_pins):
                for k in d:
                    d[k] = fix(d[k])
        for p in self.ports.values():
            # an output port driven by a buffer: keep the port's own bit id as the
            # canonical id for the port, but remember what drives it.
            p.bits = fix(p.bits)
        for n in self.netnames:
            self.netnames[n] = fix(self.netnames[n])
        self.bit_names = {}
        for nname, bits in self.netnames.items():
            for i, b in enumerate(bits):
                if b is not None:
                    self.bit_names.setdefault(b, []).append((nname, i))

    def _index_connectivity(self):
        self.bit_driver = {}
        self.bit_readers = {}
        for p in self.ports.values():
            if p.direction in ("input", "inout"):
                for b in p.bits:
                    if b is not None:
                        self.bit_driver.setdefault(b, (-1, p.name))
            if p.direction in ("output", "inout"):
                for b in p.bits:
                    if b is not None:
                        self.bit_readers.setdefault(b, []).append((-1, p.name))
        for cell in self.cells:
            if cell.kind == "buf":
                continue
            for pname, bits in cell.outputs.items():
                for b in bits:
                    if b is not None:
                        self.bit_driver.setdefault(b, (cell.idx, pname))
            for pname, bits in cell.inputs.items():
                for b in bits:
                    if b is not None:
                        self.bit_readers.setdefault(b, []).append((cell.idx, pname))
            for pname, bits in cell.clock_pins.items():
                for b in bits:
                    if b is not None:
                        self.bit_readers.setdefault(b, []).append((cell.idx, pname))

    # ----------------------------------------------------------------- names
    def names_of_bit(self, b: int) -> List[Tuple[str, int]]:
        return self.bit_names.get(self.canon(b), [])

    def lookup_name(self, name: str) -> Optional[List[int]]:
        """Return bits for a flat wire name, accepting `name`, `name[i]`."""
        if name in self.netnames:
            return self.netnames[name]
        m = re.match(r"^(.*)\[(\d+)\]$", name)
        if m and m.group(1) in self.netnames:
            bits = self.netnames[m.group(1)]
            i = int(m.group(2))
            if 0 <= i < len(bits):
                return [bits[i]]
        return None

    def param_int(self, cell: Cell, key: str, default=0) -> int:
        v = cell.params.get(key, default)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s and set(s) <= set("01xz"):
                s = s.replace("x", "0").replace("z", "0")
                return int(s, 2)
            try:
                return int(s)
            except ValueError:
                return default
        return default

    def param_str(self, cell: Cell, key: str, default="") -> str:
        v = cell.params.get(key, default)
        return v if isinstance(v, str) else str(v)
