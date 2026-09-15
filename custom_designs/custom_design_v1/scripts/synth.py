#!/usr/bin/env python3
"""Map to a real Liberty and reject a design below the standard-cell floor."""
import argparse, collections, hashlib, json, os, pathlib, re, shutil, subprocess, tempfile
p = argparse.ArgumentParser()
p.add_argument('--lib', default='../../third_party/nangate45/NangateOpenCellLibrary_typical.lib')
p.add_argument('--floor', type=int, default=50000)
p.add_argument('--depth', type=int, default=40)
a = p.parse_args()
lib = pathlib.Path(a.lib).resolve()
if not lib.is_file(): p.error(f'Liberty not found: {lib}')
if a.depth < 2: p.error('depth must be >= 2')
yosys = os.environ.get('YOSYS', 'yosys')
# ABC parses Liberty paths separately and cannot accept spaces.
original_lib = lib
if any(c.isspace() for c in str(lib)):
    lib = pathlib.Path(tempfile.gettempdir()) / ('cogito_nangate_' + hashlib.sha256(original_lib.read_bytes()).hexdigest() + '.lib')
    if not lib.exists(): shutil.copy2(original_lib, lib)
pathlib.Path('build').mkdir(exist_ok=True)
pathlib.Path('reports').mkdir(exist_ok=True)
# Preserve hierarchy for generated-clock and CDC endpoint names, not cell padding.
script = f'''
read_liberty -lib "{lib}"
read_verilog -sv rtl/benchmark.sv
chparam -set DEPTH {a.depth} benchmark
synth -top benchmark -noabc
check -assert
dfflibmap -liberty "{lib}"
abc -liberty "{lib}"
clean
# This revision keeps the whole engine in one module, so loop/iterator nets such as
# `integer k` survive as unused public wires and are written as `wire signed [31:0] k`,
# which OpenSTA's Verilog reader rejects. Purging unused public nets removes the
# declaration; no cell, port or connection changes.
opt_clean -purge
rename -enumerate
check -assert
stat -liberty "{lib}"
write_verilog -noattr -noexpr build/benchmark_mapped.v
write_json build/benchmark_mapped.json
'''
pathlib.Path('build/synth.ys').write_text(script)
subprocess.run([yosys, '-Q', '-l', 'reports/synth.log', 'build/synth.ys'], check=True,
               stdout=subprocess.DEVNULL)
mods = json.loads(pathlib.Path('build/benchmark_mapped.json').read_text())['modules']
counts = collections.Counter()
def walk(name):
    for cell in mods[name].get('cells', {}).values():
        t = cell['type']
        if t in mods and not int(mods[t].get('attributes', {}).get('blackbox', '0'), 2): walk(t)
        elif t in mods and not t.startswith('$'): counts[t] += 1
        else: raise RuntimeError(f'Unmapped or unknown leaf: {t}')
walk('benchmark')
count = sum(counts.values())
area = re.search(r"Chip area for top module .*: ([0-9.]+)", pathlib.Path('reports/synth.log').read_text())
report = dict(top='benchmark', depth=a.depth, lanes=8,
              liberty=str(lib), liberty_sha256=hashlib.sha256(lib.read_bytes()).hexdigest(),
              rtl_sha256=hashlib.sha256(pathlib.Path('rtl/benchmark.sv').read_bytes()).hexdigest(),
              yosys=subprocess.check_output([yosys, '-V'], text=True).strip(),
              standard_cells=count, mapped_cell_area=float(area.group(1)) if area else None,
              floor=a.floor, passes_floor=count>=a.floor,
              cells_by_type=dict(sorted(counts.items())))
pathlib.Path('reports/cell_count.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='cells_by_type'}, indent=2))
if count < a.floor: raise SystemExit('FAIL: mapped standard-cell floor not met')
