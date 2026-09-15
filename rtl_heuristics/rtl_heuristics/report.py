"""JSON + Markdown rendering of the analysis result."""
from __future__ import annotations
import json
from dataclasses import asdict
from .patterns import ARCHETYPES


def build_result(meta, design, domains, profiles, crossings, issues, validation, model, feedback):
    from .detectors import _local
    clocks = []
    for dom in domains.values():
        if dom.registers == 0:
            continue
        clocks.append({"id": dom.id, "sdc_name": dom.sdc_name, "kind": dom.kind, "family": dom.family, "ratio_to_master": dom.ratio,
                       "period_ns": dom.period, "period_source": dom.period_source, "source_register": dom.source_reg,
                       "registers": dom.registers, "register_bits": dom.reg_bits, "notes": dom.notes})
    mods = []
    for p in sorted(profiles.values(), key=lambda p: -p.instances * (p.local_reg_bits + sum(p.comb_cells.values()))):
        arch = ARCHETYPES.get(p.archetype, {})
        mods.append({"module": p.module, "instances": p.instances, "archetype": p.archetype, "confidence": p.confidence,
                     "archetype_description": arch.get("desc", ""), "evidence": p.evidence,
                     "registers_per_instance": p.local_regs, "register_bits_per_instance": p.local_reg_bits,
                     "comb_cells_per_instance": dict(p.comb_cells), "register_roles": dict(p.roles),
                     "known_risks": arch.get("risks", []), "playbook": arch.get("fixes", [])})
    cr = []
    for c in crossings:
        cr.append({"classification": c.classification, "kind": c.kind, "src": c.src_reg, "src_clock": c.src_clock, "dst": c.dst_reg,
                   "dst_clock": c.dst_clock, "width": c.width, "sync_depth": c.sync_depth, "through_logic": c.through_logic,
                   "module": c.module, "instance": c.inst_path, "notes": c.notes})
    return {
        "meta": meta,
        "design_profile": {
            "top": design.top, "cells_word_level": len(design.cells), "registers": len(design.regs),
            "register_bits": sum(r.width for r in design.regs.values()), "instances": len(design.inst_module),
            "clock_domains": clocks, "modules": mods, "crossings": cr,
        },
        "timing_model": asdict(model),
        "issues": [i.to_dict() for i in issues],
        "feedback": feedback,
        "validation": validation,
    }


def build_feedback(issues, validation, domains):
    """Compact, machine-consumable action list for an optimisation loop."""
    actions = []
    seen = set()
    for iss in issues:
        if iss.severity in ("info",) or not iss.fixes:
            continue
        fx = iss.fixes[0]
        key = (iss.id, iss.module, iss.signal)
        if key in seen:
            continue
        seen.add(key)
        actions.append({"rank": iss.rank, "issue": iss.id, "severity": iss.severity, "module": iss.module, "signal": iss.signal,
                        "src": iss.src, "instances": iss.instances, "action": fx.name, "algorithm": fx.algorithm,
                        "expected_gain_ns": fx.expected_gain_ns, "latency_change": fx.latency_change,
                        "equivalence": fx.equivalence, "risk": fx.risk, "measured_worst_slack_ns": iss.measured.get("worst_slack_ns")})
        if len(actions) >= 15:
            break
    missing = []
    if not any(d.period_source == "sdc" for d in domains.values()):
        missing.append("SDC with create_clock/create_generated_clock: without it slack cannot be estimated, only depth")
    if not validation.get("measured"):
        missing.append("OpenSTA endpoint slacks (--sta-endpoints) and full path report (--sta-log) with the mapped netlist (--netlist): needed to rank by measured slack and to calibrate the delay model")
    missing.append("Liberty-calibrated ns_per_level / fanout model (use --ns-per-level; validation.calibration suggests a ratio)")
    missing.append("latency/throughput contract per output: decides whether pipeline insertion fixes are admissible")
    return {"priority_actions": actions, "missing_inputs": missing,
            "closure_estimate": _closure_estimate(issues, validation)}


def _closure_estimate(issues, validation):
    crit = [i for i in issues if i.id == "DEEP_COMB_PATH"]
    if not crit:
        return {"note": "no estimated violations"}
    worst = crit[0]
    gains = sum(f.expected_gain_ns or 0 for f in worst.fixes[:2])
    return {"worst_estimated_slack_ns": worst.evidence.get("est_slack_ns"), "sum_of_top2_fix_gains_ns": round(gains, 3),
            "measured_worst_slack_ns": worst.measured.get("worst_slack_ns"),
            "note": "gains are model estimates for the same path; they are not additive with other paths"}


def to_markdown(res: dict) -> str:
    L = []
    p = res["design_profile"]; meta = res["meta"]
    L.append(f"# RTL timing heuristics report: `{p['top']}`\n")
    L.append(f"Generated {meta['date']} by rtl_heuristics {meta['version']} in {meta['runtime_s']} s. "
             f"Word-level cells: {p['cells_word_level']}, registers: {p['registers']} ({p['register_bits']} bits), instances: {p['instances']}.\n")
    v = res.get("validation", {})
    if v.get("measured"):
        m = v["measured"]
        L.append(f"Measured (OpenSTA): {m['violating']} violating endpoints, WNS {m['wns']:.3f} ns, TNS {m['tns']:.1f} ns. "
                 f"Estimated WNS {v.get('estimated_wns_ns')} ns; top-5 endpoint overlap {v.get('top5_overlap')}/5.\n")
        if v.get("calibration"):
            c = v["calibration"]
            L.append(f"Calibration on `{c['endpoint']}`: estimated arrival {c['estimated_arrival_ns']} ns vs measured {c['measured_arrival_ns']} ns (ratio {c['ratio']}). {c['note']}\n")
    L.append("## Clock domains\n")
    L.append("| clock | kind | family | ratio | period (ns) | source | registers |")
    L.append("|---|---|---|---:|---:|---|---:|")
    for c in p["clock_domains"]:
        L.append(f"| {c['sdc_name'] or c['id']} | {c['kind']} | {c['family']} | {c['ratio_to_master'] or ''} | {c['period_ns'] if c['period_ns'] is not None else '?'} | {c['period_source']} | {c['registers']} |")
    L.append("\n## Module archetypes (closest known design pattern)\n")
    L.append("| module | inst | archetype | conf | regs/inst (bits) | comb cells/inst |")
    L.append("|---|---:|---|---:|---:|---|")
    for m in p["modules"]:
        cc = ", ".join(f"{k}:{v}" for k, v in sorted(m["comb_cells_per_instance"].items(), key=lambda x: -x[1])[:4])
        L.append(f"| {m['module']} | {m['instances']} | {m['archetype']} | {m['confidence']:.2f} | {m['registers_per_instance']} ({m['register_bits_per_instance']}) | {cc} |")
    L.append("\n## Issues (ranked)\n")
    for iss in res["issues"]:
        if iss["severity"] == "info":
            continue
        meas = iss.get("measured", {})
        ms = f" · measured worst slack {meas['worst_slack_ns']} ns over {meas.get('violating_endpoints', 0)} violating endpoints" if meas.get("worst_slack_ns") is not None else ""
        hn = f" · measured net: fanout {meas['hot_net']['fanout']}, {meas['hot_net']['delay_ns']} ns" if meas.get("hot_net") else ""
        L.append(f"### {iss['rank']}. [{iss['severity'].upper()}] {iss['id']} — {iss['title']}\n")
        L.append(f"`{iss['module']}` · `{iss['signal']}` · {iss['src']} · {iss['instances']} instance(s) · score {iss['score']}{ms}{hn}\n")
        ev = {k: v for k, v in iss["evidence"].items() if k not in ("stages",)}
        L.append("Evidence: " + ", ".join(f"{k}={_short(v)}" for k, v in ev.items()) + "\n")
        if iss["evidence"].get("stages"):
            L.append("Path stages (est.):\n")
            L.append("| # | cell | module | src | width | fanout in | arrival out (ns) |")
            L.append("|---:|---|---|---|---:|---:|---:|")
            for i, s in enumerate(iss["evidence"]["stages"]):
                L.append(f"| {i} | {s['type']} | {s['module']} | {s['src']} | {s['width']} | {s['fanout_in']} | {s['arr_out']} |")
            L.append("")
        for j, f in enumerate(iss["fixes"]):
            gain = f" · est. gain {f['expected_gain_ns']} ns" if f.get("expected_gain_ns") is not None else ""
            L.append(f"**Fix {j + 1}: {f['name']}** (`{f['algorithm']}`{gain} · latency {f['latency_change']} · equivalence: {f['equivalence']} · risk {f['risk']})\n")
            L.append(f"{f['description']}\n")
            if f.get("steps"):
                for k, s in enumerate(f["steps"]):
                    L.append(f"{k + 1}. {s}")
                L.append("")
            if f.get("rtl_after"):
                L.append("```verilog\n" + (("// before\n" + f["rtl_before"] + "\n// after\n") if f.get("rtl_before") else "") + f["rtl_after"] + "\n```\n")
            if f.get("sdc_change"):
                L.append("```tcl\n" + f["sdc_change"] + "\n```\n")
    info = [i for i in res["issues"] if i["severity"] == "info"]
    if info:
        L.append("## Informational\n")
        for iss in info:
            L.append(f"- {iss['id']}: {iss['title']} ({iss['instances']} instance(s))")
    fb = res.get("feedback", {})
    L.append("\n## Feedback for the optimisation loop\n")
    L.append("| rank | issue | module.signal | action | est. gain (ns) | latency | risk | measured slack |")
    L.append("|---:|---|---|---|---:|---|---|---:|")
    for a in fb.get("priority_actions", []):
        L.append(f"| {a['rank']} | {a['issue']} | {a['module']}.{a['signal']} | {a['action']} | {a['expected_gain_ns'] if a['expected_gain_ns'] is not None else ''} | {a['latency_change']} | {a['risk']} | {a['measured_worst_slack_ns'] if a['measured_worst_slack_ns'] is not None else ''} |")
    L.append("\nMissing inputs that would improve the analysis:\n")
    for mi in fb.get("missing_inputs", []):
        L.append(f"- {mi}")
    return "\n".join(L) + "\n"


def _short(v):
    s = json.dumps(v, default=str)
    return s if len(s) < 120 else s[:117] + "..."
