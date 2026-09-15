#!/usr/bin/env python3
"""Headless Claude adapter: bounded native read tools over staged evidence only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
import shutil
from evidence import stage
from native_usage import totals
from task import verify


def main():
    args = sys.argv[1:]
    binary = shutil.which(os.environ.get('COGITO_CLAUDE', 'claude'))
    if not binary:
        raise SystemExit('Claude CLI is unavailable; put claude on PATH or set COGITO_CLAUDE.')
    if '-p' not in args and '--print' not in args:
        os.execv(binary, ['claude', *args])
    run = Path(os.environ['SHINKA_EVIDENCE_RUN']).resolve()
    out = stage(run)
    investigation = verify(run/'task_manifest.json')['task'].get('investigation', {})
    reads = min(6, max(1, int(investigation.get('max_additional_reads', 1))))
    budget = .5
    limit = float(os.environ.get('SHINKA_CAMPAIGN_COST_LIMIT', '6.25'))
    if totals(run)['budget_accounted_cost'] + budget > limit:
        raise SystemExit('Insufficient remaining native budget for another proposal')
    usage = dict(native_cost_usd=None, reserved_usd=budget, state='running')
    (out/'usage.json').write_text(json.dumps(usage))
    # Replace Headless's broader read permissions with a narrow native tool set.
    cleaned = []
    i = 0
    while i < len(args):
        if args[i] in ('--allowedTools', '--allowed-tools', '--tools'):
            i += 2
        else:
            cleaned.append(args[i])
            i += 1
    cleaned += ['--safe-mode', '--restricted', '--setting-sources', '',
        '--tools', 'Read,Grep,Glob', '--allowedTools', 'Read,Grep,Glob',
        '--strict-mcp-config', '--no-session-persistence', '--max-turns', str(reads+4),
        '--max-budget-usd', str(budget), '--append-system-prompt',
        'Use only the staged evidence in this working directory and the supplied prompt. '
        'Read BRIEF.json first; it contains the selected best evidence and recent failures. '
        f'At most {reads} additional focused reads if essential. Avoid listing directories or rereading the contract already in the prompt. '
        'Return a patch within this session; do not request permission or delegate. '
        'Earlier failed attempts are evidence to learn from. Preserve the task contract.']
    proc = subprocess.Popen([binary, *cleaned], cwd=out, stdout=subprocess.PIPE,
                            text=True, bufsize=1)
    started = time.monotonic()
    progress = dict(pid=proc.pid, proposal=out.name, state='model_running', last_event=None)
    def atomic(path, value):
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(value, indent=2)+'\n')
        tmp.replace(path)
    def reader():
        with (out/'trace.jsonl').open('w') as trace:
            for line in proc.stdout:
                trace.write(line)
                trace.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
                try:
                    event = json.loads(line)
                    progress['last_event'] = event.get('type')
                    if event.get('type') == 'result':
                        usage.update(native_cost_usd=event.get('total_cost_usd'), state=event.get('subtype'),
                            models=event.get('modelUsage'), usage=event.get('usage'))
                        atomic(out/'usage.json', usage)
                except ValueError:
                    pass
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        while proc.poll() is None:
            progress['elapsed_seconds'] = round(time.monotonic()-started, 1)
            progress.update(totals(run))
            atomic(run/'live_progress.json', progress)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        thread.join(timeout=3)
    finally:
        progress.update(state='model_finished', returncode=proc.returncode, **totals(run))
        atomic(run/'live_progress.json', progress)
    raise SystemExit(proc.returncode)


if __name__ == '__main__':
    main()
