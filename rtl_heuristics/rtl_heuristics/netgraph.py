"""Design model built from flattened + hierarchical Yosys JSON.

Everything is bit-level: a *bit* is an integer net id from Yosys JSON (or a
constant string '0'/'1'/'x'). Cells are word-level Yosys RTLIL cells.
"""
from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass, field

SEQ_TYPES = {"$dff", "$dffe", "$adff", "$adffe", "$sdff", "$sdffe", "$sdffce",
             "$dffsr", "$dffsre", "$aldff", "$aldffe", "$ff", "$dlatch",
             "$adlatch", "$dlatchsr", "$sr"}
LATCH_TYPES = {"$dlatch", "$adlatch", "$dlatchsr", "$sr"}
MEM_TYPES = {"$mem", "$mem_v2", "$memrd", "$memrd_v2", "$memwr", "$memwr_v2", "$meminit", "$meminit_v2"}
IGNORE_TYPES = {"$scopeinfo", "$assert", "$assume", "$live", "$fair", "$cover",
                "$initstate", "$anyconst", "$anyseq", "$allconst", "$allseq", "$print", "$check"}
CTRL_PORTS = {"CLK", "EN", "ARST", "SRST", "SET", "CLR", "ALOAD", "S"}


def bits_of(x):
    return list(x) if isinstance(x, list) else [x]


def param_int(cell, name, default=0):
    params = cell.params if isinstance(cell, Cell) else cell.get("parameters", {})
    v = params.get(name)
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v)
    if re.fullmatch(r"[01]+", s):
        return int(s, 2)
    try:
        return int(s)
    except ValueError:
        return default


def strip_module_name(name: str) -> str:
    """'$paramod$hash\\window_engine' or '$paramod\\window_lane\\DEPTH=...' -> 'window_lane'."""
    if name.startswith("$paramod"):
        parts = name.split("\\")
        for p in parts[1:]:
            if p and "=" not in p:
                return p
        return parts[-1]
    return name


@dataclass
class Cell:
    name: str
    type: str
    params: dict
    conns: dict            # port -> list of bits
    dirs: dict             # port -> 'input'/'output'
    src: str = ""
    inst_path: str = ""    # hierarchical instance path this cell belongs to ("" = top)
    module: str = ""       # module type name of that instance
    width: int = 0
    is_seq: bool = False

    def inputs(self):
        return [(p, b) for p, b in self.conns.items() if self.dirs.get(p) != "output"]

    def outputs(self):
        return [(p, b) for p, b in self.conns.items() if self.dirs.get(p) == "output"]

    def out_bits(self):
        r = []
        for _, b in self.outputs():
            r += [x for x in b if isinstance(x, int)]
        return r

    def in_bits(self):
        r = []
        for _, b in self.inputs():
            r += [x for x in b if isinstance(x, int)]
        return r

    @property
    def short_src(self):
        return short_src(self.src)


def short_src(src: str) -> str:
    if not src:
        return ""
    first = src.split("|")[0]
    m = re.match(r"(.*?):(\d+)\.\d+-(\d+)\.\d+", first)
    if m:
        f, a, b = m.group(1), int(m.group(2)), int(m.group(3))
        f = f.split("/")[-1]
        return f"{f}:{a}" if a == b else f"{f}:{a}-{b}"
    return first.split("/")[-1]


def src_line(src: str):
    m = re.match(r".*?:(\d+)\.", (src or "").split("|")[0])
    return int(m.group(1)) if m else None


@dataclass
class Register:
    cell: Cell
    name: str              # RTL base name (flattened hierarchical)
    width: int
    q_bits: list
    d_bits: list
    clk_bit: object
    clk_pol: int = 1
    en_bits: list = field(default_factory=list)
    arst_bit: object = None
    srst_bits: list = field(default_factory=list)
    clock: str = ""        # domain id
    family: str = ""
    role: str = ""         # pattern role
    attrs: dict = field(default_factory=dict)

    @property
    def module(self):
        return self.cell.module

    @property
    def inst_path(self):
        return self.cell.inst_path

    @property
    def local_name(self):
        return self.name[len(self.inst_path) + 1:] if self.inst_path and self.name.startswith(self.inst_path + ".") else self.name


class Design:
    def __init__(self, flat: dict, hier: dict, top: str):
        self.top = top
        self.flat = flat["modules"][top]
        self.hier_mods = hier["modules"]
        self.cells: dict[str, Cell] = {}
        self.ports: dict[str, dict] = {}
        self.driver: dict[int, tuple] = {}       # bit -> (kind, obj, port, idx)  kind in {'cell','port'}
        self.loads: dict[int, list] = defaultdict(list)  # bit -> [(cell, port, idx)]
        self.bit_names: dict[int, str] = {}
        self.bit_all_names: dict[int, list] = {}
        self.port_bit_name: dict[int, str] = {}
        self.regs: dict[str, Register] = {}
        self.reg_of_qbit: dict[int, Register] = {}
        self.reg_of_cell: dict[str, Register] = {}
        self.inst_module: dict[str, str] = {}    # instance path -> module type (base name)
        self.inst_module_full: dict[str, str] = {}
        self.module_ports: dict[str, dict] = {}
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self):
        m = self.flat
        # scopeinfo cells: instance path -> module
        for cname, c in m["cells"].items():
            if c["type"] == "$scopeinfo":
                mod = c.get("attributes", {}).get("module", "")
                self.inst_module_full[cname] = mod
                self.inst_module[cname] = strip_module_name(mod)
        for mn, mod in self.hier_mods.items():
            self.module_ports[strip_module_name(mn)] = mod.get("ports", {})
        # public net names -> bit names (prefer non-$ names, shortest)
        cand: dict[int, list] = defaultdict(list)
        for nname, n in m["netnames"].items():
            if n.get("hide_name"):
                continue
            bits = n["bits"]
            for i, b in enumerate(bits):
                if isinstance(b, int):
                    cand[b].append(f"{nname}[{i}]" if len(bits) > 1 else nname)
        top_port_bits = {}
        for pname, p in m["ports"].items():
            for i, b in enumerate(p["bits"]):
                if isinstance(b, int):
                    top_port_bits[b] = f"{pname}[{i}]" if len(p["bits"]) > 1 else pname
        for b, names in cand.items():
            # prefer: top-level port name; else an internal (non-port) wire of the
            # shallowest instance; module-port aliases (clk, div2, ...) come last.
            names.sort(key=lambda s: (s.startswith("$"), s.count("$"), self._is_module_port(s), s.count("."), len(s)))
            self.bit_names[b] = names[0]
            self.bit_all_names[b] = names
        # ports
        for pname, p in m["ports"].items():
            self.ports[pname] = {"dir": p["direction"], "bits": p["bits"]}
            for i, b in enumerate(p["bits"]):
                if isinstance(b, int):
                    self.port_bit_name[b] = f"{pname}[{i}]" if len(p["bits"]) > 1 else pname
            if p["direction"] in ("input", "inout"):
                for i, b in enumerate(p["bits"]):
                    if isinstance(b, int):
                        self.driver[b] = ("port", pname, pname, i)
                        self.bit_names.setdefault(b, f"{pname}[{i}]" if len(p["bits"]) > 1 else pname)
        # cells
        for cname, c in m["cells"].items():
            t = c["type"]
            if t in IGNORE_TYPES:
                continue
            conns = {p: bits_of(b) for p, b in c["connections"].items()}
            dirs = c.get("port_directions", {})
            cell = Cell(cname, t, c.get("parameters", {}), conns, dirs,
                        src=c.get("attributes", {}).get("src", ""))
            cell.is_seq = t in SEQ_TYPES or t in MEM_TYPES
            cell.width = param_int(c, "WIDTH", 0) or param_int(c, "Y_WIDTH", 0) or \
                max((len(b) for p, b in conns.items() if dirs.get(p) == "output"), default=0)
            self.cells[cname] = cell
            for p, bits in conns.items():
                if dirs.get(p) == "output":
                    for i, b in enumerate(bits):
                        if isinstance(b, int):
                            self.driver[b] = ("cell", cell, p, i)
                else:
                    for i, b in enumerate(bits):
                        if isinstance(b, int):
                            self.loads[b].append((cell, p, i))
        # instance path of each cell from its (flattened) name or its output net names
        for cell in self.cells.values():
            cell.inst_path, cell.module = self._locate(cell)
        self._rename_by_driver()
        # registers
        for cell in self.cells.values():
            if cell.type in SEQ_TYPES:
                self._make_register(cell)

    def split_instance(self, name: str):
        """'domain[0].divider.count[0]' -> ('domain[0].divider', 'count[0]', 'clock_divider')."""
        n = re.sub(r"^\$flatten\\", "", name.lstrip("\\")).replace("\\", "")
        best = ""
        for path in self.inst_module:
            if n.startswith(path + ".") and len(path) > len(best):
                best = path
        if best:
            return best, n[len(best) + 1:], self.inst_module[best]
        return "", n, self.top

    def _is_module_port(self, name: str) -> bool:
        path, local, mod = self.split_instance(name)
        base = re.sub(r"\[\d+\]$", "", local)
        return base in self.module_ports.get(mod, {})

    def _rename_by_driver(self):
        """Prefer, for every driven bit, the alias that lives in the driving cell's own instance."""
        for cell in self.cells.values():
            for b in cell.out_bits():
                names = self.bit_all_names.get(b)
                if not names or len(names) == 1:
                    continue
                own = [n for n in names if self.split_instance(n)[0] == cell.inst_path]
                if own:
                    own.sort(key=lambda n: (n.startswith("$"), self._is_module_port(n), len(n)))
                    self.bit_names[b] = own[0]

    def _locate(self, cell: Cell):
        """Longest instance-path prefix among the cell's name / output net names."""
        if cell.name.startswith("$flatten"):
            names = [cell.name]
        else:
            outs = cell.out_bits()
            if outs:
                path, local, mod = self.split_instance(self.bit_names.get(outs[0], ""))
                return path, mod
            names = [cell.name]
        best = ("", self.top)
        for n in names:
            n = n.lstrip("\\")
            # flattened names look like: $flatten\domain[0].cdc.$eq$... or domain[0].cdc.payload
            n = re.sub(r"^\$flatten\\", "", n)
            n = n.replace("\\", "")
            for path, mod in self.inst_module.items():
                if n.startswith(path + ".") and len(path) > len(best[0]):
                    best = (path, mod)
        return best

    def _make_register(self, cell: Cell):
        q = [b for b in cell.conns.get("Q", []) if isinstance(b, int)]
        d = cell.conns.get("D", [])
        score = defaultdict(int)
        for b in q:
            for n in self.bit_all_names.get(b, []):
                if n.startswith("$"):
                    continue
                score[re.sub(r"\[\d+\]$", "", n)] += 1
        if score:
            base = max(score, key=lambda n: (score[n], n.count("."), -len(n)))
        else:
            base = cell.name
        base = re.sub(r"^\$flatten\\", "", base).replace("\\", "")
        reg = Register(cell=cell, name=base, width=len(q), q_bits=q, d_bits=d,
                       clk_bit=(cell.conns.get("CLK") or cell.conns.get("EN") or [None])[0],
                       clk_pol=param_int(cell, "CLK_POLARITY", 1),
                       en_bits=cell.conns.get("EN", []) if cell.type not in LATCH_TYPES else [],
                       arst_bit=(cell.conns.get("ARST") or cell.conns.get("ALOAD") or [None])[0],
                       srst_bits=cell.conns.get("SRST", []),
                       attrs=cell.params)
        if cell.type in LATCH_TYPES:
            reg.clk_bit = None
        # merge multi-cell registers with the same base name (Yosys slices of one RTL reg)
        if base in self.regs:
            other = self.regs[base]
            if other.clk_bit == reg.clk_bit and other.cell.src == cell.src:
                other.q_bits += q; other.d_bits += d; other.width += len(q)
                for b in q:
                    self.reg_of_qbit[b] = other
                self.reg_of_cell[cell.name] = other
                return
            k = 2
            while f"{base}#{k}" in self.regs:
                k += 1
            base = f"{base}#{k}"
            reg.name = base
        self.regs[base] = reg
        self.reg_of_cell[cell.name] = reg
        for b in q:
            self.reg_of_qbit[b] = reg

    # ------------------------------------------------------------ navigation
    def bit_name(self, b):
        if isinstance(b, int):
            return self.bit_names.get(b, f"net{b}")
        return str(b)

    def driver_cell(self, b):
        d = self.driver.get(b)
        return d[1] if d and d[0] == "cell" else None

    def comb_cells(self):
        return [c for c in self.cells.values() if not c.is_seq]

    def seq_cells(self):
        return [c for c in self.cells.values() if c.is_seq]

    def effective_fanout(self, b) -> int:
        """Load count weighted by the width a 1-bit control pin steers."""
        tot = 0
        for cell, port, idx in self.loads.get(b, []):
            if port in CTRL_PORTS and len(cell.conns[port]) == 1:
                tot += max(cell.width, 1)
            else:
                tot += 1
        return tot

    def net_fanout_cells(self, b):
        return len(self.loads.get(b, []))

    def base_name(self, b):
        return re.sub(r"\[\d+\]$", "", self.bit_name(b))

    def local(self, name: str, inst_path: str):
        if inst_path and name.startswith(inst_path + "."):
            return name[len(inst_path) + 1:]
        return name

    def instance_counts(self):
        cnt = defaultdict(int)
        for mod in self.inst_module.values():
            cnt[mod] += 1
        cnt[self.top] = 1
        return dict(cnt)
