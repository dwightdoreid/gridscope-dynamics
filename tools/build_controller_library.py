"""
Build dynawo_par/controller_library.json: the catalogue of Dynawo pre-assembled synchronous-machine
models (machine + governor + AVR + PSS + limiters) offered in the app, with default parameters per
control component.

Sources (all from Dynawo itself, so names and values match the solver):
  * <dynawo>/ddb/<Model>.desc.xml          parameter names/types; user-facing = writable without default
  * PreassembledModels/<Model>.xml         which Modelica class implements each component
  * nrt/data/**/*.dyd + .par, examples/    Dynawo regression tests / examples: parameter values per model
  * Examples/RVS/.../Parameters*.mo        IEEE RTS (RVS) parameter tables (IEEEG1, HYGOV, SCRX, IEEET1, EXAC1, PSS2B)

Only combinations whose every user-facing control parameter has a sourced value are kept; the
generator-transformer (…Tfo) variants are skipped because the case has no step-up transformer data.

Usage:
  python tools/build_controller_library.py <dynawo install dir> <dynawo source checkout (tag v1.7.0)>
"""
import collections
import glob
import json
import os
import re
import sys
from pathlib import Path

import pypowsybl.dynamic as dyn

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROLES = {'governor': 'governor', 'voltageRegulator': 'avr', 'powerSystemStabilizer': 'pss', 'pss': 'pss',
         'overExcitationLimiter': 'limiter', 'underExcitationLimiter': 'limiter',
         'statorCurrentLimiter': 'limiter', 'transformer': 'transformer'}

# readable names for the control blocks
LABELS = {
    'GoverProportional': 'Proportional (simplified)', 'GovSteam1': 'GOVSTEAM1 (IEEE steam)',
    'GovSteamEu': 'GOVSTEAMEU (steam)', 'GovCt2': 'GOVCT2 (gas turbine)', 'TGov1': 'TGOV1 (steam)',
    'IEEEG1': 'IEEEG1 (IEEE steam)', 'HyGov': 'HYGOV (hydro)', 'GovHydro4': 'GOVHYDRO4 (hydro)',
    'GoverNordic': 'Nordic governor', 'VRProportional': 'Proportional (simplified)',
    'VRProportionalIntegral': 'PI (simplified)', 'St4b': 'ST4B (IEEE static)', 'SEXS': 'SEXS (simplified exciter)',
    'Ac6a': 'AC6A (IEEE)', 'Ac7b': 'AC7B (IEEE)', 'Ac7c': 'AC7C (IEEE)', 'Ac8b': 'AC8B (IEEE)', 'St5b': 'ST5B (IEEE)',
    'St6b': 'ST6B (IEEE)', 'St6c': 'ST6C (IEEE)', 'St7b': 'ST7B (IEEE)', 'St9c': 'ST9C (IEEE)',
    'SCRX': 'SCRX (bus-fed static)', 'IEEET1': 'IEEET1 (IEEE DC)', 'ExAc1': 'EXAC1 (IEEE AC)',
    'VRNordic': 'Nordic AVR', 'VRKundur': 'Kundur AVR', 'Pss2a': 'PSS2A (IEEE)', 'Pss2b': 'PSS2B (IEEE)',
    'Pss2c': 'PSS2C (IEEE)', 'Pss3b': 'PSS3B (IEEE)', 'Pss6c': 'PSS6C (IEEE)', 'PssKundur': 'Kundur PSS',
}


# parameters in physical units, filled from each generator instead of the reference machine
BINDINGS = {('GoverProportional', 'PMax'): 'PMax', ('GoverProportional', 'PMin'): 'PMin',
            ('GoverProportional', 'PNom'): 'PNom', ('GoverNordic', 'PNom'): 'PNom', ('*pss', 'SNom'): 'SNom'}


# classes Dynawo never parameterises: start from the closest relative's reference set
SIBLINGS = {'Ac1a': 'Ac1c', 'Ac6c': 'Ac6a', 'Ac8c': 'Ac8b', 'St4c': 'St4b', 'St7c': 'St7b', 'Dc1c': 'Dc1a',
            'IEEEX2': 'IEEX2A', 'Pss3c': 'Pss3b', 'Pss1aPGen': 'Pss1aOmega',
            'Ac7c': 'Ac7b', 'St6c': 'St6b', 'St5c': 'St5b', 'IEEEG2': 'IEEEG1'}
# Dynawo ships no parameter set for these classes. Lag time constants that the IEEE sample data set to
# zero are 1e-5 s, as in Dynawo's own test cases (a zero lag divides by zero).
# Typical sample values (IEEE Std 421.5 Annex H style
# data for exciters, PSS/E-style data for governors) so the model runs out of the box; flagged in the UI.
_POS = dict(PositionOel=0, PositionUel=0, PositionScl=0, PositionPss=1)
TYPICAL = {
    'Gast': dict(At=1.0, DTurb=0.0, Kt=2.0, R=0.05, VMax=1.0, VMin=-0.05, t1=0.4, t2=0.1, t3=3.0),
    'IEEEG2': dict(K=20.0, PMaxPu=1.0, PMinPu=0.0, t1=10.0, t2=1.0, t3=0.5, t4=1.0),
    'Dc1a': dict(AEx=0.0039, BEx=1.555, Ka=46.0, Ke=1.0, Kf=0.1, VrMaxPu=7.3, VrMinPu=-7.3, tA=0.06, tB=1e-5,
                 tC=0.0, tE=0.46, tF=1.0, tR=1e-5, **_POS),
    'Dc1c': dict(AEx=0.0039, BEx=1.555, EfdMinPu=0.0, Ka=46.0, Ke=1.0, Kf=0.1, VrMaxPu=7.3, VrMinPu=-7.3,
                 tA=0.06, tB=1e-5, tC=0.0, tE=0.46, tF=1.0, tR=1e-5, **_POS),
    'IEEX2A': dict(AEx=0.0039, BEx=1.555, Ka=400.0, Ke=1.0, Kf=0.03, VrMaxPu=7.3, VrMinPu=-7.3, tA=0.04, tB=1e-5,
                   tC=0.0, tE=0.8, tF1=1.0, tR=1e-5),
    'IEEEX2': dict(AEx=0.0039, BEx=1.555, EfdMinPu=-7.3, Ka=400.0, Ke=1.0, Kf=0.03, VrMaxPu=7.3, VrMinPu=-7.3,
                   tA=0.04, tB=1e-5, tC=0.0, tE=0.8, tF1=1.0, tF2=1e-5, tR=1e-5),
    'St1a': dict(Ka=210.0, Kc=0.038, Kf=0.0, Klr=4.54, IlrPu=4.4, VaMaxPu=999.0, VaMinPu=-999.0, ViMaxPu=999.0,
                 ViMinPu=-999.0, VrMaxPu=6.43, VrMinPu=-6.0, tA=1e-5, tB=1.0, tB1=1e-5, tC=1.0, tC1=0.0, tF=1.0,
                 tR=1e-5, UOel0Pu=999.0, **_POS),
    'BbSex1': dict(EfdMaxPu=5.0, EfdMinPu=-5.0, K=50.0, VrMaxPu=5.0, VrMinPu=-5.0, t1=1.0, t2=1.0, t3=1.0,
                   t4=1.0, tR=0.02),
    'Ac1a': dict(**_POS), 'Ac1c': dict(**_POS), 'Ac6c': dict(**_POS), 'St5c': dict(**_POS),
    'St4c': dict(Sw1=True, VgMaxPu=20.0, tG=0.0, **_POS),
    'Ac8c': dict(Kc1=0.0, Ki=0.0, Kp=4.96, Sw1=True, Thetap=0.0, VbMaxPu=6.9, XlPu=0.0, **_POS),
    'St7c': dict(tA=1e-5, **_POS),
    'Pss3c': dict(PPssOffPu=-10.0, PPssOnPu=-9.0),
}
TYPICAL['St1c'] = dict(TYPICAL['St1a'])
TYPICAL_SOURCE = {k: 'typical sample values (no Dynawo reference set) - review before use' for k in TYPICAL}
for _k in ('Ac1a', 'Ac1c', 'Ac6c', 'St5c', 'St4c', 'Ac8c', 'St7c', 'Pss3c'):
    TYPICAL_SOURCE[_k] = 'switch / option values chosen by GridScope'


LABELS.update({'Gast': 'GAST (gas turbine)', 'TGov3': 'TGOV3 (steam, fast valving)', 'IEEEG2': 'IEEEG2 (IEEE hydro)',
               'BbSex1': 'BBSEX1 (static)', 'IEEX2A': 'IEEEX2A (IEEE DC)', 'IEEEX2': 'IEEEX2 (IEEE DC)',
               'Pss1aOmega': 'PSS1A (speed input)', 'Pss1aPGen': 'PSS1A (power input)',
               'VRProportionalReactiveFeedback': 'Proportional with Q feedback (simplified)'})


def _txt(v):
    return ('true' if v else 'false') if isinstance(v, bool) else repr(v)


def label(cls):
    short = cls.split('.')[-1]
    if short in LABELS:
        return LABELS[short]
    if re.fullmatch(r'(Ac|Dc|St|Pss)\d+[abc]', short):
        return short.upper() + ' (IEEE)'
    return short


def modelica_docs(src_dir, cls, depth=0):
    """{param: (type, description)} from the class source and the classes it extends."""
    import subprocess
    path = 'dynawo/sources/Models/Modelica/' + cls.replace('.', '/') + '.mo'
    r = subprocess.run(['git', '-C', src_dir, 'show', f'v1.7.0:{path}'], capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    text = r.stdout
    out = {}
    for t, n, d in re.findall(r'parameter\s+(?:Types\.|Modelica\.Units\.SI\.|Real|Integer|Boolean|String)?([\w.]*)\s+(\w+)[^;\"]*\"([^\"]*)\"', text):
        out.setdefault(n, (t, d))
    if depth < 2:
        pkg = cls.rsplit('.', 1)[0]
        for e in re.findall(r'extends\s+([\w.]+)', text):
            full = e if e.startswith('Dynawo') else f'{pkg}.{e}'
            for k, v in modelica_docs(src_dir, full, depth + 1).items():
                out.setdefault(k, v)
    return out


def desc(ddb, model):
    x = Path(ddb, f'{model}.desc.xml').read_text(errors='ignore')
    out = {}
    for p in re.findall(r'<parameter ([^>]*)/>', x):
        d = dict(re.findall(r'(\w+)="([^"]*)"', p))
        out[d['name']] = d
    return out


def parsets(path):
    x = Path(path).read_text(errors='ignore')
    out = {}
    for sid, body in re.findall(r'<set id="([^"]+)">(.*?)</set>', x, re.S):
        vals = {}
        for tag in re.findall(r'<par [^>]*/>', body):
            a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            vals[a['name']] = (a.get('type', 'DOUBLE'), a.get('value'))
        out[sid] = vals
    return out


def main(dynawo_dir, src_dir):
    ddb = Path(dynawo_dir, 'ddb')
    pre = Path(src_dir, 'dynawo/sources/Models/Modelica/PreassembledModels')
    names = dyn.ModelMapping().get_supported_models_information('SynchronousGenerator').index.tolist()
    names = [n for n in names if Path(ddb, f'{n}.so').exists() and Path(pre, f'{n}.xml').exists()
             and 'InternalParameters' not in n]
    units = {}
    for f in sorted(pre.glob('*.xml')):     # every pre-assembled model: their test parameter sets feed the classes
        units[f.stem] = dict(re.findall(r'<dyn:unitDynamicModel id="([^"]+)" name="([^"]+)"', f.read_text()))
    # reference sets from newer Dynawo regression tests (copied from master: AC1C, PSS1A)
    for f in sorted(Path(src_dir, 'nrt_master').glob('*/model.xml')):
        x = f.read_text()
        units[re.search(r'modelicaModel id="([^"]+)"', x).group(1)] = dict(
            re.findall(r'<dyn:unitDynamicModel id="([^"]+)" name="([^"]+)"', x))

    # user-facing parameters per (model, unit)
    req, writable_all = {}, {}
    for n in names:
        d = desc(ddb, n)
        for u in units[n]:
            writable_all[(n, u)] = {k.split('_', 1)[1]: v['valueType'] for k, v in d.items()
                                    if k.startswith(u + '_') and v['readOnly'] == 'false'}
        req[n] = {u: {k.split('_', 1)[1]: v['valueType'] for k, v in d.items() if k.startswith(u + '_')
                      and v['readOnly'] == 'false' and 'defaultValue' not in v} for u in units[n]}

    # parameter values per component class
    cdef, csrc, exc = collections.defaultdict(dict), {}, {}
    dyds = glob.glob(f'{src_dir}/nrt/data/**/*.dyd', recursive=True) + \
        glob.glob(f'{src_dir}/nrt_master/**/*.dyd', recursive=True) + \
        glob.glob(f'{dynawo_dir}/examples/**/*.dyd', recursive=True)
    cache = {}
    for dyd in dyds:
        x = Path(dyd).read_text(errors='ignore')
        for tag in re.findall(r'<dyn:blackBoxModel[^>]*>', x):
            a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            lib, pf, pid = a.get('lib'), a.get('parFile'), a.get('parId')
            if lib not in units or not pf or not pid:
                continue
            path = os.path.normpath(os.path.join(os.path.dirname(dyd), pf))
            if not os.path.exists(path):
                continue
            cache.setdefault(path, parsets(path))
            s = cache[path].get(pid)
            if not s:
                continue
            for u, cls in units[lib].items():
                if u == 'generator':
                    continue
                vals = {k.split('_', 1)[1]: v for k, v in s.items() if k.startswith(u + '_')}
                if vals and len(vals) > len(cdef.get(cls, {})):
                    cdef[cls] = vals
                    csrc[cls] = f'Dynawo regression test / example ({os.path.relpath(path, src_dir) if path.startswith(src_dir) else os.path.basename(path)})'
                    if u == 'voltageRegulator':
                        exc[cls] = dict(ExcitationPu=int(float(s.get('generator_ExcitationPu', ('INT', 1))[1])),
                                        MdPuEfd=float(s.get('generator_MdPuEfd', ('DOUBLE', 0))[1]))

    # IEEE RTS (RVS) tables
    rvs = Path(src_dir, 'dynawo/sources/Models/Modelica/Dynawo/Examples/RVS/Components/GeneratorWithControl/BaseClasses')
    rvs_cls = {'IEEEG1': 'Dynawo.Electrical.Controls.Machines.Governors.Standard.Steam.IEEEG1',
               'HYGOV': 'Dynawo.Electrical.Controls.Machines.Governors.Standard.Hydraulic.HyGov',
               'SCRX': 'Dynawo.Electrical.Controls.Machines.VoltageRegulators.Standard.SCRX',
               'IEEET1': 'Dynawo.Electrical.Controls.Machines.VoltageRegulators.Standard.IEEET1',
               'EXAC': 'Dynawo.Electrical.Controls.Machines.VoltageRegulators.Standard.ExAc1',
               'PSS2B': 'Dynawo.Electrical.Controls.Machines.PowerSystemStabilizers.Standard.Pss2b'}
    for key, cls in rvs_cls.items():
        f = rvs / f'Parameters{key}.mo'
        if not f.exists() or (cls in cdef and key != 'IEEEG1'):
            continue
        x = f.read_text()
        pn = re.search(r'type \w+ParamNames = enumeration\(([^)]*)\)', x)
        names_ = [t.strip() for t in pn.group(1).split(',')]
        body = x[x.index('= {', pn.end()) + 3:]
        rows = []
        for vals, comment in re.findall(r'\{([^{}]*)\}\s*,?\s*(//[^\n]*)?', body):
            v = [float(t) for t in vals.split(',') if t.strip()]
            if len(v) == len(names_):
                rows.append(dict(zip(names_, v)))
        if key == 'IEEEG1':   # take a unit that actually has its governor in service
            rows = [r for r in rows if r.get('hasGov', 1) > 0] or rows
        row = rows[0]
        row.pop('hasGov', None)
        cdef[cls] = {k: ('DOUBLE', repr(v)) for k, v in row.items()}
        csrc[cls] = f'Dynawo IEEE RTS (RVS) example, {f.name}'
        if 'VoltageRegulators' in cls:
            exc[cls] = dict(ExcitationPu=1, MdPuEfd=0.0)    # RVS frames use ExcitationPuType.NoLoad (= 1)

    components, models, tables = {}, {}, set()
    for n in names:
        if any(ROLES.get(u) == 'transformer' for u in units[n]):
            continue
        ok, parts = True, {}
        for u, cls in units[n].items():
            if u == 'generator':
                continue
            need = dict(req[n][u])
            have = cdef.get(cls, {})
            if set(need) - set(have):
                ok = False
                break
            role = ROLES.get(u, u)
            parts.setdefault(role, []).append(dict(unit=u, cls=cls))
            if cls not in components:
                docs = modelica_docs(src_dir, cls)
                params = {}
                # user-facing = parameters without a built-in default, PLUS every parameter the reference set
                # overrides explicitly (e.g. ST4B's UOel0Pu = 10: leaving its default 0 pins Efd to zero)
                writable = writable_all.get((n, u), {})
                for k in have:
                    if k not in need and k in writable:
                        need[k] = writable[k]
                for k, t in sorted(need.items()):
                    d = dict(type=t, value=have[k][1])
                    if k in docs:
                        d['doc'] = docs[k][1]
                    b = BINDINGS.get((cls.split('.')[-1], k)) or BINDINGS.get(('*' + role, k))
                    if b:
                        d['bind'] = b            # set from the generator's own data at run time
                    if k == 'TablesFile':
                        d['value'] = 'tables/' + Path(d['value']).name
                        tables.add(d['value'])
                    params[k] = d
                components[cls] = dict(label=label(cls), role=role, source=csrc.get(cls, ''), params=params)
                if cls in exc:
                    components[cls]['excitation'] = exc[cls]
        if not ok:
            continue
        machine = 'Round rotor (4 windings)' if 'FourWindings' in n else 'Salient pole (3 windings)'
        gov = parts.get('governor', [None])[0]
        models[n] = dict(machine=machine,
                         governor=label(gov['cls']) if gov else 'None (constant mechanical power)',
                         avr=label(parts['avr'][0]['cls']) if 'avr' in parts else 'None (constant field voltage)',
                         pss=label(parts['pss'][0]['cls']) if 'pss' in parts else 'None',
                         limiter=', '.join(label(p['cls']) for p in parts.get('limiter', [])) or 'None',
                         units={p['unit']: p['cls'] for ps in parts.values() for p in ps},
                         description=dyn.ModelMapping().get_supported_models_information('SynchronousGenerator')
                         .loc[n, 'description'])
    # ---- free composition: every governor / AVR / PSS class, with wiring and parameter values
    import composition_catalog as cc
    classes, problems = cc.build(ddb, pre)
    by_short = {c.split('.')[-1]: c for c in classes}
    for cls, e in classes.items():
        short = cls.split('.')[-1]
        comp = components.setdefault(cls, dict(label=label(cls), role=e['role'], source='', params={}))
        comp.update({k: e[k] for k in ('init', 'connect', 'initConnect', 'wiring_source', 'pssIn', 'pssOut',
                                        'unresolved') if k in e})
        if comp['params']:          # already filled from a verified pre-assembled model
            continue
        refs = [(cdef.get(cls, {}), csrc.get(cls))]
        sib = by_short.get(SIBLINGS.get(short, ''))
        if sib:
            refs.append((cdef.get(sib, {}), f'borrowed from {label(sib)}: ' + (csrc.get(sib) or '')))
        refs.append(({k: ('DOUBLE', _txt(v)) for k, v in TYPICAL.get(short, {}).items()},
                     TYPICAL_SOURCE.get(short, 'typical values')))
        used, params = [], {}
        for k, d in sorted(e['params'].items()):
            q = dict(type=d['type'], value=None)
            if d.get('doc'):
                q['doc'] = d['doc']
            for vals, src in refs:
                if k in vals:
                    q['value'] = vals[k][1]
                    if src not in used:
                        used.append(src)
                    break
            params[k] = q
        for vals, src in (refs[0], refs[-1]):     # explicit overrides of defaulted parameters (reference / typical)
            for k, (t, v) in vals.items():
                if k not in params and k in e['optional']:
                    params[k] = dict(type=e['optional'][k]['type'], value=v, doc=e['optional'][k].get('doc', ''))
        for k, q in params.items():
            b = BINDINGS.get((short, k)) or BINDINGS.get(('*' + e['role'], k))
            if b:
                q['bind'] = b
            if k == 'TablesFile' and q['value']:
                q['value'] = 'tables/' + Path(q['value']).name
                tables.add(q['value'])
        comp['params'] = params
        comp['source'] = '; '.join(u for u in used if u) or 'no reference values in Dynawo: enter them'
        comp['optional'] = e['optional']
        if e['role'] == 'avr':
            comp['excitation'] = exc.get(cls) or exc.get(sib) or dict(ExcitationPu=1, MdPuEfd=0.0)
    for cls, comp in components.items():          # verified components also get their optional list
        if cls in classes and 'optional' not in comp:
            comp['optional'] = classes[cls]['optional']
    out = dict(dynawo_version='1.7.0', models=models, components=components, tables=sorted(tables))
    target = Path(__file__).resolve().parent.parent / 'dynawo_par' / 'controller_library.json'
    if target.exists():              # keep earlier verification results (tools/verify_controller_library.py)
        old = json.loads(target.read_text()).get('models', {})
        for n, v in models.items():
            for k in ('verified', 'verification'):
                if k in old.get(n, {}):
                    v[k] = old[n][k]
    target.write_text(json.dumps(out, indent=1))
    print(f'{len(models)} model combinations, {len(components)} control components -> {target}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
