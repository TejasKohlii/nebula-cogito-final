#!/usr/bin/env python3
"""A design-independent Shinka loop; all task semantics belong to the configuration/adapter."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from task import digest, load_task, prompt, snapshot, succeeds, verify
from guard import evaluate
from native_usage import totals
from ppa_objective import enabled, continuation, closed, config as optimization_config


def configuration(task, results, generations, cost):
    from shinka.core import EvolutionConfig
    from shinka.database import DatabaseConfig
    from shinka.launch import LocalJobConfig
    search = task.get('search', {})
    system_prompt = prompt(task)
    if task.get('connection') == 'api' and task.get('investigation') and archive(results)[1]:
        from evidence import packet
        system_prompt += '\n\n' + packet(results)
    evo = EvolutionConfig(task_sys_msg=system_prompt, language=task['language'],
        init_program_path=task['seed'], results_dir=str(results), num_generations=generations,
        max_api_costs=cost, llm_models=[task['model']], llm_dynamic_selection='fixed',
        llm_kwargs=search.get('llm_kwargs', dict(temperatures=[0.0], max_tokens=12000)),
        embedding_model=None, meta_rec_interval=None, meta_llm_models=None,
        evolve_prompts=False, use_text_feedback=True,
        patch_types=['diff', 'full', 'cross'], patch_type_probs=[.7,.2,.1])
    cfg = optimization_config(task)
    early_ppa = bool(cfg and cfg['preclosure_ppa_budget'])
    archive_capacity = search.get('archive_size', 40)
    if early_ppa:
        # Keep admitted alternatives available to weighted parent selection.
        # The DB already stores their code; archive membership adds only IDs.
        _, existing = archive(results)
        archive_capacity = max(archive_capacity, len(existing) + generations * max(2, search.get('islands', 2)))
    db = DatabaseConfig(db_path=str(results/'programs.sqlite'),
        num_islands=search.get('islands', 2), archive_size=archive_capacity,
        parent_selection_strategy=search.get('parent_selection_strategy', 'weighted'),
        parent_selection_lambda=search.get('parent_selection_lambda', 10.),
        num_beams=search.get('num_beams', 5),
        num_archive_inspirations=search.get('archive_inspirations', 1),
        num_top_k_inspirations=search.get('top_k_inspirations', 1))
    job = LocalJobConfig(eval_program_path=str(Path(__file__).with_name('guard.py')))
    return evo, db, job


def archive(results):
    path = results/'programs.sqlite'
    if not path.exists():
        return None, []
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT * FROM programs ORDER BY generation, timestamp')]
        best = db.execute("SELECT value FROM metadata_store WHERE key='best_program_id'").fetchone()
    return next((r for r in rows if best and r['id'] == best[0]), None), rows


def publish(manifest_path, winner, results):
    task = verify(manifest_path)['task']
    if not winner or not winner['correct']:
        raise ValueError('Shinka has no admissible selected winner')
    out = results/'verification'/winner['id']
    out.mkdir(parents=True, exist_ok=True)
    candidate = out/('candidate'+Path(task['seed']).suffix)
    candidate.write_text(winner['code'])
    record = json.loads((results/f"gen_{winner['generation']}"/'results/metrics.json').read_text())
    if digest(candidate) != record['extra_data']['candidate_sha256']:
        raise ValueError('Archive code differs from evaluated code')
    correct, metrics = evaluate(manifest_path, candidate, out/'results')
    if not correct['correct'] or metrics['public'] != json.loads(winner['public_metrics']):
        raise ValueError('Selected winner failed fresh reproduction')
    if abs(metrics['combined_score']-winner['combined_score']) > 1e-9:
        raise ValueError('Selected score did not reproduce')
    best, rows = archive(results)
    if best['id'] != winner['id']:
        raise ValueError('Official selection changed')
    by_id = {r['id']:r for r in rows}
    ancestry, seen, node = [], set(), winner
    while node:
        if node['id'] in seen:
            raise ValueError('Archive ancestry cycle')
        seen.add(node['id'])
        ancestry.append(dict(id=node['id'], generation=node['generation'], parent_id=node['parent_id']))
        node = by_id[node['parent_id']] if node['parent_id'] else None
    summary = dict(optimizer='ShinkaEvolveRunner', model=task['model'],
        winner_id=winner['id'], generation=winner['generation'], ancestry=ancestry,
        seed_sha256=digest(task['seed']), winner_sha256=digest(candidate),
        candidate=str(candidate), metrics=metrics['public'], verified=True,
        success=succeeds(task, True, metrics), source='official best_program_id')
    spec_path = task.get('environment', {}).get('COGITO_GENERIC_SPEC')
    if spec_path:
        spec = json.loads(Path(spec_path).read_text())
        if spec.get('candidate_format') == 'rtl_bundle_v1':
            from rtl_bundle import materialize
            sources, _ = materialize(spec, candidate, out / 'rtl')
            summary.update(source_directory=str(out / 'rtl'), source_files=list(map(str, sources)),
                           main_source=str(sources[spec['main_source_index']]))
    (results/'winner.json').write_text(json.dumps(summary, indent=2)+'\n')
    return summary


def finalize(task, root, manifest_path, results):
    if not task.get('finalizer'):
        return
    import subprocess
    import sys
    finalizer = (root/task['finalizer']).resolve()
    if str(finalizer) not in verify(manifest_path)['hashes']:
        raise ValueError('Finalizer is not a frozen task dependency')
    subprocess.run([sys.executable, str(finalizer), '--run', str(results)],
                   check=True, timeout=task.get('finalizer_timeout_seconds', 900))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task', required=True)
    p.add_argument('--results', required=True)
    p.add_argument('--max-generations', type=int, default=40)
    p.add_argument('--max-api-cost', type=float, default=10.)
    p.add_argument('--max-hours', type=float, default=2.)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--check', action='store_true', help='Evaluate the original seed only; no model calls')
    p.add_argument('--allow-design-upload', action='store_true')
    a = p.parse_args()
    task = load_task(a.task)
    results = Path(a.results).resolve()
    root = Path(task['workspace'])
    if not results.is_relative_to(root) or results == root:
        p.error('Use a dedicated results directory inside the task workspace')
    if a.max_generations < 2 or a.max_api_cost <= 0 or a.max_hours <= 0:
        p.error('Invalid resource limits')
    if not a.check and not a.allow_design_upload:
        p.error('Model execution requires --allow-design-upload')
    manifest_path = results/'task_manifest.json'
    elapsed = 0.
    previous_cost = 0.
    if a.resume:
        old = verify(manifest_path)
        if old['task'] != task or old['hashes'] != snapshot(task):
            p.error('Task/evaluator changed; use a fresh run')
        if (results/'status.json').exists():
            previous_status = json.loads((results/'status.json').read_text())
            elapsed = previous_status.get('elapsed_seconds', 0.)
            previous_cost = previous_status.get('reported_api_cost', 0.)
    elif results.exists() and any(results.iterdir()):
        p.error('Results already exist; resume or choose a fresh directory')
    results.mkdir(parents=True, exist_ok=True)
    with (results/'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not a.resume:
            manifest_path.write_text(json.dumps(dict(task=task, hashes=snapshot(task)), indent=2)+'\n')
        os.environ.update(task.get('environment', {}))
        os.environ['SHINKA_TASK_MANIFEST'] = str(manifest_path)
        os.environ['SHINKA_CAMPAIGN_COST_LIMIT'] = str(a.max_api_cost)
        if task.get('investigation'):
            os.environ['SHINKA_EVIDENCE_RUN'] = str(results)
        if a.check:
            correct, metrics = evaluate(manifest_path, task['seed'], results/'seed_check')
            print(json.dumps(dict(correct=correct, metrics=metrics['public'],
                                 success=succeeds(task, correct['correct'], metrics))))
            if not correct['correct']:
                raise SystemExit(1)
            return
        from shinka.core import ShinkaEvolveRunner
        started = time.monotonic()
        status = dict(state='running', optimizer='ShinkaEvolveRunner', model=task['model'],
                      reported_api_cost=previous_cost, pid=os.getpid(),
                      limits=dict(generations=a.max_generations, api_cost=a.max_api_cost, hours=a.max_hours))
        def save():
            status['elapsed_seconds'] = elapsed+time.monotonic()-started
            status.update(totals(results))
            (results/'status.json').write_text(json.dumps(status, indent=2)+'\n')
        save()
        try:
            while True:
                verify(manifest_path)
                winner, rows = archive(results)
                next_gen = max((r['generation'] for r in rows), default=-1)+1
                progress = continuation(task, winner, rows)
                status.update({k: v for k, v in progress.items() if k != 'stop'})
                feasible = winner and succeeds(task, bool(winner['correct']), {'public':json.loads(winner['public_metrics'])})
                bounded_stop = progress.get('closing_policy') in ('two_plus_one', 'stop_at_closure') and progress['stop']
                stopping = (feasible and (not enabled(task) or progress['stop'])) or bounded_stop
                if stopping:
                    if enabled(task) and (not winner or not winner['correct'] or not closed(json.loads(winner['public_metrics']))):
                        raise RuntimeError('Closure policy stopped without an official timing-closed winner')
                    summary = publish(manifest_path, winner, results)
                    finalize(task, root, manifest_path, results)
                    status['timing_closed'] = closed(summary['metrics'])
                    # Missing power never earns another generation or a false
                    # success label. Publish the verified closed fallback.
                    status['state'] = ('success_verified' if summary['success'] else
                                       'incomplete_power_measurement' if summary['metrics'].get('power_valid') is not True
                                       else 'incomplete_success_conditions')
                    break
                if next_gen >= a.max_generations:
                    status['state'] = 'incomplete_generation_limit'
                    break
                # The native proposer reserves $0.50 before each request.
                # Stop cleanly rather than repeatedly asking it to exceed budget.
                reserve = .5 if task.get('investigation') and task.get('connection') != 'api' else 0.
                if max(status.get('reported_api_cost', 0.), totals(results)['budget_accounted_cost']) + reserve > a.max_api_cost:
                    status['state'] = 'incomplete_cost_limit'
                    break
                if elapsed+time.monotonic()-started >= a.max_hours*3600:
                    status['state'] = 'incomplete_time_limit'
                    break
                # Inspect even generation zero before authorizing a proposal.
                minimum_target = 1 if progress.get('closing_policy') == 'stop_at_closure' else 2
                target = min(a.max_generations, max(minimum_target, next_gen+1))
                evo, db, job = configuration(task, results, target, a.max_api_cost)
                runner = ShinkaEvolveRunner(evo_config=evo, db_config=db, job_config=job,
                    max_evaluation_jobs=1, max_proposal_jobs=1, max_db_workers=1, verbose=True)
                runner.run()
                status['reported_api_cost'] = runner.total_api_cost
                status['completed_target'] = target
                save()
                _, after = archive(results)
                if max((r['generation'] for r in after), default=-1)+1 <= next_gen:
                    if runner.total_api_cost >= a.max_api_cost:
                        status['state'] = 'incomplete_cost_limit'
                        break
                    raise RuntimeError('No archive progress; inspect Shinka logs')
            if status['state'].startswith('incomplete') and status['state'] not in ('incomplete_power_measurement', 'incomplete_success_conditions'):
                winner, _ = archive(results)
                if winner and winner['correct']:
                    summary = publish(manifest_path, winner, results)
                    if summary['success'] or (enabled(task) and closed(summary['metrics'])):
                        finalize(task, root, manifest_path, results)
                    status['stop_reason'] = status['state']
                    status['timing_closed'] = summary['metrics'].get('timing_closed', False)
        except KeyboardInterrupt:
            status['state'] = 'interrupted'
            raise
        except BaseException as exc:
            status.update(state='failed', error=str(exc))
            raise
        finally:
            save()


if __name__ == '__main__':
    main()
