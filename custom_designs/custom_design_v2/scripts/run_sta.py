#!/usr/bin/env python3
"""Run STA, treating Tcl errors as failures even if the executable returns 0."""
import argparse, hashlib, json, os, pathlib, re, subprocess, sys
p=argparse.ArgumentParser()
p.add_argument('--sta',default=os.environ.get('STA', 'sta'))
p.add_argument('--lib',required=True)
a=p.parse_args()
lib=pathlib.Path(a.lib).resolve()
report=json.loads(pathlib.Path('reports/cell_count.json').read_text())
for path,key in [(lib,'liberty_sha256'),(pathlib.Path('rtl/benchmark.sv'),'rtl_sha256')]:
    if hashlib.sha256(path.read_bytes()).hexdigest()!=report[key]:
        raise SystemExit(f'{path} changed since synthesis; rerun make synth')
subprocess.run([sys.executable,'scripts/cdc_constraints.py'],check=True)
env=dict(os.environ,LIBERTY=str(lib))
r=subprocess.run([a.sta,'-exit','scripts/sta.tcl'],env=env,text=True,
                 stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
pathlib.Path('reports/timing.log').write_text(r.stdout)
if r.returncode or re.search(r'(?m)^(?:Error|Warning)(?:[: ]|$)',r.stdout):
    print(r.stdout)
    raise SystemExit('STA failed; see reports/timing.log')
print(r.stdout)

groups={}
for block in r.stdout.split('Startpoint: ')[1:]:
    group=re.search(r'Path Group: ([^\n]+)',block)
    slack=re.search(r'([-+\d.]+)\s+slack \(',block)
    if group and slack:
        name=group[1]; value=float(slack[1])
        if name not in groups or value<groups[name]['worst_slack_ns']:
            groups[name]=dict(worst_slack_ns=value,startpoint=block.splitlines()[0],
                             endpoint=re.search(r'Endpoint: ([^\n]+)',block)[1])
summary=dict(tool=r.stdout.splitlines()[0],stage='pre-layout, ideal clocks, no parasitics',
             clock_count=15,constraint_warnings=0,
             wns_ns=float(re.search(r'wns max ([-\d.]+)',r.stdout)[1]),
             tns_ns=float(re.search(r'tns max ([-\d.]+)',r.stdout)[1]),
             worst_reported_path_by_group=groups)
pathlib.Path('reports/timing_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
