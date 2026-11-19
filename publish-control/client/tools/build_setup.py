"""Build a single EXE from a fully staged offline bundle; fail closed on enrolment."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def validate(payload):
    for rel in ('runtime/python.exe', 'wheels', 'browsers', 'bin/ffmpeg.exe',
                'chrome-offline/ChromeStandaloneSetup64.exe', 'bootstrap.bat',
                'MANIFEST.json', 'config/vendor_pubkey.hex'):
        if not (payload / rel).exists():
            raise ValueError('missing offline payload: ' + rel)
    version = (payload / 'VERSION').read_text(encoding='utf-8-sig').strip()
    if not version:
        raise ValueError('payload VERSION is empty')
    config = json.loads((payload / 'config/activation.json').read_text(encoding='utf-8-sig'))
    url = urlsplit(config['endpoint'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or len(config['enrollment_token']) < 32):
        raise ValueError('invalid activation provisioning')
    # Allowlisted payload staging excludes signing tools, tests, docs/evidence.
    if (payload / 'control_plane').exists() or (payload / 'tools/devsign.py').exists():
        raise ValueError('control-plane signing code must not ship')
    # Per-customer relay credentials are imported into the app data directory
    # after installation.  A payload carrying one is a release blocker.
    for path in payload.rglob('*'):
        if path.is_file() and path.name in {'relay_key', 'client_key', 'known_hosts'}:
            raise ValueError('remote credential must not be packaged: ' + path.name)
    import build_bundle
    if not build_bundle.verify(payload)['ok']:
        raise ValueError('payload manifest failed')


def stage(materials, payload):
    """Reuse v39 offline binaries, copy current runtime only; never alter v39."""
    source = Path(__file__).resolve().parents[1]
    if payload.exists():
        raise ValueError('staging target already exists; choose a fresh directory')
    payload.mkdir(parents=True)
    for name in ('runtime', 'wheels', 'browsers', 'bin', 'chrome-offline'):
        shutil.copytree(materials / name, payload / name)
    for name in ('agent', 'scripts', 'config', 'assets', 'vendor'):
        shutil.copytree(source / name, payload / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'activation.json'))
    for name in ('bootstrap.bat', 'VERSION', 'requirements.txt'):
        shutil.copyfile(source / name, payload / name)
    (payload / 'license').mkdir()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--materials', type=Path, help='stage from existing offline bundle')
    parser.add_argument('--fetch', action='store_true',
                        help='download+verify materials from materials.lock.json first (tools/fetch_materials.py)')
    parser.add_argument('--activation-config', type=Path, help='operator-provisioned customer credential')
    args = parser.parse_args()
    try:
        if args.fetch:
            if args.materials:
                raise ValueError('--fetch and --materials are mutually exclusive')
            args.materials = args.payload.with_name(args.payload.name + '-materials')
            subprocess.run([sys.executable, str(Path(__file__).resolve().parent / 'fetch_materials.py'),
                            '--out', str(args.materials)], check=True)
        if args.materials:
            stage(args.materials, args.payload)
        if args.activation_config:
            shutil.copyfile(args.activation_config, args.payload / 'config/activation.json')
        import build_bundle
        version = (args.payload / 'VERSION').read_text(
            encoding='utf-8-sig').strip()
        build_bundle.build(args.payload, version=version)
        validate(args.payload)
        compiler = shutil.which('makensis') or next(
            (c for c in (r'C:\Program Files (x86)\NSIS\makensis.exe',
                         r'C:\Program Files\NSIS\makensis.exe') if Path(c).exists()), None)
        if not compiler:
            raise ValueError('NSIS compiler missing on build host')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([compiler, '-DPAYLOAD=' + str(args.payload.resolve()),
                        '-DOUTPUT=' + str(args.output.resolve()),
                        '-DVERSION=' + version,
                        str(Path(__file__).resolve().parents[1] / 'installer/setup.nsi')], check=True)
    except Exception as exc:
        parser.exit(1, 'BLOCKED: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
