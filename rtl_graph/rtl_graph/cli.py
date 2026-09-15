"""Command-line interface.

  rtl_graph elaborate  ...   RTL  -> design.json (Yosys)
  rtl_graph sta-query  ...   netlist+lib+sdc -> OpenSTA query directory
  rtl_graph build      ...   design.json [+ sdc] [+ sta data] -> graph.{json,dot,graphml,svg}
  rtl_graph run        ...   all of the above in one go
  rtl_graph render     ...   graph.dot -> svg/png/pdf
  rtl_graph serve      ...   publish build status + graph files over HTTP (web UI)
  rtl_graph signal     ...   write a status signal by hand (UI testing)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import yosys_elab, sta_query, sta_report, export, netlist_verilog, notify
from .api import Graph
from .netlist import Design
from .abstract import AbstractGraph
from .clocks import ClockAnalysis, parse_sdc_text
from .annotate import Mapper, Annotator, port_clock_hints, node_clock_overrides


def _log(msg: str):
    print(f"[rtl_graph {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
def cmd_elaborate(a) -> str:
    _log(f"elaborating top={a.top} with {a.yosys} ({a.frontend})")
    out = yosys_elab.elaborate(a.out, a.top, sources=a.source, filelist=a.filelist, rtlil=a.rtlil,
                               yosys=a.yosys, frontend=a.frontend, defines=a.define, incdirs=a.incdir,
                               chparams=a.chparam, frontend_flags=a.frontend_flag, extra_cmds=a.yosys_cmd,
                               fsm_detect=not a.no_fsm_detect, source_cwd=getattr(a, 'source_cwd', None))
    _log(f"wrote {out}")
    return out


def cmd_sta_query(a) -> str:
    _log(f"running OpenSTA ({a.sta}) on {a.netlist} top={a.top}")
    out = sta_query.run_query(a.out, a.liberty, a.netlist, a.top, a.sdc, sta=a.sta, cwd=a.cwd,
                              extra_tcl=a.sta_cmd, critical_per_group=max(a.critical_per_group, 10),
                              only_violating=False, dump_registers=getattr(a, "tcl_registers", True))
    _log(f"STA data in {out}")
    return out


def build(design_json: str, out_dir: str, sdc: Optional[str] = None, sta_inputs: Optional[List[str]] = None,
          top: Optional[str] = None, slack_checks: str = "all", critical_per_group: int = 1,
          critical_overall: bool = False, fsm_mode: str = "both", sync: Optional[List[str]] = None,
          labeled_only: bool = False, focus: Optional[str] = None, hops: int = 1, max_nodes: Optional[int] = None,
          hide_ports: bool = False, render: bool = True, layout: str = "auto", include_cells: bool = False,
          title: str = "", collapse_arrays: bool = True, cdc_async_pins: bool = False,
          netlists: Optional[List[str]] = None, liberties: Optional[List[str]] = None,
          notify_url: Optional[str] = None) -> dict:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    notify.begin(out_dir, design=top)
    try:
        return _build(design_json, out_dir, sdc, sta_inputs, top, slack_checks, critical_per_group,
                      critical_overall, fsm_mode, sync, labeled_only, focus, hops, max_nodes, hide_ports,
                      render, layout, include_cells, title, collapse_arrays, cdc_async_pins, netlists,
                      liberties, notify_url)
    except BaseException as exc:                      # the UI must learn about a failed build too
        notify.fail(out_dir, f"{type(exc).__name__}: {exc}", design=top, notify_url=notify_url)
        raise


def _build(design_json: str, out_dir: str, sdc, sta_inputs, top, slack_checks, critical_per_group,
           critical_overall, fsm_mode, sync, labeled_only, focus, hops, max_nodes, hide_ports,
           render, layout, include_cells, title, collapse_arrays, cdc_async_pins, netlists,
           liberties, notify_url) -> dict:
    t0 = time.time()
    _log(f"loading {design_json}")
    d = Design.load(design_json, top)
    _log(f"  {len(d.cells)} cells, {len(d.ports)} ports, {len(d.netnames)} named wires ({time.time()-t0:.1f}s)")
    sdc_info = parse_sdc_text(sdc) if sdc else None
    sta = None
    if sta_inputs:
        sta = sta_report.merge([sta_report.load_report(p) for p in sta_inputs])
        _log(f"  STA: {len(sta.paths)} endpoint paths, {len(sta.critical)} detailed paths, "
             f"{len(sta.registers)} register instances, {len(sta.clocks)} clocks")
    if netlists and liberties:
        regs, info = netlist_verilog.extract_registers(netlists, liberties)
        _log(f"  netlist: {info['registers']} register instances from {info['modules']} modules"
             + (f", unknown cell types: {info['unknown_cell_types']}" if info['unknown_cell_types'] else ""))
        if sta is None:
            sta = sta_report.StaData()
        for inst, rec in regs.items():
            old = sta.registers.get(inst)
            if old and old.get("clocks"):
                rec["clocks"] = old["clocks"]
            sta.registers[inst] = rec
    sync_groups = [s.split(",") for s in (sync or [])]
    fsm_info = design_json + ".fsm_info.txt"

    def analyse(split_regs):
        g = AbstractGraph(d, collapse_arrays=collapse_arrays, split_regs=split_regs).build()
        _log(f"  abstract graph: {g.stats} ({time.time()-t0:.1f}s)")
        mapper = Mapper(g, sta) if sta is not None else None
        hints = port_clock_hints(sta, g) if sta is not None else {}
        name_to_node = {}
        if mapper is not None:
            objs = [s for c in list(sta.clocks.values()) + list((sdc_info.clocks if sdc_info else {}).values())
                    for s in c.sources]
            name_to_node = mapper.name_to_node_map(objs)
        ca = ClockAnalysis(g, sdc_info, sta.clocks if sta else None, name_to_node, hints, cdc_async_pins=cdc_async_pins)
        if mapper is not None:
            ca.node_clock_override = node_clock_overrides(mapper)
        ca.run(sync_groups).apply()
        _log(f"  clocks: roots={ca.roots} ({time.time()-t0:.1f}s)")
        ann = Annotator(g, sta, mapper, slack_checks=slack_checks, critical_per_group=critical_per_group,
                        critical_overall=critical_overall, fsm_mode=fsm_mode, clock_analysis=ca,
                        fsm_info_path=fsm_info if os.path.exists(fsm_info) else None)
        ann.label_fsm()
        ann.apply_timing()
        ann.build_crossings()
        ann.build_fsms()
        ann.build_gui_fields()
        return g, mapper, ca, ann

    g, mapper, ca, ann = analyse(None)
    if ann.split_plan:
        # registers on critical paths with some violating bits: rebuild with those bits expanded
        _log(f"  expanding {len(ann.split_plan)} register(s) on critical paths: "
             + ", ".join(f"{k} -> {len(v)} parts" for k, v in list(ann.split_plan.items())[:6])
             + (" ..." if len(ann.split_plan) > 6 else ""))
        plan = ann.split_plan
        g, mapper, ca, ann = analyse(plan)
        ann.meta["register_splits"] = {k: [list(r) for r in v] for k, v in plan.items()}
    meta = dict(ann.meta)
    from .path_groups import compress_paths
    compress_paths(g, meta)
    from .schedule import annotate_schedule
    meta["asap"] = annotate_schedule(g)
    meta.update({"design": d.top, "design_json": os.path.abspath(design_json), "sdc": sdc,
                 "sta_inputs": sta_inputs or [], "clocks": ca.summary(), "slack_checks": slack_checks,
                 "generated_by": "rtl_graph"})
    if mapper is not None:
        meta["unmapped_register_instances"] = len(mapper.unmapped_instances)
    _log(f"  labels: " + ", ".join(f"{k}={v}" for k, v in sorted(g.stats.items()) if k.endswith(("nodes", "regs", "sinks"))))
    # ---- outputs
    doc = export.to_json(g, meta, include_cells=include_cells)
    with open(os.path.join(out_dir, "graph.json"), "w") as f:
        json.dump(doc, f, indent=1)
    with open(os.path.join(out_dir, "graph.slim.json"), "w") as f:
        json.dump(Graph(doc).slim(), f)
    ids = export.select_nodes(g, labeled_only=labeled_only, focus=focus, hops=hops, max_nodes=max_nodes,
                              hide_ports=hide_ports)
    dot = export.write_dot(g, os.path.join(out_dir, "graph.dot"), ids, title=title or d.top)
    export.write_graphml(g, os.path.join(out_dir, "graph.graphml"), ids)
    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write(export.summary_text(g, meta))
    _log(f"  wrote graph.json / graph.slim.json / graph.dot ({len(ids)} nodes shown of {len(g.nodes)}) / graph.graphml / summary.txt")
    if render:
        svg = export.render(dot, os.path.join(out_dir, "graph.svg"), layout=layout, n_nodes=len(ids))
        _log(f"  rendered {svg}" if svg else "  (svg not rendered: graphviz missing or layout timed out)")
    # the graph is complete on disk: signal the web UI (status.json + optional webhook)
    st = notify.finish(out_dir, design=d.top, notify_url=notify_url,
                       stats={"nodes": len(g.nodes), "edges": len(g.edges), "nodes_drawn": len(ids),
                              "critical_paths": len(meta.get("critical_paths", [])),
                              "crossings": len(meta.get("crossings", [])), "clock_roots": ca.roots})
    _log(f"  signalled ready: generation {st['generation']} -> {notify.status_path(out_dir)}"
         + (f" + POST {notify_url}" if notify_url else ""))
    _log(f"done in {time.time()-t0:.1f}s -> {out_dir}")
    return doc


def cmd_build(a):
    build(a.design_json, a.out, sdc=a.sdc, sta_inputs=a.sta_data, top=a.top, slack_checks=a.slack_checks,
          critical_per_group=a.critical_per_group, critical_overall=a.critical_overall, fsm_mode=a.fsm_mode,
          sync=a.sync, labeled_only=a.labeled_only, focus=a.focus, hops=a.hops, max_nodes=a.max_nodes,
          hide_ports=a.hide_ports, render=not a.no_render, layout=a.layout, include_cells=a.include_cells,
          title=a.title, collapse_arrays=not a.no_collapse_arrays, cdc_async_pins=a.cdc_async_pins,
          netlists=a.netlist, liberties=a.liberty, notify_url=a.notify_url)


def cmd_run(a):
    Path(a.out).mkdir(parents=True, exist_ok=True)
    a.out_json = os.path.join(a.out, "design.json")
    class E: pass
    e = E(); e.__dict__.update(vars(a)); e.out = a.out_json
    design_json = cmd_elaborate(e)
    sta_inputs = list(a.sta_data or [])
    if a.netlist and not a.skip_sta:
        if shutil.which(a.sta) or os.path.exists(a.sta):
            q = E(); q.__dict__.update(vars(a)); q.out = os.path.join(a.out, "sta")
            q.tcl_registers = a.tcl_registers
            sta_inputs.append(cmd_sta_query(q))
        else:
            _log(f"OpenSTA executable '{a.sta}' not found: skipping the timing query "
                 f"(netlist is still used for name mapping)")
    build(design_json, a.out, sdc=a.sdc, sta_inputs=sta_inputs, top=a.top, slack_checks=a.slack_checks,
          critical_per_group=a.critical_per_group, critical_overall=a.critical_overall, fsm_mode=a.fsm_mode,
          sync=a.sync, labeled_only=a.labeled_only, focus=a.focus, hops=a.hops, max_nodes=a.max_nodes,
          hide_ports=a.hide_ports, render=not a.no_render, layout=a.layout, include_cells=a.include_cells,
          title=a.title, collapse_arrays=not a.no_collapse_arrays, cdc_async_pins=a.cdc_async_pins,
          netlists=a.netlist, liberties=a.liberty, notify_url=a.notify_url)


def cmd_serve(a):
    httpd = notify.serve(a.dir, host=a.host, port=a.port, cors=a.cors, quiet=not a.verbose)
    _log(f"serving {os.path.abspath(a.dir)} on http://{a.host}:{a.port}  "
         f"(/api/status, /api/events, /api/graph)")
    st = notify.read_status(a.dir)
    _log(f"  current state: {st['state']} generation {st['generation']}" if st else
         "  current state: never built (build into this directory to signal ready)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("stopped")


def cmd_signal(a):
    """Write/POST a status signal by hand (test the UI without running a build)."""
    fn = {"ready": notify.finish, "building": notify.begin, "error": notify.fail}[a.state]
    doc = fn(a.dir, a.error, notify_url=a.notify_url) if a.state == "error" else (
        fn(a.dir, notify_url=a.notify_url) if a.state == "ready" else fn(a.dir))
    print(json.dumps(doc, indent=1))


def cmd_render(a):
    n = sum(1 for l in open(a.dot) if "[label=" in l)
    out = export.render(a.dot, a.out, layout=a.layout, n_nodes=n)
    print(out or "render failed")


# ---------------------------------------------------------------------------
def _add_elab_args(p):
    p.add_argument("--source-cwd", help="RTL working directory for relative memory initialization files")
    p.add_argument("--top", required=True)
    p.add_argument("--source", "-s", action="append", default=[], help="RTL file (repeatable)")
    p.add_argument("--filelist", "-f", help="file list (one path per line, relative to the list's directory)")
    p.add_argument("--rtlil", help="use an existing Yosys RTLIL dump instead of reading RTL")
    p.add_argument("--yosys", default=os.environ.get("YOSYS", "yosys"))
    p.add_argument("--frontend", choices=["verilog", "slang"], default="verilog",
                   help="verilog: read_verilog -sv ; slang: read_slang (yosys -m slang)")
    p.add_argument("--frontend-flag", action="append", default=None, help="extra read_slang flag (repeatable)")
    p.add_argument("--define", "-D", action="append", default=[])
    p.add_argument("--incdir", "-I", action="append", default=[])
    p.add_argument("--chparam", action="append", default=[], help="NAME=VALUE parameter override on the top module")
    p.add_argument("--yosys-cmd", action="append", default=[], help="extra Yosys command run before write_json")
    p.add_argument("--no-fsm-detect", action="store_true")


def _add_sta_args(p):
    p.add_argument("--netlist", action="append", default=[], help="gate-level Verilog netlist (repeatable)")
    p.add_argument("--liberty", action="append", default=[], help="liberty file (repeatable)")
    p.add_argument("--sta", default=os.environ.get("STA", "sta"), help="OpenSTA executable")
    p.add_argument("--cwd", help="directory OpenSTA runs in (for relative `source` inside the SDC); default: current")
    p.add_argument("--sta-cmd", action="append", default=[], help="extra Tcl after read_sdc (e.g. set_propagated_clock [all_clocks])")
    p.add_argument("--skip-sta", action="store_true", help="run: do not run OpenSTA even if a netlist is given")
    p.add_argument("--tcl-registers", action="store_true",
                   help="also dump registers/clock names from OpenSTA (slow: minutes for 50k flops); default parses the netlist in Python")


def _add_build_args(p):
    p.add_argument("--sdc", help="SDC file (clock names, clock groups, io delays; plain-text subset)")
    p.add_argument("--sta-data", action="append", default=[],
                   help="STA input: sta-query directory, or report_checks text/json/end report (repeatable)")
    p.add_argument("--slack-checks", choices=["all", "data"], default="all",
                   help="register worst slack over all checks (setup+recovery...) or data (setup) checks only")
    p.add_argument("--critical-per-group", type=int, default=1, help="worst paths per path group labelled critical")
    p.add_argument("--critical-overall", action="store_true", help="label only the N worst paths overall")
    p.add_argument("--fsm-mode", choices=["detect", "names", "both", "off"], default="both")
    p.add_argument("--sync", action="append", default=[], help="comma-separated clock roots to treat as synchronous")
    p.add_argument("--labeled-only", action="store_true", help="DOT/GraphML: only labelled nodes and neighbours")
    p.add_argument("--focus", help="DOT/GraphML: regex on node names; keeps matches and --hops neighbours")
    p.add_argument("--hops", type=int, default=1)
    p.add_argument("--max-nodes", type=int, help="DOT/GraphML: cap on nodes drawn (labelled/worst first)")
    p.add_argument("--hide-ports", action="store_true")
    p.add_argument("--no-render", action="store_true")
    p.add_argument("--layout", default="auto", help="graphviz engine: auto|dot|sfdp|neato|fdp")
    p.add_argument("--include-cells", action="store_true", help="graph.json: list Yosys cells per node")
    p.add_argument("--title", default="")
    p.add_argument("--cdc-async-pins", action="store_true",
                   help="also count crossings that land on async reset/set pins as CDC (reset domain crossings)")
    p.add_argument("--no-collapse-arrays", action="store_true",
                   help="keep mem2reg'd array words (name[0], name[1], ...) as separate register nodes")
    p.add_argument("--notify-url", help="POST the status document here when the graph is ready or failed "
                                        "(web UI hook; see README section 4)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtl_graph", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("elaborate", help="RTL -> design.json via Yosys")
    _add_elab_args(p); p.add_argument("--out", required=True, help="output design.json")
    p.set_defaults(fn=cmd_elaborate)

    p = sp.add_parser("sta-query", help="run OpenSTA and dump endpoint/register/clock data")
    _add_sta_args(p)
    p.set_defaults(tcl_registers=True)
    p.add_argument("--top", required=True); p.add_argument("--sdc", required=True)
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--critical-per-group", type=int, default=10, help="point-level paths kept per path group")
    p.set_defaults(fn=cmd_sta_query)

    p = sp.add_parser("build", help="design.json (+sdc, +sta data) -> graph")
    p.add_argument("design_json"); p.add_argument("--top"); p.add_argument("--out", required=True)
    _add_build_args(p)
    p.add_argument("--netlist", action="append", default=[], help="gate-level netlist: maps STA instance names to RTL registers")
    p.add_argument("--liberty", action="append", default=[], help="liberty file(s) for --netlist")
    p.set_defaults(fn=cmd_build)

    p = sp.add_parser("run", help="elaborate + sta-query + build")
    _add_elab_args(p); _add_sta_args(p); _add_build_args(p)
    p.add_argument("--out", required=True, help="output directory")
    p.set_defaults(fn=cmd_run)

    p = sp.add_parser("render", help="graph.dot -> image")
    p.add_argument("dot"); p.add_argument("--out", required=True); p.add_argument("--layout", default="auto")
    p.set_defaults(fn=cmd_render)

    p = sp.add_parser("serve", help="publish build status + graph files over HTTP (for the web UI)")
    p.add_argument("dir", help="a build output directory, or a parent holding several (?design=<name>)")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cors", default="*", help="Access-Control-Allow-Origin value ('' to disable)")
    p.add_argument("--verbose", action="store_true", help="log every request")
    p.set_defaults(fn=cmd_serve)

    p = sp.add_parser("signal", help="write a status signal by hand (test the UI without a build)")
    p.add_argument("dir"); p.add_argument("--state", choices=["ready", "building", "error"], default="ready")
    p.add_argument("--error", default="manual test"); p.add_argument("--notify-url")
    p.set_defaults(fn=cmd_signal)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
