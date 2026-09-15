"""Supplied-benchmark adapter. Search orchestration lives in the generic loop."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'timing_closure'))
from evaluate import DESIGN, FLOW_SCRIPTS, LIB, SCRIPTS, SEED, TASK, START, END, validate_source, run, prove
from implement import implement
sys.path.insert(0, str(ROOT/'shinka_optimizer'))
from task import verify
from ppa_objective import enabled, assess
from adapters.power_window import estimate
from feedback import diagnostics
from equivalence import prove_pair
from adapters.experiment_policy import resolve, measure

TAG = '// SHINKA_IMPLEMENTATION '
DEFAULT = dict(flow='original', abc_fast=False, target_ps=1000, fanout=16)
PPA_OPTIONS = dict(compact_buffers=False, buffer_cell='BUF_X4', domain_targets_ps=[],
                   slow_mapping_domains=[], full_adder_mapping=False, hierarchical_mapping=False)


def settings(source):
    matches = [line.strip()[len(TAG):] for line in source.splitlines() if line.strip().startswith(TAG)]
    if len(matches) > 1:
        raise ValueError('At most one implementation directive is allowed')
    options = json.loads(matches[0]) if matches else dict(DEFAULT)
    if not isinstance(options, dict) or not set(DEFAULT) <= set(options) or set(options) - (set(DEFAULT) | set(PPA_OPTIONS)):
        raise ValueError('Implementation directive needs the four core settings and only known PPA options')
    if options['flow'] not in ('original', 'configured') or type(options['abc_fast']) is not bool:
        raise ValueError('Invalid implementation flow or ABC mode')
    if type(options['target_ps']) is not int or options['target_ps'] not in (250, 500, 750, 1000, 1500):
        raise ValueError('Invalid declared ABC delay target')
    if type(options['fanout']) is not int or options['fanout'] not in (4, 8, 12, 16, 24, 32):
        raise ValueError('Invalid declared fanout setting')
    extra = {**PPA_OPTIONS, **{k: v for k, v in options.items() if k in PPA_OPTIONS}}
    for key in ('compact_buffers', 'full_adder_mapping', 'hierarchical_mapping'):
        if type(extra[key]) is not bool:
            raise ValueError('PPA switches must be boolean')
    if extra['buffer_cell'] not in ('BUF_X1', 'BUF_X2', 'BUF_X4'):
        raise ValueError('Invalid buffer cell')
    targets, slow = extra['domain_targets_ps'], extra['slow_mapping_domains']
    if not isinstance(targets, list) or (targets and (len(targets) != 5 or any(type(t) is not int or not 250 <= t <= 2000 for t in targets))):
        raise ValueError('Expected five domain targets in 250..2000 ps')
    if not isinstance(slow, list) or any(type(i) is not int or i not in range(5) for i in slow) or len(set(slow)) != len(slow) or (slow and not targets):
        raise ValueError('Invalid slow mapping domains')
    if targets and extra['hierarchical_mapping']:
        raise ValueError('Choose domain mapping or retained hierarchy')
    if options['flow'] == 'original' and extra != PPA_OPTIONS:
        raise ValueError('PPA mapping options require configured flow')
    return options


def rtl_digest(source):
    code = '\n'.join(line for line in source.splitlines() if not line.strip().startswith((TAG, '// SHINKA_HYPOTHESIS ')))
    return hashlib.sha256(code.encode()).hexdigest()


def proof(work):
    return prove_pair(DESIGN/'rtl/benchmark.sv', work/'rtl/benchmark.sv', 'window_engine', work)


def evaluate(program, results, force_options=None, skip_frozen=False):
    out = Path(results).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out/'design'
    for folder in ('rtl', 'reports', 'build', 'scripts', 'constraints'):
        (work/folder).mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    metrics = dict(combined_score=0., public={}, private={}, extra_data={}, text_feedback='')
    correct = False
    error = ''
    stage = 'contract'
    source = Path(program).read_text()
    try:
        task = verify(os.environ['SHINKA_TASK_MANIFEST'])['task'] if not skip_frozen else {}
        validate_source(source, SEED.read_text())
        options = settings(source) if force_options is None else force_options
        stage = 'experiment_policy'
        context = resolve(program, source, os.environ['SHINKA_TASK_MANIFEST'], settings, rtl_digest) if not skip_frozen and force_options is None else None
        for name in FLOW_SCRIPTS:
            shutil.copy2(SCRIPTS/name, work/'scripts'/name)
        shutil.copy2(DESIGN/'constraints/benchmark.sdc', work/'constraints/benchmark.sdc')
        (work/'rtl/benchmark.sv').write_text(source)
        stage = 'syntax_or_elaboration'
        (work/'syntax.ys').write_text('read_verilog -sv rtl/benchmark.sv\nhierarchy -check -top benchmark\n')
        run(['yosys', '-Q', 'syntax.ys'], work, 'syntax.log', 20)
        stage = 'formal_inconclusive'
        proof_result = proof(work)
        # Retain the completed proof verdict if a later implementation step fails.
        metrics['public']['formal_equivalence'] = True
        stage = 'implementation'
        if options['flow'] == 'original':
            abc_lib = Path('/tmp') / ('cogito_nangate_' + hashlib.sha256(LIB.read_bytes()).hexdigest()[:16] + '.lib')
            if not abc_lib.exists():
                shutil.copy2(LIB, abc_lib)
            run([sys.executable, 'scripts/synth.py', '--lib', str(abc_lib)], work, 'synthesis.log', 240)
            stage = 'timing_analysis'
            run([sys.executable, 'scripts/run_sta.py', '--sta', os.environ.get('STA', shutil.which('sta')),
                 '--lib', str(LIB)], work, 'sta.log', 120)
        else:
            implement(Path(program).resolve(), work, fanout=options['fanout'],
                      target_ps=options['target_ps'], fast=options['abc_fast'],
                      **{k: v for k, v in options.items() if k in PPA_OPTIONS})
        timing = json.loads((work/'reports/timing_summary.json').read_text())
        cells = json.loads((work/'reports/cell_count.json').read_text())
        violations = (work/'reports/violating_endpoints.tsv').read_text().splitlines()[1:]
        groups = timing['worst_reported_path_by_group']
        slack = min(v['worst_slack_ns'] for v in groups.values())
        if not math.isfinite(slack) or not math.isfinite(timing['tns_ns']):
            raise ValueError('Nonfinite timing values')
        required = {f'm{i}' for i in range(5)} | {f'g{i}_{d}' for i in range(5) for d in (2,4)}
        if timing['clock_count'] != 15 or timing['constraint_warnings'] or not required <= groups.keys():
            raise ValueError('Missing clock coverage or invalid constraints')
        if cells['standard_cells'] < 50000:
            raise ValueError('Functional cell floor not met')
        closed = slack >= 0 and timing['tns_ns'] == 0 and not violations
        metrics['public'] = dict(formal_equivalence=True, wns_ns=slack, tns_ns=timing['tns_ns'],
            standard_cells=cells['standard_cells'], cell_area_um2=cells['mapped_cell_area'],
            violating_endpoints=len(violations), timing_closed=closed,
            clock_count=15, constraint_warnings=0)
        metrics['combined_score'] = (1 + .1/(1+cells['mapped_cell_area']/50000)
            + .01*min(slack/.1,1)) if closed else .99/(1+max(0,-slack)+max(0,-timing['tns_ns'])/100)
        power = estimate(work, LIB) if enabled(task) else None
        if power is not None:
            metrics['combined_score'] = assess(metrics['public'], task, power)
        measured = diagnostics(work, top='benchmark')
        measured['power'] = power
        measured['metrics'] = metrics['public']
        measured['implementation'] = options
        measured['rtl_hash'] = rtl_digest(source)
        hypothesis = measure(context, metrics['public'], measured) if context else None
        if hypothesis:
            (out/'hypothesis.json').write_text(json.dumps(hypothesis, indent=2)+'\n')
        # Keep exact path text separately, allowing complete focused reads.
        (out/'critical_paths.txt').write_text('\n\n'.join(
            p.pop('raw_path') for p in measured['critical_paths'])+'\n')
        (out/'diagnostics.json').write_text(json.dumps(measured, indent=2)+'\n')
        metrics['extra_data'] = dict(implementation=options, proof=proof_result,
            rtl_without_implementation_directive_sha256=rtl_digest(source),
            stage=timing['stage'], power=power, objective=task.get('optimization', 'setup_and_tns_with_explicit_implementation_v1'),
            rtl_sha256=hashlib.sha256(source.encode()).hexdigest(),
            sdc_sha256=hashlib.sha256((work/'constraints/benchmark.sdc').read_bytes()).hexdigest())
        # A compact summary is always supplied; full diagnostics are available on demand.
        metrics['text_feedback'] = json.dumps(dict(implementation=options,
            hypothesis_verdict=hypothesis, metrics=metrics['public'], power=power,
            rtl_hash=rtl_digest(source), critical_paths=measured['critical_paths'][:2],
            note='Full cell paths are staged separately. Check mapped depth and same-RTL mapping experiments; tighter knobs need not improve timing.'), indent=2)[:8500]
        correct = True
    except Exception as exc:
        error = str(exc)
        # Preserve full logs, but send a short, actionable terminal failure.
        lines = error.splitlines()
        terminal = [line for line in lines if 'ERROR:' in line or 'timed out' in line or 'unproven' in line]
        brief = '\n'.join(terminal[-8:]) if terminal else error[-2000:]
        metrics['text_feedback'] = f'Failure stage: {stage}\n{brief}\nNo admissible timing score. '
        if stage == 'formal_inconclusive':
            metrics['text_feedback'] += 'Equivalence was not established; this is not a proved counterexample.'
        metrics['private']['failure_stage'] = stage
        (out/'diagnostics.json').write_text(json.dumps(dict(failure_stage=stage, reason=brief), indent=2)+'\n')
    metrics['private']['runtime_seconds'] = time.monotonic()-started
    (out/'metrics.json').write_text(json.dumps(metrics, indent=2)+'\n')
    (out/'correct.json').write_text(json.dumps(dict(correct=correct, error=error), indent=2)+'\n')
    return correct, metrics


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--program_path', required=True)
    p.add_argument('--results_dir', required=True)
    a = p.parse_args()
    evaluate(a.program_path, a.results_dir)
