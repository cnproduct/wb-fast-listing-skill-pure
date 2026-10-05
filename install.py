"""Install a local isolated runtime and register the skill for this OS account."""
import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def install():
    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10+ required. Install Python from https://www.python.org/downloads/')
    p = argparse.ArgumentParser()
    p.add_argument('--no-browser', action='store_true', help='Skip Chromium download (packaging validation only)')
    p.add_argument('--no-schedule', action='store_true', help='Run worker manually instead of a native timer')
    args = p.parse_args()
    source = Path(__file__).resolve().parent
    target = Path.home() / '.local' / 'share' / 'wb-fast-listing-skill-pure'
    target.mkdir(parents=True, exist_ok=True)
    package = target / 'source'
    package.mkdir(exist_ok=True)
    shutil.copytree(source / 'wb_pure', package / 'wb_pure', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('pyproject.toml', 'SKILL.md', 'README.md'):
        shutil.copy2(source / name, package / name)
    runtime = target / 'venv'
    python = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(runtime)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', str(package)], check=True)
    if not args.no_browser:
        subprocess.run([str(python), '-m', 'playwright', 'install', 'chromium'], check=True)
    if os.name == 'nt':
        launcher = target / 'wb-pure.cmd'
        launcher.write_text(f'@echo off\r\n"{python}" -m wb_pure %*\r\n', encoding='utf-8')
    else:
        import shlex
        launcher = target / 'wb-pure'
        launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' -m wb_pure "$@"\n')
        launcher.chmod(0o755)
    for root in (Path.home() / '.gemini/config/skills', Path.home() / '.codex/skills'):
        skill = root / 'wb-fast-listing-skill-pure'
        skill.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / 'SKILL.md', skill / 'SKILL.md')
        shutil.copy2(source / 'README.md', skill / 'README.md')
    if not args.no_schedule:
        subprocess.run([str(python), '-m', 'wb_pure', 'schedule'], check=True)
    print(f'Installed {launcher}\nOpen a new assistant chat and select wb-fast-listing-skill-pure.')


if __name__ == '__main__':
    from wb_pure.state import home_dir, lock
    with lock(home_dir()):
        install()
