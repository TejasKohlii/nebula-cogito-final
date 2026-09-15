"""Link OpenSTA measurements back to RTL names.

* parse a hierarchical gate-level netlist (Verilog) to map `inst/path/_123_` DFF
  instances to the RTL register bit connected to their Q pin
* parse endpoint slack lists (`endpoint<TAB>slack` or `report_checks -format end`)
* parse full `report_checks` text for per-path fanout/delay hot spots
"""
from __future__ import annotations
import re
from collections import defaultdict

_INST_RE = re.compile(r"^\s*(\\?\S+)\s+(\\?\S+?)\s*\(\s*(\.[\s\S]*?)\)\s*;", re.M)
_PIN_RE = re.compile(r"\.(\w+)\(([^()]*)\)")
_MOD_RE = re.compile(r"^\s*module\s+(\\?\S+?)\s*\(", re.M)


class Netlist:
    def __init__(self, path: str):
        self.modules: dict[str, dict] = {}     # name -> {inst_name: (type, {pin: net})}
        self.assigns: dict[str, dict] = defaultdict(dict)
        self.top = None
        with open(path, errors="ignore") as f:
            self._parse(f.read())

    def _parse(self, txt: str):
        pos = 0
        for m in _MOD_RE.finditer(txt):
            name = m.group(1).lstrip("\\")
            end = txt.find("endmodule", m.end())
            body = txt[m.end():end]
            insts = {}
            for im in _INST_RE.finditer(body):
                t, n, pins = im.group(1).lstrip("\\"), im.group(2).lstrip("\\"), im.group(3)
                if t in ("input", "output", "wire", "reg", "assign", "inout"):
                    continue
                insts[n] = (t, {p: re.sub(r"\s+", "", v).lstrip("\\") for p, v in _PIN_RE.findall(pins)})
            for am in re.finditer(r"assign\s+(\S+)\s*=\s*([^;]+);", body):
                self.assigns[name][am.group(1)] = am.group(2).strip()
            self.modules[name] = insts
        used = {t for insts in self.modules.values() for t, _ in insts.values()}
        tops = [n for n in self.modules if n not in used]
        self.top = tops[0] if tops else next(iter(self.modules), None)

    def resolve(self, path: str):
        """'domain[1].engine/reducer/_2053_/D' -> (rtl_name or None, pin, module, info)."""
        path = path.strip()
        pin = None
        segs = path.split("/")
        if segs and re.fullmatch(r"[A-Z][A-Z0-9]*", segs[-1]):
            pin = segs.pop()
        mod = self.top
        prefix = []
        for seg in segs[:-1]:
            insts = self.modules.get(mod, {})
            if seg in insts:
                mod = insts[seg][0]; prefix.append(seg)
            else:
                return None, pin, mod, "instance not found: " + seg
        leaf = segs[-1] if segs else ""
        insts = self.modules.get(mod, {})
        if leaf not in insts:
            # maybe a port / net of the current module
            return ".".join(prefix + [leaf]) if leaf else None, pin, mod, "net"
        ctype, pins = insts[leaf]
        q = pins.get("Q") or pins.get("QN")
        if q and not re.fullmatch(r"_\d+_", q):
            return ".".join(prefix + [q]), pin, mod, ctype
        # unnamed: try any output-ish pin
        for p in ("Z", "ZN", "Y", "Q"):
            v = pins.get(p)
            if v and not re.fullmatch(r"_\d+_", v):
                return ".".join(prefix + [v]), pin, mod, ctype
        return None, pin, mod, ctype


def parse_endpoint_slacks(path: str):
    """Return list of (endpoint_string, slack)."""
    out = []
    with open(path, errors="ignore") as fh:
        lines = fh.readlines()
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.lower().startswith("endpoint"):
            continue
        m = re.match(r"(\S+)\s+(-?\d+(?:\.\d+)?)", ln)
        if m:
            out.append((m.group(1), float(m.group(2))))
    return out


_ROW_RE = re.compile(r"^\s*(?:(\d+)\s+([\d.]+)\s+)?([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([v^])\s+(\S+)\s+\((\S+)\)")


def parse_timing_report(path: str):
    """Parse OpenSTA report_checks (full/full_clock) text into paths."""
    paths = []
    cur = None
    with open(path, errors="ignore") as fh:
        lines = fh.readlines()
    for ln in lines:
        if ln.startswith("Startpoint:"):
            cur = {"start": ln.split(":", 1)[1].strip().split()[0], "rows": [], "group": "", "slack": None, "end": ""}
        elif ln.startswith("Endpoint:") and cur is not None:
            cur["end"] = ln.split(":", 1)[1].strip().split()[0]
        elif ln.startswith("Path Group:") and cur is not None:
            cur["group"] = ln.split(":", 1)[1].strip()
        elif "slack" in ln and cur is not None:
            m = re.search(r"(-?[\d.]+)\s+slack", ln)
            if m:
                cur["slack"] = float(m.group(1)); paths.append(cur); cur = None
        elif cur is not None:
            m = _ROW_RE.match(ln)
            if m:
                fan, cap, slew, delay, t, edge, pin, cell = m.groups()
                cur["rows"].append({"fanout": int(fan) if fan else 0, "cap": float(cap) if cap else 0.0, "slew": float(slew),
                                    "delay": float(delay), "time": float(t), "pin": pin, "cell": cell})
    return paths


def hot_nets(paths, fanout_min=32, delay_min=0.25):
    hot = {}
    for p in paths:
        for r in p["rows"]:
            if r["fanout"] >= fanout_min or r["delay"] >= delay_min:
                k = r["pin"]
                if k not in hot or r["delay"] > hot[k]["delay"]:
                    hot[k] = dict(r, worst_slack=p["slack"], group=p["group"])
    return sorted(hot.values(), key=lambda r: -r["delay"])


class Measurements:
    def __init__(self, netlist: Netlist | None, endpoints=None, paths=None):
        self.netlist = netlist
        self.endpoints = endpoints or []      # (name, slack)
        self.paths = paths or []
        self.by_rtl = defaultdict(list)       # rtl reg base name (no bit) -> [(slack, kind, endpoint)]
        self.full_names = {}
        self.unmapped = 0
        self.kind_counts = defaultdict(int)
        self._map()

    def _map(self):
        for ep, slack in self.endpoints:
            rtl, pin, mod, info = (self.netlist.resolve(ep) if self.netlist else (ep, None, "", ""))
            kind = "recovery" if pin in ("RN", "SN", "SE", "RB", "SB") else ("setup" if pin else "output")
            self.kind_counts[kind] += 1
            if rtl is None:
                self.unmapped += 1
                rtl = ep
            base = re.sub(r"\[\d+\]$", "", rtl)
            self.by_rtl[base].append((slack, kind, ep))
            self.full_names.setdefault(base, rtl)

    def worst_for(self, rtl_base: str):
        lst = self.by_rtl.get(rtl_base)
        if not lst:
            return None
        return min(s for s, _, _ in lst)

    def summary(self):
        neg = [s for lst in self.by_rtl.values() for s, _, _ in lst if s < 0]
        return {"endpoints": len(self.endpoints), "violating": len(neg), "wns": min(neg) if neg else 0.0,
                "tns": round(sum(neg), 3), "unmapped_endpoints": self.unmapped, "by_kind": dict(self.kind_counts)}


def correlate(issues, design, meas: Measurements, timing=None):
    """Attach measured slacks to issues; return validation dict."""
    from .detectors import _sig_key
    # measured worst per (module, local-signal-with-[*])
    by_sig = defaultdict(list)
    for base, lst in meas.by_rtl.items():
        sig = _sig_key(design, meas.full_names.get(base, base))
        for s, kind, ep in lst:
            by_sig[sig].append((s, kind, ep))
    for iss in issues:
        sig = (iss.module, re.sub(r"\[\d+\]", "[*]", iss.signal)) if iss.signal else None
        cands = []
        if iss.id in ("DEEP_COMB_PATH", "RETIMING_IMBALANCE", "DEEP_ENABLE_CONE", "IO_PATH_VIOLATION", "ADDER_CHAIN"):
            end = iss.evidence.get("end") or (iss.evidence.get("output_registers") or [None])[0]
            if iss.id == "ADDER_CHAIN" and iss.evidence.get("output_registers"):
                for r in iss.evidence["output_registers"]:
                    cands += [x for x in by_sig.get((iss.module, re.sub(r"\[\d+\]", "[*]", r)), []) if x[1] != "recovery"]
            elif end:
                cands = [x for x in by_sig.get(_sig_key(design, end), []) if x[1] != "recovery"]
            elif sig:
                cands = [x for x in by_sig.get(sig, []) if x[1] != "recovery"]
        elif iss.id == "ASYNC_RESET_FANOUT":
            cands = [x for lst in by_sig.values() for x in lst if x[1] == "recovery"]
        elif iss.id in ("HIGH_FANOUT_NET", "COMPARATOR_FLAG_BROADCAST", "LATE_CONTROL_SELECT"):
            # hot nets from the full report whose RTL name matches
            hot = hot_nets(meas.paths)
            for h in hot:
                rtl, pin, mod, info = meas.netlist.resolve(h["pin"]) if meas.netlist else (None, None, "", "")
                if rtl and sig and _sig_key(design, rtl) == sig:
                    iss.measured.setdefault("hot_net", {"pin": h["pin"], "fanout": h["fanout"], "delay_ns": h["delay"], "cap_fF": h["cap"], "worst_path_slack_ns": h["worst_slack"]})
            # also compare-driven nets are unnamed after mapping; match on consumer endpoints being violated
        hn = iss.measured.get("hot_net")
        if hn and iss.fixes:
            # measured net delay beats the model: re-estimate the gain of the first (RTL) fix
            import math
            f = hn["fanout"]
            k = max(2, math.ceil(f / 64))
            residual = 0.06 * math.ceil(math.log(max(f / k / 4, 1), 4))
            gain = round(max(hn["delay_ns"] - residual, 0.0), 3)
            if gain > (iss.fixes[0].expected_gain_ns or 0):
                iss.fixes[0].expected_gain_ns = gain
                iss.fixes[0].expected_gain_note = (f"measured: this net costs {hn['delay_ns']} ns as mapped (fanout {f}, {hn['cap_fF']} fF); "
                                                   f"launching it from {k} replicated flops bounds it to ≈{residual:.2f} ns")
        if cands:
            neg = [s for s, _, _ in cands if s < 0]
            iss.measured.update({"worst_slack_ns": round(min(s for s, _, _ in cands), 4), "matched_endpoints": len(cands),
                                 "violating_endpoints": len(neg), "example_endpoint": min(cands, key=lambda x: x[0])[2]})
    # validation: estimated vs measured worst endpoints
    val = {"measured": meas.summary()}
    if timing is not None:
        est = sorted([e for e in timing.endpoints if e.slack is not None and e.kind != "recovery"], key=lambda e: e.slack)
        est_sigs = []
        for e in est:
            s = _sig_key(design, e.name)
            if s not in est_sigs:
                est_sigs.append(s)
            if len(est_sigs) >= 5:
                break
        meas_sorted = sorted(((min(x[0] for x in lst), sig) for sig, lst in by_sig.items() if any(x[1] != "recovery" for x in lst)))
        meas_sigs = [sig for _, sig in meas_sorted[:5]]
        overlap = [s for s in est_sigs if s in meas_sigs]
        val.update({"estimated_top5_endpoints": [f"{m}.{l}" for m, l in est_sigs], "measured_top5_endpoints": [f"{m}.{l}" for m, l in meas_sigs],
                    "top5_overlap": len(overlap), "estimated_wns_ns": round(est[0].slack, 4) if est else None})
        if est and meas_sorted:
            # calibration hint: ratio of measured to estimated arrival on the worst measured endpoint
            wsig = meas_sorted[0][1]
            e_match = next((e for e in est if _sig_key(design, e.name) == wsig), None)
            if e_match and e_match.period:
                meas_arr = e_match.period - meas_sorted[0][0] - 0.04 - 0.035
                val["calibration"] = {"endpoint": f"{wsig[0]}.{wsig[1]}", "estimated_arrival_ns": round(e_match.arrival, 3),
                                      "measured_arrival_ns": round(meas_arr, 3), "ratio": round(meas_arr / max(e_match.arrival, 1e-6), 2),
                                      "note": "ratio >> 1: unbuffered high-fanout nets in the mapped netlist (compare est_penalty_unbuffered_ns); ratio < 1: mapping produced faster arithmetic than the ripple model, try --style prefix or a smaller --ns-per-level"}
    return val
