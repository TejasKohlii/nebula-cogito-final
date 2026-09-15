"""Final verification and attribution for Shinka's chosen RTL/implementation pair."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task import verify
from ppa_objective import enabled
from window_engine import evaluate, settings, DEFAULT, DESIGN, ROOT, SEED, TASK, LIB, run
from power_window import estimate
from native_usage import totals


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', required=True)
    a = p.parse_args()
    result = Path(a.run).resolve()
    manifest = verify(result/'task_manifest.json')
    os.environ['SHINKA_TASK_MANIFEST'] = str(result/'task_manifest.json')
    summary = json.loads((result/'winner.json').read_text())
    candidate = Path(summary['candidate'])
    options = settings(candidate.read_text())
    out = result/'attribution'
    measurements = {}
    for label, source, flow in [('original_rtl_original_flow', SEED, DEFAULT),
        ('winner_rtl_original_flow', candidate, DEFAULT),
        ('original_rtl_winner_flow', SEED, options)]:
        ok, metrics = evaluate(source, out/label, force_options=flow)
        if not ok:
            raise RuntimeError('Attribution evaluation failed: '+label)
        measurements[label] = metrics['public']
    measurements['winner_rtl_winner_flow'] = summary['metrics']
    sim = out/'simulation'
    sim.mkdir(parents=True, exist_ok=True)
    verilator = os.environ.get('VERILATOR') or shutil.which('verilator')
    if not verilator:
        raise RuntimeError('Verilator is required for final simulation; set VERILATOR or add it to PATH')
    # Verilator's generated Makefiles cannot handle workspace paths with spaces.
    with tempfile.TemporaryDirectory(prefix='shinka_sim_') as temporary:
        scratch = Path(temporary)
        shutil.copy2(candidate, scratch/'candidate.sv')
        shutil.copy2(DESIGN/'tests/tb.sv', scratch/'tb.sv')
        try:
            run([verilator, '--binary', '--timing', '--assert', '-Wno-TIMESCALEMOD', '-j', '2',
                 '--top-module', 'tb', '--Mdir', 'obj', 'candidate.sv', 'tb.sv'], scratch, 'build.log', 240)
            run([str(scratch/'obj/Vtb')], scratch, 'run.log', 60)
        finally:
            for name in ('build.log', 'run.log'):
                if (scratch/name).exists():
                    shutil.copy2(scratch/name, sim/name)
    if 'PASS: full-size rolling windows' not in (sim/'run.log').read_text():
        raise RuntimeError('Full-design simulation did not complete successfully')
    report = dict(measurements=measurements, implementation=options, simulation=True,
        ppa_optimization=enabled(manifest['task']), optimization=manifest['task'].get('optimization'),
        source='Official Shinka winner', limitation='Pre-layout setup only; no physical signoff or workload power')
    verification = candidate.parent/'results/design'
    report['power'] = dict(baseline=estimate(out/'original_rtl_original_flow/design', LIB),
                         winner=estimate(verification, LIB))
    report['cost'] = totals(result)
    failures = []
    evaluated_seconds = 0.
    for path in sorted(result.glob('gen_*/results/metrics.json')):
        m = json.loads(path.read_text())
        evaluated_seconds += m.get('private', {}).get('runtime_seconds', 0)
        if m.get('private', {}).get('failure_stage'):
            failures.append(dict(generation=path.parents[1].name, stage=m['private']['failure_stage'],
                                 feedback=m.get('text_feedback', '')))
    report['evaluation_seconds'] = evaluated_seconds
    report['failures'] = failures
    report['delivery_gaps'] = ['Cross-design scalability and convergence not demonstrated',
                               'Workload-dependent power not measured; uniform vectorless estimate only']
    if not all(p['valid'] for p in report['power'].values()):
        report['delivery_gaps'].append('Vectorless power comparison failed')
    (result/'attribution.json').write_text(json.dumps(report, indent=2)+'\n')
    lines = ['# Shinka result and gain attribution', '',
        '| Configuration | WNS ns | TNS ns | Cells | Area µm² | Vectorless W |', '|---|---:|---:|---:|---:|---:|']
    for label, m in measurements.items():
        lines.append(f"| {label} | {m['wns_ns']} | {m['tns_ns']} | {m['standard_cells']} | {m['cell_area_um2']} | {m.get('power_w', 'UNAVAILABLE')} |")
    lines += ['', 'Optimization policy: `'+json.dumps(manifest['task'].get('optimization'))+'`. '+
              'Timing is mandatory; among closed candidates the area/power geometric mean determines fitness. '+
              'A plateau or budget stop is not proof of optimality.', '', 'Selected implementation: `'+json.dumps(options)+'`.', '',
              'Fresh equivalence, STA and full-design multiclock simulation passed.', '',
              'Setup scope is pre-layout with ideal clocks and the unchanged supplied SDC.']
    baseline = measurements['original_rtl_original_flow']
    winner = summary['metrics']
    lines += ['', '## PPA, frequency and turnaround', '',
        'Master targets remain 1.0 / 1.2 / 0.8 / 0.65 / 0.5 GHz; /2 and /4 generated clocks remain constrained. '
        'These are fixed target frequencies, not measured Fmax.', '',
        f"Cell-area change: {100*(winner['cell_area_um2']/baseline['cell_area_um2']-1):+.2f}%.",
        f"Violating endpoint count: {baseline['violating_endpoints']} -> {winner['violating_endpoints']} "
        '(count reduction, not a tracked issue-identity fix rate).',
        f"Native API-equivalent usage: ${report['cost']['native_reported_cost']:.4f}; "
        f"candidate evaluation runtime sum: {evaluated_seconds:.1f}s. See status.json for campaign elapsed time.", '',
        '| Power estimate | Total W |', '|---|---:|']
    for label, power in report['power'].items():
        lines.append(f"| {label} | {power['watts']['total'] if power['valid'] else 'UNAVAILABLE'} |")
    lines += ['', 'Power assumes global uniform 0.1 transitions/ns, duty 0.5, inactive reset, '
        '0.03 ns input transitions and no extracted parasitics. It is not workload or signoff power.', '',
        '## Formal and benchmark evidence', '',
        'The default-size window_engine is sequentially proved after async2sync and arithmetic normalization; '
        'all output obligations must pass. Internal-name retries reconstruct fresh proofs. '
        'Outside-region RTL (five master domains, CDC mailboxes, reset and /2,/4 dividers) is immutable. '
        'Full-design simulation complements this proof; it is not asynchronous-event equivalence.', '',
        f'Proof: {verification}/proof_summary.json',
        f'Mapped netlist: {verification}/build/benchmark_mapped.v',
        f'Timing paths: {verification}/reports/timing.log',
        f'Simulation: {sim}/run.log', '',
        '## Failure cases and outstanding scope', '',
        *[f"- {f['generation']}: {f['stage']}. {f['feedback'][:700]}" for f in failures],
        *[f'- {gap}.' for gap in report['delivery_gaps']], '',
        'Proof timeouts need simpler state invariants or additional solver capacity; '
        'mapping regressions need same-RTL comparisons; workload power needs representative switching activity. '
        'The orchestration is reusable, but this benchmark/PDK adapter is specialized.', '',
        'Detailed results and candidate RTL are available in the Cogito UI and this run directory.']
    (result/'RESULT.md').write_text('\n'.join(lines)+'\n')


if __name__ == '__main__':
    main()
