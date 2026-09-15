"""Resolve per-design inputs without a design registry or machine-specific paths."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

from pipeline import ROOT
sys.path.insert(0, str(ROOT / 'shinka_optimizer'))


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    base = (path.parent / config.get('path_base', '.')).resolve()
    for key in ('source', 'task', 'sdc', 'liberty', 'netlist', 'design_cwd', 'bundle_spec', 'source_cwd'):
        if config.get(key):
            config[key] = str((base / config[key]).resolve())
    for key in ('additional_sources', 'include_dirs', 'source_order'):
        config[key] = [str((base / item).resolve()) for item in config.get(key, [])]
    if not config.get('top'):
        raise ValueError('Design config requires top')
    for key in ('sdc', 'liberty'):
        if not config.get(key) or not Path(config[key]).is_file():
            raise ValueError(f'Design config requires an existing {key} file')
    if config.get('netlist') and not Path(config['netlist']).is_file():
        raise ValueError('Configured initial netlist does not exist')
    config.setdefault('design_cwd', str(base))
    for key in ('yosys', 'sta'):
        tool = os.environ.get(key.upper(), config.get(key, key))
        if '/' in tool:
            tool = str((base / tool).resolve())
        resolved = shutil.which(tool)
        if not resolved:
            raise ValueError(f'{key} executable unavailable: {tool}; set {key.upper()} or configure it')
        config[key] = resolved
    return config


def task_contract(config, design):
    # Reuse the existing task validation without modifying the optimizer package.
    spec = importlib.util.spec_from_file_location('sum_task_contract', ROOT / 'shinka_optimizer/task.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not config.get('task'):
        raise ValueError('Optimization requires a compatible Shinka task/evaluator in the design config')
    task = module.load_task(config['task'])
    seed = Path(task['seed'])
    if config.get('bundle_spec'):
        from rtl_bundle import unpack
        spec = json.loads(Path(config['bundle_spec']).read_text())
        files = unpack(seed.read_text(), spec['editable_files'])
        if design.resolve() != Path(spec['golden_sources'][spec['main_source_index']]).resolve():
            raise ValueError('Configured main source does not match the uploaded design')
        for name, content in files.items():
            if content != (Path(spec['golden_root']) / name).read_text():
                raise ValueError(f'Task seed differs from the uploaded file: {name}')
        return task, module.snapshot(task)
    stripped = ''.join(line for line in seed.read_text().splitlines(True)
                       if line.strip() not in ('// EVOLVE-BLOCK-START', '// EVOLVE-BLOCK-END'))
    source = ''.join(line for line in design.read_text().splitlines(True)
                     if line.strip() not in ('// EVOLVE-BLOCK-START', '// EVOLVE-BLOCK-END'))
    if stripped != source:
        raise ValueError('The configured task seed must match the uploaded RTL (apart from EVOLVE markers); supply its own task/evaluator')
    return task, module.snapshot(task)
