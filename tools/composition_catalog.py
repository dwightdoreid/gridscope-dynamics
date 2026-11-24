"""
Catalogue of every Dynawo governor / voltage regulator / PSS class that can be wired to the
synchronous machine, for free composition (any governor x any AVR x any PSS).

For each class it records
  * init       the Modelica initialisation model Dynawo pairs with it
  * connect    [[unit variable, generator variable], ...]   (run-time connections)
  * initConnect[[unit variable, generator variable], ...]   (initialisation connections)
  * pssIn      AVR input fed by the PSS (AVRs) / pssOut: PSS output (PSSs)
  * params     user-facing parameters {name: {type, doc, value?}}

Wiring is harvested from Dynawo's own pre-assembled model definitions; classes that Dynawo never
pre-assembles are wired from their .extvar interface and initialisation model using the same
variable conventions, and say so in `wiring_source`.
"""
import collections
import glob
import re
from pathlib import Path

ROLE_OF_UNIT = {'governor': 'governor', 'voltageRegulator': 'avr', 'powerSystemStabilizer': 'pss', 'pss': 'pss'}
ROLE_DIR = {'governor': 'Governors', 'avr': 'VoltageRegulators', 'pss': 'PowerSystemStabilizers'}
NOT_CONTROLLERS = {'MAXEX2'}        # an over-excitation limiter that lives in the VoltageRegulators package

# generator-side variable for each controller input / output (run time) ...
GEN_VAR = {'UsPu': 'UStatorPu.value', 'UStatorPu': 'UStatorPu.value', 'IrPu': 'IRotorPu.value',
           'IRotorPu': 'IRotorPu.value', 'itPu': 'iStatorPu', 'utPu': 'uPu', 'UtPu': 'UPu',
           'omegaPu': 'omegaPu.value', 'fPu': 'omegaPu.value', 'PGenPu': 'PGenPu', 'QGenPu': 'QGenPu',
           'EfdPu': 'efdPu.value', 'efdPu': 'efdPu.value', 'PmPu': 'PmPu.value', 'Pm1Pu': 'PmPu.value'}
# ... and for each initialisation variable
GEN_INIT = {'Efd0Pu': 'Efd0Pu', 'Efd0PuLF': 'Efd0Pu', 'Us0Pu': 'UStator0Pu', 'UStator0Pu': 'UStator0Pu',
            'Ir0Pu': 'IRotor0Pu', 'IRotor0Pu': 'IRotor0Pu', 'it0Pu': 'i0Pu', 'ut0Pu': 'u0Pu', 'Ut0Pu': 'U0Pu',
            'Pm0Pu': 'Pm0Pu', 'PGen0Pu': 'PGen0Pu', 'QGen0Pu': 'QGen0Pu'}
PSS_IN = ('UPssPu', 'UpssPu')

# initialisation models of classes Dynawo never pre-assembles (from the INIT models' own documentation)
INIT_FOR = {
    'St1c': 'VoltageRegulators.Standard.St15c_INIT', 'St5c': 'VoltageRegulators.Standard.St15c_INIT',
    'St1a': 'VoltageRegulators.Standard.St15c_INIT',
    'Ac1c': 'VoltageRegulators.Standard.Ac168_INIT', 'Ac6c': 'VoltageRegulators.Standard.Ac168_INIT',
    'Ac8c': 'VoltageRegulators.Standard.Ac78c_INIT', 'St4c': 'VoltageRegulators.Standard.Stxc_INIT',
    'St7c': 'VoltageRegulators.Standard.St7c_INIT', 'Dc1c': 'VoltageRegulators.Exciter_INIT',
    'IEEEX2': 'VoltageRegulators.Exciter_INIT', 'Gast': 'Governors.Governor_INIT',
    'Pss1aPGen': 'PowerSystemStabilizers.Pss_INIT', 'Pss1aOmega': None,
    'Pss3c': 'PowerSystemStabilizers.Pss_INIT',
}
PKG = 'Dynawo.Electrical.Controls.Machines.'


# ---------------------------------------------------------------------------------- Modelica parsing
def _strip(text):
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'//[^\n]*', ' ', text)
    return text


def _balanced_drop(text, word):
    """Remove `word( ... )` blocks with balanced parentheses (annotations, placements)."""
    out, i = [], 0
    while True:
        j = text.find(word, i)
        if j < 0:
            out.append(text[i:])
            return ''.join(out)
        k = text.find('(', j)
        if k < 0 or text[j + len(word):k].strip():
            out.append(text[i:j + len(word)])
            i = j + len(word)
            continue
        depth, m = 0, k
        while m < len(text):
            depth += text[m] == '('
            depth -= text[m] == ')'
            m += 1
            if depth == 0:
                break
        out.append(text[i:j])
        i = m


class ModelicaTree:
    def __init__(self, ddb):
        self.root = Path(ddb)
        self._cache = {}

    def path(self, cls):
        return self.root / (cls.replace('.', '/') + '.mo')

    def exists(self, cls):
        return self.path(cls).exists()

    def resolve(self, name, context):
        if name.startswith('Dynawo.'):
            return name
        pkg = context.rsplit('.', 1)[0]
        while pkg:
            cand = f'{pkg}.{name}'
            if self.exists(cand):
                return cand
            if '.' not in pkg:
                break
            pkg = pkg.rsplit('.', 1)[0]
        return name

    def declarations(self, cls, depth=0):
        """{name: dict(kind, type, default, doc)} incl. inherited ones; kind in parameter/input/output/variable."""
        if cls in self._cache:
            return self._cache[cls]
        p = self.path(cls)
        if not p.exists() or depth > 6:
            return {}
        text = _strip(p.read_text(errors='ignore'))
        text = text.split('\nequation', 1)[0].split('\ninitial equation', 1)[0]
        text = _balanced_drop(text, 'annotation')
        out = {}
        for stmt in text.split(';'):
            s = ' '.join(stmt.split())
            s = re.sub(r'^.*?\b(?:partial\s+)?(?:model|block|class)\s+\w+\s*("[^"]*")?\s*', '', s) \
                if re.search(r'\b(model|block|class)\s+\w+', s.split('parameter')[0]) else s
            m = re.match(r'(?:.*\s)?extends\s+([\w.]+)\s*(\((.*)\))?\s*$', s)
            if m:
                base = self.resolve(m.group(1), cls)
                inherited = self.declarations(base, depth + 1)
                mods = {}
                for k, v in re.findall(r'(?:^|,)\s*(\w+)\s*=\s*([^,()]+)', m.group(3) or ''):
                    mods[k] = v.strip()
                for k, d in inherited.items():
                    d = dict(d)
                    if k in mods:
                        d['default'] = mods[k]
                    out.setdefault(k, d)
                continue
            m = re.match(r'(?:(final)\s+)?parameter\s+([\w.]+)\s+(\w+)(\[[^\]]*\])?\s*(\([^\"]*?\))?\s*(=\s*[^\"]+?)?\s*(\"([^\"]*)\")?\s*$', s)
            if m:
                out[m.group(3)] = dict(kind='parameter', final=bool(m.group(1)), array=bool(m.group(4)),
                                       type=m.group(2).split('.')[-1],
                                       default=(m.group(6) or '')[1:].strip() or None, doc=m.group(8) or '')
                continue
            m = re.match(r'(?:.*\s)?(?:Modelica\.Blocks\.)?Interfaces\.(RealInput|RealOutput|BooleanInput|IntegerInput)\s+(\w+)', s)
            if m:
                out[m.group(2)] = dict(kind='input' if 'Input' in m.group(1) else 'output', type=m.group(1))
                continue
            m = re.match(r'(?:input|output)\s+([\w.]+)\s+(\w+)', s)
            if m:
                out[m.group(2)] = dict(kind='input' if s.startswith('input') else 'output', type=m.group(1))
                continue
            m = re.match(r'(?:Types|Modelica\.Units\.SI|Dynawo\.Types)\.(\w+)\s+(\w+)\b', s)
            if m and not s.startswith(('parameter', 'final')):
                out.setdefault(m.group(2), dict(kind='variable', type=m.group(1)))
        self._cache[cls] = out
        return out


def extvar(ddb, cls):
    p = Path(ddb, cls.replace('.', '/') + '.extvar')
    if not p.exists():
        return {}
    return {a: ('optional="true"' in rest) for a, rest in
            re.findall(r'<variable id="(\w+)"([^>]*)/>', p.read_text())}


# ---------------------------------------------------------------------------------- harvest
def harvest(pre_dir):
    """Wiring templates for every controller class used in Dynawo's pre-assembled synchronous models."""
    tmpl = {}
    for f in sorted(glob.glob(f'{pre_dir}/*.xml')):
        x = open(f).read()
        units = {u: (n, i) for u, n, i in
                 re.findall(r'<dyn:unitDynamicModel id="([^"]+)" name="([^"]+)"(?: initName="([^"]*)")?', x)}
        g = units.get('generator')
        if not g or 'OmegaRef.GeneratorSynchronous' not in g[0]:
            continue
        for u, (cls, ini) in units.items():
            if u in ROLE_OF_UNIT:
                t = tmpl.setdefault(cls, dict(role=ROLE_OF_UNIT[u], init=ini or None, connect=collections.Counter(),
                                              initConnect=collections.Counter(), pssIn=None, pssOut=None,
                                              examples=[]))
                t['examples'].append(Path(f).stem)
        for kind, a, va, b, vb in re.findall(
                r'<dyn:(initConnect|connect) id1="([^"]+)" var1="([^"]+)" id2="([^"]+)" var2="([^"]+)"', x):
            if 'generator' in (a, b):
                other, ov, gv = (b, vb, va) if a == 'generator' else (a, va, vb)
                if other in ROLE_OF_UNIT:
                    tmpl[units[other][0]]['initConnect' if kind == 'initConnect' else 'connect'][(ov, gv)] += 1
            elif {ROLE_OF_UNIT.get(a), ROLE_OF_UNIT.get(b)} == {'avr', 'pss'}:
                avr_u, avr_v, pss_u, pss_v = (a, va, b, vb) if ROLE_OF_UNIT[a] == 'avr' else (b, vb, a, va)
                tmpl[units[avr_u][0]]['pssIn'] = avr_v
                tmpl[units[pss_u][0]]['pssOut'] = pss_v
    return tmpl


def all_classes(ddb):
    base = Path(ddb, 'Dynawo/Electrical/Controls/Machines')
    out = []
    for role, d in ROLE_DIR.items():
        for p in sorted(base.glob(f'{d}/**/*.mo')):
            n = p.stem
            if n == 'package' or n.endswith('_INIT') or 'BaseClasses' in str(p) or n in NOT_CONTROLLERS:
                continue
            if not p.with_suffix('.extvar').exists():
                continue
            out.append((role, 'Dynawo.Electrical.Controls.Machines.' + str(p.relative_to(base))[:-3].replace('/', '.')))
    return out


def build(ddb, pre_dir):
    tree = ModelicaTree(ddb)
    tmpl = harvest(pre_dir)
    classes, problems = {}, {}
    for role, cls in all_classes(ddb):
        short = cls.split('.')[-1]
        decl = tree.declarations(cls)
        ev = extvar(ddb, cls)
        outputs = [k for k, d in decl.items() if d['kind'] == 'output']
        t = tmpl.get(cls)
        if t:
            src = 'Dynawo pre-assembled models: ' + ', '.join(t['examples'][:3])
            init = t['init']
            connect = _dedupe(t['connect'])
            iconn = _dedupe(t['initConnect'])
            pss_in, pss_out = t['pssIn'], t['pssOut']
        else:
            src = 'inferred from the class interface (.extvar) and its initialisation model'
            init = PKG + INIT_FOR[short] if INIT_FOR.get(short) else None
            connect = sorted({(v, GEN_VAR[v]) for v, opt in ev.items() if v in GEN_VAR and not opt} |
                             {(v, GEN_VAR[v]) for v in outputs if v in GEN_VAR})
            pss_in = next((v for v in PSS_IN if v in ev), None)
            pss_out = next((v for v in outputs if v in ('VPssPu', 'UPssPu')), None)
            ivars = {}
            if init:
                ivars = {k for k, d in tree.declarations(init).items() if d['kind'] != 'parameter'}
            iconn = sorted({(v, GEN_INIT[v]) for v in ivars if v in GEN_INIT} |
                           {(k, GEN_INIT[k]) for k, d in decl.items() if d['kind'] == 'parameter'
                            and k in GEN_INIT and not d['default'] and k not in ivars})
            if not init:   # no INIT model: generator init values go straight into the parameters
                iconn = sorted((k, GEN_INIT[k]) for k, d in decl.items()
                               if d['kind'] == 'parameter' and k in GEN_INIT and not d['default'])
        # which `...0Pu` parameters are produced by initialisation
        init_vars = set()
        if init:
            init_vars = {k for k, d in tree.declarations(init).items() if d['kind'] != 'parameter'}
        init_vars |= {u for u, _ in iconn}
        params, unresolved = {}, []
        for k, d in decl.items():
            if d['kind'] != 'parameter' or d['final'] or d['default'] is not None:
                continue
            if k in init_vars:
                continue
            if re.search(r'[A-Za-z]0Pu(LF)?$', k):
                unresolved.append(k)
                continue
            params[k] = dict(type=_dyn_type(d['type']), doc=d['doc'])
        # parameters with a default the user may still want to see (time constants & gains are already
        # required above); keep a short list of optional ones for the editor
        optional = {k: dict(type=_dyn_type(d['type']), doc=d['doc'], default=d['default'])
                    for k, d in decl.items() if d['kind'] == 'parameter' and not d['final'] and not d['array']
                    and d['default'] is not None and re.fullmatch(r'-?[\d.eE+-]+|true|false', d['default'])}
        entry = dict(role=role, init=init, connect=[list(c) for c in connect],
                     initConnect=[list(c) for c in iconn], wiring_source=src, params=params, optional=optional)
        if role == 'avr':
            entry['pssIn'] = pss_in
        if role == 'pss':
            entry['pssOut'] = pss_out
        missing_out = {'governor': {'PmPu', 'Pm1Pu'}, 'avr': {'EfdPu', 'efdPu'}}.get(role)
        if missing_out and not ({c[0] for c in connect} & missing_out):
            unresolved.append('no output to the machine')
        if role == 'pss' and not pss_out:
            unresolved.append('no PSS output')
        if short == 'TGov3':
            unresolved.append('needs a fast-valving table (no reference data in Dynawo)')
        if unresolved:
            entry['unresolved'] = unresolved
            problems[cls] = unresolved
        classes[cls] = entry
    return classes, problems


def _dedupe(counter):
    """One unit variable per generator variable (and vice versa): keep the most used pairing."""
    out, used_u, used_g = [], set(), set()
    for (u, g), _ in sorted(counter.items(), key=lambda kv: (kv[0][0].lower() != kv[0][1].split('.')[0].lower(),
                                                              -kv[1], kv[0])):
        if u in used_u or (g in used_g and g.endswith('.value') and g in ('PmPu.value', 'efdPu.value')):
            continue
        out.append((u, g))
        used_u.add(u)
        used_g.add(g)
    return sorted(out)


def _dyn_type(t):
    return {'Boolean': 'BOOL', 'Integer': 'INT', 'String': 'STRING'}.get(t, 'DOUBLE')


if __name__ == '__main__':
    import sys
    c, p = build(sys.argv[1], sys.argv[2])
    for k, v in c.items():
        print(v['role'], k.split('.')[-1], len(v['params']), v.get('pssIn') or v.get('pssOut') or '',
              'INFERRED' if 'inferred' in v['wiring_source'] else '', v.get('unresolved', ''))
    print(len(c), 'classes,', len(p), 'with problems')
