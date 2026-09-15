"""Consistent HDL reads for discovery, evaluation, proof, and visualization."""
import hashlib
import os
from pathlib import Path
import tempfile
from .yosys_paths import include_option


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _slang_command(args):
    # Some plugin versions retain Yosys's quotes in inline filenames. Slang's
    # own command-file parser handles quoted paths and macro values correctly.
    content = '\n'.join(quote(arg) for arg in args) + '\n'
    base = Path('/tmp') / f'cogito-slang-args-{os.getuid()}'
    base.mkdir(mode=0o700, exist_ok=True)
    target = base / (hashlib.sha256(content.encode()).hexdigest() + '.f')
    with tempfile.NamedTemporaryFile(mode='w', dir=base, delete=False) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    temporary.replace(target)
    return 'read_slang -f ' + str(target)


def read_sources(sources, include_dirs=(), defines=(), *, frontend='verilog',
                 top=None, parameters=(), deferred=False, frontend_flags=None):
    if frontend == 'slang':
        flags = ['--single-unit', '--best-effort-hierarchy'] if frontend_flags is None else list(frontend_flags)
        args = [*flags, *('-I' + str(p) for p in include_dirs)]
        args += ['-D' + str(d) for d in defines]
        args += ['-G' + str(p) for p in parameters]
        if top:
            args += ['--top', str(top)]
        args += [str(source) for source in sources]
        return ['plugin -i slang', _slang_command(args)]
    if frontend != 'verilog':
        raise ValueError(f'Unsupported HDL frontend: {frontend}')
    commands = ['verilog_defaults -add ' + include_option(p) for p in include_dirs]
    commands += ['verilog_defines -D' + str(d) for d in defines]
    commands.append('read_verilog -sv ' + ('-defer ' if deferred else '') +
                    ' '.join(quote(source) for source in sources))
    return commands
