#!/usr/bin/env python3
"""Run real-browser visual acceptance; pass secrets in env, never command args."""
import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--base', default='http://127.0.0.1:18013')
p.add_argument('--accounts', type=Path, default=ROOT/'.private/accounts.json')
p.add_argument('--chromium', required=True)
p.add_argument('--theme', default='light')
p.add_argument('--width', type=int, default=1512)
p.add_argument('--height', type=int, default=1000)
p.add_argument('routes', nargs='*')
a = p.parse_args()
account = next(x for x in json.loads(a.accounts.read_text()) if x['role']=='admin')
out = ROOT/'artifacts'/f'ui-{a.theme}-{a.width}'
out.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, SHOT_BASE=a.base, SHOT_USER=account['username'],
           SHOT_PASSWORD=account['password'], PLAYWRIGHT_CHROMIUM=a.chromium)
cmd = ['node', 'scripts/screenshots.mjs', '--theme', a.theme, '--width', str(a.width),
       '--height', str(a.height), '--out', str(out), *a.routes]
with (out/'result.log').open('w') as f:
    r = subprocess.run(cmd, cwd=ROOT/'web', env=env, stdout=f, stderr=subprocess.STDOUT)
print(json.dumps(dict(theme=a.theme,width=a.width,exit_code=r.returncode,out=str(out))))
raise SystemExit(r.returncode)
