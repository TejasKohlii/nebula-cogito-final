"""RTL adapter policy. Resolve the actual archived parent before classification."""
import json
from pathlib import Path
import sqlite3
from experiment import classify, verdict
from feedback import compare_paths

TAG = '// SHINKA_HYPOTHESIS '
PATH_METRICS = {'logic_stages', 'buffer_stages', 'logic_delay_ns', 'buffer_delay_ns', 'clock_to_q_ns', 'arrival_ns'}
PUBLIC_METRICS = {'wns_ns', 'tns_ns', 'violating_endpoints', 'cell_area_um2', 'standard_cells', 'power_w', 'ppa_quality'}


def source_identity(text):
    """Normalize only line endings and final blank space for archive lookup."""
    return text.replace('\r\n', '\n').rstrip('\n')


def archived_parent(db, source, parent_id=None):
    """Resolve an exact RTL parent despite transport-only final-newline changes."""
    wanted = source_identity(source)
    if parent_id is not None:
        row = db.execute('SELECT * FROM programs WHERE id=? AND correct=1', (parent_id,)).fetchone()
        return row if row and source_identity(row['code']) == wanted else None
    matches = [row for row in db.execute('SELECT * FROM programs WHERE correct=1')
               if source_identity(row['code']) == wanted]
    if len(matches) != 1:
        # Initial island copies can be identical; choose their shared generation.
        generations = {row['generation'] for row in matches}
        codes = {row['code'] for row in matches}
        if not matches or len(generations) != 1 or len(codes) != 1:
            return None
    return matches[0]


def prediction_metadata(source, parent_source):
    def declarations(text):
        return [s.strip()[len(TAG):] for s in text.splitlines() if s.strip().startswith(TAG)]
    current, inherited = declarations(source), declarations(parent_source)
    # Ignore unchanged inherited annotations when exactly one fresh one is present.
    fresh = list(current)
    for item in inherited:
        if item in fresh:
            fresh.remove(item)
    choices = fresh if fresh else current
    choices = list(dict.fromkeys(choices))
    warning = None
    try:
        if len(choices) != 1:
            raise ValueError('Missing or ambiguous hypothesis metadata')
        prediction = json.loads(choices[0])
        if not isinstance(prediction, dict) or set(prediction) != {'metric','direction','group'}:
            raise ValueError('Hypothesis requires metric, direction and group')
        if prediction['metric'] not in PATH_METRICS | PUBLIC_METRICS or prediction['direction'] not in ('increase','decrease'):
            raise ValueError('Invalid measurable hypothesis metric or direction')
        if not isinstance(prediction['group'], str) or (prediction['metric'] in PATH_METRICS and not prediction['group']):
            raise ValueError('Path hypotheses require a clock group')
        if len(current) > 1:
            warning = 'Resolved inherited/duplicate annotations; replace the existing declaration next time.'
    except (ValueError, TypeError):
        prediction = None
        warning = 'Hypothesis metadata missing, malformed or ambiguous; evaluation continues without a prediction verdict. Replace inherited annotations with one valid declaration.'
    return prediction, warning


def resolve(program, source, manifest_path, settings, rtl_digest):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if not manifest['task'].get('experiment_policy'):
        return None
    run = manifest_path.parent
    original = Path(program).parent/'original.sv'
    seed = Path(manifest['task']['seed']).read_text()
    if not original.exists() and source == seed:
        return None  # Original seed reproduction, including --check.
    with sqlite3.connect((run/'programs.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if original.exists():
            parent_source = original.read_text()
            provenance = Path(program).parent/'mutation_parent.json'
            parent_id = json.loads(provenance.read_text())['parent_id'] if provenance.exists() else None
            parent = archived_parent(db, parent_source, parent_id)
        else:
            # Fresh verification uses archive ancestry rather than a sibling file.
            row = db.execute('SELECT parent_id FROM programs WHERE code=? LIMIT 1', (source,)).fetchone()
            parent = db.execute('SELECT * FROM programs WHERE id=? AND correct=1',
                                (row['parent_id'],)).fetchone() if row else None
        if parent is None:
            raise ValueError('Cannot establish an admissible archived mutation parent')
        parent = dict(parent)
    classification = classify(rtl_digest(parent['code']), rtl_digest(source),
                              settings(parent['code']), settings(source),
                              manifest['task'].get('allow_implementation_bundle', False))
    prediction, warning = prediction_metadata(source, parent['code'])
    return dict(**classification, prediction=prediction, metadata_warning=warning, parent_generation=parent['generation'],
                parent_id=parent['id'], parent_results=str(run/f"gen_{parent['generation']}/results"))


def measure(context, public, measured):
    out = Path(context['parent_results'])
    before = json.loads((out/'metrics.json').read_text())['public']
    old = json.loads((out/'diagnostics.json').read_text()).get('critical_paths', [])
    new = measured.get('critical_paths', [])
    prediction = context['prediction']
    if prediction is None:
        return {**context, 'before':None, 'after':None, 'measured_delta':None,
                'verdict':'inconclusive', 'reason':context['metadata_warning'],
                'path_deltas':compare_paths(new, old)}
    metric, group = prediction['metric'], prediction['group']
    same = True
    if metric in PATH_METRICS:
        a = next((p for p in old if p['group']==group), {})
        b = next((p for p in new if p['group']==group), {})
        same = bool(a.get('source_rtl') and a.get('destination_rtl') and
                    a.get('source_rtl') == b.get('source_rtl') and a.get('destination_rtl') == b.get('destination_rtl'))
        before, after = a.get(metric), b.get(metric)
    else:
        before, after = before.get(metric), public.get(metric)
    return {**context, **verdict(prediction, before, after, same),
            'path_deltas':compare_paths(new, old),
            'next_step':'Address unsupported or inconclusive predictions explicitly before repeating the same strategy. Fitness follows the frozen task objective; PPA improvements cannot compensate for lost timing closure.'}
