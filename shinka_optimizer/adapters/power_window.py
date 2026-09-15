"""Labeled, vectorless power estimate; never substitute it for workload power."""
import math
import os
from pathlib import Path
import re
import shutil
import subprocess


def tcl(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('[', '\\[').replace(']', '\\]') + '"'


def estimate(work, liberty, *, netlist=None, top='benchmark', sdc=None, cwd=None,
             reset_port='arst_n', sta=None):
    work = Path(work).resolve()
    (work/'reports').mkdir(parents=True, exist_ok=True)
    netlist = Path(netlist or work/'build/benchmark_mapped.v').resolve()
    sdc = Path(sdc or work/'constraints/benchmark.sdc').resolve()
    script = (f'cd {tcl(Path(cwd or work).resolve())}\nread_liberty $::env(LIBERTY)\n'
              f'read_verilog {tcl(netlist)}\nlink_design {tcl(top)}\nread_sdc {tcl(sdc)}\n'
              'set_input_transition 0.03 [all_inputs]\n')
    if reset_port:
        script += f'set_case_analysis 1 [get_ports {tcl(reset_port)}]\n'
    script += ('report_checks -path_delay max -group_path_count 1\n'
               'set_power_activity -global -density 0.1 -duty 0.5\n'
               'report_power -digits 6\n')
    (work/'power.tcl').write_text(script)
    report = dict(kind='vectorless uniform density, not workload power',
                  density_per_ns=0.1, duty=0.5, input_transition_ns=0.03,
                  reset='inactive high' if reset_port else 'no additional reset assumption', parasitics=False, valid=False, watts=None)
    try:
        r = subprocess.run([sta or os.environ.get('STA') or shutil.which('sta'), '-no_init', '-exit', 'power.tcl'],
            cwd=work, env=dict(os.environ, LIBERTY=str(liberty)),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        (work/'reports/power.log').write_text(r.stdout)
        match = re.search(r'(?m)^Total\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)', r.stdout)
        values = [float(v) for v in match.groups()] if match else []
        if r.returncode or re.search(r'(?m)^(Error|Warning)[: ]', r.stdout) or len(values)!=4 or not all(math.isfinite(v) and v >= 0 for v in values) or values[-1] <= 0:
            raise ValueError('Missing/nonfinite power or tool error; inspect reports/power.log')
        report.update(valid=True, watts=dict(zip(('internal','switching','leakage','total'), values)))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report['error'] = str(exc)
    return report
