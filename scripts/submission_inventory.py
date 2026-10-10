#!/usr/bin/env python3
"""Read-only frozen dataset inventory and field dictionary, no user hashes."""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/submission/evidence'
OUT.mkdir(parents=True, exist_ok=True)
result = {}
for name in ('collector', 'benchmark'):
    with sqlite3.connect(f'file:{ROOT}/data/state/{name}.sqlite3?mode=ro', uri=True) as db:
        tables = {}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            if table.startswith('sqlite_'): continue
            fields = [{'name':r[1], 'type':r[2], 'not_null':bool(r[3]), 'primary_key':bool(r[5])}
                      for r in db.execute(f'PRAGMA table_info("{table}")')]
            tables[table] = dict(rows=db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0], fields=fields)
        result[name] = dict(integrity_check=db.execute('PRAGMA integrity_check').fetchone()[0], tables=tables)
(OUT/'data-inventory.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print('Inventory saved; passwords and document contents are not included.')
