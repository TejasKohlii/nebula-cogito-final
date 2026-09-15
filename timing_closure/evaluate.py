#!/usr/bin/env python3
"""Fixed RTL verifier and timing objective for Shinka. No candidate Python runs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

TASK = Path(__file__).resolve().parent
ROOT = TASK.parent
# One evaluator, several designs: the task's `environment` block selects which one.
# Unset, every path is exactly the original v2 configuration.
DESIGN = Path(os.environ.get('COGITO_DESIGN_DIR') or ROOT / 'custom_designs/custom_design_v2')
# Flow scripts are design-agnostic (they read the work directory); a design whose own
# copies are older can borrow a newer set without touching its RTL or constraints.
SCRIPTS = Path(os.environ.get('COGITO_SCRIPTS_DIR') or DESIGN / 'scripts')
SEED = Path(os.environ.get('COGITO_SEED') or TASK / 'initial.sv')
LIB = ROOT / 'third_party/nangate45/NangateOpenCellLibrary_typical.lib'
START, END = '// EVOLVE-BLOCK-START', '// EVOLVE-BLOCK-END'
FLOW_SCRIPTS = ('synth.py', 'run_sta.py', 'cdc_constraints.py', 'sta.tcl', 'endpoint_slacks.tcl')


def validate_source(source, seed):
    if source.count(START) != 1 or source.count(END) != 1:
        raise ValueError('Exactly one evolve block required')
    # Shinka's patch writer strips the final newline. Ignore only EOF line
    # terminators; the actual immutable Verilog remains byte-for-byte equal.
    if (source.split(START)[0] != seed.split(START)[0] or
            source.split(END)[1].rstrip('\r\n') != seed.split(END)[1].rstrip('\r\n')):
        raise ValueError('Immutable reset/divider/CDC/top-level changed')
    mutable = source.split(START)[1].split(END)[0]
    code = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/', '', mutable)
    if re.search(r'`(?:include|define)|\$(?:readmem|system)|\(\*|\b(?:specify|initial)\b', code):
        raise ValueError('External includes, macros, initial state and synthesis attributes are prohibited')


def run(cmd, cwd, log, timeout=240):
    cmd = list(cmd)
    if cmd[0] in ('yosys', 'sta', 'verilator'):
        cmd[0] = os.environ.get(cmd[0].upper(), cmd[0])
    with (cwd / log).open('w') as out:
        p = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                           timeout=timeout, env=dict(os.environ, PYTHONUNBUFFERED='1'))
    if p.returncode:
        text = (cwd / log).read_text(errors='replace')
        # Help banners can surround the actual error (even mid-line when
        # stdout/stderr are interleaved). Keep the error and command in feedback;
        # the complete tool output remains available in the report file.
        failures = list(re.finditer(r'ERROR:[^\n]*(?:\n>[^\n]*)*', text))
        detail = '\n'.join(m.group(0) for m in failures[-3:]) if failures else text[-2000:]
        raise RuntimeError(f'{log}: exit {p.returncode}\n{detail[:2000]}')


def prove(work):
    script = []
    for role, source in [('gold', DESIGN / 'rtl/benchmark.sv'),
                         ('gate', work / 'rtl/benchmark.sv')]:
        script += [f'read_verilog -sv "{source}"',
                   'prep -top window_engine -flatten', 'async2sync', 'alumacc', 'techmap', 'opt_clean',
                   f'rename window_engine {role}', f'design -stash {role}']
    script += ['design -copy-from gold -as gold gold',
               'design -copy-from gate -as gate gate', 'equiv_make gold gate equiv',
               'hierarchy -top equiv', 'equiv_simple', 'equiv_induct -seq 4',
               'equiv_status -assert']
    (work / 'proof.ys').write_text('\n'.join(script) + '\n')
    run(['yosys', '-Q', 'proof.ys'], work, 'formal.log', 180)


def evaluate(program_path, results_dir, raw=False):
    started = time.monotonic()
    out = Path(results_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / 'design'
    work.mkdir(exist_ok=True)
    metrics = dict(combined_score=0.0, public={}, private={}, extra_data={}, text_feedback='')
    correct, error = False, ''
    try:
        if os.environ.get('SHINKA_RTL_MANIFEST'):
            from contract import verify_manifest
            verify_manifest(os.environ['SHINKA_RTL_MANIFEST'])
        source = Path(program_path).read_text()
        seed = SEED.read_text()
        validate_source(source, seed)
        for directory in ('rtl', 'reports', 'build', 'scripts', 'constraints'):
            (work / directory).mkdir(exist_ok=True)
        for name in FLOW_SCRIPTS:
            shutil.copy2(SCRIPTS / name, work / 'scripts' / name)
        shutil.copy2(DESIGN / 'constraints/benchmark.sdc', work / 'constraints/benchmark.sdc')
        (work / 'rtl/benchmark.sv').write_text(source)
        prove(work)
        metrics['public']['formal_equivalence'] = True
        if raw:
            run([sys.executable, 'scripts/synth.py', '--lib', str(LIB)], work, 'synthesis.log')
            sta = os.environ.get('STA', shutil.which('sta') or str(DESIGN / 'build/tools/sta'))
            run([sys.executable, 'scripts/run_sta.py', '--sta', sta, '--lib', str(LIB)],
                work, 'sta.log')
        else:
            from implement import implement
            implement(Path(program_path).resolve(), work, fanout=8, target_ps=500, fast=True)
        cells = json.loads((work / 'reports/cell_count.json').read_text())
        timing = json.loads((work / 'reports/timing_summary.json').read_text())
        groups = timing['worst_reported_path_by_group']
        wns = min(v['worst_slack_ns'] for v in groups.values())
        data_wns = min(v['worst_slack_ns'] for k, v in groups.items() if k != 'asynchronous')
        violations = (work / 'reports/violating_endpoints.tsv').read_text().splitlines()[1:]
        # Closed candidates always outrank open ones. Within closure, reward
        # area and margin, so Shinka can continue improving a closed design.
        closed = wns >= 0 and timing['tns_ns'] >= 0 and not violations
        if closed:
            score = 1.0 + 0.10 / (1.0 + cells['mapped_cell_area'] / 275366.658)
            score += 0.01 * min(wns / 0.10, 1.0)
        else:
            score = 0.99 / (1.0 + max(0., -wns) + max(0., -timing['tns_ns']) / 10000.)
        metrics['combined_score'] = score
        metrics['public'].update(wns_ns=wns, data_wns_ns=data_wns,
            tns_ns=timing['tns_ns'], standard_cells=cells['standard_cells'],
            cell_area_um2=cells['mapped_cell_area'], violating_endpoints=len(violations),
            timing_closed=closed)
        metrics['extra_data'] = dict(clock_count=15, stage=timing['stage'],
            objective_version='closure_then_area_margin_v1',
            rtl_sha256=cells['rtl_sha256'], liberty_sha256=cells['liberty_sha256'],
            sdc_sha256=hashlib.sha256((work/'constraints/benchmark.sdc').read_bytes()).hexdigest(),
            backend='original_mapping' if raw else 'timing_mapping_fast_buffer8',
            evaluator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            formal_scope='Full default-size window_engine after async2sync; unchanged surrounding RTL')
        feedback = []
        for group in ('m1', 'asynchronous', 'path delay'):
            if group in groups:
                feedback.append(f'{group}: {groups[group]}')
        log = (work/'reports/timing.log').read_text()
        blocks = log.split('Startpoint: ')[1:]
        worst = sorted(blocks, key=lambda b: float(re.search(r'([-+\d.]+)\s+slack \(', b)[1])
                       if re.search(r'([-+\d.]+)\s+slack \(', b) else 1e9)
        metrics['text_feedback'] = '\n'.join(feedback) + '\nCritical path:\n' + (worst[0] if worst else '')
        correct = True
    except Exception as exc:
        error = str(exc)
        metrics['text_feedback'] = error
        metrics['private']['error'] = error
    metrics['private']['runtime_seconds'] = round(time.monotonic() - started, 3)
    (out/'metrics.json').write_text(json.dumps(metrics, indent=2)+'\n')
    (out/'correct.json').write_text(json.dumps(dict(correct=correct, error=error), indent=2)+'\n')
    print(json.dumps(dict(correct=correct, **metrics['public'], runtime_seconds=metrics['private']['runtime_seconds'], error=error)))
    return correct


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--program_path', required=True)
    p.add_argument('--results_dir', required=True)
    p.add_argument('--raw', action='store_true', help='Use the original benchmark mapping flow')
    args = p.parse_args()
    evaluate(args.program_path, args.results_dir, args.raw)
