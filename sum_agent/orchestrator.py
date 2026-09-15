#!/usr/bin/env python3
"""Analyze initial RTL, run one timing-first PPA Shinka task, then graph its verified winner."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from pipeline import ROOT, digest, write
from analysis_stages import emit, run_analyses
from design_config import load_config, task_contract
from final_graph import finish_run
from prepare_benchmark import prepare_benchmark


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--design', required=True, type=Path)
    p.add_argument('--config', type=Path, help='Design config; folders auto-discover cogito.json')
    p.add_argument('--out', type=Path, help='New output directory; must be inside task workspace for optimization')
    p.add_argument('--budget', required=True, type=Decimal, help='Total USD; retains the previous Shinka budget allocation')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--prepare-only', action='store_true')
    mode.add_argument('--analysis-only', action='store_true')
    p.add_argument('--max-generations', type=int, default=24)
    p.add_argument('--max-hours', type=float, default=1)
    a = p.parse_args()
    if not a.budget.is_finite() or a.budget < Decimal('0.50'):
        p.error('Budget must be finite and at least $0.50')
    import math
    if a.max_generations < 2 or not math.isfinite(a.max_hours) or a.max_hours <= 0:
        p.error('Invalid generation/time limits')
    design = a.design.resolve()
    default_preset = a.config is None and not design.is_dir()
    if a.config is None:
        a.config = design / 'cogito.json' if design.is_dir() else ROOT / 'sum_agent/design.json'
    try:
        config = load_config(a.config)
        if default_preset and design != Path(config.get('source', '')):
            raise ValueError('Uploaded RTL files require --config; or select a folder containing cogito.json')
        if design.is_dir():
            if not config.get('source'):
                raise ValueError('Folder config requires source (the main RTL file)')
            design = Path(config['source'])
        if not design.is_file():
            raise ValueError('Design must be an existing RTL file or a folder containing cogito.json')
        task, hashes = (None, {}) if a.analysis_only else task_contract(config, design)
        if task and task.get('connection') == 'api' and not os.environ.get('ANTHROPIC_API_KEY', '').strip():
            raise ValueError('Anthropic API mode requires an API key')
    except (ValueError, KeyError, OSError) as exc:
        p.error(str(exc))
    # Preserve previous search allocation so replacing the launch-only LLM does
    # not silently change the Shinka search budget. The reserve is now unspent.
    reserve = min(Decimal('0.25'), a.budget / 10)
    shinka_budget = a.budget - reserve
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    workspace = Path(task['workspace']) if task else ROOT
    out = a.out.resolve() if a.out else workspace / 'sum_agent/runs' / f'launch_{stamp}_{uuid.uuid4().hex[:8]}'
    if task and (not out.is_relative_to(workspace) or out == workspace):
        p.error('--out must be a dedicated directory inside the task workspace')
    if out.exists():
        p.error('--out must be a new directory')
    command = [sys.executable, '-u', str(ROOT / 'shinka_optimizer/run.py'),
               '--task', config.get('task', ''), '--results', str(out / 'shinka'),
               '--max-api-cost', str(shinka_budget), '--max-generations', str(a.max_generations),
               '--max-hours', str(a.max_hours), '--allow-design-upload']
    for path in (design, a.config.resolve(), Path(config['sdc']), Path(config['liberty'])):
        hashes[str(path)] = digest(path)
    for source in config.get('additional_sources', []):
        hashes[source] = digest(source)
    if config.get('netlist'):
        hashes[config['netlist']] = digest(config['netlist'])
    record = {'role': 'Python lifecycle; initial analysis, integrated Shinka, final graph',
              'created_at': datetime.now(timezone.utc).isoformat(),
              'python': sys.version, 'python_executable': sys.executable,
              'design': str(design), 'total_budget_usd': str(a.budget), 'sum_budget_usd': '0',
              'unspent_reserve_usd': str(reserve), 'shinka_budget_usd': str(shinka_budget),
              'launch_argv': command, 'results': str(out / 'shinka'),
              'log': str(out / 'shinka.launch.log'), 'hashes': hashes,
              'config': config, 'ppa_optimization': json.loads(Path(config['task']).read_text()).get('optimization', {}).get('mode') == 'timing_then_ppa'}
    if a.prepare_only:
        print(json.dumps(record, indent=2))
        return
    out.mkdir(parents=True, exist_ok=False)
    write(out / 'handoff.json', record)
    emit('run_created', run_dir=str(out), design=str(design), budget_usd=str(a.budget))
    try:
        if config.get('prepare_benchmark'):
            emit('stage_started', stage='baseline')
            started = time.monotonic()
            try:
                prepare_benchmark(config, out)
            except Exception as exc:
                emit('stage_failed', stage='baseline', error=str(exc), log=str(out / 'baseline.log'))
                raise RuntimeError(f'Initial mapping failed; see {out / "baseline.log"}') from exc
            (out / 'handoff.json').write_text(json.dumps(record, indent=2) + '\n')
            emit('stage_completed', stage='baseline', files={'log': str(out / 'baseline.log')},
                 runtime_seconds=round(time.monotonic() - started, 3))
        run_analyses(config, design, out)
        if a.analysis_only:
            emit('finished', launched=False, reason='analysis_only')
            return
        for name, expected in hashes.items():
            if digest(name) != expected:
                raise RuntimeError(f'Input changed since preparation: {name}')
        emit('launch_started')
        with (out / 'launch.claim').open('x') as claim:
            claim.write('Single launch; never retry this directory.\n')
        with (out / 'shinka.launch.log').open('x') as log:
            child = subprocess.Popen(command, cwd=workspace, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            receipt = {'pid': child.pid, 'log': str(out / 'shinka.launch.log')}
            write(out / 'launch_receipt.json', receipt)
            emit('shinka_launched', **receipt, results=str(out / 'shinka'))
            returncode = child.wait()
        if returncode:
            raise RuntimeError(f'Shinka exited with code {returncode}; inspect shinka.launch.log')
        result = finish_run(config, out)
        emit('finished', launched=True, **result)
    except Exception as exc:
        emit('failed', error=str(exc), run_dir=str(out))
        record['error'] = str(exc)
        raise SystemExit(1)
    finally:
        from reports import run_reports
        try:
            files = run_reports(out, record.get('error', ''))
            emit('tool_reports_ready', files=files, label='Run reports')
        except Exception as exc:
            emit('artifact_ready', label='Report generation error; inspect run logs', path=str(out), error=str(exc))


if __name__ == '__main__':
    main()
