"""Extract clocks / IO delays / exceptions from an SDC.

Strategy 1 (preferred): evaluate the SDC with `tclsh` using stub procs, so Tcl
loops, `format`, `expr` and arrays all work.  Strategy 2: regex fallback for
literal commands.  Unknown commands are ignored, `source` of missing files is
tolerated.
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, tempfile
from dataclasses import dataclass, field

TCL_STUBS = r'''
set ::__out {}
proc __emit {kind args} { lappend ::__out [list $kind {*}$args] }
proc __flat {l} { set r {}; foreach x $l { if {[llength $x] > 1} { lappend r {*}$x } else { lappend r $x } }; return $r }
proc get_ports {args} { return [__flat [lindex $args end]] }
proc get_pins {args} { return [__flat [lindex $args end]] }
proc get_cells {args} { return [__flat [lindex $args end]] }
proc get_nets {args} { return [__flat [lindex $args end]] }
proc get_clocks {args} { return [__flat [lindex $args end]] }
proc all_inputs {args} { return {*all_inputs*} }
proc all_outputs {args} { return {*all_outputs*} }
proc all_clocks {args} { return {*all_clocks*} }
proc all_registers {args} { return {*all_registers*} }
proc current_design {args} { return {} }
set ::__flags {max min rise fall add_delay add allow_paths asynchronous physically_exclusive logically_exclusive combinational setup hold start end network_latency_included source_latency_included ignore_clock_latency invert level_sensitive early late clock_fall reference_pin dont_touch through_fall through_rise rise_from fall_from rise_to fall_to positive negative}
proc __opts {arglist} {
  set o [dict create]; set pos {}
  for {set i 0} {$i < [llength $arglist]} {incr i} {
    set a [lindex $arglist $i]
    if {[string index $a 0] eq "-" && ![string is double -strict $a]} {
      set key [string range $a 1 end]
      set nxt [lindex $arglist [expr {$i+1}]]
      if {[lsearch -exact $::__flags $key] >= 0} { dict set o $key 1; continue }
      if {$i+1 < [llength $arglist] && !([string index $nxt 0] eq "-" && ![string is double -strict $nxt])} {
        dict set o $key $nxt; incr i
      } else { dict set o $key 1 }
    } else { lappend pos $a }
  }
  return [list $o $pos]
}
foreach cmd {create_clock create_generated_clock set_clock_groups set_input_delay set_output_delay set_false_path set_multicycle_path set_max_delay set_min_delay set_clock_uncertainty set_clock_transition set_clock_latency set_input_transition set_load set_driving_cell set_propagated_clock set_ideal_network set_case_analysis set_disable_timing set_max_transition set_max_fanout set_max_capacitance set_units set_wire_load_mode set_operating_conditions group_path set_clock_gating_check set_data_check set_max_area set_dont_touch} {
  proc $cmd {args} [format {
    lassign [__opts $args] o pos
    __emit %s [dict get [dict create opts $o pos $pos] opts] [dict get [dict create opts $o pos $pos] pos]
    return {}
  } $cmd]
}
proc __find {f} {
  if {[file exists $f]} { return $f }
  foreach base $::__search {
    set c [file join $base $f]
    if {[file exists $c]} { return $c }
    set c [file join $base [file tail $f]]
    if {[file exists $c]} { return $c }
  }
  return {}
}
proc source {args} {
  set f [lindex $args end]
  set r [__find $f]
  if {$r eq {}} { __emit missing_source $f; return {} }
  if {[catch {uplevel 1 [list __orig_source $r]} err]} { __emit tcl_error $err }
  return {}
}
'''

TCL_TAIL = r'''
set fh [open [lindex $argv 1] w]
foreach e $::__out { puts $fh [string map {"\n" " "} $e] }
close $fh
'''


@dataclass
class Clock:
    name: str
    period: float | None = None
    source: str = ""          # port or pin name
    generated: bool = False
    master: str = ""          # master source port/clock name
    divide_by: float | None = None
    multiply_by: float | None = None
    edges: list = field(default_factory=list)
    targets: list = field(default_factory=list)   # pins/ports it is attached to


@dataclass
class SDC:
    clocks: dict = field(default_factory=dict)       # name -> Clock
    clock_groups: list = field(default_factory=list) # list of list of clock names (async groups)
    input_delays: list = field(default_factory=list) # (clock, max/min, value, ports)
    output_delays: list = field(default_factory=list)
    false_paths: list = field(default_factory=list)
    multicycle: list = field(default_factory=list)
    max_delays: list = field(default_factory=list)
    uncertainty: dict = field(default_factory=dict)
    raw: list = field(default_factory=list)
    parser: str = ""
    warnings: list = field(default_factory=list)

    def clock_by_port(self, port: str):
        for c in self.clocks.values():
            if not c.generated and (c.source == port or port in c.targets):
                return c
        return None

    def generated_for(self, master_port: str, ratio: float):
        best = None
        for c in self.clocks.values():
            if c.generated and c.master == master_port:
                if c.divide_by and abs(c.divide_by - ratio) < 1e-6:
                    return c
                if c.edges and len(c.edges) >= 3:
                    per = (c.edges[2] - c.edges[0]) / 2.0
                    if abs(per - ratio) < 1e-6:
                        return c
                best = best or c
        return best


def _parse_tcl_list(s: str):
    """Very small Tcl list splitter for {a b} c {d e} forms (one level)."""
    out, buf, depth = [], "", 0
    for ch in s:
        if ch == "{":
            depth += 1
            if depth == 1:
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append(buf); buf = ""; continue
        if depth == 0 and ch.isspace():
            if buf:
                out.append(buf); buf = ""
            continue
        buf += ch
    if buf:
        out.append(buf)
    return out


def _dict_from_tcl(s: str):
    items = _parse_tcl_list(s)
    return {items[i]: items[i + 1] for i in range(0, len(items) - 1, 2)}


def parse_sdc(path: str, cwd: str | None = None) -> SDC:
    sdc = SDC()
    if path is None:
        sdc.parser = "none"
        return sdc
    tclsh = shutil.which("tclsh")
    if tclsh:
        try:
            sdc = _parse_with_tclsh(path, tclsh, cwd or os.path.dirname(os.path.abspath(path)))
            if sdc.clocks:
                return sdc
            sdc.warnings.append("tclsh evaluation produced no clocks; falling back to regex parser")
        except Exception as e:  # fall back
            sdc.warnings.append(f"tclsh evaluation failed: {e}")
    return _parse_regex(path, sdc)


def _parse_with_tclsh(path, tclsh, cwd):
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "eval.tcl"); outp = os.path.join(td, "out.txt")
        sdc_dir = os.path.dirname(os.path.abspath(path))
        search = [cwd, sdc_dir, os.path.dirname(sdc_dir), os.path.dirname(os.path.dirname(sdc_dir))]
        body = ("rename source __orig_source\n" +
                "set ::__search [list " + " ".join("{%s}" % x for x in search) + "]\n" + TCL_STUBS +
                "if {[catch {__orig_source [lindex $argv 0]} err]} { __emit tcl_error $err }\n" + TCL_TAIL)
        with open(script, "w") as fh:
            fh.write(body)
        res = subprocess.run([tclsh, script, os.path.abspath(path), outp], cwd=cwd,
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip()[-500:])
        with open(outp) as fh:
            lines = fh.read().splitlines()
    sdc = SDC(parser="tclsh")
    for ln in lines:
        items = _parse_tcl_list(ln)
        if not items:
            continue
        cmd = items[0]
        opts = _dict_from_tcl(items[1]) if len(items) > 1 else {}
        pos = _parse_tcl_list(items[2]) if len(items) > 2 else []
        sdc.raw.append((cmd, opts, pos))
        _apply(sdc, cmd, opts, pos)
    return sdc


def _unbrace(x):
    return x.strip().strip("{}").strip() if isinstance(x, str) else x


def _apply(sdc: SDC, cmd, opts, pos):
    pos = [_unbrace(p) for p in (pos or [])]
    opts = {k: _unbrace(v) for k, v in opts.items()}
    targets = pos[:]
    if cmd == "create_clock":
        name = opts.get("name") or (targets[0] if targets else "clk")
        c = Clock(name=name, period=float(opts.get("period", 0) or 0) or None,
                  source=targets[0] if targets else "", targets=targets)
        sdc.clocks[name] = c
    elif cmd == "create_generated_clock":
        name = opts.get("name") or (targets[0] if targets else "gclk")
        c = Clock(name=name, generated=True, master=opts.get("source", ""), targets=targets,
                  source=targets[0] if targets else "")
        if "divide_by" in opts:
            c.divide_by = float(opts["divide_by"])
        if "multiply_by" in opts:
            c.multiply_by = float(opts["multiply_by"])
        if "edges" in opts:
            c.edges = [float(x) for x in _parse_tcl_list(opts["edges"])]
        sdc.clocks[name] = c
    elif cmd == "set_clock_groups":
        grp = []
        for k, v in opts.items():
            if k == "group":
                grp.append(_parse_tcl_list(v))
        sdc.clock_groups.append(grp)
    elif cmd in ("set_input_delay", "set_output_delay"):
        nums = [p for p in pos if re.fullmatch(r"-?[\d.]+(e-?\d+)?", p)]
        val = float(nums[0]) if nums else None
        ports = [p for p in pos if p not in nums]
        rec = (opts.get("clock"), "max" if "max" in opts else ("min" if "min" in opts else "both"), val, ports)
        (sdc.input_delays if cmd == "set_input_delay" else sdc.output_delays).append(rec)
    elif cmd == "set_false_path":
        sdc.false_paths.append(opts)
    elif cmd == "set_multicycle_path":
        sdc.multicycle.append((pos[0] if pos else None, opts))
    elif cmd in ("set_max_delay", "set_min_delay"):
        sdc.max_delays.append((cmd, pos[0] if pos else None, opts))
    elif cmd == "set_clock_uncertainty":
        nums = [p for p in pos if re.fullmatch(r"-?[\d.]+(e-?\d+)?", p)]
        val = float(nums[0]) if nums else None
        for grp in pos:
            if grp in nums:
                continue
            for cn in grp.split():
                sdc.uncertainty[cn] = val
    elif cmd == "missing_source":
        sdc.warnings.append(f"sourced file not found: {pos[0] if pos else opts}")
    elif cmd == "tcl_error":
        sdc.warnings.append(f"Tcl error while evaluating SDC (constraints after this point were skipped): {' '.join(pos)[:200]}")
    # derive generated clock periods
    for c in sdc.clocks.values():
        if c.generated and c.period is None:
            m = None
            for mc in sdc.clocks.values():
                if not mc.generated and (mc.source == c.master or mc.name == c.master or c.master in mc.targets):
                    m = mc
            if m and m.period:
                if c.divide_by:
                    c.period = m.period * c.divide_by
                elif c.multiply_by:
                    c.period = m.period / c.multiply_by
                elif len(c.edges) >= 3:
                    c.period = m.period * (c.edges[2] - c.edges[0]) / 2.0


def _parse_regex(path, sdc: SDC):
    sdc.parser = "regex"
    with open(path) as fh:
        txt = fh.read()
    for m in re.finditer(r"create_clock\s+(.*)", txt):
        line = m.group(1)
        name = re.search(r"-name\s+(\S+)", line)
        per = re.search(r"-period\s+([\d.]+)", line)
        port = re.search(r"get_ports\s+\{?([^\]\}]+)\}?", line)
        if per:
            n = name.group(1) if name else (port.group(1).strip() if port else "clk")
            sdc.clocks[n] = Clock(n, float(per.group(1)), port.group(1).strip() if port else "")
    for m in re.finditer(r"create_generated_clock\s+(.*)", txt):
        line = m.group(1)
        name = re.search(r"-name\s+(\S+)", line)
        src = re.search(r"-source\s+\[?get_\w+\s+\{?([^\]\}\s]+)", line)
        div = re.search(r"-divide_by\s+(\d+)", line)
        n = name.group(1) if name else "gclk"
        c = Clock(n, generated=True, master=src.group(1) if src else "", divide_by=float(div.group(1)) if div else None)
        sdc.clocks[n] = c
    _apply(sdc, "noop", {}, [])
    if not sdc.clocks:
        sdc.warnings.append("regex SDC parser found no literal create_clock; use --clock NAME=PERIOD[:PORT]")
    return sdc


def parse_sta_clock_table(path: str) -> dict:
    """Parse the 'Clock  Period  Waveform' table OpenSTA prints (report_clock_properties)."""
    out = {}
    with open(path, errors="ignore") as fh:
        lines = fh.readlines()
    for ln in lines:
        m = re.match(r"\s*(\S+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(\(generated\))?\s*$", ln)
        if m:
            out[m.group(1)] = {"period": float(m.group(2)), "rise": float(m.group(3)),
                               "fall": float(m.group(4)), "generated": bool(m.group(5))}
    return out
