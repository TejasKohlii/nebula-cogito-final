"""Drive Yosys to elaborate + flatten the RTL and dump JSON with FSM detection."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional
from .yosys_paths import include_option
from .yosys_frontend import read_sources


def _q(p) -> str:
    """Quote a path for a Yosys script (Yosys accepts double quotes, not shell quoting)."""
    return '"' + str(p).replace('"', '\\"') + '"'


def elaborate(
    out_json: str,
    top: str,
    sources: Optional[List[str]] = None,
    filelist: Optional[str] = None,
    rtlil: Optional[str] = None,
    yosys: str = "yosys",
    frontend: str = "verilog",        # verilog | slang
    defines: Optional[List[str]] = None,
    incdirs: Optional[List[str]] = None,
    chparams: Optional[List[str]] = None,   # "NAME=VALUE" for the top module
    frontend_flags: Optional[List[str]] = None,
    extra_cmds: Optional[List[str]] = None,
    fsm_detect: bool = True,
    log: Optional[str] = None,
    timeout: int = 3600,
    source_cwd: Optional[str] = None,
) -> str:
    """Return the path of the written JSON.  Raises on Yosys failure."""
    out_json = str(Path(out_json).resolve())
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    files: List[str] = []
    if filelist:
        base = Path(filelist).parent
        for line in open(filelist):
            line = line.split("//")[0].strip()
            if not line or line.startswith("#") or line.startswith("+"):
                continue
            p = Path(line)
            files.append(str(p if p.is_absolute() else base / p))
    if sources:
        files.extend(sources)
    defines = defines or []
    incdirs = incdirs or []
    lines: List[str] = []
    if rtlil:
        lines.append(f"read_rtlil {_q(rtlil)}")
    elif frontend == "slang":
        lines += read_sources(files, incdirs, defines, frontend='slang', top=top,
                              parameters=chparams or (), frontend_flags=frontend_flags)
    else:
        for d in defines:
            lines.append(f"verilog_defines -D{d}")
        for f in files:
            lines.append("read_verilog -sv " + " ".join(include_option(i) for i in incdirs) + f" {_q(f)}")
    for cp in (chparams or []) if frontend != 'slang' or rtlil else []:
        k, _, v = cp.partition("=")
        lines.append(f"chparam -set {k} {v} {top}")
    lines += [
        f"hierarchy -top {top}",
        "proc",
        "flatten",
        "memory_collect",
        "opt_clean",
    ]
    if fsm_detect:
        lines.append("fsm_detect")
    lines += extra_cmds or []
    # Older Yosys tee versions do not unquote output filenames. Keep reports
    # in the normal log, whose CLI path supports spaces, and split them below.
    lines += ["log COGITO_STAT_BEGIN", "stat", "log COGITO_STAT_END"]
    lines.append(f"write_json {_q(out_json)}")
    if fsm_detect:
        # state tables for the GUI; runs after write_json so a failure here cannot lose the design dump
        lines.append("fsm_extract")
        lines += ["log COGITO_FSM_BEGIN", "fsm_info", "log COGITO_FSM_END"]
    script = out_json + ".ys"
    Path(script).write_text("\n".join(lines) + "\n")
    argv = [yosys]
    log_path = Path(log or (out_json + ".log"))
    argv += ["-q", "-l", str(log_path), "-s", script]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=source_cwd)
    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    for label, suffix in (("STAT", ".stat.txt"), ("FSM", ".fsm_info.txt")):
        _, begin, report = log_text.partition(f"\nCOGITO_{label}_BEGIN\n")
        report, end, _ = report.partition(f"\nCOGITO_{label}_END\n")
        if begin and end:
            Path(out_json + suffix).write_text(report)
    if not os.path.exists(out_json):
        raise RuntimeError(f"yosys failed (rc={r.returncode}):\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}")
    if r.returncode != 0:
        print(f"[rtl_graph] warning: yosys exited {r.returncode} after write_json (fsm_extract/fsm_info step); "
              f"design.json is complete, FSM state tables may be missing", file=sys.stderr)
    return out_json
