"""Extract register instances (instance path -> reference cell, output nets) from a
gate-level structural Verilog netlist plus the liberty file(s) that define which
cells are flip-flops / latches.  Produces the same records as the OpenSTA
`registers.tsv` dump, in a few seconds and without running OpenSTA.

Names follow OpenSTA conventions: hierarchy separator '/', escaped identifiers
un-escaped ('\\foo[2] [15]' -> 'foo[2][15]').
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

_CELL_RE = re.compile(r"\bcell\s*\(\s*\"?([^\")\s]+)\"?\s*\)\s*\{")
_PIN_RE = re.compile(r"\b(?:pin|bus|bundle)\s*\(\s*\"?([^\")\s]+)\"?\s*\)\s*\{")
_DIR_RE = re.compile(r"\bdirection\s*:\s*\"?(\w+)\"?")
_SEQ_RE = re.compile(r"\b(ff|ff_bank|latch|latch_bank)\s*\(")


def _block(text: str, start: int) -> int:
    """Index just past the '}' matching the '{' at text[start]."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def parse_liberty_cells(paths: List[str]) -> Dict[str, dict]:
    """cell name -> {'seq': bool, 'outputs': [pin], 'inputs': [pin]}"""
    cells: Dict[str, dict] = {}
    for path in paths:
        text = open(path, errors="replace").read()
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"//[^\n]*", "", text)
        for m in _CELL_RE.finditer(text):
            name = m.group(1)
            body_start = m.end() - 1
            body_end = _block(text, body_start)
            body = text[body_start:body_end]
            seq = bool(_SEQ_RE.search(body))
            outs, ins = [], []
            for pm in _PIN_RE.finditer(body):
                pstart = pm.end() - 1
                pend = _block(body, pstart)
                pbody = body[pstart:pend]
                dm = _DIR_RE.search(pbody)
                d = dm.group(1) if dm else "input"
                for pname in pm.group(1).split(","):
                    (outs if d in ("output", "inout") else ins).append(pname.strip())
            cells[name] = {"seq": seq, "outputs": outs, "inputs": ins}
    return cells


# ---------------------------------------------------------------------------
_MODULE_RE = re.compile(r"\bmodule\s+(\\\S+\s|[A-Za-z_][\w$]*)", re.S)
_INST_RE = re.compile(
    r"(\\\S+\s|[A-Za-z_][\w$]*)\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?(\\\S+\s|[A-Za-z_][\w$]*)\s*\(((?:[^()]|\([^()]*\))*)\)\s*;",
    re.S)
_CONN_RE = re.compile(r"\.\s*(\\\S+\s|[A-Za-z_][\w$]*)\s*\(\s*([^()]*)\)", re.S)
_KEYWORDS = {"module", "endmodule", "input", "output", "inout", "wire", "reg", "assign", "always", "initial",
             "parameter", "localparam", "supply0", "supply1", "tri", "genvar", "generate", "endgenerate",
             "function", "endfunction", "task", "endtask", "specify", "endspecify", "integer", "real", "time"}


def _unescape(name: str) -> str:
    name = name.strip()
    if name.startswith("\\"):
        name = name[1:].rstrip()
    return name


def _norm_net(expr: str) -> Optional[str]:
    e = expr.strip()
    if not e or e.startswith("{") or "'" in e:
        return None
    m = re.match(r"^(\\\S+\s*|[A-Za-z_][\w$]*)\s*(\[\s*(\d+)\s*\])?$", e)
    if not m:
        return None
    base = _unescape(m.group(1))
    base = re.sub(r"\s+\[", "[", base)
    if m.group(3) is not None:
        return f"{base}[{m.group(3)}]"
    return base


def parse_netlist_modules(paths: List[str]) -> Dict[str, List[Tuple[str, str, Dict[str, str]]]]:
    """module name -> list of (ref type, instance name, {pin: net expr})"""
    mods: Dict[str, List[Tuple[str, str, Dict[str, str]]]] = {}
    for path in paths:
        text = open(path, errors="replace").read()
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"\(\*.*?\*\)", "", text, flags=re.S)       # attributes
        pos = 0
        while True:
            m = _MODULE_RE.search(text, pos)
            if not m:
                break
            name = _unescape(m.group(1))
            end = text.find("endmodule", m.end())
            if end < 0:
                end = len(text)
            body = text[m.end():end]
            # skip the module header port list
            hdr_end = body.find(";")
            body = body[hdr_end + 1:] if hdr_end >= 0 else body
            insts = []
            for im in _INST_RE.finditer(body):
                ref = _unescape(im.group(1))
                if ref in _KEYWORDS:
                    continue
                inst = _unescape(im.group(2))
                conns = {}
                for cm in _CONN_RE.finditer(im.group(3)):
                    conns[_unescape(cm.group(1))] = cm.group(2)
                insts.append((ref, inst, conns))
            mods[name] = insts
            pos = end + len("endmodule")
    return mods


def extract_registers(netlists: List[str], liberties: List[str], top: Optional[str] = None,
                      max_depth: int = 64) -> Tuple[Dict[str, dict], dict]:
    """Return (registers, info).  registers: instance path -> {ref, q_nets{pin: net}, clocks[]}"""
    lib = parse_liberty_cells(liberties)
    mods = parse_netlist_modules(netlists)
    if top is None:
        instantiated = {ref for insts in mods.values() for ref, _, _ in insts}
        cands = [m for m in mods if m not in instantiated]
        top = cands[0] if len(cands) == 1 else (top or next(iter(mods)))
    regs: Dict[str, dict] = {}
    unknown: Set[str] = set()
    stack = [(top, "", 0)]
    while stack:
        mod, prefix, depth = stack.pop()
        for ref, inst, conns in mods.get(mod, []):
            path = f"{prefix}{inst}"
            if ref in mods:
                if depth < max_depth:
                    stack.append((ref, path + "/", depth + 1))
                continue
            c = lib.get(ref)
            if c is None:
                unknown.add(ref)
                continue
            if not c["seq"]:
                continue
            q = {}
            for pin in c["outputs"]:
                if pin in conns:
                    net = _norm_net(conns[pin])
                    if net:
                        q[pin] = f"{prefix}{net}"
            regs[path] = {"ref": ref, "q_nets": q, "clocks": []}
    info = {"top": top, "modules": len(mods), "library_cells": len(lib), "unknown_cell_types": sorted(unknown)[:20],
            "registers": len(regs)}
    return regs, info
