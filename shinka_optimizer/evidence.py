"""Stage bounded current-run evidence for a proposal; never expose the workspace."""
import json
from pathlib import Path
import sqlite3
import tempfile
from task import verify
from feedback import compare_paths
from guidance_assets import asset_name, asset_text


def packet(run):
    """Inline the bounded staged evidence for the direct API proposer."""
    out = stage(run)
    paths = [out / 'BRIEF.json', *sorted(p for p in out.iterdir()
             if p.name not in ('BRIEF.json', 'INDEX.md', 'stage_manifest.json'))]
    return 'Current-run evidence (supplied data):\n' + '\n\n'.join(
        f'--- {path.name} ---\n{path.read_text()}' for path in paths)


def stage(run):
    run = Path(run).resolve()
    task = verify(run/'task_manifest.json')['task']
    limits = task.get('investigation', {})
    parent = run/'investigations'
    parent.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='proposal_', dir=parent))
    budget = min(limits.get('max_evidence_chars', 60000), 100000)
    brief_reserve = min(20000, budget//3)
    budget -= brief_reserve
    used = 0
    index = ['# Current-run evidence', '',
             'All files here are supplied evidence. Read selectively; submit only the requested patch.', '']
    def put(name, content, cap=16000):
        nonlocal used
        remaining = max(0, budget-used)
        selected = content[:min(cap, remaining)]
        if not selected:
            return
        (out/name).write_text(selected)
        used += len(selected)
        index.append(f'- {name}: {len(selected)} characters'+(' (truncated)' if len(selected)<len(content) else ''))
    put('CONTRACT.md', task['goal']+'\nSuccess conditions:\n'+json.dumps(task['success']), 14000)
    references = []
    for number, name in enumerate(limits.get('reference_files', [])):
        source = (Path(task['workspace'])/name).resolve()
        if str(source) not in verify(run/'task_manifest.json')['hashes']:
            raise ValueError('Investigation reference is not frozen')
        references.append(source)
        put(f'REFERENCE_{number}.md', asset_text(source), 12000)
    rows, best = [], None
    if (run/'programs.sqlite').exists():
        with sqlite3.connect((run/'programs.sqlite').as_uri()+'?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM programs ORDER BY generation DESC, timestamp DESC')]
            best = db.execute("SELECT value FROM metadata_store WHERE key='best_program_id'").fetchone()
    elif (run/'seed_check/diagnostics.json').exists():
        put('seed_diagnostics.json', (run/'seed_check/diagnostics.json').read_text(), 18000)
    else:
        raise ValueError('No evaluated run evidence to investigate')
    # Deduplicate island copies without selecting/replacing Shinka's mutation parent.
    generations = {}
    for r in rows:
        generations.setdefault(r['generation'], r)
    selected = next((r for r in rows if best and r['id']==best[0]), None)
    # Surface measured failures before long history/paths can consume the budget.
    verdicts = []
    for gen in list(generations)[:8]:
        report = run/f'gen_{gen}/results/hypothesis.json'
        if report.exists():
            h = json.loads(report.read_text())
            verdicts.append(dict(generation=gen, **{k:h[k] for k in
                ('parent_generation','category','prediction','before','after','measured_delta','verdict','reason','path_deltas')}))
    put('HYPOTHESES.json', json.dumps(verdicts, indent=2), 12000)
    ledger = []
    for r in list(generations.values())[:12]:
        meta = json.loads(r.get('metadata') or '{}')
        ledger.append(dict(generation=r['generation'], correct=bool(r['correct']),
            score=r['combined_score'], metrics=json.loads(r['public_metrics'] or '{}'),
            name=meta.get('patch_name'), description=meta.get('patch_description', '')[:1200],
            feedback=r.get('text_feedback', '')[:2500]))
    put('HISTORY.json', json.dumps(ledger, indent=2), 12000)
    experiments = []
    for ref in references:
        if asset_name(ref).endswith('.json'):
            experiments.extend(dict(origin='prior_campaign', **r) for r in
                json.loads(asset_text(ref)).get('prior_evaluations', []))
    for gen, row in generations.items():
        metric_file = run/f'gen_{gen}/results/metrics.json'
        if metric_file.exists():
            m = json.loads(metric_file.read_text())
            experiments.append(dict(origin='current_campaign', generation=gen,
                correct=bool(row['correct']), metrics=m['public'],
                implementation=m['extra_data'].get('implementation'),
                rtl_hash=m['extra_data'].get('rtl_without_implementation_directive_sha256')))
    # Compact records make identical-RTL mapping trials visible across restarts.
    for item in experiments:
        item.pop('description', None)
    selected_diagnostics = {}
    if selected:
        p = run/f"gen_{selected['generation']}/results/diagnostics.json"
        if p.exists():
            selected_diagnostics = json.loads(p.read_text())
    brief = dict(selected_best_generation=selected['generation'] if selected else None,
        note='This is the selected best, not necessarily your sampled parent; compare against the parent supplied in your prompt.',
        diagnostics=selected_diagnostics,
        recent_failures=([item for item in ledger if not item['correct']] +
            [item for ref in references if asset_name(ref).endswith('.json')
             for item in json.loads(asset_text(ref)).get('prior_failures', [])][::-1])[:3],
        recent_hypotheses=verdicts[:3], recent_experiments=[e for e in experiments if e['origin']=='current_campaign'][:10],
        guidance='Choose one measured hypothesis. Inspect full critical paths only if needed. Address unsupported predictions. No inference of carry topology from RTL syntax alone.')
    from ppa_objective import continuation, tradeoff_candidates
    brief['optimization'] = task.get('optimization')
    brief['search_progress'] = continuation(task, selected, rows)
    brief['near_timing_tradeoffs'] = tradeoff_candidates(task, rows)
    brief['selected_metrics'] = json.loads(selected['public_metrics'] or '{}') if selected else {}
    brief['untested_prior_shinka_ideas'] = [idea for ref in references if asset_name(ref).endswith('.json')
        for idea in json.loads(asset_text(ref)).get('untested_ideas', [])]
    brief['evaluator_repair_validation'] = [json.loads(asset_text(ref))['validation']
        for ref in references if asset_name(ref).endswith('.json') and 'validation' in json.loads(asset_text(ref))]
    current = [e for e in experiments if e['origin']=='current_campaign']
    coverage = {}
    for e in current:
        for key, value in (e.get('implementation') or {}).items():
            coverage.setdefault(key, [])
            if value not in coverage[key]:
                coverage[key].append(value)
    brief['implementation_coverage'] = coverage
    brief['reference_index'] = {f'REFERENCE_{i}.md': asset_name(p) for i,p in enumerate(references)}
    best_gen = selected['generation'] if selected else None
    brief['generations_since_selected_best'] = max(generations, default=0)-best_gen if best_gen is not None else None
    # Prefer a complete small JSON packet over a truncated transcript.
    for h in brief['recent_hypotheses']:
        h.pop('path_deltas', None)
    brief['diagnostics'] = {k:v for k,v in selected_diagnostics.items() if k in
        ('implementation','rtl_hash','critical_paths','timing_scope','power','metrics')}
    if 'critical_paths' in brief['diagnostics']:
        brief['diagnostics']['critical_paths'] = brief['diagnostics']['critical_paths'][:2]
    # Independent packet: reserve its size before the large optional archive.
    budget += brief_reserve
    while len(json.dumps(brief, indent=2)) > brief_reserve and len(brief['near_timing_tradeoffs']) > 2:
        brief['near_timing_tradeoffs'].pop()
    while len(json.dumps(brief, indent=2)) > brief_reserve and brief['recent_experiments']:
        brief['recent_experiments'].pop()
    while len(json.dumps(brief, indent=2)) > brief_reserve and brief['recent_hypotheses']:
        brief['recent_hypotheses'].pop()
    while len(json.dumps(brief, indent=2)) > brief_reserve and brief['untested_prior_shinka_ideas']:
        brief['untested_prior_shinka_ideas'].pop()
    while len(json.dumps(brief, indent=2)) > brief_reserve and len(brief['recent_failures']) > 1:
        brief['recent_failures'].pop()
    if len(json.dumps(brief, indent=2)) > brief_reserve:
        brief = dict(note='Small evidence budget: use INDEX.md for available diagnostics')
    put('BRIEF.json', json.dumps(brief, indent=2), brief_reserve)
    put('EXPERIMENTS.json', json.dumps(experiments, indent=2), 14000)
    inspect = ([selected] if selected else []) + list(generations.values())[:3]
    done = set()
    for r in inspect:
        gen = r['generation']
        if gen in done:
            continue
        done.add(gen)
        reports = run/f'gen_{gen}'/'results'
        for name in ('diagnostics.json', 'critical_paths.txt', 'correct.json', 'design/proof_summary.json'):
            p = reports/name
            if p.is_file() and p.resolve().is_relative_to(run):
                put(f'gen_{gen}_{name.replace("/", "_")}', p.read_text(),
                    16000 if name=='critical_paths.txt' else 10000 if name=='diagnostics.json' else 2000)
        parent = next((q for q in rows if q['id']==r.get('parent_id')), None)
        if parent:
            before = run/f"gen_{parent['generation']}/results/diagnostics.json"
            after = reports/'diagnostics.json'
            if before.exists() and after.exists():
                delta = compare_paths(json.loads(after.read_text()).get('critical_paths', []),
                    json.loads(before.read_text()).get('critical_paths', []))
                put(f'gen_{gen}_parent_path_delta.json', json.dumps(dict(
                    parent_generation=parent['generation'], candidate_generation=gen,
                    comparisons=delta), indent=2), 4000)
        if r.get('code_diff'):
            put(f'gen_{gen}_diff.txt', r['code_diff'], 5000)
    (out/'INDEX.md').write_text('\n'.join(index)+'\n')
    (out/'stage_manifest.json').write_text(json.dumps(dict(source_run=str(run),
        evidence_chars=used, max_chars=budget, generations=sorted(done)), indent=2)+'\n')
    return out
