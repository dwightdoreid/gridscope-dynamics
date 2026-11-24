"""
Free composition of synchronous-machine controls: any governor x any AVR x any PSS (or none).

Dynawo runs pre-assembled models: one compiled library per machine + controls combination. It ships
~240 of them. For any other combination this module

  1. writes the pre-assembled model definition (machine + controller units + connections), using the
     wiring catalogue in controller_library.json (harvested from Dynawo's own definitions);
  2. compiles it once with Dynawo's own tool chain (generate-preassembled, then dumpModel for the
     parameter description) into a cache directory (vendor/gridscope_cache in the project by default);
  3. registers the cache with pypowsybl: a merged model directory (DYNAWO_DDB_DIR) plus the
     `additionalModelsFile` provider parameter that tells pypowsybl the new model names.

Compilation needs a C/C++ compiler and CMake (Debian/Ubuntu: sudo apt install build-essential cmake).
The Dynawo install itself is never modified: compilation runs against a symbolic-link copy of it in
the cache, where one Boost header is patched for glibc >= 2.34 (PTHREAD_STACK_MIN is no longer a
constant there, which breaks the Boost version bundled with Dynawo 1.7.0).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import dynamics as D

CACHE = Path(os.environ.get('GRIDSCOPE_DYNAWO_CACHE',
                           Path(__file__).resolve().parent.parent / 'vendor' / 'gridscope_cache'))
GEN_CLASS = 'Dynawo.Electrical.Machines.OmegaRef.GeneratorSynchronous'
GEN_INIT = {'four': 'Dynawo.Electrical.Machines.OmegaRef.GeneratorSynchronousExt4W_INIT',
            'three': 'Dynawo.Electrical.Machines.OmegaRef.GeneratorSynchronousExt3W_INIT'}
UNIT_ID = {'governor': 'governor', 'avr': 'voltageRegulator', 'pss': 'powerSystemStabilizer'}
# GridScope's own Modelica classes (gridscope/modelica), compiled with Dynawo's library
MODELICA_DIR = Path(__file__).resolve().parent / 'modelica'
GEN_PROTECTION = 'Dynawo.Electrical.Controls.Machines.Protections.GsGeneratorProtection'
GEN_SETPOINTS = 'Dynawo.Electrical.Controls.Machines.GsSetpoints'
GOV_INIT = 'Dynawo.Electrical.Controls.Machines.Governors.Governor_INIT'
# protected line and load: single-unit models whose unit ids ('line', 'load') match pypowsybl's wiring
FIXED_MODELS = {
    'GsLineProtected': dict(category='BASE_LINE', doc='Line with distance and overcurrent protection (GridScope)',
                            xml='  <dyn:modelicaModel id="GsLineProtected">\n'
                                '    <dyn:unitDynamicModel id="line" name="Dynawo.Electrical.Lines.GsLineProtected"/>\n'
                                '  </dyn:modelicaModel>'),
    'GsNetworkTransformer': dict(category='TRANSFORMER', doc='Network transformer with branch state (GridScope)',
                                 xml='  <dyn:modelicaModel id="GsNetworkTransformer">\n'
                                     '    <dyn:unitDynamicModel id="transformer" '
                                     'name="Dynawo.Electrical.Transformers.GsNetworkTransformer" '
                                     'initName="Dynawo.Electrical.Transformers.NetworkTransformer_INIT"/>\n'
                                     '  </dyn:modelicaModel>'),
    'GsLoadShedding': dict(category='BASE_LOAD', properties=['SYNCHRONIZED'],
                           doc='Alpha-beta load with UFLS and UVLS (GridScope)',
                           xml='  <dyn:modelicaModel id="GsLoadShedding">\n'
                               '    <dyn:unitDynamicModel id="load" name="Dynawo.Electrical.Loads.GsLoadShedding" '
                               'initName="Dynawo.Electrical.Loads.Load_INIT"/>\n'
                               '  </dyn:modelicaModel>'),
    # same relays, frequency measured from the bus voltage angle (all-Modelica network: no OmegaRef)
    'GsLoadSheddingLocal': dict(category='BASE_LOAD', doc='Alpha-beta load with UFLS and UVLS, local frequency (GridScope)',
                                xml='  <dyn:modelicaModel id="GsLoadSheddingLocal">\n'
                                    '    <dyn:unitDynamicModel id="load" '
                                    'name="Dynawo.Electrical.Loads.GsLoadSheddingLocal" '
                                    'initName="Dynawo.Electrical.Loads.Load_INIT"/>\n'
                                    '  </dyn:modelicaModel>'),
}
NONE_LABEL = {'governor': 'None (constant mechanical power)', 'avr': 'None (constant field voltage)', 'pss': 'None'}
_lock = threading.Lock()


# ------------------------------------------------------------------ catalogue
_VER_FILE = D.PAR_DIR / 'composition_verification.json'
VERIFICATION = json.loads(_VER_FILE.read_text()) if _VER_FILE.exists() else {}


def class_status(cls: str) -> dict:
    """Verification record of a controller class in a representative composed model (or {})."""
    return VERIFICATION.get(cls, {})


def composable(role: str, include_unverified: bool = True) -> dict:
    """{label: class} of the controller classes of `role` that can be freely combined."""
    out = {}
    for cls, c in D.CONTROL_LIB['components'].items():
        if c['role'] == role and 'connect' in c and not c.get('unresolved'):
            if include_unverified or class_status(cls).get('verified', True):
                out[c['label']] = cls
    return dict(sorted(out.items(), key=lambda kv: (not kv[0].startswith('Proportional'), kv[0].lower())))


def class_of(role: str, label: str) -> str | None:
    return composable(role).get(label)


def _short(cls):
    return cls.split('.')[-1] if cls else 'None'


def model_id(machine: str, gov: str | None, avr: str | None, pss: str | None, protection: bool = False) -> str:
    w = '4W' if machine.startswith('Round') else '3W'
    return f'GsSync{w}_{_short(gov)}_{_short(avr)}_{_short(pss)}' + ('_Prot' if protection else '')


def describe(machine, gov, avr, pss, protection: bool = False) -> dict:
    """Library entry for a composed model (same shape as the pre-assembled ones)."""
    units = {}
    for role, cls in (('governor', gov), ('avr', avr), ('pss', pss)):
        if cls:
            units[UNIT_ID[role]] = cls
    lab = lambda role, cls: D.component(cls)['label'] if cls else NONE_LABEL[role]  # noqa: E731
    return dict(machine=machine, governor=lab('governor', gov), avr=lab('avr', avr), pss=lab('pss', pss),
                limiter='None', units=units, custom=True, verified=False, protection=protection,
                description='Composed by GridScope from Dynawo controller classes'
                            + (' with the generator protection relay' if protection else ''))


def with_protection(model: str) -> str:
    """Name of the same machine + controls with the generator protection relay added (registered)."""
    if model.endswith('_Prot'):
        return model
    info = D.CONTROL_LIB['models'][model]
    if info.get('limiter', 'None') != 'None':
        raise ValueError(f'{model} includes a limiter that GridScope cannot recompose; choose another combination '
                         'to add generator protection.')
    by_role = {D.component(cls)['role']: cls for cls in info.get('units', {}).values()}
    gov, avr, pss = by_role.get('governor'), by_role.get('avr'), by_role.get('pss')
    name = model_id(info['machine'], gov, avr, pss, True)
    register_entry(name, describe(info['machine'], gov, avr, pss, True))
    return name


def remap_controls(old_model: str, new_model: str, controls: dict) -> dict:
    """Carry parameter overrides over to another model with the same controller classes
    (unit ids can differ, e.g. 'pss' vs 'powerSystemStabilizer')."""
    old_units = D.CONTROL_LIB['models'][old_model].get('units', {})
    new_units = {cls: u for u, cls in D.CONTROL_LIB['models'][new_model].get('units', {}).items()}
    out = {}
    for k, v in (controls or {}).items():
        unit, _, par = k.partition('_')
        cls = old_units.get(unit)
        if cls in new_units:
            out[f'{new_units[cls]}_{par}'] = v
    return out


def without_protection(model: str) -> str:
    return model[:-5] if model.endswith('_Prot') else model


def entry_from_name(name: str) -> dict | None:
    """Rebuild (and register) the library entry of a composed model from its name."""
    m = re.fullmatch(r'GsSync(4W|3W)_([A-Za-z0-9]+)_([A-Za-z0-9]+)_([A-Za-z0-9]+)(_Prot)?', name)
    if not m:
        return None
    by_short = {(c['role'], _short(k)): k for k, c in D.CONTROL_LIB['components'].items() if 'connect' in c}
    cls = []
    for role, short in zip(('governor', 'avr', 'pss'), m.groups()[1:4]):
        if short == 'None':
            cls.append(None)
        elif (role, short) in by_short:
            cls.append(by_short[(role, short)])
        else:
            return None
    machine = 'Round rotor (4 windings)' if m.group(1) == '4W' else 'Salient pole (3 windings)'
    entry = describe(machine, *cls, protection=bool(m.group(5)))
    register_entry(name, entry)
    return entry


def register_entry(name: str, entry: dict):
    D.CONTROL_LIB['models'].setdefault(name, entry)


def resolve(machine: str, gov_label: str, avr_label: str, pss_label: str) -> tuple[str, bool]:
    """Model name for a (machine, governor, AVR, PSS) choice by label: (name, is_custom).
    A Dynawo pre-assembled model is used when one exists and passed verification (some fail to start in
    Dynawo 1.7, e.g. TGov1 + SEXS without a PSS); otherwise the controls are composed."""
    pre = D.find_model(machine, gov_label, avr_label, pss_label)
    if pre and not D.CONTROL_LIB['models'][pre].get('custom') and D.CONTROL_LIB['models'][pre].get('verified'):
        return pre, False
    gov, avr, pss = (class_of(r, l) for r, l in (('governor', gov_label), ('avr', avr_label), ('pss', pss_label)))
    if pss and not avr:
        raise ValueError('A PSS needs a voltage regulator to act on.')
    name = model_id(machine, gov, avr, pss)
    register_entry(name, describe(machine, gov, avr, pss))
    return name, True


# ------------------------------------------------------------------ XML
# governors whose power reference has another base or initial value (no 'Generator ΔP setpoint' step)
NO_PMREF = {'GovCt2', 'GovSteamEu', 'GovHydro4', 'GoverNordic',
            'IEEEG1', 'GovSteam1', 'HyGov'}          # these compute their initial reference internally
# governors whose reference input starts at (parameter × Pm0Pu): the step is scaled the same way
PMREF_SCALE = {'TGov1': 'R'}


def setpoint_support(model: str) -> dict:
    """Which set-point steps a generator model accepts: {'P': bool, 'U': bool, 'via': 'model'|'pypowsybl'}."""
    if model.startswith('GsSync'):
        units = D.model_units(model)
        gov, avr = units.get('governor'), units.get('voltageRegulator')
        return dict(P=bool(gov) and gov.rsplit('.', 1)[-1] not in NO_PMREF, U=bool(avr), via='model')
    ctl = model in CONTROLLABLE_LIBS
    return dict(P=ctl, U=ctl, via='pypowsybl')


# Dynawo library machines pypowsybl 1.16 declares CONTROLLABLE (the only ones its set-point events reach)
CONTROLLABLE_LIBS = {'GeneratorSynchronousFourWindingsGoverPropVRPropInt',
                     'GeneratorSynchronousFourWindingsProportionalRegulations',
                     'GeneratorSynchronousThreeWindingsGoverPropVRPropInt',
                     'GeneratorSynchronousThreeWindingsProportionalRegulations'}


def model_xml(name: str) -> str:
    info = D.CONTROL_LIB['models'][name]
    w = 'four' if info['machine'].startswith('Round') else 'three'
    lines = [f'  <dyn:modelicaModel id="{name}">',
             f'    <dyn:unitDynamicModel id="generator" name="{GEN_CLASS}" initName="{GEN_INIT[w]}"/>']
    conns = []
    for unit, cls in info['units'].items():
        c = D.component(cls)
        init = f' initName="{c["init"]}"' if c.get('init') else ''
        lines.append(f'    <dyn:unitDynamicModel id="{unit}" name="{cls}"{init}/>')
        for uv, gv in c.get('initConnect', []):
            conns.append(f'    <dyn:initConnect id1="{unit}" var1="{uv}" id2="generator" var2="{gv}"/>')
    # set-point steps (see GsSetpoints.mo): power reference into the governor, voltage step where the AVR
    # measures the stator voltage
    lines.append(f'    <dyn:unitDynamicModel id="setpoints" name="{GEN_SETPOINTS}" initName="{GOV_INIT}"/>')
    conns.append('    <dyn:initConnect id1="setpoints" var1="Pm0Pu" id2="generator" var2="Pm0Pu"/>')
    for unit, cls in info['units'].items():
        role = D.component(cls)['role']
        for uv, gv in D.component(cls).get('connect', []):
            if role == 'avr' and gv == 'UStatorPu.value':
                conns.append('    <dyn:connect id1="setpoints" var1="UsPu" id2="generator" var2="UStatorPu.value"/>')
                conns.append(f'    <dyn:connect id1="{unit}" var1="{uv}" id2="setpoints" var2="UsMeasPu"/>')
                continue
            conns.append(f'    <dyn:connect id1="{unit}" var1="{uv}" id2="generator" var2="{gv}"/>')
        if role == 'governor' and cls.rsplit('.', 1)[-1] not in NO_PMREF:
            conns.append(f'    <dyn:connect id1="{unit}" var1="PmRefPu" id2="setpoints" var2="PmRefPu"/>')
    if info.get('protection'):
        lines.append(f'    <dyn:unitDynamicModel id="protection" name="{GEN_PROTECTION}"/>')
        conns += ['    <dyn:connect id1="protection" var1="omegaMonitoredPu" id2="generator" var2="omegaPu.value"/>',
                  '    <dyn:connect id1="protection" var1="UMonitoredPu" id2="generator" var2="UPu"/>',
                  '    <dyn:connect id1="protection" var1="thetaMonitored" id2="generator" var2="theta"/>',
                  '    <dyn:connect id1="protection" var1="switchOffSignal" id2="generator" var2="switchOffSignal3"/>']
    avr, pss = info['units'].get('voltageRegulator'), info['units'].get('powerSystemStabilizer')
    if avr and pss:
        pin, pout = D.component(avr).get('pssIn'), D.component(pss).get('pssOut')
        if not pin:
            raise ValueError(f'{D.component(avr)["label"]} has no PSS input in Dynawo; choose PSS "None".')
        conns.append(f'    <dyn:connect id1="voltageRegulator" var1="{pin}" id2="powerSystemStabilizer" var2="{pout}"/>')
    lines += conns + ['  </dyn:modelicaModel>']
    return '\n'.join(lines)


def models_xml(names) -> str:
    body = [FIXED_MODELS[n]['xml'] if n in FIXED_MODELS else model_xml(n) for n in names]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<dyn:dynamicModelsArchitecture xmlns:dyn="http://www.rte-france.com/dynawo">\n'
            + '\n'.join(body) + '\n</dyn:dynamicModelsArchitecture>\n')


# ------------------------------------------------------------------ tool chain
def dynawo_home() -> Path | None:
    ddb = D._dynawo_ddb()
    return ddb.parent if ddb else None


def toolchain_status(home: Path | None = None) -> dict:
    """{'ok': bool, 'issues': [...]} — what is needed to compile new combinations."""
    home = Path(home) if home else dynawo_home()
    issues = []
    if home is None or not (home / 'sbin' / 'generate-preassembled').exists():
        issues.append('Dynawo install not found (homeDir in ~/.itools/config.yml) or it lacks sbin/generate-preassembled.')
    for tool, pkg in (('cmake', 'cmake'), ('c++', 'build-essential'), ('gcc', 'build-essential'), ('make', 'build-essential')):
        if shutil.which(tool) is None:
            issues.append(f'"{tool}" not found — install it (Debian/Ubuntu: sudo apt install {pkg}).')
    return dict(ok=not issues, issues=issues, home=str(home) if home else None)


def _shadow(home: Path) -> Path:
    """Symbolic-link copy of the Dynawo install with the glibc-incompatible Boost header patched."""
    sh = CACHE / 'dynawo_shadow'
    stamp = sh / '.source'
    if not (stamp.exists() and stamp.read_text() == str(home.resolve())):
        if sh.exists():
            shutil.rmtree(sh)
        sh.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['cp', '-as', str(home.resolve()), str(sh)], check=True)
        hdr = sh / 'include/boost/thread/pthread/thread_data.hpp'
        if hdr.exists():
            txt = hdr.read_text()
            hdr.unlink()                                       # replace the link by a patched real file
            hdr.write_text(txt.replace('#if PTHREAD_STACK_MIN > 0', '#ifdef PTHREAD_STACK_MIN'))
        stamp.write_text(str(home.resolve()))
    _install_modelica(sh)
    return sh


def _install_modelica(sh: Path):
    """Put GridScope's Modelica classes into the shadow copy of Dynawo's model library.

    OpenModelica resolves a package from the real path of its package.mo, so the package files of the
    shadow library are made real copies; the class files themselves stay links."""
    marker = sh / '.packages_copied'
    if not marker.exists():
        for f in (sh / 'ddb').rglob('package.*'):
            if f.is_symlink():
                txt = f.read_bytes()
                f.unlink()
                f.write_bytes(txt)
        marker.write_text('1')
    for mo in sorted(MODELICA_DIR.glob('*.mo')):
        txt = mo.read_text()
        pkg = re.search(r'^within\s+([\w.]+)\s*;', txt, re.M).group(1)
        folder = sh / 'ddb' / pkg.replace('.', '/')
        dest = folder / mo.name
        if dest.is_symlink() or (dest.exists() and dest.read_text() != txt):
            dest.unlink()
        if not dest.exists():
            dest.write_text(txt)
        ev = mo.with_suffix('.extvar')
        if ev.exists():
            evd = folder / ev.name
            if evd.is_symlink() or (evd.exists() and evd.read_text() != ev.read_text()):
                evd.unlink()
            if not evd.exists():
                evd.write_text(ev.read_text())
        order = folder / 'package.order'
        names = order.read_text().split() if order.exists() else []
        if mo.stem not in names:
            names.append(mo.stem)
            content = '\n'.join(names) + '\n'
            if order.is_symlink():
                order.unlink()
            order.write_text(content)


def _env(sh: Path) -> dict:
    env = dict(os.environ)
    omc = sh / 'OpenModelica'
    ver = subprocess.run([str(omc / 'bin' / 'omcDynawo'), '--version'], capture_output=True, text=True).stdout
    ver = ver.strip().split('v', 1)[-1].replace('.', '_')
    env.update(DYNAWO_INSTALL_DIR=str(sh), DYNAWO_INSTALL_OPENMODELICA=str(omc), OPENMODELICAHOME=str(omc),
               DYNAWO_OPENMODELICA_VERSION=ver, DYNAWO_LIBXML_HOME=str(sh), DYNAWO_BOOST_HOME=str(sh),
               DYNAWO_LIBIIDM_EXTENSIONS=str(sh / 'lib'), DYNAWO_LOCALE='en_GB', DYNAWO_USE_XSD_VALIDATION='false',
               DYNAWO_PYTHON_COMMAND='python3', DYNAWO_DDB_DIR=str(sh / 'ddb'), DYNAWO_SCRIPTS_DIR=str(sh / 'sbin'),
               DYNAWO_DICTIONARIES='dictionaries_mapping', DYNAWO_RESOURCES_DIR=str(sh / 'share'),
               DYNAWO_NRT_DIR=str(sh / 'sbin' / 'nrt'))
    for v in ('ADEPT', 'SUITESPARSE', 'SUNDIALS', 'LIBIIDM', 'XERCESC'):
        env[f'DYNAWO_{v}_INSTALL_DIR'] = str(sh)
    env['LD_LIBRARY_PATH'] = f'{sh}/lib:' + env.get('LD_LIBRARY_PATH', '')
    env['PATH'] = f'{omc}/bin:{sh}/sbin:' + env.get('PATH', '')
    return env


def compiled_dir() -> Path:
    return CACHE / 'ddb'


def _modelica_hash() -> str:
    import hashlib
    h = hashlib.sha1()
    for mo in sorted(MODELICA_DIR.glob('*.*')):
        h.update(mo.read_bytes())
    return h.hexdigest()[:12]


def _uses_gs_modelica(name: str) -> bool:
    return name in FIXED_MODELS or name.startswith('GsSync')      # composed machines carry GsSetpoints


def is_compiled(name: str) -> bool:
    d = compiled_dir()
    if not all((d / f'{name}{s}').exists() for s in ('.so', '.desc.xml')):
        return False
    if _uses_gs_modelica(name):      # rebuilt when GridScope's Modelica classes change
        stamp = d / f'{name}.gsmodelica'
        return stamp.exists() and stamp.read_text() == _modelica_hash()
    return True


def compile_models(names, home: Path | None = None, log=None) -> dict:
    """Compile the given composed models (skipping those already compiled). Returns {name: error or ''}."""
    names = [n for n in dict.fromkeys(names) if not is_compiled(n)]
    if not names:
        return {}
    st = toolchain_status(home)
    if not st['ok']:
        raise RuntimeError('Cannot compile new Dynawo models:\n' + '\n'.join(st['issues']))
    home = Path(st['home'])
    with _lock:
        sh = _shadow(home)
    env = _env(sh)
    work = CACHE / 'build' / ('_'.join(sorted(names))[:80] + f'_{os.getpid()}')
    if work.exists():
        shutil.rmtree(work)
    (work / 'out').mkdir(parents=True)
    (work / 'modelList.xml').write_text(models_xml(names))
    r = subprocess.run([str(sh / 'sbin' / 'generate-preassembled'), '--model-list', 'modelList.xml',
                        '--output-dir', 'out'], cwd=work, env=env, capture_output=True, text=True)
    (work / 'generate.log').write_text(r.stdout + '\n' + r.stderr)
    if log:
        log(r.stdout[-2000:])
    out, dest = {}, compiled_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        so = work / 'out' / f'{n}.so'
        if not so.exists():
            out[n] = _compile_error(r.stdout + r.stderr, n) + f' (full log: {work / "generate.log"})'
            (dest / f'{n}.failed').write_text(_fail_key(n) + '\n' + out[n])
            continue
        dr = subprocess.run([str(sh / 'sbin' / 'dumpModel'), '-m', str(so), '-o', str(work / 'out' / f'{n}.desc.xml')],
                            cwd=work, env=env, capture_output=True, text=True)
        if dr.returncode != 0 or not (work / 'out' / f'{n}.desc.xml').exists():
            out[n] = 'dumpModel failed: ' + (dr.stderr or dr.stdout)[-500:]
            continue
        for suf in ('.so', '.desc.xml', '.extvar'):
            f = work / 'out' / f'{n}{suf}'
            if f.exists():
                shutil.copy2(f, dest / f.name)
        if _uses_gs_modelica(n):
            (dest / f'{n}.gsmodelica').write_text(_modelica_hash())
        (dest / f'{n}.failed').unlink(missing_ok=True)
        out[n] = ''
    with _lock:
        _save_index()
    if not any(out.values()):
        shutil.rmtree(work, ignore_errors=True)
    activate()
    return out


def _compile_error(text, name):
    lines = [ln for ln in text.splitlines() if re.search(r'error|Error|failed|Failed', ln)]
    return (f'Compilation of {name} failed. ' + ' | '.join(lines[-6:]))[:1500] or 'Compilation failed.'


# ------------------------------------------------------------------ registration
def _index_path():
    return CACHE / 'models.json'


def _save_index():
    known = {}
    if _index_path().exists():
        known = json.loads(_index_path().read_text())
    for n, v in D.CONTROL_LIB['models'].items():
        if v.get('custom') and is_compiled(n):
            known[n] = v
    _index_path().parent.mkdir(parents=True, exist_ok=True)
    _index_path().write_text(json.dumps(known, indent=1))


def load_index():
    """Make previously compiled composed models known to the library."""
    if _index_path().exists():
        for n, v in json.loads(_index_path().read_text()).items():
            if is_compiled(n):
                D.CONTROL_LIB['models'].setdefault(n, v)


def activate():
    """Point Dynawo at the standard model library plus the compiled cache."""
    names = sorted(p.name[:-3] for p in compiled_dir().glob('*.so')) if compiled_dir().exists() else []
    home = dynawo_home()
    if not names or home is None:
        return
    merged = CACHE / 'ddb_merged'
    merged.mkdir(parents=True, exist_ok=True)
    std = home / 'ddb'
    for f in list(std.iterdir()) + list(compiled_dir().iterdir()):
        link = merged / f.name
        if link.is_symlink() or link.exists():
            if link.resolve() == f.resolve():
                continue
            link.unlink()
        link.symlink_to(f.resolve())
    os.environ['DYNAWO_DDB_DIR'] = str(merged)
    D.ADDITIONAL_LIBS['SYNCHRONOUS_GENERATOR'] = [{'lib': n, 'doc': 'GridScope composed model'} for n in names
                                                  if n.startswith('GsSync')]
    for n, f in FIXED_MODELS.items():
        if n in names:
            e = {'lib': n, 'doc': f['doc']}
            if f.get('properties'):
                e['properties'] = f['properties']
            libs = D.ADDITIONAL_LIBS.setdefault(f['category'], [])
            if all(x['lib'] != n for x in libs):
                libs.append(e)


def _fail_key(name: str) -> str:
    """What a failed compilation depended on: retried only when it changes."""
    return _modelica_hash() + ':' + str(sorted(D.CONTROL_LIB['models'].get(name, {}).get('units', {}).items()))


def previous_failure(name: str) -> str:
    f = compiled_dir() / f'{name}.failed'
    if f.exists():
        key, _, msg = f.read_text().partition('\n')
        if key == _fail_key(name):
            return msg
    return ''


def ensure_compiled(models, log=None) -> dict:
    """Compile whatever composed models in `models` are not compiled yet; activate the cache.
    A model whose compilation already failed (same sources) is not retried here: use compile_models."""
    need = [m for m in models if (m in FIXED_MODELS or D.CONTROL_LIB['models'].get(m, {}).get('custom'))
            and not is_compiled(m)]
    res = {m: 'Earlier compilation failed (not retried automatically; use Compile now to retry). '
           + previous_failure(m) for m in need if previous_failure(m)}
    need = [m for m in need if m not in res]
    res.update(compile_models(need, log=log) if need else {})
    activate()
    return res


load_index()
activate()
