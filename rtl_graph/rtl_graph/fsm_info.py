"""Parse the output of Yosys `fsm_info` (after fsm_detect + fsm_extract) into state tables."""
from __future__ import annotations

import re
from typing import Dict, List, Optional


def parse_fsm_info(path: str) -> List[dict]:
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return []
    out: List[dict] = []
    blocks = re.split(r"\n\s*Information on FSM ", text)
    for blk in blocks[1:]:
        m = re.match(r"(\S+)\s+\(([^)]*)\)", blk)
        if not m:
            continue
        cell, wire = m.group(1), m.group(2).strip().lstrip("\\")
        rec = {"state_wire": wire, "fsm_cell": cell, "state_bits": None, "inputs": [], "outputs": [],
               "states": [], "reset_state": None, "transitions": []}
        mb = re.search(r"Number of state bits:\s+(\d+)", blk)
        if mb:
            rec["state_bits"] = int(mb.group(1))
        sec = None
        for line in blk.splitlines():
            t = line.strip()
            if t.startswith("Input signals:"):
                sec = "in"; continue
            if t.startswith("Output signals:"):
                sec = "out"; continue
            if t.startswith("State encoding:"):
                sec = "enc"; continue
            if t.startswith("Transition Table"):
                sec = "tr"; continue
            if not t or t.startswith("---") or t.startswith("Number of"):
                if t.startswith("---"):
                    sec = None
                continue
            if sec in ("in", "out"):
                mm = re.match(r"\d+:\s+(.*)$", t)
                if mm:
                    rec["inputs" if sec == "in" else "outputs"].append(mm.group(1).strip().lstrip("\\"))
            elif sec == "enc":
                mm = re.match(r"(\d+):\s+(\S+)(.*)$", t)
                if mm:
                    rec["states"].append({"index": int(mm.group(1)), "encoding": mm.group(2)})
                    if "RESET" in mm.group(3):
                        rec["reset_state"] = int(mm.group(1))
            elif sec == "tr":
                mm = re.match(r"\d+:\s+(\d+)\s+(\S+)\s+->\s+(\d+)\s+(\S+)", t)
                if mm:
                    rec["transitions"].append({"from": int(mm.group(1)), "ctrl_in": mm.group(2),
                                               "to": int(mm.group(3)), "ctrl_out": mm.group(4)})
        out.append(rec)
    return out
