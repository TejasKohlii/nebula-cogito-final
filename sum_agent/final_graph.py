"""Publish UI artifacts after the unmodified Shinka process has exited."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from analysis_stages import commands, emit, run_stage
from pipeline import digest, write


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def atomic_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def timing_closed(winner):
    metrics = winner.get('metrics', {})
    values = [metrics.get(k) for k in ('wns_ns', 'tns_ns', 'violating_endpoints')]
    return (winner.get('verified') is True
            and metrics.get('formal_equivalence') is True and metrics.get('timing_closed') is True
            and all(type(v) in (int, float) and math.isfinite(v) for v in values)
            and values[0] >= 0 and values[1] == 0 and values[2] == 0)


def final_config(config, results, candidate):
    cfg = dict(config)
    # Initial mapping never describes the optimized RTL. Only accept a mapping
    # explicitly located within this winner's fresh verification artifacts.
    cfg.pop('netlist', None)
    if config.get('bundle_spec'):
        from rtl_bundle import unpack
        spec = read_json(Path(config['bundle_spec']))
        exported = candidate.parent / 'rtl'
        files = unpack(candidate.read_text(), spec['editable_files'])
        for name, content in files.items():
            if (exported / name).read_text() != content:
                raise ValueError(f'Exported winner file differs from the verified candidate: {name}')
        golden = Path(spec['golden_root'])
        sources = [str(exported / Path(p).relative_to(golden)) for p in spec['golden_sources']]
        cfg['source_order'] = sources
        cfg['source'] = sources[spec['main_source_index']]
        cfg['source_cwd'] = str(exported)
        cfg['include_dirs'] = [str(exported / Path(p).relative_to(golden)) for p in spec['include_dirs']]
    substitutions = dict(results=str(results), candidate=str(candidate), verification=str(candidate.parent))
    if config.get('final_netlist'):
        netlist = Path(config['final_netlist'].format(**substitutions)).resolve()
        if not netlist.is_relative_to(candidate.parent) or not netlist.is_file():
            raise ValueError('Final netlist must exist inside the selected winner verification directory')
        cfg['netlist'] = str(netlist)
    if config.get('final_design_cwd'):
        cfg['design_cwd'] = config['final_design_cwd'].format(**substitutions)
    return cfg


def finish_run(config, out):
    results = out / 'shinka'
    status = read_json(results / 'status.json', {})
    state = status.get('state', 'missing_status')
    if state != 'success_verified' and not state.startswith('incomplete_'):
        raise RuntimeError(f'Shinka did not finish normally: {state}')
    winner = read_json(results / 'winner.json', {})
    closed = timing_closed(winner)
    if state == 'success_verified' and not closed:
        raise RuntimeError('Success status lacks verified equivalence/setup closure metrics')
    initial = read_json(results / 'gen_0/results/metrics.json', {}).get('public', {})
    attribution = read_json(results / 'attribution.json', {})
    generations = None
    db_path = results / 'programs.sqlite'
    if db_path.is_file():
        with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
            generations = db.execute('SELECT COUNT(DISTINCT generation) FROM programs').fetchone()[0]
    report = dict(schema_version=1, state=state, timing_closed=closed,
                  initial=initial, final=winner.get('metrics', {}),
                  generations=generations, elapsed_seconds=status.get('elapsed_seconds'),
                  reported_api_cost_usd=status.get('reported_api_cost'),
                  native_reported_cost_usd=status.get('native_reported_cost'),
                  budget_accounted_cost_usd=status.get('budget_accounted_cost'),
                  power=attribution.get('power'),
                  ppa_optimization=read_json(results / 'task_manifest.json', {}).get('task', {}).get('optimization', {}).get('mode') == 'timing_then_ppa',
                  optimization_phase=status.get('phase'), stop_reason=status.get('stop_reason'),
                  winner=str(results / 'winner.json') if winner else None,
                  final_graph=None)
    ui = read_json(out / 'ui_outputs.json', {'schema_version': 1, 'outputs': {}, 'errors': {}})
    if winner:
        candidate = Path(winner['candidate']).resolve()
        if (winner.get('verified') is not True
                or not candidate.is_relative_to(results / 'verification')
                or digest(candidate) != winner.get('winner_sha256')):
            raise RuntimeError('Final RTL is not the verified, unchanged Shinka winner')
        final = out / 'final'
        emit('stage_started', stage='final_graph', design=str(candidate), timing_closed=closed)
        try:
            final.mkdir(exist_ok=False)
            cfg = final_config(config, results, candidate)
            argv, cwd = commands(cfg, Path(cfg['source']) if config.get('bundle_spec') else candidate, final)['graph']
            graph = run_stage('graph', argv, cwd, final)
        except Exception as exc:
            ui['errors']['final_graph'] = str(exc)
            atomic_json(out / 'ui_outputs.json', ui)
            report['final_graph_error'] = str(exc)
            atomic_json(out / 'final_report.json', report)
            emit('stage_failed', stage='final_graph', error=str(exc), log=str(final / 'graph.log'))
            raise
        ui['outputs']['final_graph'] = graph
        report['final_graph'] = graph['files']['json']
        report['final_rtl'] = str(candidate)
        if winner.get('source_directory'):
            report['final_rtl_directory'] = winner['source_directory']
            emit('artifact_ready', label='Verified RTL folder', path=winner['source_directory'])
        emit('stage_completed', stage='final_graph', **graph)
        emit('final_graph_ready', timing_closed=closed, design=str(candidate), **graph)
    else:
        emit('final_graph_skipped', reason='No verified winner', state=state)
    handoff = read_json(out / 'handoff.json', {})
    report['total_elapsed_seconds'] = (
        (datetime.now(timezone.utc) - datetime.fromisoformat(handoff['created_at'])).total_seconds()
        if handoff.get('created_at') else None)
    ui['final_report'] = str(out / 'final_report.json')
    atomic_json(out / 'ui_outputs.json', ui)
    write(out / 'final_report.json', report)
    emit('report_ready', report=str(out / 'final_report.json'), timing_closed=closed, state=state)
    return dict(state=state, timing_closed=closed, report=str(out / 'final_report.json'))


def main():
    p = argparse.ArgumentParser(description='Generate final UI outputs for an already completed orchestration run')
    p.add_argument('--run', required=True, type=Path)
    a = p.parse_args()
    out = a.run.resolve()
    handoff = read_json(out / 'handoff.json')
    finish_run(handoff['config'], out)


if __name__ == '__main__':
    main()
