#!/usr/bin/env python3
"""Read-only relationship and provenance checks on the frozen analytical copy."""
import json
import sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
queries={
    'events_without_version': 'SELECT count(*) FROM event_records e LEFT JOIN document_versions d ON d.version_id=e.version_id WHERE d.version_id IS NULL',
    'claims_without_event': 'SELECT count(*) FROM event_claims c LEFT JOIN event_records e ON e.event_id=c.event_id WHERE e.event_id IS NULL',
    'verdicts_without_claim': 'SELECT count(*) FROM claim_verdicts v LEFT JOIN event_claims c ON c.claim_id=v.claim_id WHERE c.claim_id IS NULL',
    'evidence_without_version': 'SELECT count(*) FROM evidence_paragraphs p LEFT JOIN document_versions d ON d.version_id=p.version_id WHERE d.version_id IS NULL',
    'duplicate_members_without_version': 'SELECT count(*) FROM duplicate_cluster_members m LEFT JOIN document_versions d ON d.version_id=m.version_id WHERE d.version_id IS NULL',
}
with sqlite3.connect(f'file:{ROOT}/data/state/collector.sqlite3?mode=ro',uri=True) as db:
    counts={name:db.execute(sql).fetchone()[0] for name,sql in queries.items()}
    groups={
        'events_by_company':dict(db.execute('SELECT company_id,count(*) FROM event_records GROUP BY company_id')),
        'verdicts':dict(db.execute('SELECT verdict,count(*) FROM claim_verdicts GROUP BY verdict')),
        'alerts_by_level':dict(db.execute('SELECT risk_level,count(*) FROM alerts GROUP BY risk_level')),
    }
report={'checks':counts,'passed':all(n==0 for n in counts.values()),'groups':groups,
        'limitation':'Referential consistency is not factual accuracy or full raw-file hash verification.'}
(ROOT/'docs/submission/evidence/data-consistency.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))
