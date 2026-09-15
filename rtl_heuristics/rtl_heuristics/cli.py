"""Command line: python -m rtl_heuristics analyze ..."""
from __future__ import annotations
import argparse, datetime, json, os, sys, time
from pathlib import Path

from . import __version__
from .yosys_front import elaborate, load_json
from .netgraph import Design
from .sdc_parse import parse_sdc, parse_sta_clock_table
from .clocks import infer_clocks
from .delay_model import DelayModel
from .timing import TimingEngine
from .patterns import classify_registers, classify_modules
from .cdc import find_crossings, reconvergence
from .detectors import Context, run_all
from .issues import Issue, Fix
from .report import build_result, build_feedback, to_markdown
from . import sta_link


def build_parser():
    ap = argparse.ArgumentParser(prog="rtl_heuristics", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="analyse RTL and emit issues/fixes JSON")
    a.add_argument("--top", required=True)
    a.add_argument("--source", action="append", default=[], help="RTL file (repeatable)")
    a.add_argument("--source-cwd", help="RTL working directory for relative memory initialization files")
    a.add_argument("--filelist", help="file with one RTL path per line")
    a.add_argument("--chparam", action="append", default=[], help="NAME=VALUE top-level parameter")
    a.add_argument("-D", dest="defines", action="append", default=[])
    a.add_argument("-I", dest="incdirs", action="append", default=[])
    a.add_argument("--frontend", choices=["verilog", "slang"], default="verilog")
    a.add_argument("--yosys", help="yosys binary (default $YOSYS or PATH)")
    a.add_argument("--sdc", help="SDC constraints (evaluated with tclsh if available)")
    a.add_argument("--sdc-cwd", help="directory the SDC's relative `source` paths refer to")
    a.add_argument("--sta-clocks", help="OpenSTA report containing the clock period table (fallback for periods)")
    a.add_argument("--clock", action="append", default=[], help="override period: CLOCKID=PERIOD (e.g. clk[1]=0.8333 or port:clk[1]=0.8333)")
    a.add_argument("--default-period", type=float, help="period for clocks with no constraint")
    a.add_argument("--netlist", help="mapped gate-level Verilog (for STA name mapping)")
    a.add_argument("--sta-endpoints", help="endpoint slack list (TSV: endpoint<TAB>slack)")
    a.add_argument("--sta-log", help="OpenSTA report_checks text (full paths)")
    a.add_argument("--ns-per-level", type=float, default=0.045)
    a.add_argument("--style", choices=["ripple", "prefix"], default="ripple", help="assumed arithmetic mapping style")
    a.add_argument("--fanout-threshold", type=int, default=32)
    a.add_argument("--reset-fanout-threshold", type=int, default=256)
    a.add_argument("--max-critical-groups", type=int, default=25)
    a.add_argument("--out", default="rtl_heuristics_out")
    a.add_argument("--quiet", action="store_true")
    return ap


def analyze(args) -> dict:
    t0 = time.time()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    sources = list(args.source)
    if args.filelist:
        base = Path(args.filelist).parent
        with open(args.filelist) as fh:
            flist = fh.readlines()
        for ln in flist:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                sources.append(str((base / ln).resolve()) if not os.path.isabs(ln) else ln)
    if not sources:
        raise SystemExit("no --source / --filelist given")
    chparams = dict(kv.split("=", 1) for kv in args.chparam)
    log = lambda *a: (None if args.quiet else print(*a, file=sys.stderr))

    log(f"[1/7] elaborating {len(sources)} file(s) with yosys ...")
    hier_json, flat_json, ylog = elaborate(sources, args.top, out, chparams, args.defines, args.incdirs, args.frontend, args.yosys,
                                         source_cwd=getattr(args, 'source_cwd', None))
    design = Design(load_json(flat_json), load_json(hier_json), args.top)
    log(f"      {len(design.cells)} word-level cells, {len(design.regs)} registers, {len(design.inst_module)} instances")

    log("[2/7] constraints ...")
    sdc = parse_sdc(args.sdc, args.sdc_cwd) if args.sdc else parse_sdc(None)
    overrides = {}
    for kv in args.clock:
        k, v = kv.split("=", 1); overrides[k] = float(v)
    if args.sta_clocks:
        tbl = parse_sta_clock_table(args.sta_clocks)
        for name, info in tbl.items():
            if name in sdc.clocks and sdc.clocks[name].period is None:
                sdc.clocks[name].period = info["period"]
    domains = infer_clocks(design, sdc, args.default_period, overrides)
    log(f"      {sum(1 for d in domains.values() if d.registers)} clock domains, SDC parser={sdc.parser}, {len(sdc.clocks)} SDC clocks")

    log("[3/7] register / module pattern classification ...")
    classify_registers(design)
    profiles = classify_modules(design)

    log("[4/7] bit-level depth estimation ...")
    model = DelayModel(ns_per_level=args.ns_per_level, style=args.style)
    te = TimingEngine(design, domains, model, sdc).run()
    log(f"      {len(te.endpoints)} endpoints, {len(te.loop_cells)} cells in combinational loops")

    log("[5/7] CDC / reset crossings ...")
    crossings = find_crossings(design, te)
    reconv = reconvergence(design, crossings)

    log("[6/7] detectors ...")
    cfg = {"fanout_threshold": args.fanout_threshold, "reset_fanout_threshold": args.reset_fanout_threshold,
           "max_critical_groups": args.max_critical_groups}
    ctx = Context(design, te, domains, sdc, model, profiles, crossings, cfg)
    issues = run_all(ctx)
    for key, regs in reconv:
        issues.append(Issue("CDC_RECONVERGENCE", "cdc", "medium", f"Synchronised bits {', '.join(sorted(set(regs))[:4])} from {key[0]} reconverge in {key[1]} logic", design.top, ",".join(sorted(set(regs))[:2]), "", 1, [key[2]],
                            fixes=[Fix("Synchronise a single control bit / use gray code", "cdc_reconvergence", "Independently synchronised bits can arrive in different cycles; combine them only through a handshake or gray-coded value.", risk="medium")]))
    for w in sdc.warnings:
        issues.append(Issue("SDC_PARSE_WARNING", "constraints", "low", w, design.top))

    validation = {}
    meas = None
    if args.sta_endpoints or args.sta_log:
        log("[7/7] correlating with OpenSTA measurements ...")
        nl = sta_link.Netlist(args.netlist) if args.netlist else None
        eps = sta_link.parse_endpoint_slacks(args.sta_endpoints) if args.sta_endpoints else []
        paths = sta_link.parse_timing_report(args.sta_log) if args.sta_log else []
        if not eps and paths:
            eps = [(p["end"] + ("/D" if p["group"] not in ("asynchronous",) else "/RN"), p["slack"]) for p in paths]
        meas = sta_link.Measurements(nl, eps, paths)
        validation = sta_link.correlate(issues, design, meas, te)
        if not nl:
            validation["note"] = "no --netlist: STA endpoint names could not be mapped to RTL registers"
    else:
        log("[7/7] no STA data given; estimates only")

    for iss in issues:
        iss.compute_score()
    issues.sort(key=lambda i: -i.score)
    for k, iss in enumerate(issues, 1):
        iss.rank = k; iss.uid = f"{iss.id}#{k}"
    feedback = build_feedback(issues, validation, domains)
    meta = {"tool": "rtl_heuristics", "version": __version__, "date": datetime.datetime.now().isoformat(timespec="seconds"),
            "top": args.top, "sources": sources, "sdc": args.sdc, "netlist": args.netlist, "sta_endpoints": args.sta_endpoints,
            "sta_log": args.sta_log, "runtime_s": round(time.time() - t0, 2), "yosys_log": str(ylog)}
    res = build_result(meta, design, domains, profiles, crossings, issues, validation, model, feedback)
    (out / "analysis.json").write_text(json.dumps(res, indent=2, default=str))
    (out / "feedback.json").write_text(json.dumps({"meta": meta, **feedback}, indent=2, default=str))
    (out / "report.md").write_text(to_markdown(res))
    log(f"done in {meta['runtime_s']} s: {len(issues)} issues -> {out / 'analysis.json'}, {out / 'report.md'}, {out / 'feedback.json'}")
    return res


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "analyze":
        res = analyze(args)
        if args.quiet:
            return 0
        top = [i for i in res["issues"] if i["severity"] != "info"][:8]
        print(f"\nTop issues for {args.top}:")
        for i in top:
            print(f"  {i['rank']:>2}. [{i['severity']:<8}] {i['id']:<26} {i['module']}.{i['signal']}  x{i['instances']}  score={i['score']}" +
                  (f"  measured={i['measured']['worst_slack_ns']}" if i.get('measured', {}).get('worst_slack_ns') is not None else ""))
        return 0
    return 1
