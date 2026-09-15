"""Readable multi-file candidates for Shinka's single-document patch protocol."""
import json
from pathlib import Path, PurePosixPath
import shutil

BEGIN = '// COGITO-FILE-BEGIN '
END = '// COGITO-FILE-END'
START = '// EVOLVE-BLOCK-START'
STOP = '// EVOLVE-BLOCK-END'


def relative_name(name):
    if not isinstance(name, str) or not name or '\\' in name or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid candidate file path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name or path.suffix.lower() not in ('.sv', '.v', '.svh', '.vh'):
        raise ValueError(f'Candidate path must name a relative RTL file: {name}')
    return name


def normalize(text):
    # Existing evolution markers are comments; only the bundle has a region.
    text = ''.join(line for line in text.splitlines(True) if line.strip() not in (START, STOP))
    if any(line.strip().startswith((BEGIN.strip(), END)) for line in text.splitlines()):
        raise ValueError('Uploaded RTL contains reserved COGITO-FILE markers')
    return text if text.endswith('\n') else text + '\n'


def pack(files):
    return START + '\n' + ''.join(
        BEGIN + json.dumps(relative_name(name)) + '\n' + normalize(content) + END + '\n'
        for name, content in files.items()) + STOP + '\n'


def unpack(text, allowed):
    """Reject lost, duplicated, or injected files before writing any candidate."""
    allowed = {relative_name(name) for name in allowed}
    files, current, body = {}, None, []
    lines = text.strip().splitlines(True)
    if not lines or lines[0].strip() != START or lines[-1].strip() != STOP:
        raise ValueError('Multi-file candidate must retain its EVOLVE-BLOCK markers')
    for line in lines[1:-1]:
        token = line.strip()
        if token.startswith(BEGIN):
            if current is not None:
                raise ValueError('Nested candidate file sections')
            current = relative_name(json.loads(token[len(BEGIN):]))
            if current not in allowed or current in files:
                raise ValueError(f'Unexpected or duplicated candidate file: {current}')
            body = []
        elif token == END:
            if current is None:
                raise ValueError('Candidate file end without a beginning')
            files[current] = ''.join(body)
            current = None
        elif current is not None:
            if token in (START, STOP) or token.startswith('// COGITO-FILE'):
                raise ValueError('Invalid marker inside candidate file')
            body.append(line)
        elif token:
            raise ValueError('Candidate content outside a file section')
    if current is not None or set(files) != allowed:
        raise ValueError('Candidate must retain every uploaded RTL/header file and its file markers')
    return files


def materialize(spec, candidate, destination):
    golden = Path(spec['golden_root']).resolve()
    destination = Path(destination).resolve()
    files = unpack(Path(candidate).read_text(), spec['editable_files'])
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(golden, destination)
    for name, content in files.items():
        target = destination / name
        if not target.resolve().is_relative_to(destination) or target.is_symlink():
            raise ValueError(f'Candidate file escapes its source tree: {name}')
        target.write_text(content)
    sources = [destination / Path(p).relative_to(golden) for p in spec['golden_sources']]
    includes = [destination / Path(p).relative_to(golden) for p in spec['include_dirs']]
    return sources, includes
