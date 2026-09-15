"""Write the annotated graph as JSON, DOT, GraphML and (optionally) SVG."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Set

from .abstract import AbstractGraph, Node, Edge

KIND_SHAPE = {"port_in": "invhouse", "port_out": "house", "port_inout": "hexagon",
              "seq": "box", "latch": "box", "memory": "box3d", "blackbox": "component", "comb": "ellipse"}
KIND_FILL = {"port_in": "#e0e0e0", "port_out": "#e0e0e0", "port_inout": "#e0e0e0",
             "seq": "#cfe2f3", "latch": "#d9d2e9", "memory": "#cfe2f3", "blackbox": "#f4cccc", "comb": "#fff2cc"}


def _fmt(v: Optional[float], unit=" ns") -> str:
    return "n/a" if v is None else f"{v:.3f}{unit}"


def node_label(n: Node) -> str:
    lines: List[str] = []
    if n.kind == "comb":
        owners = n.attrs.get("modules") or [{"hier": n.attrs.get("hier"), "module": n.attrs.get("module")}]
        for i, owner in enumerate(owners):
            where = owner.get("hier") or "(top)"
            mod = owner.get("module")
            lines.append((n.id + "  " if i == 0 else "") + where
                         + (f" : {mod}" if mod and mod != where else ""))
        # the node's own delay (through-delay of the worst STA path inside it); slack lives on
        # the register/port nodes
        dl = n.attrs.get("delay") if n.attrs.get("delay_exact") is not False else None
        if dl is not None:
            approx = "" if n.attrs.get("delay_exact", True) else " (merged)"
            if n.attrs.get("negative_delay"):
                approx += " (slew violation upstream)"
            lines.append(f"delay {_fmt(dl)}{approx}")
        elif n.attrs.get("delay_exact") is not False:
            lines.append("delay n/a")
        extra = f"{n.attrs.get('cell_count', 0)} cells -> {n.attrs.get('sink_count', 0)} sink(s)"
        if n.attrs.get("fanout", 0) >= 16:
            extra += f", fanout {n.attrs['fanout']}"
        lines.append(extra)
    elif n.is_port:
        lines.append(n.name)
        lines.append(f"[{n.width}] {n.kind.replace('port_', '')}")
        ws = n.attrs.get("worst_slack")
        if ws is not None:
            lines.append(f"slack {_fmt(ws)}")
    else:
        lines.append(n.name)
        w = f"[{n.width}]"
        if n.kind == "memory" or n.attrs.get("array"):
            w = f"[{n.width}] x {n.attrs.get('depth', '?')} words"
        elif n.attrs.get("split_of"):
            lo, hi = n.attrs["bit_range"]
            w = (f"bit {lo}" if lo == hi else f"bits {lo}:{hi}") + f" of [{n.attrs.get('declared_width', '?')}]"
        elif n.attrs.get("partial_vector"):
            w += f" of {n.attrs.get('declared_width')}"
        lines.append(w + ("" if n.kind == "seq" else f" ({n.kind})"))
        arr = n.attrs.get("arrival")
        lines.append(f"slack {_fmt(n.attrs.get('worst_slack'))}" + (f" | arrival {_fmt(arr)}" if arr is not None else ""))
        clks = n.attrs.get("clocks") or n.attrs.get("clock_roots")
        if clks:
            lines.append("clk: " + ",".join(clks)[:60])
    asap = n.attrs.get("asap", {})
    if n.kind == "comb" or n.is_sink:
        lines.append(f"ASAP ready {_fmt(asap.get('input_ready'))}"
                     + (f" | out {_fmt(asap.get('output_arrival'))}" if n.kind == "comb" else ""))
    if n.attrs.get("critical_path"):
        lines.append(f"critical path {n.attrs['critical_path']}")
        lines.append(f"instances {n.attrs.get('instances', 1)}")
    if n.labels:
        lines.append("<" + ", ".join(sorted(n.labels)) + ">")
    return "\\n".join(html.escape(l, quote=False).replace('"', '\\"') for l in lines)


def node_style(n: Node) -> Dict[str, str]:
    st = {"shape": KIND_SHAPE.get(n.kind, "ellipse"), "style": "filled", "fillcolor": KIND_FILL.get(n.kind, "white"),
          "color": "black", "penwidth": "1"}
    if "fsm_reg" in n.labels:
        st["fillcolor"] = "#b6d7a8"
    if "violating" in n.labels:
        st["fillcolor"] = "#f9cb9c"
    if n.attrs.get("violated"):
        st["fillcolor"] = "#ea9999"
    if "CDC" in n.labels:
        st["color"] = "#8e44ad"; st["penwidth"] = "2"
    if n.attrs.get("critical_path"):
        st["color"] = "red"; st["penwidth"] = "3"
    if n.kind == "seq":
        st["style"] = "filled,rounded"
    return st


def select_nodes(g: AbstractGraph, labeled_only: bool = False, focus: Optional[str] = None,
                 hops: int = 1, max_nodes: Optional[int] = None, hide_ports: bool = False) -> Set[str]:
    ids = set(g.nodes)
    if labeled_only:
        core = {i for i, n in g.nodes.items() if n.labels}
        ids = _expand(g, core, 1)
    if focus:
        rx = re.compile(focus)
        core = {i for i, n in g.nodes.items() if rx.search(n.name) or rx.search(i)}
        ids &= _expand(g, core, hops)
    if focus:
        # keep combinational nodes that sit between two selected nodes (they have no RTL name)
        for _ in range(2):
            add = set()
            for i, n in g.nodes.items():
                if n.kind == "comb" and i not in ids:
                    if any(s in ids for (s, t) in g.edges if t == i) and any(t in ids for (s, t) in g.edges if s == i):
                        add.add(i)
            if not add:
                break
            ids |= add
    if hide_ports:
        ids = {i for i in ids if not g.nodes[i].is_port}
    if max_nodes and len(ids) > max_nodes:
        # keep labeled first, then worst slack / largest delay
        def prio(i):
            n = g.nodes[i]
            ws = n.attrs.get("worst_slack")
            wd = n.attrs.get("worst_delay")
            return (-len(n.labels), ws if ws is not None else 1e9, -(wd or 0))
        ids = set(sorted(ids, key=prio)[:max_nodes])
    return ids


def _expand(g: AbstractGraph, core: Set[str], hops: int) -> Set[str]:
    out = set(core)
    frontier = set(core)
    for _ in range(hops):
        nxt = set()
        for (s, t) in g.edges:
            if s in frontier and t not in out:
                nxt.add(t)
            if t in frontier and s not in out:
                nxt.add(s)
        out |= nxt
        frontier = nxt
        if not frontier:
            break
    return out


def write_dot(g: AbstractGraph, path: str, ids: Optional[Set[str]] = None, title: str = "") -> str:
    ids = ids if ids is not None else set(g.nodes)
    out = ["digraph rtl_graph {", '  rankdir=LR; splines=true; overlap=false; concentrate=false;',
           '  node [fontname="Helvetica" fontsize=10]; edge [fontname="Helvetica" fontsize=8];']
    if title:
        out.append(f'  label="{title}"; labelloc=t;')
    ins = [i for i in ids if g.nodes[i].kind == "port_in"]
    outs = [i for i in ids if g.nodes[i].kind == "port_out"]
    def q(s: str) -> str:
        return '"' + s.replace('"', '\\"') + '"'
    for i in sorted(ids):
        n = g.nodes[i]
        st = node_style(n)
        attrs = " ".join(f'{k}="{v}"' for k, v in st.items())
        tip = _tooltip(n)
        out.append(f'  {q(i)} [label="{node_label(n)}" {attrs} tooltip="{tip}"];')
    if ins:
        out.append("  { rank=source; " + " ".join(q(i) for i in sorted(ins)) + " }")
    if outs:
        out.append("  { rank=sink; " + " ".join(q(i) for i in sorted(outs)) + " }")
    for (s, t), e in g.edges.items():
        if s not in ids or t not in ids:
            continue
        a = {}
        if e.attrs.get("cdc"):
            a["color"] = "#8e44ad"; a["penwidth"] = "2"
        if e.async_width:
            a["arrowhead"] = "odot"
        lab = f"{e.width}b"
        if e.attrs.get("cdc"):
            lab += " CDC"
        if e.async_width:
            lab += " async"
        if e.attrs.get("arrival") is not None:
            lab += f" @{e.attrs['arrival']:.2f}"
        schedule = e.attrs.get("asap") or {}
        if schedule.get("arrival") is not None:
            lab += f" ASAP {schedule['arrival']:.2f}ns"
            if schedule.get("wait", 0):
                lab += f" wait {schedule['wait']:.2f}ns"
        a["tooltip"] = f"{s} -> {t} ({e.width} bits)"
        if e.width > 1 or e.attrs.get("cdc") or e.async_width or e.attrs.get("arrival") is not None or schedule:
            a["label"] = lab
        if e.attrs.get("slack") is not None and e.attrs["slack"] < 0:
            a["color"] = a.get("color", "#c0392b"); a["penwidth"] = "2"
        attrs = " ".join(f'{k}="{v}"' for k, v in a.items())
        out.append(f"  {q(s)} -> {q(t)} [{attrs}];" if attrs else f"  {q(s)} -> {q(t)};")
    out.append("}")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    return path


def _tooltip(n: Node) -> str:
    parts = [f"{n.id} kind={n.kind}"]
    for k in ("module", "hier", "delay", "clocks", "clock_roots", "worst_slack", "worst_slack_setup", "cell_count",
              "sink_count", "cdc_crossings", "fsm_source", "declared_width", "depth", "critical_path",
              "split_of", "bit_range", "violating_bits"):
        if k in ("critical_path", "hier", "module") and not n.attrs.get(k):
            continue
        if k in n.attrs and n.attrs[k] not in (None, [], {}):
            parts.append(f"{k}={n.attrs[k]}")
    if n.kind == "comb":
        parts.append("sinks=" + ",".join(n.attrs.get("sinks", [])[:6]))
        parts.append("sources=" + ",".join(n.attrs.get("sources", [])[:6]))
    return html.escape("; ".join(str(p) for p in parts)).replace('"', "&quot;")


def to_json(g: AbstractGraph, meta: dict, include_cells: bool = False) -> dict:
    nodes = []
    for i, n in g.nodes.items():
        attrs = {k: v for k, v in n.attrs.items() if not k.startswith("_")}
        rec = {"id": i, "kind": n.kind, "name": n.name, "width": n.width, "labels": sorted(n.labels),
               "key": attrs.get("key", i), "hier": attrs.get("hier", ""), "src": attrs.get("src"),
               "through_delay": attrs.get("through_delay"), "through_slack": attrs.get("through_slack"),
               "through_paths": attrs.get("through_paths"), "through_path": attrs.get("through_path"),
               "through_exact": attrs.get("through_exact"), "negative_delay": attrs.get("negative_delay"),
               "fanout": attrs.get("fanout"), "worst_slew": attrs.get("worst_slew"), "worst_cap": attrs.get("worst_cap"),
               "crossings": attrs.get("crossings"), "fsm": attrs.get("fsm"), "fsm_of": attrs.get("fsm_of"),
               "critical_path": attrs.get("critical_path", 0), "instances": attrs.get("instances", 0), "asap": attrs.get("asap"),
               "combinational_loop": attrs.get("combinational_loop", False)}
        if n.kind == "comb":
            rec.update({"module": attrs.get("module"), "modules": attrs.get("modules", []),
                        "delay": attrs.get("delay"), "delay_exact": attrs.get("delay_exact"),
                        "worst_delay": attrs.get("worst_delay"), "worst_delay_path": attrs.get("worst_delay_path"),
                        "sinks": attrs.get("sinks"), "sources": attrs.get("sources"), "all_sources": attrs.get("all_sources"),
                        "comb_preds": attrs.get("comb_preds"), "cell_count": attrs.get("cell_count"),
                        "cell_types": attrs.get("cell_types"), "cdc_crossings": attrs.get("cdc_crossings"),
                        "violating_sinks": attrs.get("violating_sinks")})
            if include_cells:
                rec["cells"] = [g.design.cells[c].name for c in n.cells]
        elif n.is_port:
            rec.update({"worst_slack": attrs.get("worst_slack"), "clocks": attrs.get("clocks"),
                        "clock_roots": attrs.get("clock_roots")})
        else:
            rec.update({"worst_slack": attrs.get("worst_slack"), "worst_slack_setup": attrs.get("worst_slack_setup"),
                        "arrival": attrs.get("arrival"), "required": attrs.get("required"),
                        "violated": attrs.get("violated", False), "clocks": attrs.get("clocks"),
                        "clock_roots": attrs.get("clock_roots"), "sta_launch_clocks": attrs.get("sta_launch_clocks"),
                        "sta_capture_clocks": attrs.get("sta_capture_clocks"),
                        "endpoints_timed": attrs.get("endpoints_timed"), "worst_slack_path": attrs.get("worst_slack_path"),
                        "cell_count": attrs.get("cell_count"), "cell_types": attrs.get("cell_types"),
                        "declared_width": attrs.get("declared_width"), "depth": attrs.get("depth"),
                        "fsm_source": attrs.get("fsm_source"), "clock_generator": attrs.get("clock_generator"),
                        "async_pins": attrs.get("async_pins"),
                        "split_of": attrs.get("split_of"), "bit_range": attrs.get("bit_range"),
                        "bit_slack": attrs.get("bit_slack"), "violating_bits": attrs.get("violating_bits"),
                        "launch_violating_bits": attrs.get("launch_violating_bits")})
            if include_cells:
                rec["cells"] = [g.design.cells[c].name for c in n.cells]
        nodes.append(rec)
    edges = [{"src": e.src, "dst": e.dst, "width": e.width, "async_width": e.async_width,
              "cdc": bool(e.attrs.get("cdc")), "arrival": e.attrs.get("arrival"), "slack": e.attrs.get("slack"),
              "paths": e.attrs.get("paths"), "asap": e.attrs.get("asap")} for e in g.edges.values()]
    return {"meta": meta, "stats": g.stats, "nodes": nodes, "edges": edges}


def write_graphml(g: AbstractGraph, path: str, ids: Optional[Set[str]] = None):
    ids = ids if ids is not None else set(g.nodes)
    keys = [("kind", "string"), ("name", "string"), ("width", "int"), ("labels", "string"),
            ("worst_slack", "double"), ("delay", "double"), ("module", "string"), ("modules", "string"), ("hier", "string"),
            ("clocks", "string"), ("cell_count", "int"), ("critical_path", "int"), ("instances", "int"),
            ("asap", "string")]
    ekeys = [("width", "int"), ("cdc", "boolean"), ("async_width", "int"), ("asap", "string")]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">']
    for k, t in keys:
        out.append(f'  <key id="n_{k}" for="node" attr.name="{k}" attr.type="{t}"/>')
    for k, t in ekeys:
        out.append(f'  <key id="e_{k}" for="edge" attr.name="{k}" attr.type="{t}"/>')
    out.append('  <graph id="G" edgedefault="directed">')
    for i in sorted(ids):
        n = g.nodes[i]
        vals = {"kind": n.kind, "name": n.name, "width": n.width, "labels": ";".join(sorted(n.labels)),
                "worst_slack": n.attrs.get("worst_slack"), "delay": n.attrs.get("delay"),
                "module": n.attrs.get("module"), "hier": n.attrs.get("hier"),
                "modules": json.dumps(n.attrs.get("modules", [])),
                "clocks": ";".join(n.attrs.get("clocks") or n.attrs.get("clock_roots") or []),
                "cell_count": n.attrs.get("cell_count", len(n.cells)),
                "critical_path": n.attrs.get("critical_path", 0), "instances": n.attrs.get("instances", 0),
                "asap": json.dumps(n.attrs.get("asap"))}
        out.append(f'    <node id="{html.escape(i)}">')
        for k, _ in keys:
            v = vals.get(k)
            if v is not None and v != "":
                out.append(f'      <data key="n_{k}">{html.escape(str(v))}</data>')
        out.append("    </node>")
    k = 0
    for (s, t), e in g.edges.items():
        if s in ids and t in ids:
            out.append(f'    <edge id="e{k}" source="{html.escape(s)}" target="{html.escape(t)}">')
            out.append(f'      <data key="e_width">{e.width}</data>'
                       f'<data key="e_cdc">{"true" if e.attrs.get("cdc") else "false"}</data>'
                       f'<data key="e_async_width">{e.async_width}</data>')
            if e.attrs.get("asap") is not None:
                out.append(f'      <data key="e_asap">{html.escape(json.dumps(e.attrs["asap"]))}</data>')
            out.append("    </edge>")
            k += 1
    out.append("  </graph>\n</graphml>")
    with open(path, "w") as f:
        f.write("\n".join(out))
    return path


def render(dot_path: str, out_path: str, layout: str = "auto", n_nodes: int = 0, timeout: int = 600) -> Optional[str]:
    if layout == "auto":
        layout = "dot" if n_nodes <= 1500 else "sfdp"
    exe = shutil.which(layout) or shutil.which("dot")
    if exe is None:
        return None
    fmt = os.path.splitext(out_path)[1].lstrip(".") or "svg"
    cmd = [exe, f"-T{fmt}", "-o", out_path, dot_path]
    if layout == "sfdp":
        cmd[1:1] = ["-Goverlap=prism"]
    try:
        r = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print(f"[rtl_graph] render timed out after {timeout}s: {' '.join(cmd)}", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"[rtl_graph] render failed: {r.stderr[-500:]}", file=sys.stderr)
        return None
    return out_path


def summary_text(g: AbstractGraph, meta: dict) -> str:
    L = []
    L.append("rtl_graph summary")
    L.append("=================")
    timing = meta.get("timing_summary", {})
    if timing:
        L.append(f"STA endpoints: {timing['endpoint_count']}; violating: {timing['violating_endpoint_count']}; "
                 f"TNS: {timing['tns_ns']:.6f} ns; WNS: {timing['wns_ns']:.6f} ns")
    for k, v in g.stats.items():
        L.append(f"{k:>24}: {v}")
    for k in ("mapped_paths", "unmapped_endpoints", "unmapped_startpoints", "unmapped_register_instances"):
        if k in meta:
            L.append(f"{k:>24}: {meta[k]}")
    labels = {}
    for n in g.nodes.values():
        for l in n.labels:
            labels[l] = labels.get(l, 0) + 1
    L.append("labels: " + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())))
    clk = meta.get("clocks", {})
    if clk:
        L.append("clock roots: " + ", ".join(f"{r}({c})" for r, c in clk.get("root_clock", {}).items()))
    L.append("")
    L.append("critical paths:")
    for cp in meta.get("critical_paths", []):
        L.append(f"  [{cp['path_group']}] instances {cp.get('instances', 1)} slack {_fmt(cp['slack'])}  {cp['startpoint']} -> {cp['endpoint']}")
        if cp.get("segments"):
            L.append("      " + " -> ".join(f"{sg['node']}({sg['through']:.2f})"
                                            if sg.get('through') is not None and not sg.get('merged')
                                            else sg['node'] for sg in cp["segments"]))
        else:
            L.append(f"      nodes: {' -> '.join(cp['nodes']) if cp['nodes'] else '(unmapped)'}  (approximate)")
    L.append("")
    L.append(f"clock-domain crossings: {len(meta.get('crossings', []))}   FSMs: {len(meta.get('fsms', []))}")
    viol = sorted((n for n in g.nodes.values() if n.is_sink and n.attrs.get("worst_slack") is not None),
                  key=lambda n: n.attrs["worst_slack"])[:15]
    L.append("")
    L.append("worst endpoints (nodes):")
    for n in viol:
        L.append(f"  {n.attrs['worst_slack']:9.3f}  {n.id}  [{n.width}]  {','.join(n.attrs.get('clocks') or [])}")
    combs = sorted((n for n in g.nodes.values() if n.kind == "comb" and n.attrs.get("worst_delay") is not None),
                   key=lambda n: -n.attrs["worst_delay"])[:15]
    L.append("")
    L.append("worst through-delay (slack of the worst path through the node):")
    thr = sorted((n for n in g.nodes.values() if n.kind == "comb" and n.attrs.get("through_delay") is not None),
                 key=lambda n: (n.attrs.get("through_slack") if n.attrs.get("through_slack") is not None else 0,
                                -n.attrs["through_delay"]))[:15]
    for n in thr:
        tp = n.attrs.get("through_path") or {}
        L.append(f"  through {n.attrs['through_delay']:6.3f}{'~' if not n.attrs.get('through_exact', True) else ' '} "
                 f"slack {n.attrs['through_slack']:8.3f}  {n.id:8s} cells={n.attrs['cell_count']:<5d} "
                 f"fanout={n.attrs.get('fanout', 0):<5d} hier={n.attrs.get('hier','')}")
    L.append("")
    L.append("slowest combinational nodes (path delay ending at their sinks):")
    for n in combs:
        wp = n.attrs.get("worst_delay_path") or {}
        L.append(f"  {n.attrs['worst_delay']:9.3f}  {n.id}  cells={n.attrs['cell_count']}  "
                 f"{wp.get('startpoint','?')} -> {wp.get('endpoint','?')}  labels={','.join(sorted(n.labels))}")
    return "\n".join(L) + "\n"
