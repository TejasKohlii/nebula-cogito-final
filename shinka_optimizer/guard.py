"""Invoke a task-owned evaluator through the standard Shinka artifact protocol."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from task import digest, verify
from reports import evaluation_reports


def evaluate(manifest_path, program_path, results_dir):
    out = Path(results_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    candidate = Path(program_path).resolve()
    source_hash = digest(candidate)
    metrics = dict(combined_score=0., public={}, private={}, extra_data={}, text_feedback='')
    correct = dict(correct=False, error='')
    try:
        task = verify(manifest_path)['task']
        # A retry must produce new artifacts; never accept stale success files.
        stamp = time.time_ns()
        for name in ('metrics.json', 'correct.json'):
            previous = out/name
            if previous.exists():
                previous.rename(out/f'previous_{stamp}_{name}')
        env = dict(os.environ, **task.get('environment', {}))
        env['SHINKA_TASK_MANIFEST'] = str(Path(manifest_path).resolve())
        # Legacy adapter manifest variables are not this loop's contract.
        env.pop('SHINKA_RTL_MANIFEST', None)
        with (out/'adapter.log').open('w') as log:
            subprocess.run([sys.executable, task['evaluator'], '--program_path', str(candidate),
                '--results_dir', str(out), *task.get('evaluator_args', [])],
                cwd=task['workspace'], env=env, stdout=log, stderr=subprocess.STDOUT,
                timeout=task.get('evaluation_timeout_seconds', 600), check=True)
        metrics = json.loads((out/'metrics.json').read_text())
        correct = json.loads((out/'correct.json').read_text())
        if type(correct.get('correct')) is not bool or not isinstance(metrics.get('public'), dict):
            raise ValueError('Invalid evaluator artifact schema')
        score = metrics.get('combined_score')
        if type(score) not in (float, int) or not math.isfinite(score):
            raise ValueError('Evaluator score must be finite')
        if not correct['correct']:
            metrics['combined_score'] = 0.
        verify(manifest_path)
        if digest(candidate) != source_hash:
            raise ValueError('Evaluator changed candidate source')
    except Exception as exc:
        correct = dict(correct=False, error=str(exc))
        metrics = dict(combined_score=0., public={}, private={'failure_stage': 'guard'}, extra_data={}, text_feedback=str(exc))
    metrics.setdefault('extra_data', {})['candidate_sha256'] = source_hash
    (out/'metrics.json').write_text(json.dumps(metrics, indent=2)+'\n')
    (out/'correct.json').write_text(json.dumps(correct, indent=2)+'\n')
    evaluation_reports(out)
    return correct, metrics


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--program_path', required=True)
    p.add_argument('--results_dir', required=True)
    a = p.parse_args()
    evaluate(os.environ['SHINKA_TASK_MANIFEST'], a.program_path, a.results_dir)
