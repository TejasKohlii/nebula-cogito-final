"""Derive compact timing/RTL evidence from mapped netlists and actual STA reports."""
import json
from pathlib import Path
import re


def path_details(text):
    """Parse complete reported max paths; never treat global fanout as path evidence."""
    paths = []
    for block in re.split(r'(?=^Startpoint:)', text, flags=re.M):
        start = re.search(r'^Startpoint: (.+)', block, re.M)
        end = re.search(r'^Endpoint: (.+)', block, re.M)
        group = re.search(r'^Path Group: (.+)', block, re.M)
        slack = re.search(r'^\s*([-+\d.]+)\s+slack \(', block, re.M)
        if not all((start, end, group, slack)) or 'Path Type: max' not in block:
            continue
        data = block.split('data arrival time')[0]
        rows = []
        for row in data.splitlines():
            m = re.match(r'^\s*(\d+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([\^v])\s+(\S+)\s+\(([^)]+)\)', row)
            if m:
                fanout, cap, slew, delay, arrival, edge, pin, cell = m.groups()
                rows.append(dict(pin=pin, cell=cell, fanout=int(fanout),
                    cap=float(cap), slew_ns=float(slew), delay_ns=float(delay), arrival_ns=float(arrival)))
        if not rows:
            continue
        # The first Q stage is clock-to-Q. Everything else is classified using
        # the actual reported cell; no arithmetic structure is guessed here.
        launch = rows[0] if rows[0]['pin'].endswith(('/Q', '/QN')) else None
        rest = rows[1:] if launch else rows
        buffers = [r for r in rest if re.fullmatch(r'BUF_X\d+', r['cell'])]
        logic = [r for r in rest if r not in buffers]
        paths.append(dict(group=group[1], startpoint=start[1], endpoint=end[1],
            worst_slack_ns=float(slack[1]), cell_stages=len(rows), logic_stages=len(logic),
            buffer_stages=len(buffers), clock_to_q_ns=launch['delay_ns'] if launch else 0.,
            buffer_delay_ns=round(sum(r['delay_ns'] for r in buffers), 4),
            logic_delay_ns=round(sum(r['delay_ns'] for r in logic), 4),
            arrival_ns=rows[-1]['arrival_ns'],
            slowest_cells=sorted(rest, key=lambda r:r['delay_ns'], reverse=True)[:4],
            max_fanout=max(r['fanout'] for r in rows),
            raw_path=block.strip()))
    return sorted(paths, key=lambda p:p['worst_slack_ns'])


def compare_paths(current, previous):
    """Group-level comparison, explicitly flagging path switches."""
    old = {p['group']:p for p in reversed(previous)}
    result = []
    for p in current:
        q = old.get(p['group'])
        if not q:
            continue
        same = (p.get('source_rtl'), p.get('destination_rtl')) == (q.get('source_rtl'), q.get('destination_rtl'))
        result.append(dict(group=p['group'], same_rtl_endpoints=same,
            interpretation='Reported group worst paths; a path switch is not a same-path experiment',
            deltas={key:round(p[key]-q[key], 4) for key in
                ('worst_slack_ns','logic_stages','buffer_stages','clock_to_q_ns','logic_delay_ns','buffer_delay_ns','arrival_ns')}))
    return result


def aliases(modules, top, path):
    parts = path.split(' (')[0].strip().split('/')
    module = modules[top]
    for part in parts[:-1]:
        cell = module.get('cells', {}).get(part)
        if not cell or cell['type'] not in modules:
            return []
        module = modules[cell['type']]
    cell = module.get('cells', {}).get(parts[-1])
    if cell is None:
        return [parts[-1]]
    bits = set(cell['connections'].get('Q', []))
    result = []
    for name, net in module.get('netnames', {}).items():
        if name.startswith(('_', '$')):
            continue
        for index, bit in enumerate(net['bits']):
            if bit in bits:
                result.append('/'.join(parts[:-1]+[name+(f'[{index}]' if len(net['bits'])>1 else '')]))
    return result[:12]


def diagnostics(work, top):
    work = Path(work)
    modules = json.loads((work/'build/benchmark_mapped.json').read_text())['modules']
    summary = json.loads((work/'reports/timing_summary.json').read_text())
    paths = []
    for group, values in summary['worst_reported_path_by_group'].items():
        paths.append(dict(group=group, **values,
            source_rtl=aliases(modules, top, values['startpoint']),
            destination_rtl=aliases(modules, top, values['endpoint'])))
    paths.sort(key=lambda p:p['worst_slack_ns'])
    text = (work/'reports/timing.log').read_text()
    detailed = path_details(text)
    selected, seen = [], set()
    for item in detailed:
        if item['group'] in seen:
            continue
        seen.add(item['group'])
        item['source_rtl'] = aliases(modules, top, item['startpoint'])
        item['destination_rtl'] = aliases(modules, top, item['endpoint'])
        selected.append(item)
        if len(selected) == 3:
            break
    high = []
    for row in text.splitlines():
        match = re.match(r'^\s*(\d+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+(.*)', row)
        if match:
            fanout, cap, slew, delay, arrival, desc = match.groups()
            high.append(dict(fanout=int(fanout), capacitance=float(cap), slew_ns=float(slew),
                             cell_delay_ns=float(delay), arrival_ns=float(arrival), pin=desc))
    unique = {json.dumps(p, sort_keys=True):p for p in high}
    high = sorted(unique.values(), key=lambda p:p['fanout'], reverse=True)[:12]
    return dict(paths=paths[:8], critical_paths=selected, high_fanout=high,
        evidence='Mapped Q-bit RTL aliases and OpenSTA path rows; aliases can be absent after synthesis',
        timing_scope=summary['stage'], units='STA ns; capacitance in Liberty units')
