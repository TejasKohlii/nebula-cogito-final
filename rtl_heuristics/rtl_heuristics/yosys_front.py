"""Run Yosys to elaborate RTL into word-level JSON (hierarchical + flattened)."""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'rtl_graph'))
from rtl_graph.yosys_paths import include_option
from rtl_graph.yosys_frontend import read_sources


def _yosys_bin(explicit: str | None = None) -> str:
    for cand in (explicit, os.environ.get("YOSYS"), shutil.which("yosys")):
        if cand:
            return cand
    raise RuntimeError("yosys not found: set $YOSYS or pass --yosys")


def elaborate(sources, top, out_dir, chparams=None, defines=None, incdirs=None,
              frontend="verilog", yosys=None, keep_logs=True, source_cwd=None):
    """Return (hier_json_path, flat_json_path). Both word-level (no techmap).

    The optimisation recipe is deliberately mild: `proc` builds registers/muxes,
    `wreduce` trims unused MSBs, `opt_dff` folds enable/reset muxes into the
    flop so that control muxes are not mistaken for datapath depth.
    """
    out_dir = Path(out_dir).resolve(); out_dir.mkdir(parents=True, exist_ok=True)
    ybin = _yosys_bin(yosys)
    reads = []
    for inc in incdirs or []:
        reads.append(f"verilog_defaults -add {include_option(inc)}")
    for d in defines or []:
        reads.append(f"verilog_defaults -add -D{d}")
    if frontend == "slang":
        reads = read_sources(sources, incdirs or (), defines or (), frontend='slang', top=top,
                             parameters=[f'{k}={v}' for k, v in (chparams or {}).items()])
    else:
        for s in sources:
            reads.append(f'read_verilog -sv "{s}"')
    chp = "" if frontend == 'slang' else " ".join(f"-chparam {k} {v}" for k, v in (chparams or {}).items())
    hier_json = out_dir / "design_hier.json"
    flat_json = out_dir / "design_flat.json"
    script = "\n".join(reads + [
        f"hierarchy -check -top {top} {chp}",
        "proc",
        "opt_expr", "opt_clean", "wreduce", "opt_dff", "opt_clean",
        "check",
        f'write_json "{hier_json}"',
        "flatten",
        "opt_clean",
        f'write_json "{flat_json}"',
    ])
    ys = out_dir / "elaborate.ys"; ys.write_text(script + "\n")
    log = out_dir / "elaborate.log"
    res = subprocess.run([ybin, "-q", "-l", str(log), "-s", str(ys)],
                         capture_output=True, text=True, cwd=source_cwd)
    if res.returncode != 0:
        raise RuntimeError(f"yosys failed (see {log}):\n{res.stderr[-4000:]}")
    return hier_json, flat_json, log


def load_json(path):
    with open(path) as f:
        return json.load(f)
