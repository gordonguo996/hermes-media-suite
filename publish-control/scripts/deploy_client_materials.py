#!/usr/bin/env python3
"""Run on the customer Windows host only AFTER separate human approval.
Default dry-run is entirely local, without writes, network or process control.
Keep this bundle and backups OUTSIDE the installed program tree.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

BUNDLE = Path(__file__).resolve().parents[1]
NAMES = ('remote_ops.py', 'agentpoll.py', 'remote_agent.py')
SOURCE_REL = Path('client') / 'agent' / 'winport'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def proposal(bundle=BUNDLE):
    baseline = bundle / 'fix_evidence_client' / 'baseline'
    if not baseline.is_dir():
        baseline = bundle / 'bundle_1028a' / 'fix_evidence_client' / 'baseline'
    source = bundle / SOURCE_REL
    result = {}
    for name in NAMES:
        data = (source / name).read_bytes()
        data.decode('utf-8')
        compile(data, name, 'exec')
        baseline_file = baseline / name
        expected = (digest(baseline_file.read_bytes())
                    if baseline_file.is_file() else '')
        actual = digest(data)
        result['agent/winport/' + name] = {
            'bytes': len(data), 'sha256': actual,
            'expected_sha256': expected,
            'source': str(SOURCE_REL / name),
            'baseline': str(baseline),
            'baseline_match': bool(expected) and actual == expected}
    return result

def hidden():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}

def ps(script):
    return subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive',
                                    '-Command',script], text=True, **hidden()).strip()

def process_info(pid):
    return json.loads(ps('Get-CimInstance Win32_Process -Filter "ProcessId = %d" | '
                         'Select-Object ProcessId,ExecutablePath,CommandLine,CreationDate | ConvertTo-Json -Compress' % pid))

def windows_argv(command):
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    ptr = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not ptr: raise RuntimeError('cannot parse process command line')
    try: return [ptr[i] for i in range(count.value)]
    finally: kernel.LocalFree(ptr)


def agent_command_matches(argv, data):
    if argv.count('-m') != 1 or argv.count('--data') != 1:
        return False
    try:
        return (argv[argv.index('-m') + 1] == 'winport'
                and Path(argv[argv.index('--data') + 1]).resolve() == data)
    except IndexError:
        return False


def verify(program, python):
    env = dict(os.environ, PYTHONPATH=str(program/'agent'), PYTHONDONTWRITEBYTECODE='1')
    raw = subprocess.check_output([str(python),'-B','-c',
        'import json,sys;from winport.integrity import verify_program;'
        'print(json.dumps(verify_program(sys.argv[1])))',str(program)],
        env=env,cwd=str(program),text=True,**hidden())
    result=json.loads(raw)
    if result.get('ok') is not True: raise RuntimeError('integrity failed: '+raw)
    return result

def atomic(path, data):
    temp=path.with_name(path.name+'.materials-tmp')
    with temp.open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp,path)

def apply(ns, changes):
    if os.name != 'nt': raise RuntimeError('apply requires Windows')
    if ns.approval != 'HUMAN_APPROVED_MAINTENANCE':
        raise RuntimeError('separate human maintenance approval required')
    if not all((ns.program,ns.data,ns.backup,ns.pid)):
        raise RuntimeError('apply requires --program --data --backup --pid')
    drift = [rel for rel, change in changes.items()
             if not change.get('baseline_match')]
    if drift:
        raise RuntimeError(
            'source baseline drift: ' + ', '.join(drift) +
            '; use the signed full installer or client/tools/ota_patch.py instead')
    program=Path(ns.program).resolve(); data=Path(ns.data).resolve()
    backup=Path(ns.backup).resolve()
    if backup==program or program in backup.parents or backup.exists():
        raise RuntimeError('backup must be a new directory outside program')
    python=program/'runtime'/'python.exe'; pythonw=program/'runtime'/'pythonw.exe'
    if not pythonw.is_file() or not python.is_file(): raise RuntimeError('runtime missing')
    info=process_info(ns.pid)
    if not info or Path(info['ExecutablePath']).resolve() not in (python,pythonw):
        raise RuntimeError('PID does not belong to the selected runtime')
    command=info.get('CommandLine') or ''
    if not agent_command_matches(windows_argv(command), data):
        raise RuntimeError('PID is not the selected agent/data instance')
    manifest_path=program/'MANIFEST.json'
    old_manifest=manifest_path.read_bytes()
    expected=(BUNDLE/'fix_evidence_client/baseline/MANIFEST.json').read_bytes()
    if digest(old_manifest)!=digest(expected): raise RuntimeError('MANIFEST drift; rebase required')
    manifest=json.loads(old_manifest.decode('utf-8-sig'))
    old_version=(program/'VERSION').read_bytes()
    version=old_version.decode('ascii').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+',version): raise RuntimeError('unsupported VERSION')
    if manifest.get('version', version) != version:
        raise RuntimeError('MANIFEST version and VERSION disagree; rebase required')
    new_version=version.rsplit('.',1)[0]+'.'+str(int(version.rsplit('.',1)[1])+1)
    payloads={rel:(BUNDLE/SOURCE_REL/Path(rel).name).read_bytes() for rel in changes}
    payloads['VERSION']=(new_version+'\n').encode('ascii')
    for rel,change in changes.items():
        if digest((program/rel).read_bytes())!=change['expected_sha256']:
            raise RuntimeError('source drift: '+rel)
    if digest(old_version)!=manifest['files']['VERSION']['sha256']:
        raise RuntimeError('VERSION drift')
    verify(program,python)
    for rel,content in payloads.items():
        manifest['files'][rel].update(bytes=len(content),sha256=digest(content))
    if 'version' in manifest: manifest['version']=new_version
    payloads['MANIFEST.json']=(json.dumps(manifest,ensure_ascii=True,indent=2)+'\n').encode('ascii')
    backup.mkdir(parents=True)
    for rel in payloads:
        dest=backup/rel; dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(program/rel,dest)
    (backup/'deployment.json').write_text(json.dumps({'program':str(program),'data':str(data),
        'old_pid':ns.pid,'old_version':version,'new_version':new_version,
        'backup_sha256':{rel:digest((backup/rel).read_bytes()) for rel in payloads}},indent=2))
    # Revalidate the process immediately before stopping; never kill by name.
    if process_info(ns.pid)!=info: raise RuntimeError('process changed')
    ps('Stop-Process -Id %d -ErrorAction Stop; Wait-Process -Id %d -ErrorAction SilentlyContinue' % (ns.pid,ns.pid))
    for rel,content in payloads.items(): atomic(program/rel,content)
    verify(program,python)
    env=dict(os.environ,PYTHONPATH=str(program/'agent'),PYTHONDONTWRITEBYTECODE='1',WINPORT_PROGRAM=str(program))
    proc=subprocess.Popen([str(pythonw),'-B','-m','winport','--data',str(data)],
        cwd=str(program),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=subprocess.DETACHED_PROCESS)
    for _ in range(30):
        if proc.poll() is not None: raise RuntimeError('new agent exited; use backup for manual rollback')
        try:
            with urllib.request.urlopen('http://127.0.0.1:9223/healthz',timeout=2) as reply:
                health=json.load(reply)
            owner=ps('(Get-NetTCPConnection -LocalPort 9223 -State Listen -ErrorAction Stop | Select-Object -First 1).OwningProcess')
            runtime_integrity=json.loads((data/'run/integrity.json').read_text(encoding='utf-8-sig'))
            if (health.get('ok') is True and health.get('version')==new_version
                    and int(owner)==proc.pid and runtime_integrity.get('ok') is True):
                result={'ok':True,'pid':proc.pid,'healthz':health,'integrity':verify(program,python)}
                (backup/'verification.json').write_text(json.dumps(result,indent=2))
                print(json.dumps(result)); return
        except (OSError,ValueError,subprocess.CalledProcessError): pass
        time.sleep(2)
    raise RuntimeError('post-restart verification failed; manual rollback required; backup='+str(backup))

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run',action='store_true',default=True)
    mode.add_argument('--apply',action='store_true')
    for arg in ('program','data','backup','approval'): parser.add_argument('--'+arg)
    parser.add_argument('--pid',type=int)
    ns=parser.parse_args(argv)
    changes=proposal()
    if not ns.apply:
        print(json.dumps({'dry_run':True,'changes':changes,
            'steps':['verify baseline/PID/integrity','backup','stop selected PID',
                     'write code/VERSION/MANIFEST','silent pythonw restart','verify PID/healthz/integrity'],
            'executed':False},indent=2)); return 0
    apply(ns,changes)
    return 0

if __name__=='__main__':
    sys.exit(main())
