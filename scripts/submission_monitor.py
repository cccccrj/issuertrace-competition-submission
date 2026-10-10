#!/usr/bin/env python3
"""Light production liveness sampling while load runs on a separate instance."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import requests

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--base',default='http://127.0.0.1:8000',help='Explicit target for read-only health sampling; no writes or load traffic.')
p.add_argument('--seconds',type=int,default=1200)
p.add_argument('--out',type=Path,required=True)
a=p.parse_args();a.out.parent.mkdir(parents=True,exist_ok=True)
finish=time.monotonic()+a.seconds
with a.out.open('w') as log:
    while time.monotonic()<finish:
        begin=time.perf_counter()
        row={'at':datetime.now(timezone.utc).isoformat()}
        try:
            r=requests.get(a.base.rstrip('/')+'/api/health',timeout=20)
            row.update(status=r.status_code,healthy=r.status_code==200 and r.json().get('status')=='ok')
        except requests.RequestException as e:row.update(healthy=False,error=type(e).__name__)
        row['ms']=round((time.perf_counter()-begin)*1000,2)
        log.write(json.dumps(row)+'\n');log.flush()
        time.sleep(min(60,max(0,finish-time.monotonic())))
