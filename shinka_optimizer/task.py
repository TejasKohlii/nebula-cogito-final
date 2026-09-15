"""Task contracts only: no circuit names, technology choices or optimization recipes."""
import hashlib
import json
import math
import operator
import os
import shlex
from ppa_objective import config as optimization_config
from pathlib import Path
from guidance_assets import asset_text

OPS = {'eq': operator.eq, 'ge': operator.ge, 'le': operator.le,
       'gt': operator.gt, 'lt': operator.lt}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_task(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    root = (path.parent/config['workspace']).resolve()
    def local(name):
        p = (root/name).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise ValueError(f'Task input must be a file inside workspace: {name}')
        return str(p)
    if config.get('goal_file'):
        if config.get('goal'):
            raise ValueError('Specify goal or goal_file, not both')
        config['goal'] = asset_text(local(config['goal_file']))
    task = dict(config, workspace=str(root), config_path=str(path),
                seed=local(config['seed']), evaluator=local(config['evaluator']))
    task['frozen_inputs'] = [local(n) for n in config['frozen_inputs']]
    if config.get('goal_file'):
        task['frozen_inputs'].append(local(config['goal_file']))
    if not config.get('goal') or not config.get('language') or not config.get('model'):
        raise ValueError('Explicit goal, language and model are required')
    if not config.get('success'):
        raise ValueError('Explicit success conditions are required')
    for condition in config['success']:
        if condition['op'] not in OPS or not condition.get('metric'):
            raise ValueError('Invalid success condition')
    optimization_config(task)
    task['environment'] = {k:v.replace('{workspace}', str(root))
                           for k,v in config.get('environment', {}).items()}
    if os.environ.get('COGITO_MODEL'):
        task['model'] = os.environ['COGITO_MODEL']
    provider = os.environ.get('COGITO_PROVIDER')
    if provider not in (None, 'cli', 'api'):
        raise ValueError('COGITO_PROVIDER must be cli or api')
    if provider:
        task['connection'] = provider
    if provider == 'cli' and not task['model'].startswith('headless/claude'):
        raise ValueError('CLI mode requires a headless/claude model in COGITO_MODEL')
    if provider == 'api':
        task['model'] = os.environ.get('COGITO_API_MODEL', 'claude-sonnet-5')
        from shinka.llm.providers.model_resolver import resolve_model_backend
        if resolve_model_backend(task['model']).provider != 'anthropic':
            raise ValueError('API mode requires a direct Anthropic model in COGITO_API_MODEL')
        task['search'] = dict(task.get('search', {}))
        task['search']['llm_kwargs'] = dict(task['search'].get('llm_kwargs', {}),
                                          reasoning_efforts=['medium'])
        for name in ('CLAUDE_CODE_BIN', 'SHINKA_HEADLESS_COMMAND', 'SHINKA_HEADLESS_TIMEOUT'):
            task['environment'].pop(name, None)
        for name in ('kwargs.py', 'providers/anthropic.py', 'providers/pricing.csv',
                     'providers/model_resolver.py', 'client.py'):
            task['frozen_inputs'].append(local('ShinkaEvolve/shinka/llm/' + name))
    # Quote the executable for Headless's shlex parser, including clone paths
    # containing spaces. Explicit caller settings take precedence.
    if provider != 'api':
        task['environment']['SHINKA_HEADLESS_COMMAND'] = os.environ.get(
            'SHINKA_HEADLESS_COMMAND', shlex.quote(str(root / '.tools/node_modules/.bin/headless')))
    return task


def snapshot(task):
    files = {task['config_path'], task['seed'], task['evaluator'], *task['frozen_inputs']}
    files.update(str(p) for p in Path(__file__).parent.glob('*.py'))
    return {p:digest(p) for p in sorted(files)}


def verify(path):
    manifest = json.loads(Path(path).read_text())
    for name, expected in manifest['hashes'].items():
        if digest(name) != expected:
            raise ValueError('Frozen input changed: '+name)
    return manifest


def succeeds(task, correct, metrics):
    if correct is not True:
        return False
    public = metrics.get('public', {})
    for condition in task['success']:
        value = public.get(condition['metric'])
        target = condition['value']
        if value is None:
            return False
        if isinstance(value, (int, float)) and not math.isfinite(value):
            return False
        if isinstance(target, bool) and not isinstance(value, bool):
            return False
        try:
            if not OPS[condition['op']](value, target):
                return False
        except TypeError:
            return False
    return True


def prompt(task):
    reads = task.get('investigation', {}).get('max_additional_reads', 1)
    tools = ('Current-run evidence is included below when available. Read BRIEF.json '
        'and the measured feedback in that packet before proposing a patch. '
        'You have no filesystem tools; use the supplied evidence and return the requested patch. '
        if task.get('connection') == 'api' and task.get('investigation') else
        'You may use the read-only tools to inspect the staged evidence directory. '
        f'Start with BRIEF.json, including recent failures. Use at most {reads} additional focused reads, then return the '
        'requested patch. Inspect measurements and previous failures before selecting a '
        'hypothesis. Do not inspect files outside this directory or change any files. '
        if task.get('investigation') else 'Do not use tools or read local files. ')
    return ('You are the mutation proposer inside ShinkaEvolve. Shinka selects parents, '
        'applies patches, evaluates candidates and selects the winner. Propose changes '
        'using only the supplied candidate, archive and evaluator feedback. Preserve '
        'the declared behavioral contract and immutable regions. '+tools+
        'Do not change tests or constraints or alter the evaluator. '
        'Discover optimization strategies from the candidate and measured feedback.\n\n'
        'Optimization policy (success is a feasibility gate; enabled PPA search continues in the same archive until plateau or budget):\n'+json.dumps(optimization_config(task))+'\n\nTask goal:\n'+task['goal']+'\n\nSuccess conditions on evaluator public metrics:\n'
        +json.dumps(task['success'], indent=2))
