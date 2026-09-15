#!/usr/bin/env python3
"""Fixed timing mapping and transparent buffer trees; no clock/SDC relaxation.

Buffer proof contracts every inserted BUF_X4 to its input and compares every
original cell connection, module port and parameter against the mapped input.
This proves only the buffering transformation, not synthesis or physical PPA.
"""
import argparse
from collections import Counter, defaultdict
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from evaluate import DESIGN, FLOW_SCRIPTS, LIB, SCRIPTS, TASK, run


def abc_command(liberty, constraints, target_ps, fast=False, selection=''):
    # Yosys 0.68 removed -fast. Spell out its constrained Liberty recipe so
    # existing optimization settings retain their meaning on old and new Yosys.
    # User scripts do not get the default script's automatic retime insertion.
    # Inline {D} is expanded by ABC's Yosys pass using the supplied -D target.
    # Some ABC versions leave dangling mapped nodes; buffer rejects these even
    # though they drive no outputs. Cleanup removes only unused logic here.
    mode = ('-script +strash;dretime;retime,-o,{D};map,{D};cleanup;buffer;'
            'upsize,{D};dnsize,{D};stime,-p ' if fast else '')
    return (f'abc {mode}-liberty "{liberty}" -constr {constraints} '
            f'-D {target_ps}' + (f' {selection}' if selection else ''))


def buffer_netlist(netlist, fanout, rebalance=True, compact_buffers=False, buffer_cell="BUF_X4"):
    old = copy.deepcopy(netlist)
    count = 0
    for mname, mod in netlist['modules'].items():
        if int(mod.get('attributes', {}).get('blackbox', '0'), 2):
            continue
        cells = mod.get('cells', {})
        clock_bits = set()
        for cell in cells.values():
            for pin, bits in cell['connections'].items():
                if pin in ('CK', 'clk', 'src_clk', 'dst_clk'):
                    clock_bits.update(bits)
        # ABC can create a long serial buffer chain for a high-fanout net.
        # Contract data buffers first, then rebuild a balanced distribution tree.
        removed = {}
        for name, cell in list(cells.items()):
            if rebalance and re.fullmatch(r'BUF_X\d+', cell['type']):
                a, z = cell['connections']['A'][0], cell['connections']['Z'][0]
                if a not in clock_bits and z not in clock_bits:
                    removed[z] = a
                    del cells[name]
        def unbuffer(bit):
            seen = set()
            while bit in removed:
                assert bit not in seen, 'Existing buffer cycle'
                seen.add(bit)
                bit = removed[bit]
            return bit
        for cell in cells.values():
            cell['connections'] = {p: [unbuffer(b) for b in bs] for p, bs in cell['connections'].items()}
        for obj in list(mod['netnames'].values()) + list(mod['ports'].values()):
            obj['bits'] = [unbuffer(b) for b in obj['bits']]
        consumers = defaultdict(list)
        for cname, cell in cells.items():
            for pin, bits in cell['connections'].items():
                if pin in ('CK', 'clk', 'src_clk', 'dst_clk'):
                    clock_bits.update(bits)
                if cell['port_directions'][pin] == 'input':
                    for i, b in enumerate(bits):
                        if isinstance(b, int):
                            consumers[b].append((cname, pin, i))
        all_bits = [b for n in mod['netnames'].values() for b in n['bits'] if isinstance(b, int)]
        next_bit = max(all_bits, default=1) + 1
        identities = {}

        def tree(source, sinks):
            nonlocal next_bit, count
            if len(sinks) <= fanout:
                for name, pin, index in sinks:
                    cells[name]['connections'][pin][index] = source
                return
            # At most fanout child buffers, each recursively drives a subtree.
            if compact_buffers:
                capacity = fanout
                while capacity * fanout < len(sinks):
                    capacity *= fanout
                chunk = capacity
            else:
                chunk = math.ceil(len(sinks) / fanout)
            for start in range(0, len(sinks), chunk):
                bit = next_bit
                next_bit += 1
                name = f'timing_buffer_{count}'
                count += 1
                if name in cells:
                    raise ValueError('Buffer name collision')
                cells[name] = dict(hide_name=0, type=buffer_cell, parameters={}, attributes={},
                    port_directions={'A': 'input', 'Z': 'output'}, connections={'A': [source], 'Z': [bit]})
                mod['netnames'][name + '_net'] = dict(hide_name=0, bits=[bit], attributes={})
                identities[bit] = source
                tree(bit, sinks[start:start+chunk])

        for bit, sinks in consumers.items():
            if bit not in clock_bits and len(sinks) > fanout:
                tree(bit, sinks)

        def contract(bit):
            seen = set()
            while bit in identities:
                assert bit not in seen, 'Buffer cycle'
                seen.add(bit)
                bit = identities[bit]
            return bit

        original = old['modules'][mname]
        for p, port in original['ports'].items():
            assert [unbuffer(b) for b in port['bits']] == mod['ports'][p]['bits']
        for name, cell in original.get('cells', {}).items():
            if name not in cells:
                assert re.fullmatch(r'BUF_X\d+', cell['type'])
                assert unbuffer(cell['connections']['A'][0]) == unbuffer(cell['connections']['Z'][0])
                continue
            after = copy.deepcopy(cells[name])
            after['connections'] = {p: [contract(b) for b in bs] for p, bs in after['connections'].items()}
            before = copy.deepcopy(cell)
            before['connections'] = {p: [unbuffer(b) for b in bs] for p, bs in before['connections'].items()}
            assert after == before, (mname, name)
    return count


def implement(rtl, out, fanout=12, target_ps=500, fast=False, rebalance=True,
              compact_buffers=False, buffer_cell='BUF_X4', domain_targets_ps=None,
              slow_mapping_domains=None, full_adder_mapping=False, hierarchical_mapping=False):
    hierarchical, fa_map = hierarchical_mapping, full_adder_mapping
    domain_targets = domain_targets_ps or []
    slow_mapping_domains = slow_mapping_domains or []
    if fanout < 2 or buffer_cell not in ('BUF_X1', 'BUF_X2', 'BUF_X4'):
        raise ValueError('Invalid buffering recipe')
    if domain_targets and (len(domain_targets) != 5 or any(type(t) is not int or not 250 <= t <= 2000 for t in domain_targets)):
        raise ValueError('Five bounded domain mapping targets required')
    if any(type(i) is not int or i not in range(5) for i in slow_mapping_domains) or (slow_mapping_domains and not domain_targets):
        raise ValueError('Slow mapping requires declared domain targets')
    map_arg = ''
    if fa_map:
        map_path = Path('/tmp') / ('cogito_fa_' + hashlib.sha256(Path(__file__).with_name('fa_map.v').read_bytes()).hexdigest()[:16] + '.v')
        shutil.copy2(Path(__file__).with_name('fa_map.v'), map_path)
        map_arg = '-extra-map ' + str(map_path)
    out = Path(out).resolve()
    rtl = Path(rtl).resolve()
    for name in ('rtl', 'scripts', 'constraints', 'reports', 'build'):
        (out/name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(rtl, out/'rtl/benchmark.sv')
    for name in FLOW_SCRIPTS:
        shutil.copy2(SCRIPTS/name, out/'scripts'/name)
    shutil.copy2(DESIGN/'constraints/benchmark.sdc', out/'constraints/benchmark.sdc')
    abc_lib = Path('/tmp') / ('cogito_nangate_' + hashlib.sha256(LIB.read_bytes()).hexdigest()[:16] + '.lib')
    if not abc_lib.exists():
        shutil.copy2(LIB, abc_lib)
    abc_constr = Path('/tmp') / ('cogito_abc_' + hashlib.sha256(str(out).encode()).hexdigest()[:16] + '.constr')
    abc_constr.write_text('set_driving_cell BUF_X4\nset_load 5\n')
    shutil.copy2(abc_constr, out/'build/abc.constr')
    domain_abc = ''
    if domain_targets:
        for i, target in enumerate(domain_targets):
            sel = f'benchmark.domain*{i}*.engine'
            command = abc_command(abc_lib, abc_constr, target, i not in slow_mapping_domains, sel)
            domain_abc += f'select -assert-mod-count 1 {sel}\n{command}\n'
    ys = f'''read_liberty -lib "{LIB}"
read_verilog -sv rtl/benchmark.sv
hierarchy -top benchmark
{'uniquify benchmark/c:domain*.engine' if domain_targets else ('' if hierarchical else 'flatten -noscopeinfo t:*window_engine*')}
{'flatten -noscopeinfo benchmark.domain*.engine' if domain_targets else ''}
synth -top benchmark -noabc {map_arg}
opt -full
check -assert
dfflibmap -liberty "{LIB}"
{domain_abc}
{abc_command(abc_lib, abc_constr, target_ps, fast)}
{'flatten -noscopeinfo t:*engine*' if (hierarchical or domain_targets) else ''}
clean
opt_clean -purge
rename -enumerate
check -assert
write_json build/unbuffered.json
write_verilog -noattr -noexpr build/unbuffered.v
'''
    (out/'build/synth.ys').write_text(ys)
    run(['yosys', '-Q', 'build/synth.ys'], out, 'reports/synth.log', 240)
    netlist = json.loads((out/'build/unbuffered.json').read_text())
    # Check the actual Liberty buffer's identity function, not merely its name.
    libtext = LIB.read_text()
    buffer_types = {'BUF_X4', buffer_cell} | {c['type'] for m in netlist['modules'].values()
        for c in m.get('cells', {}).values() if re.fullmatch(r'BUF_X\d+', c['type'])}
    for typ in buffer_types:
        buf = re.split(r'(?m)^\s*cell \('+re.escape(typ)+r'\)\s*\{', libtext)[1].split('\n  cell ')[0]
        assert re.search(r'function\s*:\s*"A"', buf), f'{typ} must be identity'
    inserted = buffer_netlist(netlist, fanout, rebalance, compact_buffers, buffer_cell)
    (out/'build/buffered.json').write_text(json.dumps(netlist))
    export = f'''read_json build/buffered.json
hierarchy -top benchmark
check -assert
stat -liberty "{LIB}"
write_json build/benchmark_mapped.json
write_verilog -noattr -noexpr build/benchmark_mapped.v
'''
    (out/'build/export.ys').write_text(export)
    run(['yosys', '-Q', 'build/export.ys'], out, 'reports/export.log')
    counts = Counter()
    mods = netlist['modules']
    def walk(module):
        for c in mods[module]['cells'].values():
            typ = c['type']
            assert typ in mods and not typ.startswith('$'), typ
            if int(mods[typ].get('attributes', {}).get('blackbox', '0'), 2):
                counts[typ] += 1
            else:
                walk(typ)
    walk('benchmark')
    area = re.search(r'Chip area for top module .*: ([\d.]+)', (out/'reports/export.log').read_text())
    report = dict(standard_cells=sum(counts.values()), mapped_cell_area=float(area[1]),
        cells_by_type=dict(counts), passes_floor=sum(counts.values())>=50000,
        rtl_sha256=hashlib.sha256(rtl.read_bytes()).hexdigest(),
        liberty_sha256=hashlib.sha256(LIB.read_bytes()).hexdigest(),
        inserted_buffer_definitions=inserted, max_local_fanout=fanout,
        abc_target_ps=target_ps, abc_fast=fast, rebalance=rebalance, hierarchical_mapping=hierarchical, full_adder_mapping=fa_map, domain_targets_ps=domain_targets, compact_buffers=compact_buffers, buffer_cell=buffer_cell, slow_mapping_domains=slow_mapping_domains,
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        buffer_equivalence='PASS: exact structural identity contraction')
    assert report['passes_floor']
    (out/'reports/cell_count.json').write_text(json.dumps(report, indent=2)+'\n')
    run([sys.executable, 'scripts/run_sta.py', '--sta', os.environ.get('STA', shutil.which('sta')),
         '--lib', str(LIB)], out, 'reports/sta_run.log')
    timing = json.loads((out/'reports/timing_summary.json').read_text())
    print(json.dumps(dict(standard_cells=report['standard_cells'], area=report['mapped_cell_area'],
        buffers=inserted, wns_ns=timing['wns_ns'], tns_ns=timing['tns_ns'],
        groups=timing['worst_reported_path_by_group']), indent=2))

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--rtl', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--fanout', type=int, default=12)
    p.add_argument('--target-ps', type=int, default=500)
    p.add_argument('--abc-fast', action='store_true')
    p.add_argument('--keep-abc-buffers', action='store_true')
    a = p.parse_args()
    if a.fanout < 2:
        p.error('fanout must be >= 2')
    implement(a.rtl, a.out, a.fanout, a.target_ps, a.abc_fast, not a.keep_abc_buffers)
