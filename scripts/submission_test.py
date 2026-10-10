#!/usr/bin/env python3
"""HTTP acceptance and virtual-user load tests. Credentials never enter reports.

Writes are opt-in and restricted to an isolated instance. Each virtual user
owns a session, uses a one-second think time, and records every response rather
than interpreting a fast 500 as good performance. No AI calls in load tests.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import random
import statistics
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests

def require_local_isolated_target(base):
    """Write/load tests only target an explicit local isolated deployment."""
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', '::1', 'localhost'):
        raise SystemExit('Write/load tests require a loopback isolated instance; use an SSH port forward for remote testing.')

def percentile(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs)-1, int((len(xs)-1)*p))], 2) if xs else None

class Suite:
    def __init__(self, base, accounts, out):
        self.base, self.out = base.rstrip('/'), out
        self.accounts = {a['username'].removeprefix('competition-'): a for a in accounts}
        self.sessions, self.checks = {}, []
        self.out.mkdir(parents=True, exist_ok=True)

    def session(self, role):
        if role not in self.sessions:
            a = self.accounts[role]
            s = requests.Session(); s.trust_env = False
            r = s.post(self.base+'/api/auth/login', json={k:a[k] for k in ('username','password')}, timeout=30)
            r.raise_for_status()
            assert r.json()['role'] == a['role']
            self.sessions[role] = s
        return self.sessions[role]

    def req(self, name, path, role='reviewer', method='GET', expected=200, body=None, validate=None):
        start = time.perf_counter()
        try:
            s = self.session(role) if role else requests.Session()
            r = s.request(method, self.base+path, json=body, timeout=90)
            good = r.status_code in (expected if isinstance(expected, tuple) else (expected,))
            if good and validate: good = bool(validate(r))
            self.checks.append({'name':name,'path':path,'status':r.status_code,'passed':good,
                                'ms':round((time.perf_counter()-start)*1000,2)})
            return r
        except Exception as e:
            self.checks.append({'name':name,'path':path,'passed':False,'error':type(e).__name__})
            return None

    def smoke(self, isolated):
        if isolated:
            require_local_isolated_target(self.base)
        self.req('首页','/',role=None)
        self.req('健康检查','/api/health',role=None,validate=lambda r:r.json().get('status')=='ok')
        self.req('未登录禁止读取','/api/events',role=None,expected=401)
        self.req('未登录禁止助手','/api/assistant/stream',role=None,method='POST',body={},expected=401)
        for path in ['/api/overview','/api/events?limit=25','/api/claims?limit=25','/api/claims/stats',
                     '/api/issues?limit=25','/api/timeline?limit=25','/api/timeline/phases',
                     '/api/risks?limit=25','/api/risks/summary','/api/alerts?limit=25','/api/alerts/facets',
                     '/api/propagation?limit=25','/api/duplicates?limit=25','/api/diffs?limit=25',
                     '/api/elements?limit=25','/api/vocabularies','/api/search?q=募集资金&limit=5',
                     '/api/search?q=WuXi%20AppTec&limit=5','/api/ingestion/funnel',
                     '/api/review/tasks?limit=20','/api/review/metrics','/api/notifications?limit=25',
                     '/api/notify/subscriptions','/api/notify/outbox','/api/audit/runs?limit=25',
                     '/api/benchmark/summary']:
            self.req('功能 '+path, path)
        ids={}
        for kind,key in [('events','event_id'),('issues','issue_id'),('risks','alert_id')]:
            r=self.req(kind+'样例','/api/'+kind+'?limit=1')
            if r and r.json().get('items'):
                ids[kind]=r.json()['items'][0][key]
                self.req(kind+'详情',f'/api/{kind}/{ids[kind]}')
        for kind in ('events','issues','risks'):
            self.req(kind+'不存在',f'/api/{kind}/submission-missing',expected=404)
        if 'events' in ids:
            r=self.req('事件报告导出',f'/api/reports/events/{ids["events"]}.docx',
                       validate=lambda r:r.content.startswith(b'PK'))
            if r and r.status_code==200: (self.out/'case-event.docx').write_bytes(r.content)
            r=self.req('典型事件输入输出',f'/api/events/{ids["events"]}')
            if r: (self.out/'case-event.json').write_text(json.dumps(r.json(),ensure_ascii=False,indent=2))
        self.req('匿名环境文件保护','/.env',role=None,
                 validate=lambda r:'AUTH_SECRET=' not in r.text and 'LLM_API_KEY=' not in r.text)
        self.req('非法分页','/api/events?limit=0',expected=422)
        self.req('不存在API','/api/submission-missing',expected=404)
        self.req('助手输入验证','/api/assistant/ask',method='POST',body={},expected=422)
        if isolated:
            self.req('无模型密钥降级','/api/assistant/ask',method='POST',
                     body={'scope':'event','id':ids['events'],'question':'请解释事件依据'},
                     validate=lambda r:r.json().get('available') is False)
            self.collaboration(ids)
        report={'at':datetime.now(timezone.utc).isoformat(),'base':self.base,'isolated':isolated,
                'checks':self.checks,'passed':sum(c['passed'] for c in self.checks),
                'failed':sum(not c['passed'] for c in self.checks),'cases':ids}
        (self.out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k not in ('checks','cases')},ensure_ascii=False))
        return report['failed']==0

    def collaboration(self,ids):
        for role in ('viewer','analyst','analyst2','admin'): self.session(role)
        aid=ids['risks']; path=f'/api/notify/dispositions/{aid}'
        self.req('只读角色禁止修改',path,'viewer','POST',403,{'status':'investigating'})
        self.req('分析师禁止账号管理','/api/users','analyst',expected=403)
        self.req('缺结论禁止上报',path,'analyst','POST',422,{'status':'reporting','owner':'competition-reviewer'})
        self.req('分析师认领',path,'analyst','POST',200,{'status':'investigating','owner':'competition-analyst',
                 'conclusion':'partial','level_override':'medium','opinion':'比赛隔离测试：证据核查'})
        self.req('上报给复核负责人',path,'analyst','POST',200,{'status':'reporting','owner':'competition-reviewer'})
        self.req('上报后分析师不能改判',path,'analyst','POST',403,{'status':'reporting','conclusion':'confirmed'})
        self.req('退回意见必填',path,'reviewer','POST',422,{'status':'returned'})
        self.req('退回补证',path,'reviewer','POST',200,{'status':'returned','note':'请补充对应页码'})
        self.req('退回负责人恢复',f'/api/risks/{aid}',validate=lambda r:
                 r.json()['disposition']['owner']=='competition-analyst')
        self.req('补证后重新上报',path,'analyst','POST',200,{'status':'reporting','owner':'competition-reviewer',
                 'conclusion':'confirmed','opinion':'已核对引用页码'})
        self.req('复核归档',path,'reviewer','POST',200,{'status':'archived','note':'隔离验收通过'})
        self.req('归档禁止重新开启',path,'reviewer','POST',409,{'status':'investigating'})
        for role in ('analyst','reviewer','viewer'):
            self.req('个人通知 '+role,'/api/notifications?limit=200',role,
                     validate=lambda r:all(x.get('recipient') in (None,self.accounts[role]['username'])
                                          for x in r.json().get('items',[])))
        self.req('私网Webhook拒绝','/api/notify/subscriptions','analyst','POST',400,
                 {'name':'submission-ssrf','channel':'webhook','target':'http://169.254.169.254/latest/meta-data/'})
        self.req('默认干运行订阅','/api/notify/subscriptions','analyst','POST',200,
                 {'name':'submission-dryrun','channel':'webhook','target':'http://127.0.0.1:9999/hook'},
                 validate=lambda r:r.json().get('dry_run') is True)
        # Concurrent saves must produce a consistent, append-only history. This
        # application permits same-state saves; it does not promise exclusive claims.
        rows=self.session('reviewer').get(self.base+'/api/risks?limit=10',timeout=30).json()['items']
        race=next(x['alert_id'] for x in rows if x['alert_id']!=aid)
        self.req('并发测试初始化',f'/api/notify/dispositions/{race}','analyst','POST',200,
                 {'status':'investigating','owner':'competition-analyst'})
        barrier=threading.Barrier(10)
        def save(i):
            s=requests.Session();s.trust_env=False;s.cookies.update(self.session('analyst').cookies)
            barrier.wait()
            return s.post(self.base+f'/api/notify/dispositions/{race}',json={
                'status':'investigating','opinion':f'concurrent-{i}'},timeout=60).status_code
        with ThreadPoolExecutor(max_workers=10) as pool: statuses=list(pool.map(save,range(10)))
        history=self.session('reviewer').get(self.base+f'/api/risks/{race}',timeout=30).json()['history']
        self.checks.append({'name':'10路同时保存与审计完整性（同一分析师会话）','passed':statuses==[200]*10 and
                            sum((h.get('opinion') or '').startswith('concurrent-') for h in history)==10,
                            'statuses':statuses,'audit_records':len(history)})

    def load(self, duration):
        require_local_isolated_target(self.base)
        stages=[(1,30),(5,30),(10,60),(25,120),(50,duration)]
        probes=['/api/events?limit=25','/api/risks?limit=25','/api/risks/summary',
                '/api/claims?limit=25','/api/issues?limit=25','/api/search?q=募集资金&limit=5',
                '/api/timeline?limit=25','/api/notifications?limit=25']
        # Independent authenticated identities, not fifty copies of one cookie.
        load_names=[f'load-{i:02d}' for i in range(50)]
        cookies=[self.session(name).cookies.copy() for name in load_names]
        results=[]
        for users,seconds in stages:
            finish=time.monotonic()+seconds
            def worker(i):
                s=requests.Session();s.trust_env=False;s.cookies.update(cookies[i])
                rng=random.Random(i); samples=[]; counts=Counter(); errors=Counter()
                while time.monotonic()<finish:
                    path=rng.choice(probes); method='GET'; body=None
                    if rng.random()<0.1:
                        path='/api/notifications/read';method='POST';body={}
                    begin=time.perf_counter()
                    try:
                        r=s.request(method,self.base+path,json=body,timeout=30)
                        code=r.status_code
                    except requests.RequestException as e:
                        code=0;errors[type(e).__name__]+=1
                    samples.append({'path':path,'status':code,'ms':round((time.perf_counter()-begin)*1000,2)})
                    counts[code]+=1
                    time.sleep(1)
                return samples,counts,errors
            start=time.monotonic()
            with ThreadPoolExecutor(max_workers=users) as pool: outputs=list(pool.map(worker,range(users)))
            elapsed=time.monotonic()-start
            samples=[x for rows,_,_ in outputs for x in rows]
            times=[x['ms'] for x in samples];fail=sum(x['status']!=200 for x in samples)
            grouped={p:[x['ms'] for x in samples if x['path']==p] for p in set(x['path'] for x in samples)}
            row={'users':users,'think_seconds':1,'seconds':round(elapsed,1),'requests':len(samples),
                 'failed':fail,'error_rate':round(fail/max(1,len(samples)),6),
                 'rps':round(len(samples)/elapsed,2),'p50_ms':percentile(times,.5),'p95_ms':percentile(times,.95),
                 'p99_ms':percentile(times,.99),'max_ms':max(times),
                 'by_path':{p:{'requests':len(t),'p95_ms':percentile(t,.95)} for p,t in grouped.items()}}
            results.append(row)
            with (self.out/f'load-{users}-samples.jsonl').open('w') as f:
                for x in samples:f.write(json.dumps(x)+'\n')
            report={'base':self.base,'at':datetime.now(timezone.utc).isoformat(),'stages':results,
                    'identities':50,'authenticated_before_timing':True,
                    'workload':'90% mixed reads / 10% real notification read-receipt writes; no AI requests',
                    'acceptance':{'error_rate_max':.01,'p95_ms_max':2000},
                    'passed':all(r['error_rate']<=.01 and r['p95_ms']<=2000 for r in results)}
            (self.out/'load.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(row,ensure_ascii=False),flush=True)
            if row['error_rate']>.05: break
        return report['passed'] and len(results)==len(stages)

    def assistant(self):
        s=self.session('reviewer')
        aid=s.get(self.base+'/api/risks?limit=1',timeout=30).json()['items'][0]['alert_id']
        payload={'scope':'risk','id':aid,'question':'这条风险有哪些事实依据？请区分机器判断与待核验内容。'}
        start=time.perf_counter()
        response=s.post(self.base+'/api/assistant/stream',json=payload,stream=True,timeout=300)
        response.raise_for_status()
        events=[]; first=None
        for line in response.iter_lines(decode_unicode=True):
            if line.startswith('data: '):
                if first is None:first=round(time.perf_counter()-start,3)
                events.append(json.loads(line[6:]))
        finals=[x.get('result',{}) for x in events if x.get('type')=='result']
        final=finals[-1] if finals else {}
        report={'input':payload,'status':response.status_code,'first_event_seconds':first,
                'total_seconds':round(time.perf_counter()-start,3),'events':events,
                'passed':bool(final.get('available')),
                'limitation':'Availability and trace inspected; no independent factual gold accuracy claimed.'}
        (self.out/'assistant.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k not in ('input','events')},ensure_ascii=False))
        return report['passed']

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['smoke','load','assistant'])
    p.add_argument('--base',required=True);p.add_argument('--accounts',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--isolated',action='store_true')
    p.add_argument('--duration',type=int,default=1800)
    a=p.parse_args();suite=Suite(a.base,json.loads(a.accounts.read_text()),a.out)
    ok=suite.smoke(a.isolated) if a.mode=='smoke' else suite.load(a.duration) if a.mode=='load' else suite.assistant()
    raise SystemExit(0 if ok else 1)
if __name__=='__main__':main()
