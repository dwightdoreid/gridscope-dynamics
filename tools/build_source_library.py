"""
Build dynawo_par/source_library.json: the non-synchronous generator models (solar PV, wind, battery
storage, grid-forming converters, simplified generators, grid equivalents) and the load models offered
in the app, with reference parameter values taken from Dynawo's own examples / regression tests.

For every model it records
  category      pypowsybl model category (WECC, GRID_FORMING_CONVERTER, ...) -> ModelMapping.add_* method
  prefix        internal variable prefix of the compiled model (BESScb, photovoltaics, WT, ...)
  params        {full parameter name: {type, value, doc?, bind?}}  (user-facing: no built-in default)
  references    {full parameter name: IIDM field}  initial values from the solved load flow
  p_var/q_var   output variables for active / reactive power (pu on 100 MVA, generator convention)
  additional    True when pypowsybl does not know the model yet (registered via additionalModelsFile)

Usage:  python tools/build_source_library.py <dynawo install dir> <dynawo source checkout>
"""
import glob
import json
import os
import re
import sys
from pathlib import Path

# model, kind, label, category, synchronized (needs the network frequency reference), donor of parameters
SOURCES = [
    ('PhotovoltaicsWeccCurrentSourceB', 'Solar PV', 'WECC PV, current source (REPCA + REECB + REGCB)', 'WECC', True, None),
    ('PhotovoltaicsWeccVoltageSourceB', 'Solar PV', 'WECC PV, voltage source (REPCA + REECB + REGCB)', 'WECC', True, None),
    ('WTG4AWeccCurrentSource', 'Wind (type 4)', 'WECC WTG4A (REPCA + REECA + REGCB + WTGTA drive train)', 'WECC', True, None),
    ('WTG4BWeccCurrentSource', 'Wind (type 4)', 'WECC WTG4B (REPCA + REECA + REGCB + WTGTB drive train)', 'WECC', True, None),
    ('PhotovoltaicsWeccVoltageSourceA', 'Solar PV', 'WECC PV, voltage source (REPCA + REECA + REGCB)', 'WECC', True,
     ('PhotovoltaicsWeccVoltageSourceB', 'WTG4AWeccCurrentSource')),
    ('WT4AWeccCurrentSource', 'Wind (type 4)', 'WECC WT4A, no plant controller (REECA + REGCB + WTGTA)', 'WECC', False, None),
    ('WT4BWeccCurrentSource', 'Wind (type 4)', 'WECC WT4B, no plant controller (REECA + REGCB)', 'WECC', False, None),
    ('BESScbWeccCurrentSource', 'Battery storage', 'WECC BESS (REPCA + REECC + REGCB)', 'WECC', True, None),
    ('BESScbWeccCurrentSourceNoPlantControl', 'Battery storage', 'WECC BESS, no plant controller (REECC + REGCB)',
     'WECC', False, 'BESScbWeccCurrentSource'),
    ('GridFormingConverterDroopControl', 'Grid-forming converter', 'Grid-forming converter, droop control',
     'GRID_FORMING_CONVERTER', True, None),
    ('GridFormingConverterMatchingControl', 'Grid-forming converter', 'Grid-forming converter, matching control',
     'GRID_FORMING_CONVERTER', True, None),
    ('GridFormingConverterDispatchableVirtualOscillatorControl', 'Grid-forming converter',
     'Grid-forming converter, dispatchable virtual oscillator control', 'GRID_FORMING_CONVERTER', True, None),
    ('GeneratorPQ', 'Simplified generator', 'Constant P/Q with frequency droop (GeneratorPQ)', 'SYNCHRONIZED_GENERATOR', True, None),
    ('GeneratorPV', 'Simplified generator', 'Constant P, voltage control within Q limits (GeneratorPV)',
     'SYNCHRONIZED_GENERATOR', True, None),
    ('InfiniteBus', 'Grid equivalent', 'Infinite bus (constant voltage and frequency)', 'INFINITE_BUS', False, None),
    ('InertialGrid', 'Grid equivalent', 'Inertial grid (aggregate inertia + frequency response)', 'INERTIAL_GRID', False, None),
]
LOADS = [
    ('LoadAlphaBeta', 'Voltage-dependent (exponential) P = P0 (U/U0)^alpha', None),
    ('LoadPQ', 'Constant power', None),
    ('LoadZIP', 'ZIP (constant impedance / current / power mix)', None),
    ('LoadAlphaBetaRestorative', 'Restorative (load recovery after voltage dips)', None),
    ('LoadAlphaBetaRestorativeLimitsRecalc', 'Restorative with recalculated limits', None),
    ('ElectronicLoad', 'Electronic load (trips / reconnects with voltage)', None),
    ('LoadAlphaBetaTwoMotorFifthOrder', 'Alpha-beta load with two fifth-order induction motors', None),
]
INIT_REF = {'UPu': 'v_pu', 'UPhase': 'angle_pu', 'P0Pu': 'p_pu', 'Q0Pu': 'q_pu', 'U0Pu': 'v_pu', 'UPhase0': 'angle_pu'}
# WECC REEC: VRef0Pu < 0.5 means "use the initial injector voltage", which keeps a flat start flat
FIXED = {'VRef0Pu': '0'}
# Models left out: the IEC 61400-27 wind turbines (their injector is named differently from what
# pypowsybl's WECC wiring expects) and the PV "NoPlantControl" variants (their active / reactive power
# set-points are external inputs that pypowsybl leaves at zero).
BIND = {'SNom': 'SNom', 'PNom': 'PNom', 'PMax': 'PMax', 'PMin': 'PMin', 'QMax': 'QMax', 'QMin': 'QMin'}   # from the generator's data
P_VARS = ['injector_PInjPu', 'wT4Injector_PGenPu', 'converter_PGenPu', 'injectorURI_PInjPu', 'generator_PGenPu',
          'load_PPu']
Q_VARS = ['injector_QInjPu', 'wT4Injector_QGenPu', 'converter_QGenPu', 'injectorURI_QInjPu', 'generator_QGenPu',
          'load_QPu']
# values Dynawo does not provide for a model (none of its examples sets them)
EXTRA = {
    'InertialGrid': {},
}


def desc(ddb, model):
    x = Path(ddb, f'{model}.desc.xml').read_text(errors='ignore')
    params = {}
    for p in re.findall(r'<parameter ([^>]*)/>', x):
        d = dict(re.findall(r'(\w+)="([^"]*)"', p))
        params[d['name']] = d
    variables = re.findall(r'<variable name="([^"]+)"', x)
    return params, variables


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


def examples(dynawo_dir, src_dir):
    """{lib: [(par values, source path), ...]} from every example / regression-test dyd."""
    dyds = glob.glob(f'{dynawo_dir}/examples/**/*.[dD][yY][dD]', recursive=True) + \
        glob.glob(f'{src_dir}/nrt/data/**/*.dyd', recursive=True)
    out, cache = {}, {}
    for dyd in sorted(dyds):
        x = Path(dyd).read_text(errors='ignore')
        for tag in re.findall(r'<dyn:blackBoxModel[^>]*>', x):
            a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            if not a.get('parFile') or not a.get('parId'):
                continue
            path = os.path.normpath(os.path.join(os.path.dirname(dyd), a['parFile']))
            if not os.path.exists(path):
                continue
            cache.setdefault(path, parsets(path))
            s = cache[path].get(a['parId'])
            if s:
                rel = os.path.relpath(path, dynawo_dir if path.startswith(dynawo_dir) else src_dir)
                out.setdefault(a['lib'], []).append((s, rel))
    return out


def _docs(model, ddb, src_dir):
    """{full parameter name: description} from the Modelica classes the model is assembled from."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from composition_catalog import ModelicaTree
    f = Path(src_dir, 'dynawo/sources/Models/Modelica/PreassembledModels', f'{model}.xml')
    if not f.exists():
        return {}
    tree, out = ModelicaTree(ddb), {}
    for unit, cls in re.findall(r'<dyn:unitDynamicModel id="([^"]+)" name="([^"]+)"', f.read_text()):
        for k, d in tree.declarations(cls).items():
            if d['kind'] == 'parameter' and d.get('doc'):
                out[f'{unit}_{k}'] = d['doc']
    return out


def entry(model, ddb, ex, donor_vals=None, src_dir=''):
    params, variables = desc(ddb, model)
    docs = _docs(model, ddb, src_dir)
    req = {k: v for k, v in params.items() if v['readOnly'] == 'false' and 'defaultValue' not in v}
    prefix = max({k.split('_')[0] for k in req} or {''}, key=lambda p: sum(k.startswith(p + '_') for k in req))
    sets = ex.get(model, [])
    vals, src = ({}, '')
    if sets:     # the most complete example set
        vals, src = max(sets, key=lambda s: sum(k in s[0] for k in req))
    out_p, refs = {}, {}
    for k, d in sorted(req.items()):
        short = k.split('_', 1)[1] if '_' in k else k
        if short in INIT_REF:
            refs[k] = INIT_REF[short]
            continue
        v = FIXED.get(short) or vals.get(k, (None, None))[1]
        for dv in donor_vals or []:          # same parameter in a related model (full name, then short name)
            if v is None:
                v = dv.get(k, (None, None))[1]
            if v is None:
                v = next((x[1] for n, x in dv.items() if n.split('_', 1)[-1] == short), None)
        q = dict(type=d['valueType'], value=v)
        if k in docs:
            q['doc'] = docs[k]
        if short in BIND:
            q['bind'] = BIND[short]
        out_p[k] = q
    for k, (t, v) in vals.items():       # explicit overrides of defaulted parameters in the example
        if k in params and k not in out_p and k not in refs and params[k]['readOnly'] == 'false':
            out_p[k] = dict(type=params[k]['valueType'], value=v, doc=docs.get(k, ''))
    for k, v in EXTRA.get(model, {}).items():
        out_p.setdefault(k, dict(type='DOUBLE', value=repr(v)))
    pv = next((v for c in P_VARS for v in variables if v == c or v.endswith('_' + c)), None)
    qv = next((v for c in Q_VARS for v in variables if v == c or v.endswith('_' + c)), None)
    missing = [k for k, q in out_p.items() if q['value'] is None and not q.get('bind')]
    return dict(prefix=prefix, params=out_p, references=refs, p_var=pv, q_var=qv,
                source=f'Dynawo example {src}' if src else '', missing=missing), vals


def main(dynawo_dir, src_dir):
    ddb = Path(dynawo_dir, 'ddb')
    import pypowsybl.dynamic as dyn
    mm = dyn.ModelMapping()
    known = set()
    for cat in ('Wecc', 'GridFormingConverter', 'SynchronizedGenerator', 'InfiniteBus', 'InertialGrid', 'Load'):
        known |= set(mm.get_supported_models_information(cat).index)
    ex = examples(dynawo_dir, src_dir)
    sources, loads, used = {}, {}, {}
    for model, kind, label, cat, sync, donor in SOURCES:
        if not Path(ddb, f'{model}.so').exists():
            continue
        donors = [donor] if isinstance(donor, str) else list(donor or [])
        e, vals = entry(model, ddb, ex, [used[d] for d in donors if d in used], src_dir)
        used[model] = vals or (used.get(donors[0], {}) if donors else {})
        if donors and not ex.get(model):
            e['source'] = 'borrowed from ' + ', '.join(f'{d} ({sources.get(d, {}).get("source", "")})' for d in donors)
        e.update(kind=kind, label=label, category=cat, synchronized=sync, additional=model not in known)
        sources[model] = e
        print(f'{model:58s} {cat:24s} params={len(e["params"]):3d} missing={e["missing"][:6]} P={e["p_var"]}')
    for model, label, donor in LOADS:
        e, _ = entry(model, ddb, ex, None, src_dir)
        e.update(label=label, category='BASE_LOAD', additional=model not in known)
        if model == 'LoadAlphaBeta':
            e['params'] = {'load_alpha': dict(type='DOUBLE', value='2.0', doc='P exponent'),
                           'load_beta': dict(type='DOUBLE', value='2.0', doc='Q exponent')}
            e['missing'] = []
        loads[model] = e
        print(f'{model:58s} params={len(e["params"]):3d} missing={e["missing"][:6]} P={e["p_var"]}')
    target = Path(__file__).resolve().parent.parent / 'dynawo_par' / 'source_library.json'
    old = json.loads(target.read_text()) if target.exists() else {}
    for grp, cur in (('sources', sources), ('loads', loads)):
        for k, v in cur.items():
            for f in ('verified', 'verification'):
                if f in old.get(grp, {}).get(k, {}):
                    v[f] = old[grp][k][f]
    target.write_text(json.dumps(dict(dynawo_version='1.7.0', sources=sources, loads=loads), indent=1))
    print('->', target)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
