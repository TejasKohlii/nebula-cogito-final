"""Parsers for OpenSTA outputs.

Supported inputs
  * the directory produced by `rtl_graph sta-query` (endpoints.tsv, registers.tsv, clocks.tsv,
    critical.json, units.txt)
  * `report_checks -format json`
  * `report_checks -format full | full_clock | full_clock_expanded` text (any -fields)
  * `report_checks -format end` text
All times are normalised to nanoseconds.
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .clocks import ClockDef


@dataclass
class PathPoint:
    pin: str
    arrival: Optional[float] = None
    net: Optional[str] = None
    cell: Optional[str] = None
    instance: Optional[str] = None
    slew: Optional[float] = None          # ns
    cap: Optional[float] = None           # pF


@dataclass
class PathRecord:
    endpoint: str                       # pin name (inst/D, port[3], ...)
    startpoint: str = ""
    launch_clock: str = ""
    capture_clock: str = ""
    path_group: str = ""
    slack: Optional[float] = None
    arrival: Optional[float] = None     # absolute data arrival at the endpoint
    required: Optional[float] = None
    launch_time: Optional[float] = None # launch clock edge time (+ latency)
    q_arrival: Optional[float] = None   # arrival at the first data point (Q pin / input port)
    check: str = ""                     # setup | recovery | removal | output | max_delay | ...
    points: List[PathPoint] = field(default_factory=list)
    source: str = ""

    @property
    def comb_delay(self) -> Optional[float]:
        if self.arrival is None or self.q_arrival is None:
            return None
        return self.arrival - self.q_arrival

    @property
    def data_delay(self) -> Optional[float]:
        if self.arrival is None or self.launch_time is None:
            return None
        return self.arrival - self.launch_time

    @property
    def endpoint_instance(self) -> str:
        return self.endpoint.rsplit("/", 1)[0] if "/" in self.endpoint else self.endpoint

    @property
    def startpoint_instance(self) -> str:
        return self.startpoint.rsplit("/", 1)[0] if "/" in self.startpoint else self.startpoint


@dataclass
class StaData:
    paths: List[PathRecord] = field(default_factory=list)          # per-endpoint worst paths
    critical: List[PathRecord] = field(default_factory=list)       # detailed worst paths per group
    detailed: List[PathRecord] = field(default_factory=list)       # all point-level paths (critical + violating)
    registers: Dict[str, dict] = field(default_factory=dict)       # instance -> {ref, q_nets{pin:net}, clocks[]}
    clocks: Dict[str, ClockDef] = field(default_factory=dict)
    time_unit_ns: float = 1.0
    sources: List[str] = field(default_factory=list)

    def all_paths(self) -> List[PathRecord]:
        return self.paths + self.critical + self.detailed

    def point_level_paths(self) -> List[PathRecord]:
        """One point-level path per endpoint (worst slack), from critical + violating dumps."""
        best: Dict[str, PathRecord] = {}
        for p in self.critical + self.detailed:
            if not p.points:
                continue
            q = best.get(p.endpoint)
            if q is None or (p.slack is not None and (q.slack is None or p.slack < q.slack)):
                best[p.endpoint] = p
        return list(best.values())


# ---------------------------------------------------------------------------
def load_query_dir(d: str) -> StaData:
    data = StaData()
    data.sources.append(d)
    u = os.path.join(d, "units.txt")
    if os.path.exists(u):
        m = re.search(r"time_unit\s+([0-9.eE+-]+)\s*(\S*)", open(u).read())
        if m:
            scale = float(m.group(1))            # e.g. 1e-9 for ns
            data.time_unit_ns = scale / 1e-9
    c = os.path.join(d, "clocks.tsv")
    if os.path.exists(c):
        for row in csv.DictReader(open(c), delimiter="\t"):
            per = row.get("period")
            try:
                per_f = float(per) * data.time_unit_ns if per not in (None, "", "0", "0.000000") else None
            except ValueError:
                per_f = None
            data.clocks[row["name"]] = ClockDef(
                name=row["name"], period=per_f,
                is_generated=row.get("generated", "0") in ("1", "true", "True"),
                sources=[s for s in row.get("sources", "").split(",") if s])
    r = os.path.join(d, "registers.tsv")
    if os.path.exists(r):
        for row in csv.DictReader(open(r), delimiter="\t"):
            qn = {}
            for item in row.get("q_nets", "").split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    qn[k] = v
            data.registers[row["instance"]] = {
                "ref": row.get("ref", ""), "q_nets": qn,
                "clocks": [x for x in row.get("clocks", "").split(",") if x]}
    e = os.path.join(d, "endpoints.tsv")
    if os.path.exists(e):
        s = data.time_unit_ns
        for row in csv.DictReader(open(e), delimiter="\t"):
            def f(k):
                try:
                    return float(row[k]) * s
                except (KeyError, ValueError, TypeError):
                    return None
            p = PathRecord(endpoint=row["endpoint"], startpoint=row.get("startpoint", ""),
                           launch_clock=row.get("launch_clock", ""), capture_clock=row.get("capture_clock", ""),
                           slack=f("slack"), arrival=f("arrival"), required=f("required"),
                           q_arrival=f("q_arrival"), source="endpoints.tsv")
            p.check = _check_from_pin(p.endpoint)
            data.paths.append(p)
    j = os.path.join(d, "critical.json")
    if os.path.exists(j):
        data.critical = parse_json_report(j)
    v = os.path.join(d, "violating.json")
    if os.path.exists(v):
        data.detailed = parse_json_report(v)
    all_paths = os.path.join(d, "all_paths.json")
    if os.path.exists(all_paths):
        data.detailed.extend(parse_json_report(all_paths))
    # infer generated-clock masters from names if OpenSTA did not tell us (it does not)
    return data


def _check_from_pin(pin: str) -> str:
    leaf = pin.rsplit("/", 1)[-1].upper()
    if "/" not in pin:
        return "output"
    if leaf in ("RN", "SN", "R", "S", "RST", "RESET", "SET", "CLR", "CLEAR", "PRE", "PRESET", "ARST"):
        return "recovery"
    return "setup"


# ---------------------------------------------------------------------------
def parse_json_report(path: str) -> List[PathRecord]:
    with open(path) as f:
        text = f.read()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        # OpenSTA may write several concatenated documents; take the first
        doc = json.loads(text[: text.rfind("}") + 1])
    out = []
    for c in doc.get("checks", []):
        if c.get("type") != "check":
            continue
        p = PathRecord(endpoint=c.get("endpoint", ""), startpoint=c.get("startpoint", ""),
                       launch_clock=c.get("source_clock", ""), capture_clock=c.get("target_clock", ""),
                       path_group=c.get("path_group", ""), source=os.path.basename(path))
        s = 1e9  # json arrivals are in seconds
        p.slack = _f(c.get("slack"), s)
        p.arrival = _f(c.get("data_arrival_time"), s)
        p.required = _f(c.get("required_time"), s)
        scp = c.get("source_clock_path") or []
        if scp:
            # last point = clock pin of the launching register.  With ideal clocks OpenSTA still
            # prints cell arrivals through clock gates there (seen: 18 ns on an ICG stub), which the
            # data path ignores; fall back to the clock source edge time when inconsistent.
            last = _f(scp[-1].get("arrival"), s)
            first = _f(scp[0].get("arrival"), s)
            p.launch_time = last
            q0 = None
            sp = c.get("source_path") or []
            if sp:
                q0 = _f(sp[0].get("arrival"), s)
            if last is not None and q0 is not None and last > q0 + 1e-9:
                p.launch_time = first
        for pt in c.get("source_path", []):
            p.points.append(PathPoint(pin=pt.get("pin", ""), arrival=_f(pt.get("arrival"), s),
                                      net=pt.get("net"), cell=pt.get("cell"), instance=pt.get("instance"),
                                      slew=_f(pt.get("slew"), s), cap=_f(pt.get("capacitance"), 1e12)))
        if p.points:
            p.q_arrival = p.points[0].arrival
        p.check = _check_from_pin(p.endpoint)
        if p.path_group in ("asynchronous",) and p.check == "setup":
            p.check = "recovery"
        out.append(p)
    return out


def _f(v, scale=1.0) -> Optional[float]:
    try:
        return float(v) * scale
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_START = re.compile(r"^Startpoint:\s*(\S+)(?:\s+\((.*)\))?")
_END = re.compile(r"^Endpoint:\s*(\S+)(?:\s+\((.*)\))?")
_CLKBY = re.compile(r"clock(?:ed by)?\s+(\S+?)\)?$")


def parse_text_report(path: str, time_unit_ns: float = 1.0) -> List[PathRecord]:
    """Parse `report_checks` full/full_clock/full_clock_expanded text output."""
    paths: List[PathRecord] = []
    cur: Optional[PathRecord] = None
    section = None          # 'data' | 'required'
    pending_desc = ""
    last_point: Optional[PathPoint] = None
    columns: List[str] = []      # numeric columns of the point table, e.g. ["Fanout","Cap","Slew","Delay","Time"]
    with open(path, errors="replace") as f:
        lines = f.read().splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        m = _START.match(line)
        if m:
            cur = PathRecord(endpoint="", startpoint=m.group(1), source=os.path.basename(path))
            desc = m.group(2) or ""
            # continuation line "(rising edge-triggered flip-flop clocked by X)"
            if not desc and i < len(lines) and lines[i].strip().startswith("("):
                desc = lines[i].strip().strip("()")
                i += 1
            mm = _CLKBY.search(desc)
            if mm:
                cur.launch_clock = mm.group(1)
            section = "data"
            last_point = None
            paths.append(cur)
            continue
        if cur is None:
            continue
        m = _END.match(line)
        if m:
            cur.endpoint = m.group(1)
            desc = m.group(2) or ""
            if not desc and i < len(lines) and lines[i].strip().startswith("("):
                desc = lines[i].strip().strip("()")
                i += 1
            mm = _CLKBY.search(desc)
            if mm:
                cur.capture_clock = mm.group(1).rstrip("'")
            if "recovery" in desc:
                cur.check = "recovery"
            elif "removal" in desc:
                cur.check = "removal"
            elif "output port" in desc:
                cur.check = "output"
            elif "max_delay" in desc or "path delay" in desc:
                cur.check = "max_delay"
            else:
                cur.check = "setup"
            continue
        if line.startswith("Path Group:"):
            cur.path_group = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Path Type:") or line.startswith("---"):
            continue
        if line.startswith("Fanout") or line.startswith("Delay") or line.startswith("Cap ") or line.startswith("Slew"):
            columns = [t for t in line.split() if t in ("Fanout", "Cap", "Slew", "Delay", "Time")]
            continue
        if line.endswith("data arrival time"):
            nums = _nums(line)
            if nums and cur.arrival is None:
                cur.arrival = nums[-1] * time_unit_ns
            section = "required"
            continue
        if line.endswith("data required time"):
            nums = _nums(line)
            if nums and cur.required is None:
                cur.required = nums[-1] * time_unit_ns
            continue
        if "slack" in line and ("(MET)" in line or "(VIOLATED)" in line):
            nums = _nums(line)
            if nums:
                cur.slack = nums[-1] * time_unit_ns
            cur = None
            continue
        if line.endswith("(net)"):
            if last_point is not None:
                last_point.net = line[: -len("(net)")].strip()
            continue
        # clock edge lines: "clock X (rise edge)"  -> Time column is the last number
        if re.search(r"clock \S+ \((rise|fall) edge\)", line):
            nums = _nums(line)
            if nums and section == "data" and cur.launch_time is None:
                cur.launch_time = nums[-1] * time_unit_ns
            continue
        # pin lines: ... TIME ^|v PIN (CELL)
        m = re.match(r"^(.*?)\s([\^v])\s+(\S+)(?:\s+\((\S+)\))?\s*$", line)
        if m and section == "data":
            nums = _nums(m.group(1))
            pin, cell = m.group(3), m.group(4)
            arr = nums[-1] * time_unit_ns if nums else None
            pt = PathPoint(pin=pin, arrival=arr, cell=cell,
                           instance=pin.rsplit("/", 1)[0] if "/" in pin else None)
            # optional Fanout/Cap/Slew columns are right-aligned before Delay/Time: if the line has
            # as many numbers as columns, pick them by position
            if columns and len(nums) == len(columns):
                if "Slew" in columns:
                    pt.slew = nums[columns.index("Slew")] * time_unit_ns
                if "Cap" in columns:
                    pt.cap = nums[columns.index("Cap")]
            # skip the start register's own clock pin
            leaf = pin.rsplit("/", 1)[-1].upper() if "/" in pin else ""
            if not cur.points and cur.startpoint and pt.instance == cur.startpoint and leaf in (
                    "CK", "CLK", "C", "CP", "G", "E", "EN", "CKN", "CLKB"):
                continue
            cur.points.append(pt)
            last_point = pt
            if cur.q_arrival is None and arr is not None:
                cur.q_arrival = arr
            continue
    return [p for p in paths if p.endpoint]


def _nums(s: str) -> List[float]:
    out = []
    for t in s.split():
        if _NUM.match(t):
            out.append(float(t))
    return out


def parse_end_report(path: str, time_unit_ns: float = 1.0) -> List[PathRecord]:
    """Parse `report_checks -format end` output (endpoint / required / actual / slack)."""
    out = []
    group = ""
    for line in open(path, errors="replace"):
        line = line.rstrip()
        m = re.match(r"^\S+\s+group\s+(\S+)", line)
        if m:
            group = m.group(1)
            continue
        m = re.match(r"^(\S+)\s+(?:\((\S+)\)\s+)?(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)", line)
        if m:
            p = PathRecord(endpoint=m.group(1), path_group=group, source=os.path.basename(path))
            p.required = float(m.group(3)) * time_unit_ns
            p.arrival = float(m.group(4)) * time_unit_ns
            p.slack = float(m.group(5)) * time_unit_ns
            p.check = _check_from_pin(p.endpoint)
            out.append(p)
    return out


def load_report(path: str, time_unit_ns: float = 1.0) -> StaData:
    """Auto-detect a report file / query directory."""
    data = StaData()
    data.sources.append(path)
    if os.path.isdir(path):
        return load_query_dir(path)
    head = open(path, errors="replace").read(4000)
    if head.lstrip().startswith("{"):
        recs = parse_json_report(path)
        data.paths = recs
        data.critical = recs
        data.detailed = recs
    elif "Startpoint:" in head or "Startpoint:" in open(path, errors="replace").read():
        recs = parse_text_report(path, time_unit_ns)
        data.paths = recs
        data.critical = recs
        data.detailed = recs
    else:
        data.paths = parse_end_report(path, time_unit_ns)
    return data


def merge(datas: List[StaData]) -> StaData:
    out = StaData()
    for d in datas:
        out.paths.extend(d.paths)
        out.critical.extend(d.critical)
        out.detailed.extend(d.detailed)
        out.registers.update(d.registers)
        out.clocks.update(d.clocks)
        out.sources.extend(d.sources)
        out.time_unit_ns = d.time_unit_ns
    return out
