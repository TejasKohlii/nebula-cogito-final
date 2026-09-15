"""Name-independent retry for failed internal equivalence correspondences.

Rebuild from the original pair: never delete obligations from an existing proof,
assume failed points equal, or blacklist interface signals.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rtl_graph'))
from rtl_graph.yosys_frontend import read_sources


def _quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _read_sources(sources, include_dirs=(), defines=(), frontend='verilog', top=None):
    return read_sources(sources, include_dirs, defines, frontend=frontend, top=top)


def unmatched_internal_names(netlist):
    module = netlist['modules']['equiv']
    ports = {bit for port in module['ports'].values() for bit in port['bits']}
    failed = set()
    for cell in module['cells'].values():
        c = cell['connections']
        if cell['type'] == '$equiv' and c['A'] != c['B']:
            failed.update(c['Y'])
    return sorted(name for name, net in module['netnames'].items()
                  if not name.startswith('$') and not net.get('hide_name')
                  and failed.intersection(net['bits']) and not ports.intersection(net['bits']))


def prove_pair(gold, candidate, top, work, timeout=120):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    base = []
    for role, source in [('gold', gold), ('gate', candidate)]:
        base += ['design -reset-vlog', f'read_verilog -sv "{Path(source).resolve()}"',
                 f'prep -top {top} -flatten', 'async2sync', 'alumacc', 'techmap', 'opt_clean',
                 f'rename {top} {role}', f'write_json {role}_interface.json', f'design -stash {role}']
    base += ['design -copy-from gold -as gold gold', 'design -copy-from gate -as gate gate']
    excluded = set()
    attempts = []
    for attempt, depth in enumerate((4, 4, 12)):
        stem = f'formal_match_{attempt}'
        blacklist = work/'internal_matches.txt'
        blacklist.write_text(''.join(name+'\n' for name in sorted(excluded)))
        make = 'equiv_make '+('-blacklist internal_matches.txt ' if excluded else '')+'gold gate equiv'
        script = base + [make, 'hierarchy -top equiv', f'write_json {stem}_initial.json', 'equiv_simple',
                         f'write_json {stem}_simple.json',
                         f'equiv_induct -seq {depth}', f'write_json {stem}.json',
                         'equiv_status -assert']
        (work/f'{stem}.ys').write_text('\n'.join(script)+'\n')
        for artifact in (work/f'{stem}.json', work/f'{stem}_initial.json', work/f'{stem}_simple.json', work/'gold_interface.json', work/'gate_interface.json'):
            artifact.unlink(missing_ok=True)  # Never interpret a stale proof dump after a timeout.
        try:
            with (work/f'{stem}.log').open('w') as log:
                result = subprocess.run([os.environ.get('YOSYS', 'yosys'), '-Q', f'{stem}.ys'], cwd=work,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            passed = result.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
        # Interface identity is a separate mandatory check, including unused ports.
        interfaces = []
        for role in ('gold', 'gate'):
            path = work/f'{role}_interface.json'
            interfaces.append({name:(p['direction'],len(p['bits'])) for name,p in
                json.loads(path.read_text())['modules'][role]['ports'].items()} if path.exists() else None)
        if interfaces[0] is None or interfaces[0] != interfaces[1]:
            raise RuntimeError('Formal interface mismatch or incomplete elaboration')
        attempts.append(dict(attempt=attempt, depth=depth, passed=passed, excluded_internal_names=sorted(excluded)))
        report = dict(passed=passed, attempts=attempts,
                      method='Fresh equivalence construction; all interface checks retained')
        (work/'proof_summary.json').write_text(json.dumps(report, indent=2)+'\n')
        if passed:
            return report
        # Recover correspondences even when the solver timed out before its
        # final dump. Rebuild a fresh proof; never assume excluded points equal.
        for suffix in ('', '_simple', '_initial'):
            dump = work/f'{stem}{suffix}.json'
            if dump.exists():
                excluded.update(unmatched_internal_names(json.loads(dump.read_text())))
                break
    raise RuntimeError('Equivalence inconclusive after fresh internal-match retries; '
                       'no counterexample established. See proof_summary.json and formal_match_*.log. '
                       'Unmatched internal names: '+', '.join(sorted(excluded)[:20]))


def prove_sources(gold_sources, candidate_sources, top, work, include_dirs=(), defines=(),
                  yosys='yosys', timeout=120, candidate_include_dirs=None,
                  gold_cwd=None, candidate_cwd=None, frontend='verilog'):
    """Prove two complete source sets while retaining the pairwise proof policy.

    Gold and candidate have separate include trees. Existing benchmark proof
    behavior is unchanged.
    """
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    base = []
    for role, sources in [('gold', gold_sources), ('gate', candidate_sources)]:
        directories = candidate_include_dirs if role == 'gate' and candidate_include_dirs is not None else include_dirs
        # Separate processes also isolate Yosys's cached include contents.
        # Resetting Verilog defaults/macros alone can reuse a golden header.
        netlist = work / f'{role}.il'
        interface = work / f'{role}_interface.json'
        script = work / f'{role}_elaboration.ys'
        log_path = work / f'{role}_elaboration.log'
        script.write_text('\n'.join([*_read_sources(sources, directories, defines, frontend, top),
            f'hierarchy -check -top {top}', 'proc', 'memory', 'flatten',
            'async2sync', 'alumacc', 'techmap', 'opt_clean', f'rename {top} {role}',
            f'write_json {_quote(interface)}', f'write_rtlil {_quote(netlist)}']) + '\n')
        netlist.unlink(missing_ok=True)
        interface.unlink(missing_ok=True)
        with log_path.open('w') as log:
            result = subprocess.run([yosys, '-Q', '-s', str(script)],
                                    cwd=(gold_cwd if role == 'gold' else candidate_cwd) or work,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode or not netlist.is_file():
            raise RuntimeError(f'Formal {role} elaboration failed: {log_path}\n' + log_path.read_text(errors='replace')[-2500:])
        base += [f'read_rtlil {_quote(netlist)}', f'design -stash {role}']
    base += ['design -copy-from gold -as gold gold', 'design -copy-from gate -as gate gate']
    excluded = set()
    attempts = []
    for attempt, depth in enumerate((4, 4, 12)):
        stem = f'formal_match_{attempt}'
        blacklist = work / 'internal_matches.txt'
        blacklist.write_text(''.join(name + '\n' for name in sorted(excluded)))
        make = 'equiv_make ' + ('-blacklist internal_matches.txt ' if excluded else '') + 'gold gate equiv'
        script = base + [make, 'hierarchy -top equiv', f'write_json {stem}_initial.json',
                         'equiv_simple', f'write_json {stem}_simple.json',
                         f'equiv_induct -seq {depth}', f'write_json {stem}.json',
                         'equiv_status -assert']
        (work / f'{stem}.ys').write_text('\n'.join(script) + '\n')
        for artifact in (work / f'{stem}.json', work / f'{stem}_initial.json',
                         work / f'{stem}_simple.json'):
            artifact.unlink(missing_ok=True)
        try:
            with (work / f'{stem}.log').open('w') as log:
                result = subprocess.run([yosys, '-Q', f'{stem}.ys'], cwd=work,
                                        stdout=log, stderr=subprocess.STDOUT,
                                        timeout=timeout)
            passed = result.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
        interfaces = []
        for role in ('gold', 'gate'):
            interface = work / f'{role}_interface.json'
            interfaces.append({name: (port['direction'], len(port['bits'])) for name, port in
                json.loads(interface.read_text())['modules'][role]['ports'].items()}
                if interface.exists() else None)
        if interfaces[0] is None or interfaces[0] != interfaces[1]:
            raise RuntimeError('Formal interface mismatch or incomplete elaboration')
        attempts.append(dict(attempt=attempt, depth=depth, passed=passed,
                             excluded_internal_names=sorted(excluded)))
        report = dict(passed=passed, attempts=attempts,
                      method='Fresh full-design equivalence; all interface checks retained')
        (work / 'proof_summary.json').write_text(json.dumps(report, indent=2) + '\n')
        if passed:
            return report
        for suffix in ('', '_simple', '_initial'):
            dump = work / f'{stem}{suffix}.json'
            if dump.exists():
                excluded.update(unmatched_internal_names(json.loads(dump.read_text())))
                break
    raise RuntimeError('Equivalence inconclusive after fresh internal-match retries; '
                       'no counterexample established. See proof_summary.json and formal_match_*.log. '
                       'Unmatched internal names: ' + ', '.join(sorted(excluded)[:20]))
