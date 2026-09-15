"""Independent graph/heuristics subprocesses; JSON events for a Node caller."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import sys
import time

from pipeline import ROOT


def emit(event, **payload):
    print(json.dumps({'event': event, **payload}), flush=True)


def commands(config, design, out):
    sources = list(config.get('source_order') or [])
    if sources:
        index = int(config.get('main_source_index', 0))
        if index < 0 or index >= len(sources):
            raise ValueError('main_source_index is outside source_order')
        sources[index] = str(design)
    else:
        sources = [str(design), *config.get('additional_sources', [])]
    common = ['--top', config['top'], '--sdc', str(ROOT / config['sdc']),
              '--yosys', config['yosys']]
    if config.get('source_cwd'):
        common += ['--source-cwd', config['source_cwd']]
    for source in sources:
        common += ['--source', source]
    for directory in config.get('include_dirs', []):
        common += ['-I', directory]
    for define in config.get('defines', []):
        common += ['-D', define]
    common += ['--frontend', config.get('frontend', 'verilog')]
    for parameter in config.get('parameters', []):
        common += ['--chparam', parameter]
    graph = [sys.executable, '-B', str(ROOT / 'rtl_graph/rtl_graph.py'), 'run', *common,
             '--liberty', str(ROOT / config['liberty']),
             '--cwd', str(ROOT / config['design_cwd']), '--sta', config['sta'],
             '--slack-checks', 'data', '--out', str(out / 'graph')]
    heuristics = [sys.executable, '-B', '-m', 'rtl_heuristics', 'analyze', *common,
                  '--sdc-cwd', str(ROOT / config['design_cwd']),
                  '--out', str(out / 'heuristics'), '--quiet']
    if config.get('netlist'):
        graph += ['--netlist', str(ROOT / config['netlist'])]
        heuristics += ['--netlist', str(ROOT / config['netlist'])]

    return {'graph': (graph, ROOT), 'heuristics': (heuristics, ROOT / 'rtl_heuristics')}


def run_stage(name, argv, cwd, out):
    start = time.monotonic()
    with (out / f'{name}.log').open('x') as log:
        subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=900)
    if name == 'graph':
        files = {'json': out / 'graph/graph.json', 'svg': out / 'graph/graph.svg',
                 'summary': out / 'graph/summary.txt'}
        graph = json.loads(files['json'].read_text())
        if '--netlist' in argv and not (out / 'graph/sta/endpoints.tsv').is_file():
            raise RuntimeError('Graph STA output missing')
        summary = dict(graph['stats'], timing_basis=('mapped_netlist_sta' if '--netlist' in argv else 'rtl_topology_only'))
    else:
        files = {'analysis': out / 'heuristics/analysis.json',
                 'feedback': out / 'heuristics/feedback.json',
                 'report': out / 'heuristics/report.md'}
        analysis = json.loads(files['analysis'].read_text())
        json.loads(files['feedback'].read_text())
        summary = {'issues': len(analysis['issues']), 'timing_basis': 'heuristic_estimates',
                   'sta_correlated': False}
    for path in files.values():
        if not path.is_file():
            raise RuntimeError(f'Missing output: {path}')
    return {'files': {key: str(value) for key, value in files.items()},
            'summary': summary, 'runtime_seconds': round(time.monotonic() - start, 3)}


def run_analyses(config, design, out):
    specs = commands(config, design, out)
    outputs, errors = {}, {}
    # Only the coordinating thread emits events, avoiding interleaved JSON lines.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        for name, (argv, cwd) in specs.items():
            emit('stage_started', stage=name)
            futures[pool.submit(run_stage, name, argv, cwd, out)] = name
        for future in as_completed(futures):
            name = futures[future]
            try:
                outputs[name] = future.result()
                emit('stage_completed', stage=name, **outputs[name])
            except Exception as exc:
                errors[name] = str(exc)
                emit('stage_failed', stage=name, error=str(exc), log=str(out / f'{name}.log'))
    result = {'schema_version': 1, 'outputs': outputs, 'errors': errors,
              'passed_to_shinka': False}
    with (out / 'ui_outputs.json').open('x') as stream:
        json.dump(result, stream, indent=2)
    if errors:
        raise RuntimeError('Analysis stage failed; Shinka was not launched')
    emit('analysis_ready', manifest=str(out / 'ui_outputs.json'), outputs=outputs)
    return result
