#!/usr/bin/env python3
"""Prepare an ordinary RTL folder for Cogito without model or agent calls.

The preparer discovers synthesizable Verilog/SystemVerilog, asks Yosys to
elaborate it and identify the top, then writes a content-addressed, immutable
snapshot containing Cogito and Shinka configuration.  The selected source
folder is only read; generated files live under cogito_webui/generated_designs.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import importlib.util

from design_config import load_config, task_contract
from pipeline import ROOT
from upload_constraints import constraint_sources, compile_constraints, Connectivity
from rtl_bundle import pack, normalize
sys.path.insert(0, str(ROOT / 'rtl_graph'))
from rtl_graph.yosys_frontend import read_sources


SCHEMA_VERSION = 12
OUTPUT_BASE = ROOT / 'cogito_webui/generated_designs'
DEFAULT_LIBERTY = ROOT / 'third_party/nangate45/NangateOpenCellLibrary_typical.lib'
RTL_SUFFIXES = {'.v', '.sv'}
HEADER_SUFFIXES = {'.vh', '.svh'}
COPY_SUFFIXES = RTL_SUFFIXES | HEADER_SUFFIXES | {'.sdc', '.tcl', '.lib', '.mem', '.hex'}
IGNORED_PARTS = {
    '.git', '.hg', '.svn', '__pycache__', 'node_modules', 'build', 'out',
    'output', 'outputs', 'reports', 'runs', 'results', 'obj', 'dist',
    'generated_designs',
}
TEST_PARTS = {'test', 'tests', 'tb', 'testbench', 'testbenches', 'sim', 'simulation', 'verification'}
TEST_STEM = re.compile(r'(^tb($|_)|(_tb|_test)$|testbench)', re.I)
PACKAGE = re.compile(r'(?m)^\s*package\s+(?:automatic\s+)?[A-Za-z_$\\][\w$\\]*')


def _quoted(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _ignored(relative):
    return any(part.lower() in IGNORED_PARTS or part.startswith('.') for part in relative.parts)


def _testbench(relative):
    parts = {part.lower() for part in relative.parts[:-1]}
    return bool(parts & TEST_PARTS) or bool(TEST_STEM.search(relative.stem))


def discover(folder):
    """Return ordered design sources, headers, and auxiliary contract files."""
    folder = Path(folder).resolve()
    sources, headers, auxiliaries = [], [], []
    total_bytes = 0
    for item in sorted(folder.rglob('*')):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(folder)
        if _ignored(relative):
            continue
        suffix = item.suffix.lower()
        if suffix not in COPY_SUFFIXES:
            continue
        size = item.stat().st_size
        total_bytes += size
        if total_bytes > 100 * 1024 * 1024:
            raise ValueError('RTL project is larger than the 100 MB preparation limit')
        if suffix in RTL_SUFFIXES:
            if not _testbench(relative):
                sources.append(item)
        elif suffix in HEADER_SUFFIXES:
            headers.append(item)
        else:
            auxiliaries.append(item)
    if not sources:
        raise ValueError('No design RTL found. Choose a folder containing .v or .sv source files (testbenches are ignored).')
    if len(sources) > 512:
        raise ValueError('More than 512 RTL source files were found; choose the design source folder rather than a repository root')
    # SystemVerilog packages must be parsed before modules that import them.
    sources.sort(key=lambda p: (0 if PACKAGE.search(p.read_text(errors='ignore')) else 1,
                                str(p.relative_to(folder)).lower()))
    return sources, headers, auxiliaries


def _run_yosys(folder, sources, headers, yosys, work):
    include_dirs = sorted({folder, *(p.parent for p in sources), *(p.parent for p in headers)}, key=str)
    finish = [
        'hierarchy -check -auto-top',
        'proc',
        'flatten',
        'opt_clean',
        f'write_json {_quoted(work / "elaborated.json")}',
    ]
    failures = []
    for frontend in ('verilog', 'slang'):
        ys = work / f'prepare_{frontend}.ys'
        log = work / f'prepare_{frontend}.log'
        (work / 'elaborated.json').unlink(missing_ok=True)
        script = read_sources(sources, include_dirs, frontend=frontend, deferred=True) + finish
        ys.write_text('\n'.join(script) + '\n')
        result = subprocess.run([yosys, '-q', '-l', str(log), '-s', str(ys)], cwd=folder,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=180)
        if not result.returncode and (work / 'elaborated.json').is_file():
            break
        detail = log.read_text(errors='replace')[-2200:] if log.is_file() else result.stdout[-2200:]
        failures.append(f'{frontend} frontend:\n{detail}')
    else:
        raise ValueError('The folder does not elaborate as synthesizable RTL. '
                         'Cogito tried both the native Verilog reader and Slang. '
                         'Advanced SystemVerilog requires Yosys with the yosys-slang plugin; '
                         'set YOSYS to that build if Slang could not be loaded.\n' + '\n'.join(failures))
    document = json.loads((work / 'elaborated.json').read_text())
    modules = document.get('modules', {})
    tops = [name for name, module in modules.items()
            if module.get('attributes', {}).get('top') in (1, '1', '00000000000000000000000000000001')]
    if len(tops) != 1:
        raise ValueError('Could not determine one top-level design module')
    top = tops[0]
    module = modules[top]
    if not module.get('ports'):
        raise ValueError(f'Top module {top} has no ports and looks like a testbench rather than an RTL design')
    return top, module, include_dirs, frontend


def _module_source(top, sources):
    if top.startswith('\\'):
        raise ValueError('Escaped top-module identifiers are not supported by automatic preparation')
    token = re.compile(r'(?m)^\s*module\s+(?:automatic\s+)?' + re.escape(top) + r'(?=\s|#|\()')
    matches = []
    for source in sources:
        text = re.sub(r'/\*.*?\*/|//[^\n]*', '', source.read_text(errors='ignore'), flags=re.S)
        if token.search(text):
            matches.append(source)
    if len(matches) != 1:
        names = ', '.join(str(p) for p in matches) or 'none'
        raise ValueError(f'Top module {top} must be defined in exactly one source file; matches: {names}')
    return matches[0]


def _clock_ports(module):
    ports = module.get('ports', {})
    input_bits = {bit: (name, index) for name, port in ports.items() if port.get('direction') == 'input'
                  for index, bit in enumerate(port.get('bits', [])) if isinstance(bit, int)}
    clock_bits = set()
    for cell in module.get('cells', {}).values():
        typ = cell.get('type', '').lower()
        if typ.startswith(('$dff', '$adff', '$sdff', '$aldff', '$ff')):
            clock_bits.update(bit for bit in cell.get('connections', {}).get('CLK', []) if isinstance(bit, int))
    inferred = []
    for bit in sorted(clock_bits):
        if bit in input_bits:
            inferred.append(input_bits[bit])
    if not inferred:
        for name, port in ports.items():
            if port.get('direction') == 'input' and re.search(r'(^|_)(clk|clock)(_|$)', name, re.I):
                inferred.extend((name, index) for index, _ in enumerate(port.get('bits', [])))
    # Preserve port declaration order while removing repeated matches.
    seen, result = set(), []
    for name, index in inferred:
        width = len(ports[name].get('bits', []))
        item = (name, index if width > 1 else None)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _sdc_text(clocks, period_ns=10.0):
    lines = ['# Generated deterministically by Cogito for an RTL folder without an SDC.',
             f'# Baseline clock period: {period_ns:g} ns.']
    clock_names = []
    for name, index in clocks:
        port = name if index is None else f'{name}[{index}]'
        clock = re.sub(r'[^A-Za-z0-9_]', '_', port) + '_clk'
        clock_names.append(clock)
        lines.append(f'create_clock -name {{{clock}}} -period {period_ns:g} [get_ports {{{port}}}]')
    if clock_names:
        for clock in clock_names:
            lines.append(f'set_clock_uncertainty {period_ns * 0.02:g} [get_clocks {{{clock}}}]')
        if len(clock_names) > 1:
            lines.append('set_clock_groups -asynchronous ' + ' '.join(
                f'-group [get_clocks {{{clock}}}]' for clock in clock_names))
        lines.append(f'set_input_delay 0 -clock [get_clocks {{{clock_names[0]}}}] [all_inputs -no_clocks]')
        lines.append(f'set_output_delay 0 -clock [get_clocks {{{clock_names[0]}}}] [all_outputs]')
    else:
        lines += [
            f'create_clock -name {{cogito_virtual}} -period {period_ns:g}',
            'set_input_delay 0 -clock [get_clocks {cogito_virtual}] [all_inputs]',
            'set_output_delay 0 -clock [get_clocks {cogito_virtual}] [all_outputs]',
        ]
    return '\n'.join(lines) + '\n'


def _pick(paths, top, suffix):
    candidates = [p for p in paths if p.suffix.lower() == suffix]
    if not candidates:
        return None
    def score(path):
        name = path.stem.lower()
        return (top.lower() in name, 'typical' in name, 'constraint' in name or 'design' in name,
                -len(path.parts), -len(str(path)))
    return max(candidates, key=score)


def _digest(folder, files, frontend='verilog'):
    sha = hashlib.sha256(f'cogito-design-prepare-v{SCHEMA_VERSION}\0{frontend}\0'.encode())
    for code in (Path(__file__), ROOT / 'sum_agent/upload_constraints.py',
                 ROOT / 'shinka_optimizer/adapters/generic_rtl.py',
                 ROOT / 'shinka_optimizer/rtl_bundle.py',
                 ROOT / 'rtl_graph/rtl_graph/yosys_frontend.py'):
        sha.update(code.read_bytes())
    for item in sorted(set(files), key=lambda p: str(p.relative_to(folder))):
        sha.update(str(item.relative_to(folder)).encode())
        sha.update(b'\0')
        sha.update(item.read_bytes())
        sha.update(b'\0')
    return sha.hexdigest()[:20]


def _relative_to_root(path):
    return str(Path(path).resolve().relative_to(ROOT))


def _configured(folder):
    config_path = folder / 'cogito.json'
    config = load_config(config_path)
    source = Path(config.get('source', ''))
    if not source.is_file():
        raise ValueError('cogito.json does not name an existing source file')
    task_contract(config, source)
    return {
        'kind': 'configured', 'folder': str(folder), 'config': str(config_path),
        'source': str(source), 'top': config['top'],
        'rtlFiles': 1 + len(config.get('additional_sources', [])),
        'generated': False, 'constraintSource': 'cogito.json',
    }


def prepare_folder(folder, output_base=OUTPUT_BASE, yosys=None):
    output_base = Path(output_base).resolve()
    output_base.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()[:20]
    with (output_base / f'.prepare-{key}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _prepare_folder(folder, output_base, yosys)


def _prepare_folder(folder, output_base, yosys):
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError('Selected path is not a directory')
    if (folder / 'cogito.json').is_file():
        return _configured(folder)

    sources, headers, auxiliaries = discover(folder)
    yosys = yosys or os.environ.get('YOSYS') or shutil.which('yosys')
    if not yosys:
        raise ValueError('Yosys is required to validate an uploaded RTL folder')
    output_base = Path(output_base).resolve()
    output_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.inspect-', dir=output_base) as inspect_dir:
        top, module, include_dirs, frontend = _run_yosys(folder, sources, headers, yosys, Path(inspect_dir))
    main = _module_source(top, sources)
    supplied_sdc = _pick(auxiliaries, top, '.sdc')
    supplied_liberty = _pick(auxiliaries, top, '.lib')
    liberty = supplied_liberty or DEFAULT_LIBERTY
    if not liberty.is_file():
        raise ValueError('No Liberty timing library was found and Cogito\'s default library is unavailable')

    # Preserve relative includes, memory initializers, Tcl helpers, constraints,
    # and local libraries.  Build/report trees were excluded during discovery.
    dependencies = []
    constraint_text = _sdc_text(_clock_ports(module))
    reference_path = None
    reference = None
    if supplied_sdc:
        constraint_text, dependencies = constraint_sources(folder, supplied_sdc)
        # A mapped JSON is only used to interpret old constraint names, never as
        # the candidate netlist. All timing is recomputed from fresh synthesis.
        sys.path.insert(0, str(ROOT / 'rtl_graph'))
        from rtl_graph.netlist_verilog import parse_liberty_cells
        library = parse_liberty_cells([str(liberty)])
        candidates = sorted(folder.glob('build/*mapped*.json'))
        if len(candidates) > 1:
            candidates = [p for p in candidates if p.stem == f'{top}_mapped']
        if len(candidates) == 1:
            reference_path = candidates[0]
            reference = Connectivity(json.loads(reference_path.read_text()), top, library)
    constraint_plan = compile_constraints(constraint_text, reference)
    copied = list(set([*sources, *headers, *auxiliaries, *dependencies,
                       *([reference_path] if reference_path else [])]))
    identity = _digest(folder, copied, frontend)
    destination = output_base / f'{folder.name}-{identity}'
    manifest_path = destination / 'prepared.json'
    if manifest_path.is_file():
        result = json.loads(manifest_path.read_text())
        if Path(result.get('config', '')).is_file() and Path(result.get('source', '')).is_file():
            preflight = destination / 'preflight-manifest.json'
            hashes = json.loads(preflight.read_text()).get('hashes', {}) if preflight.is_file() else {}
            if hashes and all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h
                              for p, h in hashes.items()):
                return result
    if destination.exists():
        # Keep failure diagnostics, but allow a later retry after tools are fixed.
        destination.rename(destination.with_name(f'.failed-{destination.name}-{time.time_ns()}'))

    temporary = Path(tempfile.mkdtemp(prefix=f'.{folder.name}-{identity}-', dir=output_base))
    final_golden = destination / 'golden'
    try:
        for source in sorted(set(copied)):
            target = temporary / 'golden' / source.relative_to(folder)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        main_relative = main.relative_to(folder)
        golden_main = final_golden / main_relative
        seed = destination / 'seed' / 'candidate.sv'
        temporary_seed = temporary / 'seed' / 'candidate.sv'
        temporary_seed.parent.mkdir(parents=True, exist_ok=True)
        editable = {}
        for source in [*sources, *headers]:
            name = source.relative_to(folder).as_posix()
            editable[name] = normalize(source.read_text())
            (temporary / 'golden' / name).write_text(editable[name])
        temporary_seed.write_text(pack(editable))

        sdc = destination / 'constraints/generated.sdc'
        generated_sdc = temporary / 'constraints/generated.sdc'
        generated_sdc.parent.mkdir(parents=True, exist_ok=True)
        generated_sdc.write_text('# Candidate-local constraints, resolved from frozen RTL wire bindings.\n'
                                 'source cogito_constraints.sdc\n')
        constraint_source = str(supplied_sdc) if supplied_sdc else 'generated 10 ns baseline'

        final_liberty = final_golden / supplied_liberty.relative_to(folder) if supplied_liberty else liberty
        ordered_sources = [final_golden / p.relative_to(folder) for p in sources]
        main_source_index = sources.index(main)
        additional = [path for index, path in enumerate(ordered_sources) if index != main_source_index]
        generated_incdirs = sorted({final_golden, *(final_golden / p.relative_to(folder).parent for p in [*sources, *headers])}, key=str)
        spec = {
            'schema_version': SCHEMA_VERSION,
            'top': top,
            'golden_sources': list(map(str, ordered_sources)),
            'candidate_format': 'rtl_bundle_v1',
            'golden_root': str(final_golden),
            'editable_files': list(editable),
            'main_source_index': main_source_index,
            'include_dirs': list(map(str, generated_incdirs)),
            'defines': [],
            'frontend': frontend,
            'sdc': str(sdc),
            'liberty': str(final_liberty),
            'design_cwd': str(final_golden),
            'yosys': yosys,
            'sta': os.environ.get('STA') or shutil.which('sta') or 'sta',
            'constraint_source': constraint_source,
            'constraint_plan': constraint_plan,
        }
        (temporary / 'design-spec.json').write_text(json.dumps(spec, indent=2) + '\n')

        root_rel_seed = _relative_to_root(seed)
        root_rel_spec = _relative_to_root(destination / 'design-spec.json')
        root_rel_sdc = _relative_to_root(sdc)
        root_rel_lib = _relative_to_root(final_liberty)
        root_rel_source = _relative_to_root(golden_main)
        root_rel_additional = [_relative_to_root(p) for p in additional]
        root_rel_source_order = [_relative_to_root(p) for p in ordered_sources]
        root_rel_incdirs = [_relative_to_root(p) for p in generated_incdirs]
        frozen = [
            root_rel_spec,
            'shinka_optimizer/adapters/generic_rtl.py',
            'shinka_optimizer/equivalence.py',
            'ShinkaEvolve/shinka/database/dbase.py',
            'ShinkaEvolve/shinka/database/parents.py',
            'ShinkaEvolve/shinka/database/inspirations.py',
            'shinka_optimizer/adapters/power_window.py',
            'sum_agent/upload_constraints.py',
            'rtl_graph/rtl_graph/sta_query.py',
            'rtl_graph/rtl_graph/yosys_paths.py',
            'rtl_graph/rtl_graph/yosys_frontend.py',
            'rtl_graph/rtl_graph/netlist_verilog.py',
            root_rel_source,
            *root_rel_additional,
            *[_relative_to_root(final_golden / p.relative_to(folder)) for p in headers],
            *[_relative_to_root(final_golden / p.relative_to(folder)) for p in auxiliaries],
            *[_relative_to_root(final_golden / p.relative_to(folder)) for p in dependencies],
            root_rel_sdc,
            root_rel_lib,
        ]
        root_from_destination = os.path.relpath(ROOT, destination)
        task = {
            'workspace': root_from_destination,
            'name': f'uploaded_{folder.name}_{identity}',
            'language': 'verilog',
            'seed': root_rel_seed,
            'evaluator': 'shinka_optimizer/adapters/generic_rtl.py',
            'evaluation_timeout_seconds': 720,
            'optimization': {'mode': 'timing_then_ppa', 'preclosure_ppa_budget': 0.05,
                             'closing_policy': 'stop_at_closure'},
            'goal': (
                f'Prioritize reaching WNS >= 0 and TNS = 0 for uploaded RTL top module {top}, while optimizing area and vectorless power from the beginning in the same archive. Before closure, a bounded PPA penalty of at most 0.05 is added to timing cost (negative WNS in ns + negative TNS/100); thus PPA can distinguish similar-timing candidates but cannot offset large timing regressions. Optimize geometric area/power cost while reaching closure; stop search at closure. Preserve the exact full-design '
                'cycle behavior, state transitions, reset behavior, top-level ports, widths, and parameters. '
                'The candidate document contains every design RTL and header file, delimited by '
                '// COGITO-FILE-BEGIN followed by a JSON-quoted relative path and // COGITO-FILE-END. '
                'Edit across any of these files, including coordinated changes to submodules and headers. '
                'Retain every file section, its path, and the outer EVOLVE markers. File contents are '
                'materialized into a separate folder before evaluation; they are not concatenated as RTL. '
                'Keep relative includes valid. Constraints, memory data, and the cell library are frozen. '
                'Every accepted candidate must pass fresh full-design sequential equivalence against '
                'the unchanged golden folder. '
                'Do not add padding, weaken constraints, alter clocks, or use simulation-only constructs. '
                'Stop search immediately after the first admissible timing-closed evaluation, including '
                'an already-closed seed. No post-closure generations are allowed. Optimize PPA on the '
                'way to closure. Fresh winner verification and final reporting still run; missing power '
                'is reported as incomplete and never authorizes another generation.'
            ),
            'model': 'headless/claude@claude-sonnet-5?effort=medium',
            'success': [
                {'metric': 'formal_equivalence', 'op': 'eq', 'value': True},
                {'metric': 'timing_closed', 'op': 'eq', 'value': True},
                {'metric': 'wns_ns', 'op': 'ge', 'value': 0},
                {'metric': 'tns_ns', 'op': 'eq', 'value': 0},
                {'metric': 'violating_endpoints', 'op': 'eq', 'value': 0},
                {'metric': 'power_valid', 'op': 'eq', 'value': True},
            ],
            'search': {
                'islands': 2, 'archive_size': 40, 'archive_inspirations': 2,
                'parent_selection_lambda': 4.,
                'top_k_inspirations': 1,
                'llm_kwargs': {'temperatures': [0], 'max_tokens': 10000},
                'parent_selection_strategy': 'weighted',
            },
            'environment': {
                'SHINKA_HEADLESS_COMMAND': '"{workspace}/.tools/node_modules/.bin/headless"',
                'SHINKA_HEADLESS_TIMEOUT': '420',
                'COGITO_GENERIC_SPEC': '{workspace}/' + root_rel_spec,
                'MPLCONFIGDIR': '{workspace}/.cache/matplotlib',
                'XDG_CACHE_HOME': '{workspace}/.cache',
            },
            'frozen_inputs': sorted(set(frozen)),
        }
        (temporary / 'task.json').write_text(json.dumps(task, indent=2) + '\n')
        config = {
            'source': root_rel_source,
            'task': _relative_to_root(destination / 'task.json'),
            'top': top,
            'sdc': root_rel_sdc,
            'liberty': root_rel_lib,
            'design_cwd': _relative_to_root(destination / 'baseline/design'),
            'netlist': _relative_to_root(destination / 'baseline/design/build/design_mapped.v'),
            'yosys': 'yosys', 'sta': 'sta', 'path_base': root_from_destination,
            'parameters': [], 'additional_sources': root_rel_additional,
            'source_order': root_rel_source_order, 'main_source_index': main_source_index,
            'bundle_spec': root_rel_spec,
            'source_cwd': _relative_to_root(final_golden),
            'include_dirs': root_rel_incdirs, 'defines': [], 'frontend': frontend,
            'final_netlist': '{verification}/results/design/build/design_mapped.v',
            'final_design_cwd': '{verification}/results/design',
        }
        (temporary / 'cogito.json').write_text(json.dumps(config, indent=2) + '\n')
        result = {
            'kind': 'generated', 'folder': str(folder),
            'config': str(destination / 'cogito.json'), 'source': str(golden_main),
            'top': top, 'rtlFiles': len(sources), 'generated': True,
            'editableFiles': len(editable), 'editScope': 'All design RTL and headers',
            'frontend': frontend,
            'constraintSource': constraint_source,
            'libertySource': str(supplied_liberty) if supplied_liberty else 'Cogito Nangate45 default',
            'workspace': str(destination),
        }
        try:
            temporary.rename(destination)
        except FileExistsError:
            shutil.rmtree(temporary)
            if manifest_path.is_file():
                return json.loads(manifest_path.read_text())
            raise ValueError('This design is already being prepared, or an earlier preparation is incomplete. See baseline/preflight.log in ' + str(destination))
        # Exercise the exact evaluator contract before exposing Optimize. This
        # performs formal proof, synthesis and STA only; it makes no model calls.
        task_module_spec = importlib.util.spec_from_file_location('upload_task', ROOT / 'shinka_optimizer/task.py')
        task_module = importlib.util.module_from_spec(task_module_spec)
        task_module_spec.loader.exec_module(task_module)
        loaded_task = task_module.load_task(destination / 'task.json')
        preflight_manifest = destination / 'preflight-manifest.json'
        preflight_manifest.write_text(json.dumps({'task': loaded_task, 'hashes': task_module.snapshot(loaded_task)}))
        baseline = destination / 'baseline'
        baseline.mkdir()
        env = dict(os.environ, **loaded_task['environment'], SHINKA_TASK_MANIFEST=str(preflight_manifest))
        with (baseline / 'preflight.log').open('w') as log:
            subprocess.run([sys.executable, '-B', loaded_task['evaluator'], '--program_path', str(seed),
                            '--results_dir', str(baseline)], env=env, cwd=ROOT,
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
        verdict = json.loads((baseline / 'correct.json').read_text())
        if not verdict.get('correct'):
            raise ValueError('Uploaded design failed evaluator preflight: ' + verdict.get('error', '')
                             + '\nDetails: ' + str(baseline / 'preflight.log'))
        result['baseline'] = json.loads((baseline / 'metrics.json').read_text())['public']
        result['reports'] = json.loads((baseline / 'reports/index.json').read_text())['files']
        manifest_path.write_text(json.dumps(result, indent=2) + '\n')
        return json.loads((destination / 'prepared.json').read_text())
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output-base', type=Path, default=OUTPUT_BASE)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_folder(args.folder, args.output_base)))
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print('COGITO_PREP_ERROR ' + json.dumps(str(exc)), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
