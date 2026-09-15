"""Rebuild a preset's initial mapping and constraints inside its new run."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def prepare_benchmark(config, out):
    if not config.get('prepare_benchmark'):
        return
    source = Path(config['design_cwd'])
    work = out / 'baseline/design'
    for name in ('rtl', 'constraints', 'scripts'):
        shutil.copytree(source / name, work / name)
    env = dict(os.environ, YOSYS=config['yosys'], STA=config['sta'])
    with (out / 'baseline.log').open('w') as log:
        for argv in ([sys.executable, 'scripts/synth.py', '--lib', config['liberty']],
                     [sys.executable, 'scripts/run_sta.py', '--sta', config['sta'],
                      '--lib', config['liberty']]):
            subprocess.run(argv, cwd=work, env=env, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=720)
    metrics = json.loads((work / 'reports/timing_summary.json').read_text())
    if metrics['clock_count'] != 15 or metrics['constraint_warnings']:
        raise RuntimeError('Initial preset mapping has invalid clock constraints; see baseline.log')
    config['netlist'] = str(work / 'build/benchmark_mapped.v')
    config['design_cwd'] = str(work)
    config['sdc'] = str(work / 'constraints/benchmark.sdc')

