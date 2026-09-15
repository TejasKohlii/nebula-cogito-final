#!/usr/bin/env python3
"""Generic full-design evaluator for algorithmically prepared RTL uploads."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import shutil
import tempfile
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'shinka_optimizer'))
sys.path.insert(0, str(ROOT / 'rtl_graph'))
sys.path.insert(0, str(ROOT / 'sum_agent'))
from equivalence import prove_sources
from rtl_graph.sta_query import run_query
from rtl_graph.netlist_verilog import parse_liberty_cells
from upload_constraints import materialize
from task import verify
from ppa_objective import enabled, assess
from adapters.power_window import estimate
from rtl_bundle import materialize as materialize_rtl
from reports import evaluation_reports
from rtl_graph.yosys_frontend import read_sources


def quoted(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def read_commands(sources, include_dirs, defines, frontend='verilog', top=None):
    return read_sources(sources, include_dirs, defines, frontend=frontend, top=top)


def run_yosys(script, work, name, yosys, timeout=240, cwd=None):
    script_path = work / f'{name}.ys'
    log_path = work / f'{name}.log'
    script_path.write_text('\n'.join(script) + '\n')
    result = subprocess.run([yosys, '-q', '-l', str(log_path), '-s', str(script_path)],
                            cwd=cwd or work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout)
    if result.returncode:
        detail = log_path.read_text(errors='replace')[-3000:] if log_path.is_file() else result.stdout[-3000:]
        raise RuntimeError(f'Yosys {name} failed:\n{detail}')
    return log_path


def mapped_metrics(document, top, synthesis_log):
    modules = document['modules']
    counts = Counter()
    visiting = set()

    def walk(module_name):
        if module_name in visiting:
            raise RuntimeError(f'Recursive module hierarchy at {module_name}')
        visiting.add(module_name)
        for cell in modules[module_name].get('cells', {}).values():
            kind = cell['type']
            target = modules.get(kind)
            blackbox = target and int(target.get('attributes', {}).get('blackbox', '0'), 2)
            if target and not blackbox:
                walk(kind)
            elif target or not kind.startswith('$'):
                counts[kind] += 1
            else:
                raise RuntimeError(f'Unmapped synthesis cell remains: {kind}')
        visiting.remove(module_name)

    walk(top)
    text = synthesis_log.read_text(errors='replace')
    area = re.findall(r'Chip area for (?:top module|module)[^:]*:\s*([0-9.]+)', text)
    return sum(counts.values()), (float(area[-1]) if area else None), dict(sorted(counts.items()))


def timing_metrics(sta_dir):
    rows = []
    with (sta_dir / 'endpoints.tsv').open(newline='') as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            try:
                slack = float(row['slack'])
            except (TypeError, ValueError):
                continue
            if math.isfinite(slack):
                row['slack'] = slack
                rows.append(row)
    if not rows:
        raise RuntimeError('OpenSTA found no constrained setup paths')
    slacks = [row['slack'] for row in rows]
    violating = [row for row in rows if row['slack'] < 0]
    clocks = []
    with (sta_dir / 'clocks.tsv').open(newline='') as stream:
        clocks = list(csv.DictReader(stream, delimiter='\t'))
    return {
        'wns_ns': min(slacks),
        'tns_ns': sum(value for value in slacks if value < 0),
        'violating_endpoints': len(violating),
        'clock_count': len(clocks),
        'worst_paths': sorted(rows, key=lambda row: row['slack'])[:8],
    }


def evaluate(program, results):
    out = Path(results).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / 'design'
    build = work / 'build'
    sta_dir = work / 'sta'
    build.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    metrics = {'combined_score': 0.0, 'public': {}, 'private': {},
               'extra_data': {}, 'text_feedback': ''}
    correct = False
    error = ''
    stage = 'contract'
    try:
        manifest = verify(os.environ['SHINKA_TASK_MANIFEST'])
        spec_path = Path(os.environ['COGITO_GENERIC_SPEC']).resolve()
        if str(spec_path) not in manifest['hashes']:
            raise ValueError('Generated design specification is not frozen by the task')
        spec = json.loads(spec_path.read_text())
        candidate = Path(program).resolve()
        golden = [Path(item) for item in spec['golden_sources']]
        golden_includes = list(map(Path, spec.get('include_dirs', [])))
        if spec.get('candidate_format') == 'rtl_bundle_v1':
            candidate_sources, include_dirs = materialize_rtl(spec, candidate, work / 'source')
            source_cwd = work / 'source'
        else:
            candidate_sources = list(golden)
            candidate_sources[int(spec.get('main_source_index', 0))] = candidate
            include_dirs = golden_includes
            source_cwd = work
        defines = spec.get('defines', [])
        top = spec['top']
        frontend = spec.get('frontend', 'verilog')
        yosys = os.environ.get('YOSYS') or spec.get('yosys', 'yosys')
        sta = os.environ.get('STA') or spec.get('sta', 'sta')

        stage = 'syntax_or_elaboration'
        syntax = [*read_commands(candidate_sources, include_dirs, defines, frontend, top),
                  f'hierarchy -check -top {top}', 'proc', 'check -assert']
        run_yosys(syntax, work, 'syntax', yosys, 60, cwd=source_cwd)

        stage = 'formal_equivalence'
        proof = prove_sources(golden, candidate_sources, top, work / 'equivalence',
                              include_dirs=golden_includes, candidate_include_dirs=include_dirs, defines=defines,
                              yosys=yosys, timeout=180, gold_cwd=spec.get('golden_root'), candidate_cwd=source_cwd,
                              frontend=frontend)

        stage = 'synthesis'
        liberty = Path(spec['liberty']).resolve()
        # ABC does not reliably quote Liberty paths containing spaces.
        abc_lib = Path(tempfile.gettempdir()) / ('cogito_lib_' + hashlib.sha256(liberty.read_bytes()).hexdigest()[:16] + '.lib')
        if not abc_lib.exists():
            shutil.copy2(liberty, abc_lib)
        netlist = build / 'design_mapped.v'
        netlist_json = build / 'design_mapped.json'
        synth = [f'read_liberty -lib {quoted(liberty)}',
                 *read_commands(candidate_sources, include_dirs, defines, frontend, top),
                 f'hierarchy -check -top {top}', f'synth -top {top} -noabc',
                 'check -assert', f'dfflibmap -liberty {quoted(liberty)}',
                 f'abc -liberty {quoted(abc_lib)}', 'clean', 'opt_clean -purge',
                 'rename -enumerate', 'check -assert',
                 f'stat -liberty {quoted(liberty)}',
                 f'write_verilog -noattr -noexpr {quoted(netlist)}',
                 f'write_json {quoted(netlist_json)}']
        synthesis_log = run_yosys(synth, work, 'synthesis', yosys, 300, cwd=source_cwd)
        document = json.loads(netlist_json.read_text())
        cell_count, area, cells_by_type = mapped_metrics(document, top, synthesis_log)

        stage = 'timing_analysis'
        if spec.get('constraint_plan'):
            materialize(spec['constraint_plan'], document, top, parse_liberty_cells([str(liberty)]), work)
        run_query(str(sta_dir), [str(liberty)], [str(netlist)], top, spec['sdc'],
                  sta=sta, cwd=str(work) if spec.get('constraint_plan') else spec.get('design_cwd') or str(Path(spec['sdc']).parent), critical_per_group=10,
                  only_violating=False, timeout=180, dump_registers=False)
        sta_log = (sta_dir / 'sta.log').read_text(errors='replace')
        failures = [line for line in sta_log.splitlines() if re.search(r'\b(Error:|Warning:)', line)]
        if failures:
            raise RuntimeError('OpenSTA constraint validation failed:\n' + '\n'.join(failures[:30]))
        timing = timing_metrics(sta_dir)
        closed = timing['wns_ns'] >= 0 and timing['tns_ns'] == 0 and timing['violating_endpoints'] == 0
        public = {
            'formal_equivalence': True,
            'wns_ns': timing['wns_ns'],
            'tns_ns': timing['tns_ns'],
            'violating_endpoints': timing['violating_endpoints'],
            'timing_closed': closed,
            'standard_cells': cell_count,
            'cell_area_um2': area,
            'clock_count': timing['clock_count'],
            'constraint_warnings': 0,
        }
        if closed:
            score = 1.0 + (0.05 / (1.0 + (area or cell_count or 1) / 10000.0))
            score += 0.01 * min(max(timing['wns_ns'], 0) / 0.1, 1.0)
        else:
            score = 0.99 / (1.0 + max(0.0, -timing['wns_ns'])
                            + max(0.0, -timing['tns_ns']) / 100.0)
        power = None
        if enabled(manifest['task']):
            power = estimate(work, liberty, netlist=netlist, top=top, sdc=spec['sdc'],
                             cwd=str(work) if spec.get('constraint_plan') else spec.get('design_cwd') or str(Path(spec['sdc']).parent),
                             reset_port=None, sta=sta)
            score = assess(public, manifest['task'], power)
        metrics['combined_score'] = score
        metrics['public'] = public
        metrics['extra_data'] = {
            'top': top, 'proof': proof, 'power': power, 'objective': manifest['task'].get('optimization'),
            'rtl_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
            'sdc_sha256': hashlib.sha256(Path(spec['sdc']).read_bytes()).hexdigest(),
            'liberty_sha256': hashlib.sha256(liberty.read_bytes()).hexdigest(),
            'constraint_source': spec.get('constraint_source'),
            'cells_by_type': cells_by_type,
        }
        feedback = {
            'top': top, 'metrics': public, 'power': power,
            'timing': {key: public[key] for key in ('wns_ns', 'tns_ns', 'violating_endpoints', 'timing_closed')},
            'worst_paths': timing['worst_paths'],
            'note': 'Every accepted score follows fresh full-design equivalence, synthesis, and OpenSTA.',
        }
        metrics['text_feedback'] = json.dumps(feedback, indent=2)[:9000]
        (out / 'diagnostics.json').write_text(json.dumps(feedback, indent=2) + '\n')
        critical = sta_dir / 'critical.rpt'
        if critical.is_file():
            (out / 'critical_paths.txt').write_text(critical.read_text(errors='replace'))
        correct = True
    except Exception as exc:
        error = str(exc)
        metrics['text_feedback'] = f'Failure stage: {stage}\n{error[-3000:]}\nNo admissible timing score.'
        metrics['private']['failure_stage'] = stage
        (out / 'diagnostics.json').write_text(json.dumps({'failure_stage': stage, 'reason': error}, indent=2) + '\n')
    metrics['private']['runtime_seconds'] = round(time.monotonic() - started, 3)
    (out / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    (out / 'correct.json').write_text(json.dumps({'correct': correct, 'error': error}, indent=2) + '\n')
    evaluation_reports(out)
    print(json.dumps({'correct': correct, **metrics['public'],
                      'runtime_seconds': metrics['private']['runtime_seconds'], 'error': error}))
    return correct, metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--program_path', required=True)
    parser.add_argument('--results_dir', required=True)
    args = parser.parse_args()
    evaluate(args.program_path, args.results_dir)
