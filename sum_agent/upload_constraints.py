"""Relocate uploaded SDC dependencies and rebind mapped objects by RTL connectivity.

No uploaded Python/build scripts are executed. Literal Tcl source dependencies are
inlined. Static mapped register references are compiled into stable wire aliases,
then resolved anew against each candidate's mapped JSON. Missing/ambiguous bindings
are errors, never silently removed timing constraints.
"""
from collections import defaultdict
import json
from pathlib import Path
import re


SOURCE = re.compile(r'^\s*source\s+(?:\{([^{}]+)\}|"([^"$\[\]]+)"|([^\s;$\[\]{}]+))\s*(?:;?\s*#.*)?$')
QUERY = re.compile(r'\[(get_pins|get_cells)\s+\{([^{}]*)\}\]')
PIN_LIST = re.compile(r'(?m)^(\s*set\s+[^\s{}]+\s+)\{([^{}]*)\}\s*$')


def constraint_sources(folder, sdc):
    """Return inlined text and the exact dependencies, including ignored build/ files."""
    folder = Path(folder).resolve()
    files = set()

    def read(path, stack):
        path = path.resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise ValueError(f'SDC dependency is missing or outside the selected folder: {path}')
        if path in stack:
            raise ValueError(f'Cyclic SDC source dependency: {path}')
        files.add(path)
        lines = []
        for line in path.read_text().replace('\\\n', ' ').splitlines():
            if re.match(r'^\s*source\s', line):
                match = SOURCE.fullmatch(line)
                if not match:
                    raise ValueError(f'Cannot resolve dynamic Tcl source in {path}: {line.strip()}')
                name = next(v for v in match.groups() if v is not None)
                target = folder / name
                if not target.is_file():
                    target = path.parent / name
                lines.append(read(target, [*stack, path]))
            else:
                lines.append(line)
        return '\n'.join(lines) + '\n'

    return read(Path(sdc), []), sorted(files)


class Connectivity:
    def __init__(self, document, top, library):
        self.cells = {}
        self.aliases = defaultdict(set)
        self.by_alias = defaultdict(set)
        self.registers = defaultdict(set)
        self.pins = defaultdict(set)
        modules = document['modules']

        def descend(name, prefix, connected, stack):
            if name in stack:
                raise ValueError('Recursive mapped hierarchy')
            module = modules[name]
            def bit(value):
                return connected.get(value, (prefix, value)) if isinstance(value, int) else ('constant', value)
            for net, rec in module.get('netnames', {}).items():
                if rec.get('hide_name') or net.startswith('$') or re.search(r'(^|[./])_\d+_($|\[)', net):
                    continue
                for i, value in enumerate(rec['bits']):
                    if not isinstance(value, int):
                        continue
                    alias = prefix + net + (f'[{i + rec.get("offset", 0)}]' if len(rec['bits']) > 1 else '')
                    self.aliases[bit(value)].add(alias)
                    self.by_alias[alias].add(bit(value))
            for instance, cell in module.get('cells', {}).items():
                full = prefix + instance
                typ = cell['type']
                if typ in modules and typ not in library and not int(str(modules[typ].get('attributes', {}).get('blackbox', '0')), 2):
                    sub = modules[typ]
                    mapping = {b: bit(parent) for port, values in cell['connections'].items()
                               for b, parent in zip(sub['ports'][port]['bits'], values)}
                    descend(typ, full + '/', mapping, [*stack, name])
                    continue
                pins = {pin: [bit(b) for b in values] for pin, values in cell['connections'].items()}
                seq = library.get(typ, {}).get('seq', False)
                outputs = library.get(typ, {}).get('outputs', [])
                # Q (not inverted QN) is preferred when both exist.
                output = 'Q' if 'Q' in outputs else (outputs[0] if outputs else None)
                qbits = pins.get(output, []) if seq else []
                self.cells[full] = (pins, qbits)
                for b in qbits:
                    self.registers[b].add(full)
                for pin, values in pins.items():
                    for b in values:
                        self.pins[(b, pin)].add(full + '/' + pin)
        descend(top, '', {}, [])

    def binding(self, name, kind):
        instance, pin = name.rsplit('/', 1) if kind == 'get_pins' else (name, None)
        if instance not in self.cells:
            return None
        pins, qbits = self.cells[instance]
        if not qbits:
            raise ValueError(f'Cannot safely rebind non-register constraint object: {name}')
        if pin is not None and pin not in pins:
            raise ValueError(f'Unknown constraint pin: {name}')
        # Clock sinks bind to the clock wire, so replication/new clock sinks are included.
        clock = pin in ('CK', 'CLK', 'C', 'CP')
        bits = pins[pin] if clock else qbits
        aliases = sorted({a for b in bits for a in self.aliases[b]})
        if not aliases:
            raise ValueError(f'No surviving RTL wire name for constraint object: {name}')
        return {'kind': kind, 'pin': pin, 'clock': clock, 'aliases': aliases, 'original': name}

    def resolve(self, binding):
        groups = [self.by_alias[a] for a in binding['aliases'] if a in self.by_alias]
        bits = set().union(*groups) if groups else set()
        if len(bits) != 1:
            raise ValueError(f'Constraint binding lost or ambiguous: {binding["original"]}')
        bit = next(iter(bits))
        if binding['clock']:
            result = self.pins.get((bit, binding['pin']), set())
        else:
            cells = self.registers.get(bit, set())
            result = cells if binding['kind'] == 'get_cells' else {
                cell + '/' + binding['pin'] for cell in cells if binding['pin'] in self.cells[cell][0]}
        if not result:
            raise ValueError(f'No candidate register/pin for constraint: {binding["original"]}')
        return sorted(result)


def compile_constraints(text, reference=None):
    bindings = []
    def replace_names(names, kind):
        result = []
        for name in names.split():
            binding = reference.binding(name, kind) if reference else None
            if binding:
                result.append(f'__COGITO_BIND_{len(bindings)}__')
                bindings.append(binding)
            else:
                # Ordinary stable names/patterns are checked by OpenSTA preflight.
                result.append(name)
        return ' '.join(result)
    text = QUERY.sub(lambda m: '[' + m[1] + ' {' + replace_names(m[2], m[1]) + '}]', text)
    def pin_list(match):
        names = match[2].split()
        if reference and names and all('/' in n and n.rsplit('/', 1)[0] in reference.cells for n in names):
            return match[1] + '{' + replace_names(match[2], 'get_pins') + '}'
        return match[0]
    text = PIN_LIST.sub(pin_list, text)
    return {'text': text, 'bindings': bindings}


def materialize(plan, document, top, library, work):
    graph = Connectivity(document, top, library) if plan['bindings'] else None
    def replace(match):
        return ' '.join(graph.resolve(plan['bindings'][int(match[1])]))
    text = re.sub(r'__COGITO_BIND_(\d+)__', replace, plan['text'])
    # Lists of clock sinks can share a wire; deduplicate the resulting lists.
    text = PIN_LIST.sub(lambda m: m[1] + '{' + ' '.join(dict.fromkeys(m[2].split())) + '}'
                        if m[2].split() and all('/' in n for n in m[2].split()) else m[0], text)
    path = Path(work) / 'cogito_constraints.sdc'
    path.write_text(text)
    return path
