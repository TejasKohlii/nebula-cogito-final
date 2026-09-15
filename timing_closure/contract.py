"""Freeze the evaluator and constraints for a whole evolutionary run."""
import hashlib
import json
import os
from pathlib import Path

TASK = Path(__file__).resolve().parent
ROOT = TASK.parent
# Mirrors evaluate.py: the frozen set follows whichever design the task selected.
DESIGN = Path(os.environ.get('COGITO_DESIGN_DIR') or ROOT/'custom_designs/custom_design_v2')
SCRIPTS = Path(os.environ.get('COGITO_SCRIPTS_DIR') or DESIGN/'scripts')
SEED = Path(os.environ.get('COGITO_SEED') or TASK/'initial.sv')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inputs():
    paths = [TASK/n for n in ('evaluate.py', 'implement.py', 'contract.py')] + [SEED]
    paths += [DESIGN/'rtl/benchmark.sv', DESIGN/'constraints/benchmark.sdc',
              ROOT/'third_party/nangate45/NangateOpenCellLibrary_typical.lib']
    paths += [SCRIPTS/n for n in
              ('synth.py', 'run_sta.py', 'cdc_constraints.py', 'sta.tcl', 'endpoint_slacks.tcl')]
    return {str(p.relative_to(ROOT)): digest(p) for p in paths}


def verify_manifest(path):
    manifest = json.loads(Path(path).read_text())
    actual = inputs()
    if actual != manifest['input_hashes']:
        changed = sorted(k for k in actual if actual[k] != manifest['input_hashes'].get(k))
        raise ValueError('Frozen evaluation contract changed: ' + ', '.join(changed))
    return manifest
