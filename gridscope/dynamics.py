"""
Dynamic (transient) stability studies with Dynawo, driven from a GridScope case.

* Every in-service generator is either a Dynawo synchronous machine with a governor, voltage
  regulator and PSS (any combination: Dynawo's pre-assembled models, or ones composed and compiled
  by custom_models.py), or a converter-interfaced / simplified model from source_library.json
  (WECC PV, wind and battery storage, grid-forming converters, GeneratorPQ/PV, grid equivalents).
  Data are given per generator in generator["dyn"]; anything missing is filled with typical values
  scaled to the rating (see default_dyn_params).
* Loads use the Dynawo load model of case["dynamicSettings"] (default LoadAlphaBeta with
  alpha = beta = 2, constant impedance), with optional per-bus overrides.
* The network is initialised from an OpenLoadFlow solution of the same case.

Studies
-------
simulate()          one time-domain simulation with user events
critical_clearing_time()   bisection on fault duration for a 3-phase bus fault
screen_contingencies()     dynamic N-1: fault + branch trip for each branch
"""
from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import pypowsybl as pp
import pypowsybl.dynamic as dyn
import pypowsybl.network as pn

from .case import _num
from . import protection as PR
from .convert import NetworkMap, build_network
from .loadflow import lf_parameters

import pypowsybl.loadflow as lf

PAR_DIR = Path(__file__).resolve().parent.parent / 'dynawo_par'

import json
import re
import re as _re

# ------------------------------------------------------------ model library
# Dynawo ships machine + controls as pre-assembled models (one compiled model per combination).
# dynawo_par/controller_library.json (built by tools/build_controller_library.py from Dynawo's own
# model descriptions, regression tests and IEEE RTS examples) lists the combinations offered here and
# reference values for every user-facing governor / AVR / PSS / limiter parameter.
CONTROL_LIB = json.loads((PAR_DIR / 'controller_library.json').read_text())

# Non-synchronous generator models (solar PV, wind, battery storage, grid-forming converters, simplified
# generators, grid equivalents) and load models, with reference parameters from Dynawo's examples
# (dynawo_par/source_library.json, built by tools/build_source_library.py).
SOURCE_LIB = json.loads((PAR_DIR / 'source_library.json').read_text())
SYNC = 'Synchronous machine'
SOURCE_KINDS = [SYNC] + list(dict.fromkeys(v['kind'] for v in SOURCE_LIB['sources'].values()))
DEFAULT_LOAD_MODEL = 'LoadAlphaBeta'
_ADD_METHOD = {'WECC': 'add_wecc', 'GRID_FORMING_CONVERTER': 'add_grid_forming_converter',
               'SYNCHRONIZED_GENERATOR': 'add_synchronized_generator', 'INFINITE_BUS': 'add_infinite_bus',
               'INERTIAL_GRID': 'add_inertial_grid'}
# models registered with pypowsybl through additionalModelsFile: {category: [lib entries]}
ADDITIONAL_LIBS: dict = {}


def source_models(kind: str | None = None, include_unverified: bool = False) -> dict:
    out = {k: v for k, v in SOURCE_LIB['sources'].items() if kind in (None, v['kind'])}
    if not include_unverified:
        out = {k: v for k, v in out.items() if v.get('verified', True)}
    return out


def is_sync(d: dict) -> bool:
    return (d.get('source') or SYNC) == SYNC


def _bound_values(snom, pnom, gen, p0, q0):
    pmax = _num(gen.get('Pmax'), 0)
    pmin = _num(gen.get('Pmin'), 0)
    qmax = _num(gen.get('Qmax'), 0)
    qmin = _num(gen.get('Qmin'), 0)
    pmax = max(pmax if 0 < pmax < 5000 else pnom, 1.05 * abs(p0))
    pmin = min(pmin if abs(pmin) < 5000 else 0.0, p0)
    qmax = max(qmax if 0 < qmax < 5000 else 0.6 * snom, q0 + 0.05 * snom)
    qmin = min(qmin if -5000 < qmin < 0 else -0.6 * snom, q0 - 0.05 * snom)
    return dict(SNom=snom, PNom=pnom, PMax=pmax, PMin=pmin, QMax=qmax, QMin=qmin)


def source_parameters(source: str, overrides: dict | None = None, load: bool = False) -> list[dict]:
    """Rows describing the user-facing parameters of a source (or load) model."""
    lib = SOURCE_LIB['loads' if load else 'sources'][source]
    overrides = overrides or {}
    rows = []
    for k, q in lib['params'].items():
        rows.append(dict(key=k, name=k.split('_', 1)[-1], type=q['type'], default=q['value'],
                         value=str(overrides.get(k, q['value'])), doc=q.get('doc', ''), bind=q.get('bind'),
                         source=lib.get('source', '')))
    return rows


def _model_par_set(set_id: str, model: str, overrides: dict | None, bound: dict, load=False) -> str:
    lib = SOURCE_LIB['loads' if load else 'sources'][model]
    lines = [f'  <set id="{set_id}">']
    for row in source_parameters(model, overrides, load):
        v = bound[row['bind']] if row['bind'] else row['value']
        if v in (None, '', 'None'):
            raise ValueError(f'{model}: parameter {row["name"]} has no value — set it in the model editor.')
        lines.append(f'    <par type="{row["type"]}" name="{row["key"]}" value="{_fmt(row["type"], v)}"/>')
    for k, orig in lib['references'].items():
        lines.append(f'    <reference name="{k}" origData="IIDM" origName="{orig}" type="DOUBLE"/>')
    lines.append('  </set>')
    return '\n'.join(lines)


def _additional_entry(model):
    v = SOURCE_LIB['sources'][model]
    e = dict(lib=model, doc=v['label'])
    if v['category'] == 'WECC':
        e['internalModelPrefix'] = v['prefix']
    if v.get('synchronized'):
        e['properties'] = ['SYNCHRONIZED']
    return e


def load_model_of(case: dict, bus) -> tuple[str, dict]:
    """(Dynawo load model, parameter overrides) of the load at `bus`."""
    st = case.get('dynamicSettings') or {}
    per = (st.get('loadModels') or {}).get(str(bus))
    model = per or st.get('loadModel') or DEFAULT_LOAD_MODEL
    if model not in SOURCE_LIB['loads']:
        model = DEFAULT_LOAD_MODEL
    return model, dict((st.get('loadParams') or {}).get(model, {}))


MACHINES = {
    'Round rotor (4 windings)': 'GeneratorSynchronousFourWindingsProportionalRegulations',
    'Salient pole (3 windings)': 'GeneratorSynchronousThreeWindingsProportionalRegulations',
}
DEFAULT_MODEL = MACHINES['Round rotor (4 windings)']
MODELS = MACHINES                    # backwards compatibility
MODEL_LABEL = {v: k for k, v in MACHINES.items()}
ROLE_LABEL = {'governor': 'Governor', 'avr': 'Voltage regulator (AVR)', 'pss': 'Power system stabilizer (PSS)',
              'limiter': 'Excitation limiter'}

# Typical machine data on the machine's own base (Dynawo IEEE 14 example, Kundur-style values)
TYPICAL = {
    'four': dict(H=5.4, RaPu=0.0028, XlPu=0.202, XdPu=2.22, XpdPu=0.384, XppdPu=0.264, Tpd0=8.094, Tppd0=0.08,
                 XqPu=2.22, XpqPu=0.393, XppqPu=0.262, Tpq0=1.572, Tppq0=0.084),
    'three': dict(H=4.975, RaPu=0.004, XlPu=0.102, XdPu=0.75, XpdPu=0.225, XppdPu=0.154, Tpd0=3.0, Tppd0=0.04,
                  XqPu=0.45, XppqPu=0.2, Tppq0=0.04),
}
MACHINE_KEYS = ['SNom', 'H', 'RaPu', 'XlPu', 'XdPu', 'XpdPu', 'XppdPu', 'Tpd0', 'Tppd0', 'XqPu', 'XpqPu', 'XppqPu',
                'Tpq0', 'Tppq0']


def _winding(model: str) -> str:
    info = CONTROL_LIB['models'].get(model)
    if info and info.get('machine'):
        return 'four' if info['machine'].startswith('Round') else 'three'
    return 'four' if 'FourWindings' in model else 'three'


def available_models(ddb: Path | None = None, include_unverified: bool = False) -> dict:
    """Library models compiled into the local Dynawo install; by default only verified combinations
    (see tools/verify_controller_library.py)."""
    models = {k: v for k, v in CONTROL_LIB['models'].items() if not v.get('custom')}
    if not include_unverified:
        models = {k: v for k, v in models.items() if v.get('verified', True)}
    ddb = ddb or _dynawo_ddb()
    if ddb is not None and ddb.exists():
        models = {k: v for k, v in models.items() if (ddb / f'{k}.so').exists()}
    return models


def known_model(model: str) -> bool:
    """True for a library model or a composed one (re-registered from its name if needed)."""
    if model in CONTROL_LIB['models']:
        return True
    if isinstance(model, str) and model.startswith('GsSync'):
        from . import custom_models
        return custom_models.entry_from_name(model) is not None
    return False


def _dynawo_ddb():
    cfg = Path.home() / '.itools' / 'config.yml'
    if cfg.exists():
        m = _re.search(r'homeDir:\s*(\S+)', cfg.read_text())
        if m:
            return Path(m.group(1)).expanduser() / 'ddb'
    return None


def model_units(model: str) -> dict:
    """{unit prefix: component class} for the controls of a library model."""
    return CONTROL_LIB['models'].get(model, {}).get('units', {})


def component(cls: str) -> dict:
    return CONTROL_LIB['components'][cls]


def find_model(machine: str, governor: str, avr: str, pss: str) -> str | None:
    for k, v in CONTROL_LIB['models'].items():
        if (v['machine'], v['governor'], v['avr'], v['pss']) == (machine, governor, avr, pss):
            return k
    return None


# Benchmark machine data, keyed by generator bus.
# IEEE 9 = WSCC 3-machine system, Anderson & Fouad (2003), Table 2.1, converted from the
# 100 MVA base to each machine's own rating. Subtransient data are not in the source:
# X''d = 0.75 X'd, T''d0 = T''q0 = 0.05 s, Xl = 0.6 X''d (typical).
_WSCC = {
    1: dict(model='GeneratorSynchronousThreeWindingsProportionalRegulations', SNom=247.5, H=9.551,
            XdPu=0.3614, XpdPu=0.1505, XqPu=0.2398, Tpd0=8.96),
    2: dict(model='GeneratorSynchronousFourWindingsProportionalRegulations', SNom=192.0, H=3.333,
            XdPu=1.7199, XpdPu=0.2300, XqPu=1.6598, XpqPu=0.3780, Tpd0=6.0, Tpq0=0.535),
    3: dict(model='GeneratorSynchronousFourWindingsProportionalRegulations', SNom=128.0, H=2.352,
            XdPu=1.6800, XpdPu=0.2321, XqPu=1.6100, XpqPu=0.3200, Tpd0=5.89, Tpq0=0.600),
}
for _d in _WSCC.values():
    _d.update(XppdPu=round(0.75 * _d['XpdPu'], 4), Tppd0=0.05, Tppq0=0.05)
    _d['XppqPu'] = _d['XppdPu']
    _d['XlPu'] = round(0.6 * _d['XppdPu'], 4)
    _d['RaPu'] = 0.0
# IEEE 39 = New England 10-machine system: inertia constants H on the 100 MVA base
# (gen 39 is the large equivalent of the external system). Other data stay typical.
_NE39_H100 = {30: 42.0, 31: 30.3, 32: 35.8, 33: 28.6, 34: 26.0, 35: 34.8, 36: 26.4, 37: 24.3, 38: 34.5, 39: 500.0}


def benchmark_machine_data(case: dict) -> tuple[dict, str]:
    """Return ({gen id: dyn dict}, description) for a recognised benchmark case, else ({}, '')."""
    gens = [g for g in case['generators'] if _num(g.get('status'), 1)]
    buses = {int(_num(g['bus'], -1)) for g in gens}
    out = {}
    if len(case['buses']) == 9 and buses == {1, 2, 3}:
        for g in gens:
            out[g['id']] = dict(_WSCC[int(_num(g['bus'], 0))])
        return out, 'WSCC 3-machine data (Anderson & Fouad): ratings, H, Xd, X\'d, Xq, X\'q, T\'d0, T\'q0.'
    if len(case['buses']) == 39 and buses == set(_NE39_H100):
        for g in gens:
            b = int(_num(g['bus'], 0))
            snom = default_dyn_params(g, case)['SNom']
            out[g['id']] = dict(SNom=snom, H=round(_NE39_H100[b] * 100.0 / snom, 3))
        return out, 'New England 10-machine inertia constants (H); other machine data typical.'
    return {}, ''


# editable machine columns shown in the GUI, in order
DYN_COLUMNS = ['SNom', 'H', 'XdPu', 'XpdPu', 'XppdPu', 'XqPu', 'Tpd0', 'Tppd0']
# legacy keys from earlier versions of the app -> control parameter
_LEGACY = {'AVRGain': 'voltageRegulator_Gain', 'GovGain': 'governor_KGover',
           'EfdMinPu': 'voltageRegulator_EfdMinPu', 'EfdMaxPu': 'voltageRegulator_EfdMaxPu'}


# ------------------------------------------------------------ machine data

def default_dyn_params(gen: dict, case: dict, p0: float | None = None, q0: float | None = None) -> dict:
    """Machine data (typical values scaled to the rating, overridden by gen['dyn']), the Dynawo model
    and the control-parameter overrides."""
    have = dict(gen.get('dyn') or {})
    model = have.get('model') or DEFAULT_MODEL
    if not known_model(model):
        model = DEFAULT_MODEL
    p = abs(_num(p0 if p0 is not None else gen.get('Pg'), 0))
    q = abs(_num(q0 if q0 is not None else gen.get('Qg'), 0))
    rated = _num(gen.get('ratedMVA'), 0)
    pmax = _num(gen.get('Pmax'), 0)
    if rated > 0:
        snom = rated
    elif 0 < pmax < 5000:
        snom = pmax / 0.9
    else:
        snom = max(100.0, 1.5 * p)
    snom = max(snom, 1.15 * math.hypot(p, q), 10.0)
    out = dict(model=model, SNom=round(snom, 1), **TYPICAL[_winding(model)])
    out.update({k: v for k, v in have.items() if v not in (None, '') and k in MACHINE_KEYS})
    controls = dict(have.get('controls') or {})
    for old, new in _LEGACY.items():
        if old in have and new not in controls:
            controls[new] = have[old]
    out['controls'] = controls
    src = have.get('source') or SYNC
    out['source'] = src if src == SYNC or src in SOURCE_LIB['sources'] else SYNC
    out['sourceParams'] = dict(have.get('sourceParams') or {})
    return out


def control_parameters(model: str, controls: dict | None = None) -> list[dict]:
    """Rows describing every user-facing control parameter of `model` (default, current value, doc)."""
    controls = controls or {}
    rows = []
    for unit, cls in model_units(model).items():
        comp = component(cls)
        for name, p in comp['params'].items():
            key = f'{unit}_{name}'
            rows.append(dict(key=key, unit=unit, role=comp['role'], component=comp['label'], name=name,
                             type=p['type'], default=p['value'], value=str(controls.get(key, p['value'])),
                             doc=p.get('doc', ''), bind=p.get('bind'), source=comp['source']))
    return rows


def _fmt(t, v):
    if t == 'BOOL':
        return 'true' if str(v).strip().lower() in ('true', '1', 'yes') else 'false'
    if t == 'INT':
        return str(int(float(v)))
    if t == 'DOUBLE':
        return repr(float(v))
    return str(v)


def _gen_par_set(set_id: str, d: dict, kv: float, p0: float, pmin: float, pmax: float, notes: list | None = None,
                 gen_label: str = '') -> str:
    model = d['model']
    snom = float(d['SNom'])
    pnom = max(0.9 * snom, 1.05 * abs(p0))
    gov_pmax = max(min(pmax, pnom) if pmax > 0 else pnom, 1.05 * p0)
    gov_pmin = min(max(pmin, 0.0), max(p0, 0.0))
    units = model_units(model)
    avr_cls = next((c for u, c in units.items() if component(c)['role'] == 'avr'), None)
    exc = component(avr_cls).get('excitation', {}) if avr_cls else {}
    par = dict(
        generator_ExcitationPu=('INT', exc.get('ExcitationPu', 1)), generator_UseApproximation=('BOOL', 'true'),
        generator_md=('DOUBLE', 0.0), generator_mq=('DOUBLE', 0.0),
        generator_nd=('DOUBLE', 0.0), generator_nq=('DOUBLE', 0.0),
        generator_MdPuEfd=('DOUBLE', exc.get('MdPuEfd', 0.0)), generator_DPu=('DOUBLE', 0.0),
        generator_H=('DOUBLE', d['H']), generator_RaPu=('DOUBLE', d['RaPu']), generator_XlPu=('DOUBLE', d['XlPu']),
        generator_XdPu=('DOUBLE', d['XdPu']), generator_XpdPu=('DOUBLE', d['XpdPu']),
        generator_XppdPu=('DOUBLE', d['XppdPu']), generator_Tpd0=('DOUBLE', d['Tpd0']),
        generator_Tppd0=('DOUBLE', d['Tppd0']), generator_XqPu=('DOUBLE', d['XqPu']),
        generator_XppqPu=('DOUBLE', d['XppqPu']), generator_Tppq0=('DOUBLE', d['Tppq0']),
        generator_UNom=('DOUBLE', kv), generator_SNom=('DOUBLE', snom),
        generator_PNomTurb=('DOUBLE', pnom), generator_PNomAlt=('DOUBLE', pnom),
        generator_SnTfo=('DOUBLE', snom), generator_UNomHV=('DOUBLE', kv), generator_UNomLV=('DOUBLE', kv),
        generator_UBaseHV=('DOUBLE', kv), generator_UBaseLV=('DOUBLE', kv),
        generator_RTfPu=('DOUBLE', 0.0), generator_XTfPu=('DOUBLE', 0.0),
    )
    if _winding(model) == 'four':
        par['generator_XpqPu'] = ('DOUBLE', d.get('XpqPu', TYPICAL['four']['XpqPu']))
        par['generator_Tpq0'] = ('DOUBLE', d.get('Tpq0', TYPICAL['four']['Tpq0']))
    bound = dict(SNom=snom, PNom=pnom, PMax=gov_pmax, PMin=gov_pmin)
    load = p0 / pnom if pnom else 0.0
    for row in control_parameters(model, d.get('controls')):
        v = row['value']
        if row['bind']:
            v = bound[row['bind']]
        elif row['name'] == 'TablesFile':
            v = str((PAR_DIR / v).resolve())
        elif row['role'] == 'governor' and row['name'] in ('PMaxPu', 'VMax') and float(v) < 1.02 * load:
            if notes is not None:
                notes.append(f'{gen_label}: governor {row["name"]} raised from {float(v):.3f} to {1.02 * load:.3f} pu '
                             'so the initial loading is within its range.')
            v = 1.02 * load
        elif row['role'] == 'governor' and row['name'] in ('PMinPu', 'VMin') and float(v) > load:
            if notes is not None:
                notes.append(f'{gen_label}: governor {row["name"]} lowered from {float(v):.3f} to {load:.3f} pu '
                             'so the initial loading is within its range.')
            v = load
        par[row['key']] = (row['type'], _fmt(row['type'], v))
    lines = [f'  <set id="{set_id}">']
    lines += [f'    <par type="{t}" name="{k}" value="{v}"/>' for k, (t, v) in par.items()]
    for name, orig in (('P0Pu', 'p_pu'), ('Q0Pu', 'q_pu'), ('U0Pu', 'v_pu'), ('UPhase0', 'angle_pu')):
        lines.append(f'    <reference name="generator_{name}" origData="IIDM" origName="{orig}" type="DOUBLE"/>')
    lines.append('  </set>')
    return '\n'.join(lines)


# ------------------------------------------------------------ preparation

@dataclass
class Prepared:
    network: object           # solved pre-disturbance network (never simulated directly)
    net_map: NetworkMap
    gen_params: dict          # IIDM gen id -> param dict
    gen_case_id: dict         # IIDM gen id -> case gen id (or VSRC_x)
    lf_status: str
    work_dir: Path
    xiidm: str = ''           # snapshot of the solved network: each run starts from a fresh copy,
                              # because Dynawo writes its final state back into the network it ran on
    notes: list = field(default_factory=list)   # automatic adjustments made while writing parameters
    load_models: dict = field(default_factory=dict)   # IIDM load id -> Dynawo load model
    load_sets: dict = field(default_factory=dict)     # IIDM load id -> parameter set id
    protected: dict = field(default_factory=dict)     # kind -> {IIDM id: case id} with protection models
    line_models: dict = field(default_factory=dict)   # IIDM line id -> case id, lines mapped to GsLineProtected
    tfo_models: list = field(default_factory=list)    # IIDM transformers mapped to GsNetworkTransformer
    gen_models: dict = field(default_factory=dict)    # case generator id -> final dynamic model

    @property
    def sync_gens(self):
        return {g: d for g, d in self.gen_params.items() if is_sync(d)}

    def fresh_network(self):
        return pn.load_from_string('case.xiidm', self.xiidm)


def _rating(br: dict, flows, cfg: dict, notes: list, label: str) -> float:
    """Branch rating (MVA) for overcurrent pick-ups; the pre-disturbance loading when no rating is given."""
    rating = _num(br.get('rate'), 0)
    if rating > 0 or not cfg.get('oc'):
        return rating
    s0 = math.hypot(float(np.nan_to_num(flows['p1'])), float(np.nan_to_num(flows['q1'])))
    if s0 <= 1e-3:
        notes.append(f'{label}: no rating and no flow — overcurrent protection not applied.')
        return 0.0
    notes.append(f'{label}: no rating (Rate MVA) — overcurrent pick-up taken as {cfg["i_pickup"]:g} % of the '
                 f'pre-disturbance loading ({s0:.1f} MVA).')
    return s0


def _report_skipped(report) -> list[str]:
    """Events pypowsybl dropped because the target model can't receive them."""
    out = []
    for ln in str(report).splitlines():
        ln = ln.strip()
        if 'requires a connection with' in ln and 'does not implement it' in ln:
            ev = ln.split()[1]
            out.append(f'Event {ev} was not applied: {ln.split(" but ", 1)[-1]} '
                       f'(pypowsybl needs {ln.split("connection with a ", 1)[-1].split(" but")[0]}).')
    return out


def _report_errors(report) -> str:
    """Dynawo's own error messages from the pypowsybl report (empty when there are none)."""
    try:
        lines = [ln.strip(' +') for ln in str(report).splitlines()]
    except Exception:  # noqa: BLE001
        return ''
    errs = [ln for ln in lines if re.search(r'error|fail|not found|cannot|exception', ln, re.I)
            and 'number of' not in ln]
    errs = list(dict.fromkeys(errs))[:6]
    return (' | Dynawo log: ' + ' | '.join(errs)) if errs else ''


def prepare(case: dict, load_alpha=2.0, load_beta=2.0, steps: dict | None = None) -> Prepared:
    """Build the network, solve the load flow (Dynawo starts from it), write models.par."""
    network, m = build_network(case)
    res = lf.run_ac(network, lf_parameters(m, enforce_q_limits=True))[0]
    status = res.status.name
    gens = network.get_generators()
    kv = {str(b['id']): _num(b['baseKV'], 1) for b in case['buses']}
    vl_kv = {v: kv[k] for k, v in m.bus_vl.items()}
    rev = m.gen_rev
    by_case_id = {g['id']: g for g in case['generators']}
    params, case_id, sets, notes = {}, {}, [], []
    gen_models = {}
    prot = PR.settings(case)
    fn = PR.f_nom(case)
    # Line protection needs the whole network in Modelica (GsLineProtected on every line). Two things
    # cannot be modelled that way with pypowsybl 1.16 + Dynawo 1.7: phase-shifting transformers (no phase
    # shift in the Modelica transformer) and converter models with a frequency input (pypowsybl then
    # feeds a constant frequency set-point that Dynawo 1.7 cannot connect to them).
    modelica = any(PR.active(c, 'lines') for c in prot['lines'].values())
    if modelica:
        blockers = []
        if len(network.get_phase_tap_changers()):
            blockers.append('the case has phase-shifting transformers (Dynawo\'s Modelica transformer has no '
                            'phase shift)')
        conv = []
        for g in case['generators']:
            if not _num(g.get('status'), 1):
                continue
            info = SOURCE_LIB['sources'].get(default_dyn_params(g, case)['source'])
            if info and info.get('synchronized') and \
                    info['category'] in ('WECC', 'GRID_FORMING_CONVERTER'):
                conv.append(str(g['id']))
        if conv:
            blockers.append('converter models (' + ', '.join(conv) + ') cannot be connected to the frequency '
                            'reference pypowsybl uses for an all-Modelica network with Dynawo 1.7')
        if blockers:
            notes.append('Line protection not applied: it needs every branch and bus modelled in Modelica, and '
                         + '; '.join(blockers) + '.')
            modelica = False
    shunt_loads = []
    if modelica and len(network.get_shunt_compensators()):
        # Line protection needs the whole network in Modelica; pypowsybl's Modelica shunt cannot report its
        # final state, so each shunt becomes a constant-impedance load drawing the same Q at the solved voltage
        sh = network.get_shunt_compensators()
        bus_v = network.get_buses()
        vnom0 = network.get_voltage_levels()['nominal_v']
        rows = []
        for iid, r in sh.iterrows():
            un = vnom0[r['voltage_level_id']]
            v = float(bus_v.loc[r['bus_id'], 'v_mag']) if r['bus_id'] in bus_v.index else un
            rows.append(dict(id=f'SHL_{iid}', voltage_level_id=r['voltage_level_id'],
                             bus_id=network.get_shunt_compensators(all_attributes=True).loc[iid, 'bus_breaker_bus_id'],
                             p0=float(r['g']) * v * v, q0=-float(r['b']) * v * v))
        network.remove_elements(list(sh.index))
        network.create_loads(pd.DataFrame(rows).set_index('id'))
        res = lf.run_ac(network, lf_parameters(m, enforce_q_limits=True))[0]
        status = res.status.name
        shunt_loads = [r['id'] for r in rows]
        notes.append('Shunts are modelled as constant-impedance loads (needed for line protection).')
    for gid, row in gens.iterrows():
        cid = rev.get(gid, gid)
        g = by_case_id.get(cid, dict(id=cid, Pg=0, Qg=0, Pmax=0, Pmin=0))
        p0, q0 = -float(np.nan_to_num(row['p'])), -float(np.nan_to_num(row['q']))
        d = default_dyn_params(g, case, p0, q0)
        params[gid], case_id[gid] = d, cid
        gp = prot['generators'].get(str(cid))
        if gp and PR.active(gp, 'generators'):
            if is_sync(d):           # the machine model gets the protection relay compiled in
                from . import custom_models
                try:
                    pm = custom_models.with_protection(d['model'])
                    d['controls'] = custom_models.remap_controls(d['model'], pm, d.get('controls'))
                    d['model'] = pm
                    d['protection'] = gp
                except ValueError as e:
                    notes.append(f'Generator {cid}: protection not applied — {e}')
            else:
                notes.append(f'Generator {cid}: generator protection is available for synchronous machines only.')
        if is_sync(d):
            st = _gen_par_set(f'GEN_{gid}', d, vl_kv[row['voltage_level_id']], p0,
                              _num(g.get('Pmin'), 0), _num(g.get('Pmax'), 0), notes, f'Generator {cid}')
            if d.get('protection'):
                st = st.replace('  </set>', '\n'.join(PR.gen_pars(d['protection'], fn)) + '\n  </set>')
            if d['model'].startswith('GsSync'):
                from . import custom_models as CMod
                gov = model_units(d['model']).get('governor', '').rsplit('.', 1)[-1]
                scale = CMod.PMREF_SCALE.get(gov)
                if scale:
                    v = re.search(rf'name="governor_{scale}" value="([^"]+)"', st)
                    st = st.replace('  </set>', f'    <par type="DOUBLE" name="setpoints_PmRefScale" '
                                                f'value="{v.group(1) if v else 1}"/>\n  </set>')
            if d['model'].startswith('GsSync') and (steps or {}).get(str(cid)):
                extra = []
                for kind, (t, v) in steps[str(cid)].items():
                    nm = {'P': ('tPmStep', 'deltaPmRefPu'), 'U': ('tUsStep', 'deltaUsRefPu')}[kind]
                    extra += [f'    <par type="DOUBLE" name="setpoints_{nm[0]}" value="{t:.6g}"/>',
                              f'    <par type="DOUBLE" name="setpoints_{nm[1]}" value="{v:.6g}"/>']
                st = st.replace('  </set>', '\n'.join(extra) + '\n  </set>')
            sets.append(st)
        else:
            snom = float(d['SNom'])
            bound = _bound_values(snom, max(0.9 * snom, 1.05 * abs(p0)), g, p0, q0)
            sets.append(_model_par_set(f'GEN_{gid}', d['source'], d['sourceParams'], bound))
        gen_models[str(cid)] = d['model'] if is_sync(d) else d['source']
    load_models, load_sets, protected_loads = {}, {}, {}
    for bus, lid in m.load.items():
        lp = prot['loads'].get(str(bus))
        if lp and PR.active(lp, 'loads'):
            # load shedding: alpha-beta load with the shedding relays (replaces the bus's load model)
            # all-Modelica network: pypowsybl gives no system frequency, the relay measures it locally
            load_models[lid] = ('GsLoadSheddingLocal' if modelica else 'GsLoadShedding', {})
            load_sets[lid] = f'LOADP_{lid}'
            protected_loads[lid] = str(bus)
            sets.append(PR.load_par_set(f'LOADP_{lid}', lp, load_alpha, load_beta, fn))
            if load_model_of(case, bus)[0] != 'LoadAlphaBeta':
                notes.append(f'Load at bus {bus}: load shedding uses the α/β load model (α = {load_alpha:g}, '
                             f'β = {load_beta:g}) instead of {load_model_of(case, bus)[0]}.')
        else:
            load_models[lid] = load_model_of(case, bus)
            load_sets[lid] = f'LOAD_{load_models[lid][0]}'
    for lid in shunt_loads:
        load_models[lid] = ('LoadAlphaBeta', {})
        load_sets[lid] = 'LOAD_SHUNT'
    if shunt_loads:
        sets.append(_model_par_set('LOAD_SHUNT', 'LoadAlphaBeta', {'load_alpha': 2.0, 'load_beta': 2.0}, {},
                                   load=True))
    for model, ov in {mo: ov for lid, (mo, ov) in load_models.items()
                      if not mo.startswith('GsLoadShedding') and lid not in shunt_loads}.items():
        if model == 'LoadAlphaBeta':
            ov = {'load_alpha': load_alpha, 'load_beta': load_beta, **ov}
        sets.append(_model_par_set(f'LOAD_{model}', model, ov, {}, load=True))
    # branch protection
    lines_df = network.get_lines()
    tfo_df = network.get_2_windings_transformers()
    vnom = network.get_voltage_levels()['nominal_v']
    br_by_id = {str(b['id']): b for b in case['branches']}
    protected_lines, protected_tfos = {}, {}
    for bid, cfg in (prot['lines'] if modelica else {}).items():
        iid = m.branch.get(bid)
        if not PR.active(cfg, 'lines') or iid is None:
            continue
        if iid not in lines_df.index:
            notes.append(f'Branch {bid}: line protection skipped (it is a transformer in the network model).')
            continue
        ln = lines_df.loc[iid]
        v1, v2 = vnom[ln['voltage_level1_id']], vnom[ln['voltage_level2_id']]
        if abs(v1 - v2) > 1e-6 * v1:
            notes.append(f'Line {bid}: protection skipped (ends at different nominal voltages).')
            continue
        zb = v1 ** 2 / 100.0
        rating = _rating(br_by_id.get(bid, {}), ln, cfg, notes, f'Line {bid}')
        sets.append(PR.line_par_set(f'LINE_{iid}', cfg, ln['r'] / zb, ln['x'] / zb, ln['g1'] * zb,
                                    ln['b1'] * zb, rating))
        protected_lines[iid] = bid
    # transformer overcurrent: pick-up in A on side 1, from the rating (or the pre-disturbance loading)
    tfo_imax = {}
    for bid, cfg in prot['transformers'].items():
        iid = m.branch.get(bid)
        if not PR.active(cfg, 'transformers') or iid is None or iid not in tfo_df.index:
            continue
        t = tfo_df.loc[iid]
        rating = _rating(br_by_id.get(bid, {}), t, cfg, notes, f'Transformer {bid}')
        if rating <= 0:
            continue
        i_rated = rating * 1e3 / (math.sqrt(3) * vnom[t['voltage_level1_id']])      # A on side 1
        tfo_imax[iid] = (cfg, cfg['i_pickup'] / 100 * i_rated)
        protected_tfos[iid] = bid
    tfo_models = []
    if protected_lines:
        # pypowsybl requires a dynamic model on every branch once one line has one: the other lines get
        # the same model with all protection functions off; transformers use GsNetworkTransformer
        # (Dynawo's NetworkTransformer + branch state + overcurrent relay)
        for iid in tfo_df.index:
            cfg, imax = tfo_imax.get(iid, (None, 1e9))
            sets.append('\n'.join([f'  <set id="TFO_{iid}">'] + [
                f'    <par type="DOUBLE" name="transformer_{k}" value="{float(tfo_df.loc[iid, c])!r}"/>'
                for k, c in (('R', 'r'), ('X', 'x'), ('G', 'g'), ('B', 'b'))] + [
                PR._par('BOOL', 'transformer_OCOn', cfg is not None), PR._par('DOUBLE', 'transformer_IMax', imax),
                PR._par('DOUBLE', 'transformer_tOC', cfg['t_oc'] if cfg else 1.0)] + [
                f'    <reference name="transformer_{k}" origData="IIDM" origName="{o}" type="DOUBLE"/>'
                for k, o in (('RatedU1', 'ratedU1'), ('RatedU2', 'ratedU2'), ('U1Nom', 'v1Nom'), ('U2Nom', 'v2Nom'),
                             ('P10Pu', 'p1_pu'), ('Q10Pu', 'q1_pu'), ('U10Pu', 'v1_pu'), ('U1Phase0', 'angle1_pu'))]
                + ['  </set>']))
            tfo_models.append(iid)
        sets.append('  <set id="BUS">\n  </set>')
        rev_branch = m.branch_rev
        for iid, ln in lines_df.iterrows():
            if iid in protected_lines:
                continue
            v1 = vnom[ln['voltage_level1_id']]
            zb = v1 ** 2 / 100.0
            cid = str(rev_branch.get(iid, iid))
            inner = {FAULT_SEP + 'a': 2, FAULT_SEP + 'b': 1}.get(cid[-2:]) if FAULT_SEP in cid else None
            sets.append(PR.line_par_set(f'LINE_{iid}', dict(PR.LINE, _inner=inner), ln['r'] / zb, ln['x'] / zb,
                                        ln['g1'] * zb, ln['b1'] * zb, 0.0))
        line_models = {iid: rev_branch.get(iid, iid) for iid in lines_df.index}
    else:
        line_models = {}
        for iid, (cfg, imax) in tfo_imax.items():       # network transformers: Dynawo's current limit automaton
            sets.append(PR.oms_par_set(f'OMS_{iid}', cfg, imax / (cfg['i_pickup'] / 100)))
    needed = set()
    if protected_lines:
        needed |= {'GsLineProtected', 'GsNetworkTransformer'}
    if protected_loads:
        needed.add('GsLoadSheddingLocal' if modelica else 'GsLoadShedding')
    custom = sorted({d['model'] for d in params.values()
                     if is_sync(d) and CONTROL_LIB['models'][d['model']].get('custom')} | needed)
    if custom:                       # composed controls: compile once (cached), then register with Dynawo
        from . import custom_models
        errors = {k: v for k, v in custom_models.ensure_compiled(custom).items() if v}
        if errors:
            raise RuntimeError('\n'.join(errors.values()))
    # one note for all branches whose overcurrent pick-up falls back to the pre-disturbance loading
    fb = [n for n in notes if 'no rating (Rate MVA)' in n]
    if len(fb) > 1:
        notes = [n for n in notes if n not in fb] + [
            'No rating (Rate MVA) for ' + ', '.join(n.split(':')[0] for n in fb)
            + ': overcurrent pick-ups are taken as a percentage of their pre-disturbance loading.']
    work = Path(tempfile.mkdtemp(prefix='gridscope_dyn_'))
    (work / 'models.par').write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<parametersSet xmlns="http://www.rte-france.com/dynawo">\n'
                                     + '\n'.join(sets) + '\n</parametersSet>\n')
    pr = Prepared(network, m, params, case_id, status, work, network.save_to_string('XIIDM'), notes)
    pr.load_models = {lid: mo for lid, (mo, _) in load_models.items()}
    pr.load_sets = load_sets
    pr.gen_models = gen_models
    pr.line_models = line_models
    pr.tfo_models = tfo_models
    pr.protected = dict(lines=protected_lines, transformers=protected_tfos, loads=protected_loads,
                        generators={g: case_id[g] for g, d in params.items() if d.get('protection')})
    return pr


# ------------------------------------------------------------------ events

EVENT_TYPES = ['Bus fault', 'Line fault', 'Trip branch', 'Trip generator', 'Load step (ΔP)', 'Load step (ΔQ)',
               'Generator ΔP setpoint', 'AVR setpoint step (ΔU)']


@dataclass
class Event:
    type: str
    element: str            # case id: bus id, branch id, generator id (loads: bus id)
    start: float
    duration: float = 0.1   # bus fault only
    value: float = 0.0      # step size (pu on 100 MVA for P/Q, pu for U); fault reactance for faults
    side: str = ''          # '', 'ONE', 'TWO' for branch trips; line fault: '' opens the line when the fault
                            # clears, 'NONE' leaves it to the protection
    location: float = 50.0  # line fault: distance from the line's 'from' end, % of its length


FAULT_SEP = '@'


def fault_point_id(line: str, location: float) -> str:
    return f'{line}{FAULT_SEP}{location:g}%'


def split_line_faults(case: dict, events: list[Event]) -> tuple[dict, list[Event], dict]:
    """Model each line fault by splitting its line at the fault point: a new bus between two sections
    (impedance and charging shared in proportion), a bus fault there and, unless side == 'NONE', both sections
    opened when it clears. Returns the modified case, the events on it and {section id: (line id, end)}."""
    faults = [e for e in events if e.type == 'Line fault']
    if not faults:
        return case, events, {}
    import copy
    case = copy.deepcopy(case)
    brs = {b['id']: b for b in case['branches']}
    buses = {str(b['id']): b for b in case['buses']}
    prot = ((case.setdefault('dynamicSettings', {})).setdefault('protection', {})).setdefault('lines', {})
    sections, split = {}, {}
    for e in faults:
        if e.element in split:
            continue
        br = brs[e.element]
        k = min(max(float(e.location), 0.01), 99.99) / 100
        fid = fault_point_id(e.element, round(k * 100, 2))
        fb = dict(buses[str(br['from'])], id=fid, name=f'Fault point {fid}', type='PQ', Pd=0, Qd=0, Gs=0, Bs=0)
        case['buses'].append(fb)
        a = dict(br, id=f'{e.element}{FAULT_SEP}a', to=fid, r=br['r'] * k, x=br['x'] * k, b=_num(br.get('b'), 0) * k)
        b = dict(br, id=f'{e.element}{FAULT_SEP}b', **{'from': fid}, r=br['r'] * (1 - k), x=br['x'] * (1 - k),
                 b=_num(br.get('b'), 0) * (1 - k))
        case['branches'] = [x for x in case['branches'] if x['id'] != e.element] + [a, b]
        split[e.element] = (fid, a['id'], b['id'])
        sections[a['id']], sections[b['id']] = (e.element, 1), (e.element, 2)
        cfg = prot.pop(e.element, None)
        if cfg:
            zx = abs(_num(br['x'], 0)) or math.hypot(_num(br['r'], 0), _num(br['x'], 0))
            # reaches on the whole line (case pu on baseMVA -> 100 MVA base used by the relays)
            zx = zx * 100.0 / float(case['baseMVA'])
            prot[a['id']] = dict(cfg, _zx_pu=zx, _ends=(1,), _inner=2)
            prot[b['id']] = dict(cfg, _zx_pu=zx, _ends=(2,), _inner=1)
    out = []
    for e in events:
        if e.type == 'Line fault':
            fid, a, b = split[e.element]
            out.append(Event('Bus fault', fid, e.start, duration=e.duration, value=e.value))
            if e.side != 'NONE':
                out += [Event('Trip branch', a, e.start + e.duration), Event('Trip branch', b, e.start + e.duration)]
        elif e.type == 'Trip branch' and e.element in split:
            _, a, b = split[e.element]
            if e.side in ('', 'ONE'):
                out.append(Event('Trip branch', a, e.start, side='ONE' if e.side else ''))
            if e.side in ('', 'TWO'):
                out.append(Event('Trip branch', b, e.start, side='TWO' if e.side else ''))
        else:
            out.append(e)
    return case, out, sections


def _event_mapping(events: list[Event], m: NetworkMap) -> dyn.EventMapping:
    em = dyn.EventMapping()
    for ev in events:
        if ev.type == 'Bus fault':
            x = ev.value if ev.value and ev.value > 0 else 1e-4
            em.add_node_fault(static_id=m.bus_iidm[str(ev.element)], start_time=ev.start,
                              fault_time=ev.duration, r_pu=0.0, x_pu=x)
        elif ev.type == 'Trip branch':
            iid = m.branch[ev.element]
            if ev.side in ('ONE', 'TWO'):
                em.add_disconnection(static_id=iid, start_time=ev.start, disconnect_only=ev.side)
            else:
                em.add_disconnection(static_id=iid, start_time=ev.start)
        elif ev.type == 'Trip generator':
            em.add_disconnection(static_id=m.gen[ev.element], start_time=ev.start)
        elif ev.type == 'Load step (ΔP)':
            em.add_active_power_variation(static_id=m.load[str(ev.element)], start_time=ev.start, delta_p=ev.value)
        elif ev.type == 'Load step (ΔQ)':
            em.add_reactive_power_variation(static_id=m.load[str(ev.element)], start_time=ev.start, delta_q=ev.value)
        elif ev.type == 'Generator ΔP setpoint':
            em.add_active_power_variation(static_id=m.gen[ev.element], start_time=ev.start, delta_p=ev.value)
        elif ev.type == 'AVR setpoint step (ΔU)':
            em.add_reference_voltage_variation(static_id=m.gen[ev.element], start_time=ev.start, delta_u=ev.value)
    return em


def validate_events(events: list[Event], case: dict, net_map: NetworkMap | None, stop: float) -> list[str]:
    buses = {str(b['id']) for b in case['buses']}
    branches = {br['id'] for br in case['branches'] if _num(br.get('status'), 1)}
    gens = {g['id'] for g in case['generators'] if _num(g.get('status'), 1)}
    loads = {str(b['id']) for b in case['buses'] if abs(_num(b['Pd'], 0)) + abs(_num(b['Qd'], 0)) > 0}
    lines = {br['id'] for br in case['branches'] if _num(br.get('status'), 1) and br.get('equipmentType') == 'line'}
    out = []
    at = {}
    for ev in events:
        if ev.type == 'Line fault':
            at.setdefault(ev.element, set()).add(round(float(ev.location), 2))
    out += [f'Line {k}: line faults on one line must use the same location (one fault point per run).'
            for k, v in at.items() if len(v) > 1]
    for k, ev in enumerate(events, 1):
        if not (0 <= ev.start < stop):
            out.append(f'Event {k}: start time must be between 0 and the stop time ({stop:g} s).')
        need = {'Bus fault': (buses, 'a bus'), 'Line fault': (lines, 'an in-service line'),
                'Trip branch': (branches, 'an in-service branch'),
                'Trip generator': (gens, 'an in-service generator'),
                'Load step (ΔP)': (loads, 'a bus with load'), 'Load step (ΔQ)': (loads, 'a bus with load'),
                'Generator ΔP setpoint': (gens, 'an in-service generator'),
                'AVR setpoint step (ΔU)': (gens, 'an in-service generator')}.get(ev.type)
        if need is None:
            out.append(f'Event {k}: unknown event type "{ev.type}".')
        elif str(ev.element) not in {str(x) for x in need[0]}:
            out.append(f'Event {k}: {ev.type} needs {need[1]} — "{ev.element}" isn\'t one.')
        if ev.type in ('Bus fault', 'Line fault') and not ev.duration > 0:
            out.append(f'Event {k}: a {ev.type.lower()} needs a positive duration.')
        if ev.type == 'Line fault' and not (0 < ev.location < 100):
            out.append(f'Event {k}: a line fault needs a location between 0 and 100 % of the line '
                       '(use a bus fault for a fault at a line end).')
    return out


# -------------------------------------------------------------- simulation

@dataclass
class DynResult:
    status: str
    status_text: str
    stable: bool | None = None
    reason: str = ''
    curves: pd.DataFrame = field(default_factory=pd.DataFrame)   # index: time (s)
    angles: pd.DataFrame = field(default_factory=pd.DataFrame)   # rotor angle vs COI (deg), per case gen id
    speed: pd.DataFrame = field(default_factory=pd.DataFrame)    # pu
    pgen: pd.DataFrame = field(default_factory=pd.DataFrame)     # MW
    qgen: pd.DataFrame = field(default_factory=pd.DataFrame)     # Mvar
    efd: pd.DataFrame = field(default_factory=pd.DataFrame)      # field voltage, pu (machine excitation base)
    pm: pd.DataFrame = field(default_factory=pd.DataFrame)       # mechanical power, pu of PNomTurb
    voltages: pd.DataFrame = field(default_factory=pd.DataFrame) # pu, per case bus id
    timeline: pd.DataFrame = field(default_factory=pd.DataFrame)
    metrics: dict = field(default_factory=dict)
    islanded_buses: list = field(default_factory=list)
    lost_gens: list = field(default_factory=list)
    protection: pd.DataFrame = field(default_factory=pd.DataFrame)   # protection operations
    notes: list = field(default_factory=list)

    @property
    def ok(self):
        return self.status == 'SUCCESS'


# extra provider parameters (e.g. additionalModelsFile for compiled custom models), set by custom_models.py
EXTRA_PROVIDER_PARAMS: dict = {}

SOLVERS = {'SIM': 'Simplified (fixed 10 ms step, robust through faults)',
           'IDA': 'IDA (variable step; may fail at faults with IEEE controllers)'}


def simulate(case: dict, events: list[Event], stop_time=10.0, load_alpha=2.0, load_beta=2.0,
             angle_limit=180.0, prepared: Prepared | None = None, record_buses: list | None = None,
             solver: str = 'SIM') -> DynResult:
    if not any(e.type == 'Line fault' for e in events):
        return _simulate(case, events, stop_time, load_alpha, load_beta, angle_limit, prepared, record_buses, solver)
    if prepared is not None:
        raise ValueError('line faults change the network: run them without a prepared model')
    case2, events2, sections = split_line_faults(case, events)
    if record_buses is not None:
        record_buses = list(record_buses) + [str(b['id']) for b in case2['buses'] if FAULT_SEP in str(b['id'])]
    r = _simulate(case2, events2, stop_time, load_alpha, load_beta, angle_limit, None, record_buses, solver)
    # report the protection of a split line against the line itself
    if len(r.protection):
        pr = r.protection.copy()
        sec = pr['id'].map(lambda i: sections.get(i, (None,))[0])
        hit = sec.notna() & (pr['kind'] == 'branch')
        pr.loc[hit, 'element'] = 'Line ' + sec[hit]
        pr.loc[hit, 'id'] = sec[hit]
        r.protection = pr
    r.notes = [n.replace(FAULT_SEP + 'a', '').replace(FAULT_SEP + 'b', '') for n in r.notes]
    return r


def _simulate(case, events, stop_time, load_alpha, load_beta, angle_limit, prepared, record_buses,
              solver) -> DynResult:
    own = prepared is None
    step_kind = {'Generator ΔP setpoint': 'P', 'AVR setpoint step (ΔU)': 'U'}
    steps = {}
    for e in events:
        if e.type in step_kind:
            steps.setdefault(str(e.element), {}).setdefault(step_kind[e.type], (e.start, e.value))
    try:
        p = prepared or prepare(case, load_alpha, load_beta, steps)
    except (RuntimeError, ValueError) as e:      # model compilation failed / a parameter without value
        return DynResult('FAILURE', str(e), False, 'The dynamic models could not be prepared: ' + str(e)[:300])
    try:
        if p.lf_status != 'CONVERGED':
            return DynResult('FAILURE', f'Initial load flow did not converge ({p.lf_status}).', False,
                             'No initial operating point')
        events, step_notes = _route_setpoint_steps(events, p, step_kind, own)
        m = p.net_map
        network = p.fresh_network()
        mm = dyn.ModelMapping()
        additional = {k: list(v) for k, v in ADDITIONAL_LIBS.items()}
        for gid, d in p.gen_params.items():
            if is_sync(d):
                mm.add_synchronous_generator(static_id=gid, parameter_set_id=f'GEN_{gid}', model_name=d['model'])
                continue
            src = SOURCE_LIB['sources'][d['source']]
            getattr(mm, _ADD_METHOD[src['category']])(static_id=gid, parameter_set_id=f'GEN_{gid}',
                                                       model_name=d['source'])
            if src.get('additional'):
                libs = additional.setdefault(src['category'], [])
                if all(e['lib'] != d['source'] for e in libs):
                    libs.append(_additional_entry(d['source']))
        for lid in network.get_loads().index:
            lm = p.load_models.get(lid, DEFAULT_LOAD_MODEL)
            mm.add_base_load(static_id=lid, parameter_set_id=p.load_sets.get(lid, f'LOAD_{lm}'), model_name=lm)
        prot = p.protected or {}
        for iid in p.line_models:
            mm.add_base_line(static_id=iid, parameter_set_id=f'LINE_{iid}', model_name='GsLineProtected')
        for iid in p.tfo_models:
            mm.add_base_transformer(static_id=iid, parameter_set_id=f'TFO_{iid}', model_name='GsNetworkTransformer')
        if p.line_models:              # all-Modelica branches need Modelica buses
            for bid in network.get_bus_breaker_view_buses().index:
                mm.add_base_bus(static_id=bid, parameter_set_id='BUS', model_name='Bus')
        for iid in ([] if p.tfo_models else prot.get('transformers', {})):
            mm.add_overload_management_system(dynamic_model_id=f'OC_{iid}', parameter_set_id=f'OMS_{iid}',
                                              controlled_branch=iid, i_measurement=iid,
                                              i_measurement_side='ONE', model_name='OverloadManagementSystem')

        vm = dyn.OutputVariableMapping()
        for gid, d in p.gen_params.items():
            if is_sync(d):
                vm.add_curves(gid, ['generator_theta', 'generator_omegaPu', 'generator_PGen', 'generator_QGen',
                                    'generator_efdPu', 'generator_PmPu'])
            else:
                src = SOURCE_LIB['sources'][d['source']]
                vm.add_curves(gid, [v for v in (src.get('p_var'), src.get('q_var')) if v])
        for gid in prot.get('generators', {}):
            vm.add_curves(gid, ['protection_tripCause'])
        for iid in prot.get('lines', {}):
            vm.add_curves(iid, ['line_tripCause'])
        for iid in prot.get('transformers', {}):
            if p.tfo_models:
                vm.add_curves(iid, ['transformer_tripCause'])
            else:
                vm.add_curves(f'OC_{iid}', ['currentLimitAutomaton_order_value'])
        for lid in prot.get('loads', {}):
            vm.add_curves(lid, ['load_shed'])
        buses = record_buses if record_buses is not None else [str(b['id']) for b in case['buses']]
        bus_var = 'bus_UPu' if p.line_models else 'Upu_value'      # Modelica bus model vs network bus
        for bid in buses:
            vm.add_curves(m.bus_iidm[str(bid)], [bus_var])
        for bid in buses:
            vm.add_final_state_values(m.bus_iidm[str(bid)], bus_var)

        params = dyn.Parameters(start_time=0.0, stop_time=float(stop_time), provider_parameters={
            'parametersFile': str(p.work_dir / 'models.par'),
            'network.parametersFile': str(PAR_DIR / 'network.par'),
            'network.parametersId': 'Network',
            'solver.parametersFile': str(PAR_DIR / 'solvers.par'),
            'solver.parametersId': solver,
            'solver.type': solver,
            **EXTRA_PROVIDER_PARAMS,
        })
        if additional:
            f = p.work_dir / 'additional_models.json'
            f.write_text(json.dumps({k: {'libs': v} for k, v in additional.items()}))
            params.provider_parameters['additionalModelsFile'] = str(f)
        report = pp.report.ReportNode()
        try:
            res = dyn.Simulation().run(network, mm, _event_mapping(events, m), vm, params, report_node=report)
        except Exception as e:  # noqa: BLE001 — pypowsybl raises when Dynawo stops before writing 2 samples
            msg = str(e).split(':', 1)[-1].strip()
            detail = _report_errors(report)
            return DynResult('FAILURE', f'Dynawo stopped at initialisation ({msg})' + detail, False,
                             'The model could not be initialised from this operating point — check the '
                             'machine and control parameters.' + detail, notes=list(p.notes))
        out = _post_process(res, p, case, events, stop_time, angle_limit)
        if not out.ok or not len(out.curves):
            detail = _report_errors(report)
            out.status_text = (out.status_text or '') + detail
            if not len(out.curves):
                out.reason = 'Simulation produced no output' + (f' — Dynawo: {out.status_text.strip()}'
                                                                 if out.status_text.strip() else '')
        out.notes = list(p.notes) + step_notes + _report_skipped(report)
        return out
    finally:
        if own:
            shutil.rmtree(p.work_dir, ignore_errors=True)


def _route_setpoint_steps(events, p: Prepared, step_kind: dict, own: bool):
    """Set-point steps reach a composed machine through its GsSetpoints parameters (written by prepare), a
    pypowsybl 'controllable' library machine through pypowsybl's event; any other model can't take them.
    Returns the events for pypowsybl and notes on steps that were applied differently or not at all."""
    from . import custom_models as CM
    out, notes, seen = [], [], set()
    label = {'P': 'Generator ΔP setpoint', 'U': 'AVR setpoint step'}
    for e in events:
        kind = step_kind.get(e.type)
        model = p.gen_models.get(str(e.element), '')
        if kind is None or not is_sync_model(model):
            out.append(e)          # converters: pypowsybl decides (a skipped event is reported from its log)
            continue
        sup = CM.setpoint_support(model)
        if not sup[kind] and sup['via'] == 'pypowsybl':
            notes.append(f'{label[kind]} on {e.element} not applied: pypowsybl sends set-point steps only to '
                         f'Dynawo\'s proportional-regulation machines, not to {model}. Compose the same governor/AVR '
                         'in the controls editor to use set-point steps.')
        elif not sup[kind]:
            notes.append(f'{label[kind]} on {e.element} not applied: the governor of {model} has a power reference '
                         'with its own base and initial value, which a step can\'t be added to.' if kind == 'P' else
                         f'{label[kind]} on {e.element} not applied: {model} has no AVR.')
        elif sup['via'] == 'pypowsybl':
            out.append(e)
        elif not own:
            notes.append(f'{label[kind]} on {e.element} not applied: set-point steps need a fresh model '
                         '(not available in clearing-time or screening studies).')
        elif (e.element, kind) in seen:
            notes.append(f'{label[kind]} on {e.element}: only the first step of each kind per generator is applied '
                         'to a composed machine model.')
        else:
            seen.add((e.element, kind))
    return out, notes


def is_sync_model(model: str) -> bool:
    return model.startswith('GsSync') or model.startswith('GeneratorSynchronous')


def _post_process(res, p: Prepared, case, events, stop_time, angle_limit) -> DynResult:
    status, text = res.status().name, res.status_text()
    curves = res.curves()
    timeline = res.timeline()
    if len(curves):
        epoch = pd.Timestamp(0, tz=curves.index.tz)
        curves.index = (curves.index - epoch).total_seconds()
        curves.index.name = 'time (s)'
        # Dynawo writes two samples at an event instant (before / after the jump). Keep both, nudging
        # the first one back by 1 µs, so plots show a step instead of a ramp from the previous sample.
        t = curves.index.to_numpy(dtype=float).copy()
        dup = np.r_[t[:-1] == t[1:], False]
        t[dup] -= 1e-6
        curves.index = pd.Index(t, name='time (s)')
        curves = curves[~curves.index.duplicated(keep='last')]
    out = DynResult(status, text, curves=curves, timeline=timeline)
    if not len(curves):
        out.stable, out.reason = False, 'Simulation produced no output'
        return out

    def pick(var):
        cols = {gid: f'{gid}_{var}' for gid in p.gen_params}
        df = pd.DataFrame({p.gen_case_id[g]: curves[c] for g, c in cols.items() if c in curves})
        return df

    theta = pick('generator_theta')
    out.speed = pick('generator_omegaPu')
    out.pgen = pick('generator_PGen')
    out.qgen = pick('generator_QGen')
    out.efd = pick('generator_efdPu')
    out.pm = pick('generator_PmPu')
    for gid, d in p.gen_params.items():      # converters & simplified models: P/Q in pu of 100 MVA
        if is_sync(d):
            continue
        src = SOURCE_LIB['sources'][d['source']]
        for attr, var in (('pgen', src.get('p_var')), ('qgen', src.get('q_var'))):
            col = f'{gid}_{var}'
            if var and col in curves:
                getattr(out, attr)[p.gen_case_id[gid]] = curves[col] * 100.0
    # what the protections did; their trips count like switching events for islanding and the metrics
    prot = p.protected or {}
    out.protection = PR.operations(curves, prot.get('generators', {}), prot.get('lines', {}),
                                   prot.get('transformers', {}), prot.get('loads', {}))
    trips = [Event('Trip generator' if r.kind == 'generator' else 'Trip branch', r.id, r.time)
             for r in out.protection.itertuples() if r.kind in ('generator', 'branch')]
    # machines that end up tripped or islanded are left out of the stability metrics
    dead_buses, lost_gens = islanding(case, list(events) + trips)
    dead_buses = [b for b in dead_buses if FAULT_SEP not in b]       # line-fault points: not real buses
    out.islanded_buses, out.lost_gens = dead_buses, lost_gens
    keep = [g for g in theta.columns if g not in lost_gens] or list(theta.columns)
    # rotor angles relative to the inertia-weighted centre of angle of the machines still connected
    w = pd.Series({p.gen_case_id[g]: d['H'] * d['SNom'] for g, d in p.sync_gens.items()}, dtype=float)[keep]
    coi = (theta[keep] * w).sum(axis=1) / w.sum()
    out.angles = np.rad2deg(theta.sub(coi, axis=0))
    rev_bus = p.net_map.bus_rev
    vcols = {c: rev_bus.get(c.removeprefix('NETWORK_').removesuffix('_Upu_value').removesuffix('_bus_UPu'))
             for c in curves.columns if c.endswith(('_Upu_value', '_bus_UPu'))}
    out.voltages = curves[list(vcols)].rename(columns=vcols)
    out.metrics, out.stable, out.reason = _stability_metrics(out, events, stop_time, angle_limit, case, keep,
                                                             [b for b in out.voltages.columns
                                                              if b not in dead_buses and FAULT_SEP not in str(b)])
    if lost_gens or dead_buses:
        note = []
        if dead_buses:
            note.append(f'bus(es) {", ".join(dead_buses)} islanded')
        if lost_gens:
            note.append(f'generator(s) {", ".join(lost_gens)} disconnected (excluded from the angle check)')
        out.reason = out.reason + ' — ' + '; '.join(note) + '.'
    return out


def islanding(case: dict, events: list) -> tuple[list, list]:
    """Buses cut off from the main island by the branch trips in `events`, and generators lost
    (tripped directly or sitting on an islanded bus). Main island = the one with most buses."""
    opened = {e.element for e in events if e.type == 'Trip branch'}
    tripped = {str(e.element) for e in events if e.type == 'Trip generator'}
    nbrs = {str(b['id']): [] for b in case['buses']}
    for br in case['branches']:
        if _num(br.get('status'), 1) and br['id'] not in opened:
            f, t = str(br['from']), str(br['to'])
            if f in nbrs and t in nbrs:
                nbrs[f].append(t)
                nbrs[t].append(f)
    seen, comps = set(), []
    for start in nbrs:
        if start in seen:
            continue
        comp, stack = {start}, [start]
        seen.add(start)
        while stack:
            u = stack.pop()
            for v in nbrs[u]:
                if v not in seen:
                    seen.add(v)
                    comp.add(v)
                    stack.append(v)
        comps.append(comp)
    main = max(comps, key=len)
    dead = sorted((b for b in nbrs if b not in main), key=lambda x: (len(x), x))
    lost = [str(g['id']) for g in case['generators'] if _num(g.get('status'), 1)
            and (str(g['id']) in tripped or str(g['bus']) in dead)]
    return dead, lost


def _stability_metrics(r: DynResult, events, stop_time, angle_limit, case, gens=None, buses=None):
    t = r.angles.index.values
    ang = r.angles[gens] if gens else r.angles
    sep = (ang.max(axis=1) - ang.min(axis=1)).dropna()
    if sep.empty and len(r.voltages) and r.angles.shape[1] == 0:
        # no synchronous machine in service: judge on completion and post-disturbance voltages
        t = r.voltages.index.values
        sep = pd.Series(0.0, index=r.voltages.index)
    if sep.empty:
        return dict(max_angle_sep=float('nan'), TSI=float('nan'), end_time=float(t[-1]) if len(t) else 0.0), False, \
            f'Simulation produced no valid rotor-angle output: {r.status_text or r.status}'
    max_sep = float(sep.max())
    t_max = float(sep.idxmax())
    first = min((e.start for e in events), default=0.0)
    last_clear = max((e.start + (e.duration if e.type == 'Bus fault' else 0) for e in events), default=0.0)
    vv = r.voltages[buses] if buses is not None and len(r.voltages) else r.voltages
    # the recorded buses can all be islanded (e.g. a CCT run records only the faulted bus, and its relays
    # may isolate it): then there is no post-disturbance voltage to report
    post = vv[vv.index > last_clear + 0.05] if len(vv) else vv
    pmin = post.min().dropna() if len(post) and post.shape[1] else pd.Series(dtype=float)
    vmin_post = float(pmin.min()) if len(pmin) else float('nan')
    vmin_bus = str(pmin.idxmin()) if len(pmin) else ''
    f0 = PR.f_nom(case)
    sp = r.speed[[g for g in gens if g in r.speed.columns]] if gens and len(r.speed) else r.speed
    fmin = float(sp.min().min() * f0) if len(sp) and sp.shape[1] else float('nan')
    fmax = float(sp.max().max() * f0) if len(sp) and sp.shape[1] else float('nan')
    tsi = 100 * (360 - max_sep) / (360 + max_sep)
    # damping of the angle-separation swing after the last disturbance (log decrement of peaks)
    zeta = float('nan')
    s = sep[sep.index > last_clear]
    if len(s) > 10:
        sv = s.values - s.values[-1]
        peaks = [i for i in range(1, len(sv) - 1) if sv[i] > sv[i - 1] and sv[i] >= sv[i + 1] and sv[i] > 0.5]
        if len(peaks) >= 2 and sv[peaks[0]] > sv[peaks[1]] > 0:
            dlt = math.log(sv[peaks[0]] / sv[peaks[1]])
            zeta = dlt / math.sqrt(4 * math.pi ** 2 + dlt ** 2)
    finished = r.ok and float(t[-1]) >= stop_time - 1e-6
    if max_sep > angle_limit:
        stable, reason = False, f'Loss of synchronism: rotor-angle separation reached {max_sep:.0f}° (> {angle_limit:.0f}°) at t = {t_max:.2f} s'
    elif not finished:
        stable, reason = False, f'Simulation stopped at t = {float(t[-1]):.2f} s: {r.status_text or r.status}'
    else:
        stable, reason = True, f'Stable: max rotor-angle separation {max_sep:.0f}°, TSI {tsi:.0f}%'
    metrics = dict(max_angle_sep=max_sep, t_max_sep=t_max, TSI=tsi, min_post_voltage=vmin_post,
                   min_post_voltage_bus=vmin_bus, f_min=fmin, f_max=fmax, damping=zeta,
                   end_time=float(t[-1]), first_event=first)
    return metrics, stable, reason


# --------------------------------------------------------------- studies

def critical_clearing_time(case: dict, fault_bus: str, trip_branch: str | None = None, t_fault=1.0,
                           t_low=0.02, t_high=0.5, tol=0.01, post_time=4.0, load_alpha=2.0, load_beta=2.0,
                           angle_limit=180.0, progress: Callable | None = None, solver: str = 'SIM') -> dict:
    """Bisection on the clearing time of a bolted 3-phase fault at fault_bus, optionally cleared by
    tripping trip_branch at the same instant. Returns CCT bracket and every run."""
    p = prepare(case, load_alpha, load_beta)
    runs = []

    def run(tc):
        evs = [Event('Bus fault', fault_bus, t_fault, duration=tc)]
        if trip_branch:
            evs.append(Event('Trip branch', trip_branch, t_fault + tc))
        r = simulate(case, evs, t_fault + tc + post_time, load_alpha, load_beta, angle_limit, prepared=p,
                     record_buses=[fault_bus], solver=solver)
        runs.append(dict(clearing_time=tc, stable=r.stable, max_angle_sep=r.metrics.get('max_angle_sep'),
                         reason=r.reason))
        if progress:
            progress(len(runs), tc, r.stable)
        return r

    try:
        lo, hi = run(t_low), run(t_high)
        if not lo.stable:
            return dict(cct=None, low=None, high=t_low, runs=runs, network_map=p.net_map,
                        message=f'Unstable even when cleared in {t_low * 1000:.0f} ms.')
        if hi.stable:
            return dict(cct=None, low=t_high, high=None, runs=runs, network_map=p.net_map,
                        message=f'Stable even when cleared in {t_high * 1000:.0f} ms: CCT > {t_high:.2f} s.')
        a, b = t_low, t_high
        while b - a > tol:
            mid = round((a + b) / 2, 4)
            if run(mid).stable:
                a = mid
            else:
                b = mid
        return dict(cct=a, low=a, high=b, runs=runs, network_map=p.net_map,
                    message=f'Critical clearing time between {a * 1000:.0f} and {b * 1000:.0f} ms.')
    finally:
        shutil.rmtree(p.work_dir, ignore_errors=True)


def screen_contingencies(case: dict, branch_ids: list[str], clearing_time=0.1, fault_end='from', t_fault=1.0,
                         post_time=5.0, load_alpha=2.0, load_beta=2.0, angle_limit=180.0,
                         progress: Callable | None = None, solver: str = 'SIM') -> pd.DataFrame:
    """Dynamic N-1: for each branch, a bolted 3-phase fault at one end, cleared after clearing_time
    by tripping the branch."""
    p = prepare(case, load_alpha, load_beta)
    rows = []
    try:
        brs = {br['id']: br for br in case['branches']}
        for k, bid in enumerate(branch_ids, 1):
            br = brs[bid]
            bus = str(br['from'] if fault_end == 'from' else br['to'])
            evs = [Event('Bus fault', bus, t_fault, duration=clearing_time), Event('Trip branch', bid, t_fault + clearing_time)]
            r = simulate(case, evs, t_fault + clearing_time + post_time, load_alpha, load_beta, angle_limit,
                         prepared=p, solver=solver)
            mt = r.metrics
            rows.append(dict(branch=bid, fault_bus=bus, verdict='Stable' if r.stable else 'Unstable',
                             max_angle_sep=mt.get('max_angle_sep', float('nan')), TSI=mt.get('TSI', float('nan')),
                             min_post_voltage=mt.get('min_post_voltage', float('nan')),
                             min_v_bus=mt.get('min_post_voltage_bus', ''),
                             f_min=mt.get('f_min', float('nan')), damping=mt.get('damping', float('nan')),
                             islanded=', '.join(r.islanded_buses), lost_generators=', '.join(r.lost_gens),
                             detail=r.reason))
            if progress:
                progress(k, len(branch_ids), bid, r.stable)
    finally:
        shutil.rmtree(p.work_dir, ignore_errors=True)
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values(['verdict', 'TSI'], ascending=[False, True]).reset_index(drop=True)
    return df
