"""Deterministic, local reports from evaluator artifacts; no model requests."""
import json
from pathlib import Path
import re
import shutil


LABELS = {'yosys': 'Yosys synthesis', 'opensta': 'OpenSTA timing',
          'equivalence': 'Formal equivalence', 'failures': 'Failure analysis'}
PATTERNS = {
    'yosys': ('syntax.log', 'synthesis.log', 'reports/synth.log', 'reports/export.log', 'reports/cell_count.json'),
    'opensta': ('sta.log', 'sta/sta.log', 'sta/critical.rpt', 'sta/endpoints.tsv', 'sta/clocks.tsv',
                'reports/sta_run.log', 'reports/timing.log', 'reports/timing_summary.json',
                'reports/*paths*.rpt', 'reports/violating_endpoints.tsv'),
    'equivalence': ('formal_match_*.log', 'proof_summary.json',
                    'equivalence/formal_match_*.log', 'equivalence/*_elaboration.log', 'equivalence/proof_summary.json'),
}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def write_text(path, text):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text)
    temp.replace(path)


def combine(target, title, paths, summary=''):
    paths = sorted({Path(p).resolve() for p in paths if Path(p).is_file()})
    temp = target.with_suffix('.tmp')
    with temp.open('w') as stream:
        stream.write(title + '\nGenerated programmatically by Cogito; no model calls.\n\n' + summary + '\n')
        if not paths:
            stream.write('No tool output exists. The stage was not reached or did not produce a report.\n')
        for source in paths:
            stream.write(f'\n===== {source} =====\n')
            with source.open(errors='replace') as raw:
                shutil.copyfileobj(raw, stream)
            stream.write('\n')
    temp.replace(target)


def failure_analysis(out):
    metrics = read_json(out / 'metrics.json')
    verdict = read_json(out / 'correct.json')
    diagnostics = read_json(out / 'diagnostics.json')
    public = metrics.get('public', {})
    stage = metrics.get('private', {}).get('failure_stage') or diagnostics.get('failure_stage')
    passed = verdict.get('correct') is True
    lines = [f'Evaluation: {out}', f'Admissible: {passed}',
             f'Failure stage: {stage or ("none" if passed else "evaluator / incomplete")}',
             'Measured metrics: ' + json.dumps(public, sort_keys=True)]
    reason = verdict.get('error') or diagnostics.get('reason') or (metrics.get('text_feedback') if not passed else '')
    if reason:
        lines += ['Recorded failure:', str(reason)]
    hints = {
        'contract': 'Candidate format or a frozen input failed validation. Check file markers and retained file paths.',
        'syntax_or_elaboration': 'Yosys could not elaborate the candidate. Check syntax, includes, module connections, and widths in yosys.txt.',
        'formal_equivalence': 'Equivalence was not established. Inspect equivalence.txt; an unproven obligation or timeout alone does not establish a behavioral counterexample.',
        'formal_inconclusive': 'Equivalence was not established. Inspect equivalence.txt; inconclusive proof is not a demonstrated functional mismatch.',
        'synthesis': 'Mapping failed. Inspect yosys.txt for unsupported constructs, unmapped cells, library errors, or timeout.',
        'implementation': 'Implementation failed. Inspect yosys.txt and opensta.txt for the recorded tool error.',
        'timing_analysis': 'Timing analysis failed. Inspect opensta.txt for constraint resolution, clock coverage, unconstrained paths, or tool errors.',
        'guard': 'The evaluator did not complete its contract. Inspect adapter.log and the recorded exception.',
    }
    if stage in hints:
        lines += ['Suggested inspection: ' + hints[stage]]
    if passed and public.get('timing_closed') is False:
        lines += ['Timing closure has not been reached: setup slack or violating endpoints remain. See opensta.txt for measured paths.']
    if public.get('power_valid') is False:
        lines += ['Power measurement is invalid or unavailable; this candidate cannot meet the full success conditions.']
    if passed and public.get('timing_closed') is True and public.get('power_valid') is not False:
        lines += ['No evaluator failure or setup timing violation was reported.']
    return '\n'.join(lines) + '\n'


def index(out, report_dir):
    files = {key: str(report_dir / (key + '.txt')) for key in LABELS}
    write_text(report_dir / 'index.json', json.dumps({'files': files, 'labels': LABELS}, indent=2) + '\n')
    return files


def evaluation_reports(out):
    out = Path(out).resolve()
    report_dir = out / 'reports'
    report_dir.mkdir(parents=True, exist_ok=True)
    work = out / 'design'
    for key, patterns in PATTERNS.items():
        paths = [p for pattern in patterns for p in work.glob(pattern)]
        summary = ('Engine: Yosys equiv_make / equiv_simple / equiv_induct / equiv_status.\n'
                   'EQY is not invoked by this evaluator. The actual proof logs and summary follow.\n'
                   if key == 'equivalence' else '')
        combine(report_dir / (key + '.txt'), LABELS[key], paths, summary)
    write_text(report_dir / 'failures.txt', 'Failure analysis\nGenerated programmatically; suggestions are rules based on recorded results.\n\n' + failure_analysis(out))
    return index(out, report_dir)


def run_reports(out, error=''):
    """Include initial analysis, every evaluated candidate, and fresh verification."""
    out = Path(out).resolve()
    report_dir = out / 'reports'
    report_dir.mkdir(parents=True, exist_ok=True)
    evaluated = sorted({*out.glob('shinka/gen_*/results'), *out.glob('shinka/verification/*/results')})
    handoff = read_json(out / 'handoff.json')
    config = handoff.get('config', {})
    if config.get('bundle_spec'):
        baseline = Path(config['bundle_spec']).parent / 'baseline'
        if baseline.is_dir():
            evaluated.insert(0, baseline)
    for key in PATTERNS:
        references = [p / 'reports' / (key + '.txt') for p in evaluated if (p / 'reports' / (key + '.txt')).is_file()]
        raw = []
        for work in (out / 'baseline/design',):
            raw += [p for pattern in PATTERNS[key] for p in work.glob(pattern)]
        for graph in (out / 'graph', out / 'final/graph'):
            if key == 'yosys':
                raw += list(graph.glob('**/*yosys*.log')) + list(graph.glob('design.json.log')) + list(graph.glob('**/*.stat.txt'))
            elif key == 'opensta':
                raw += list(graph.glob('sta/*.log')) + list(graph.glob('sta/*.rpt'))
        summary = 'Per-evaluation reports (full tool output):\n' + '\n'.join(map(str, references)) + '\n'
        if key == 'equivalence':
            summary += 'Engine: Yosys equivalence passes. EQY is not invoked by this evaluator.\n'
        combine(report_dir / (key + '.txt'), LABELS[key], raw, summary)
    status = read_json(out / 'shinka/status.json')
    failures = ['Failure analysis', 'Generated programmatically; no model calls.',
                'Run: ' + str(out), 'State: ' + status.get('state', 'analysis / pre-launch'),
                'Run error: ' + (error or status.get('error') or 'none')]
    log = out / 'shinka.launch.log'
    if log.is_file():
        # Select error evidence, not prompts or provider request headers.
        with log.open(errors='replace') as stream:
            matches = [line.rstrip()[:1500] for line in stream if re.search(
                r'credit balance is too low|invalid x-api-key|authentication_error|rate_limit_error|Failed to generate proposal|Generation budget exhausted|Giving up query_|TimeoutError', line, re.I)]
        failures += ['Runner error evidence:', *matches[-15:]]
        if any('credit balance is too low' in line.lower() for line in matches):
            failures += ['Cause reported by Anthropic: insufficient API credits. Add API credits to that account or choose an authenticated Claude Code CLI connection for a new run.']
    for result in evaluated:
        failures += ['\n--- Candidate / baseline ---', failure_analysis(result)]
    for stage_log in ('baseline.log', 'graph.log', 'heuristics.log', 'final/graph.log'):
        path = out / stage_log
        if path.is_file():
            failures += ['Stage log: ' + str(path)]
            with path.open(errors='replace') as stream:
                lines = [line.rstrip()[:1500] for line in stream if re.search(r'error|failed|timeout|traceback', line, re.I)]
            failures += lines[-8:]
    write_text(report_dir / 'failures.txt', '\n'.join(failures) + '\n')
    return index(out, report_dir)
