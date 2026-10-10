import io
import os
import sqlite3
import tempfile
import re
import unittest
import warnings
import zipfile
from pathlib import Path

import api.review_db as review_db
from processor.claim_index import bigrams

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient

from api.main import app

SCHEMA = """
CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_id TEXT, company_id TEXT,
    title TEXT, published_at TEXT, canonical_url TEXT, document_type TEXT, source_tier TEXT);
CREATE TABLE processed_documents(version_id TEXT PRIMARY KEY, page_count INTEGER,
    processor_version TEXT);
CREATE TABLE processing_runs(run_id TEXT PRIMARY KEY, started_at TEXT, completed_at TEXT,
    status TEXT, processor_version TEXT, options_json TEXT, documents_seen INTEGER,
    documents_processed INTEGER, ai_documents INTEGER, error_message TEXT);
CREATE TABLE event_records(event_id TEXT PRIMARY KEY, version_id TEXT, company_id TEXT,
    event_types_json TEXT, issue_codes_json TEXT, production_mode TEXT, event_subjects_json TEXT,
    event_time TEXT, event_time_precision TEXT, affected_objects_json TEXT, impact_direction TEXT,
    impact_magnitude TEXT, article_sentiment TEXT, relevance_score REAL, summary TEXT,
    amounts_json TEXT, ratios_json TEXT, persons_json TEXT, organizations_json TEXT,
    products_json TEXT, risk_score REAL, risk_level TEXT, recommended_action TEXT,
    model_name TEXT, extraction_confidence REAL, needs_review INTEGER, review_reasons_json TEXT,
    -- The fixture predated the eligibility split. Routes and the ledger rebuild
    -- both select on this, so a fixture without it answers about a schema that
    -- no longer exists anywhere.
    eventhood TEXT NOT NULL DEFAULT 'EVENT',
    -- The model's own label, which is also where the claims it proposed and
    -- the matcher refused are recorded.
    raw_label_json TEXT NOT NULL DEFAULT '{}',
    -- Whether a rule or a model decided, and the seven terms the risk score is
    -- made of.  Both are on event_records and neither had ever been sent to a
    -- page: 「高 85」 with nothing behind it is the third thing an outside reader
    -- called 「笼统」.
    label_source TEXT NOT NULL DEFAULT 'ai',
    risk_breakdown_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT, ordinal INTEGER, text TEXT,
    issue_code TEXT, claim_type TEXT, confidence REAL);
CREATE TABLE claim_verdicts(claim_id TEXT PRIMARY KEY, company_id TEXT, verdict TEXT,
    partial_subtype TEXT, evidence_version_id TEXT, evidence_page INTEGER, evidence_section TEXT,
    evidence_text TEXT, retrieval_score REAL, method TEXT, confidence REAL, reason TEXT,
    adjudicated_at TEXT, inquiry_question_id TEXT, model_name TEXT, prompt_version TEXT);
CREATE TABLE inquiry_questions(question_id TEXT PRIMARY KEY, company_id TEXT, version_id TEXT,
    round TEXT, document_title TEXT, number INTEGER, topic TEXT, question_text TEXT,
    question_page INTEGER, first_paragraph_id INTEGER, last_paragraph_id INTEGER,
    answer_first_paragraph_id INTEGER, answer_page INTEGER, aligned_at TEXT);
CREATE TABLE inquiry_triplets(triplet_id TEXT PRIMARY KEY, question_id TEXT, company_id TEXT,
    response_paragraph_id INTEGER, response_page INTEGER, response_excerpt TEXT, diff_id TEXT,
    diff_section TEXT, diff_kind TEXT, shared_terms TEXT, match_score REAL, created_at TEXT);
CREATE TABLE disclosure_diffs(diff_id TEXT PRIMARY KEY, company_id TEXT, pair_key TEXT,
    left_version_id TEXT, right_version_id TEXT, section TEXT, diff_kind TEXT, left_text TEXT,
    right_text TEXT, left_page INTEGER, right_page INTEGER, metric TEXT, old_value TEXT,
    new_value TEXT, delta REAL, delta_ratio REAL, similarity REAL);
CREATE TABLE issue_ledger(issue_id TEXT PRIMARY KEY, company_id TEXT, issue_code TEXT,
    first_seen_at TEXT, last_seen_at TEXT, event_count INTEGER, document_count INTEGER,
    independent_source_count INTEGER, risk_score REAL, risk_level TEXT, status TEXT,
    event_time_earliest TEXT, event_time_latest TEXT);
CREATE TABLE evidence_paragraphs(paragraph_id INTEGER PRIMARY KEY, company_id TEXT,
    version_id TEXT, document_type TEXT, title TEXT, section TEXT, page INTEGER, text TEXT);
-- The real index and its document-frequency table, so the disclosure group is
-- exercised rather than short-circuited by the missing-table guard.
CREATE VIRTUAL TABLE evidence_fts USING fts5(body, tokenize='unicode61');
CREATE TABLE evidence_terms(company_id TEXT, token TEXT, documents INTEGER,
    PRIMARY KEY(company_id, token));
CREATE TABLE event_timeline(event_id TEXT PRIMARY KEY, company_id TEXT, version_id TEXT,
    timeline_at TEXT, time_basis TEXT, time_precision TEXT, phase TEXT, phase_label TEXT,
    phase_coefficient REAL, base_risk_score REAL, adjusted_risk_score REAL,
    adjusted_risk_level TEXT, first_seen_round TEXT, is_new_this_round INTEGER,
    claim_count INTEGER, new_fact_count INTEGER, novelty REAL, updated_at TEXT);
CREATE TABLE timeline_rounds(round_id TEXT PRIMARY KEY, ordinal INTEGER, started_at TEXT,
    event_count INTEGER, new_event_count INTEGER, is_baseline INTEGER, reference_time TEXT);
CREATE TABLE disclosure_phases(phase_id TEXT PRIMARY KEY, company_id TEXT, phase TEXT,
    phase_label TEXT, ordinal INTEGER, started_at TEXT, ended_at TEXT, coefficient REAL,
    rationale TEXT, anchor_version_id TEXT, anchor_title TEXT, updated_at TEXT);
CREATE TABLE alerts(alert_id TEXT PRIMARY KEY, company_id TEXT, issue_code TEXT, issue_id TEXT,
    lead_event_id TEXT, lead_version_id TEXT, window_start TEXT, window_end TEXT, headline TEXT,
    lead_title TEXT, summary TEXT, subjects_json TEXT, affected_objects_json TEXT, credibility REAL,
    credibility_label TEXT, credibility_basis_json TEXT, evidence_json TEXT, timeline_json TEXT,
    risk_score REAL, adjusted_risk_score REAL, risk_level TEXT, phase TEXT, phase_label TEXT,
    phase_coefficient REAL, issue_status TEXT, reasons_json TEXT, event_count INTEGER,
    event_ids_json TEXT, new_this_round INTEGER, round_id TEXT, status TEXT, created_at TEXT,
    updated_at TEXT);
CREATE TABLE propagation_edges(edge_id TEXT PRIMARY KEY, company_id TEXT, edge_kind TEXT,
    from_kind TEXT, from_version_id TEXT, to_version_id TEXT, issue_code TEXT, similarity REAL,
    lag_hours REAL, basis TEXT, detected_at TEXT);
CREATE TABLE propagation_components(component_id TEXT PRIMARY KEY, company_id TEXT,
    root_version_id TEXT, member_count INTEGER, source_count INTEGER, first_published_at TEXT,
    last_published_at TEXT, created_at TEXT);
CREATE TABLE propagation_component_members(component_id TEXT, version_id TEXT, company_id TEXT,
    depth INTEGER, PRIMARY KEY(component_id, version_id));
CREATE TABLE event_causal_edges(edge_id TEXT PRIMARY KEY, company_id TEXT,
    from_event_id TEXT, to_event_id TEXT, relation TEXT, confidence REAL, reason TEXT,
    quote_from TEXT, quote_to TEXT, method TEXT, candidate_path TEXT,
    shared_element_count INTEGER, gap_hours REAL, prior_edge_kind TEXT, candidate_note TEXT,
    time_precision_low INTEGER, model_name TEXT, prompt_version TEXT, detected_at TEXT);
"""


def build_fixture(path):
    # A file rather than :memory: because each request opens and closes its own
    # connection, exactly as it does in production.
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("""INSERT INTO document_versions VALUES('v1','cnstock','cxmt','长鑫报道',
        '2026-08-01T00:00:00+00:00','https://example.com/a','news','S2')""")
    db.execute("""INSERT INTO document_versions VALUES('f1','sse','cxmt','注册稿_招股说明书',
        '2026-05-27T00:00:00+00:00','https://example.com/f','prospectus','S0')""")
    db.execute("""INSERT INTO document_versions VALUES('v2','jiemian','cxmt','长鑫报道（转载）',
        '2026-08-02T00:00:00+00:00','https://example.com/b','news','S3')""")
    db.execute("INSERT INTO processed_documents VALUES('f1', 372, 'event-processing-v2')")
    db.execute("""INSERT INTO processing_runs VALUES('r-old','2026-08-26T13:00:00+00:00',
        '2026-08-26T13:03:00+00:00','completed','event-processing-v2','{"limit": 50}',
        20,18,4,NULL)""")
    db.execute("""INSERT INTO processing_runs VALUES('r-new','2026-08-27T07:19:56+00:00',
        '2026-08-27T07:20:22+00:00','completed','event-processing-v2','{}',30,30,6,NULL)""")
    db.execute("""INSERT INTO event_records VALUES('e1','v1','cxmt','["IPO_REFINANCING"]','["FUND_RAISE"]',
        'BRIEF','["长鑫科技"]','2026-08-01','day','["investors_stock_price"]','positive','major','neutral',0.9,
        '长鑫募资295亿元','["295亿元"]','[]','[]','[]','[]',55.0,'MEDIUM','monitor','qwen-plus',0.9,0,'[]','EVENT','{"dropped_claims": ["模型提过但定位不到的一句"]}',
        'ai','[{"key":"base","points":15.0,"detail":""},{"key":"eventhood","points":20.0,"detail":"EVENT"},{"key":"magnitude","points":20.0,"detail":"major"}]')""")
    db.execute("""INSERT INTO event_records VALUES('e2','v1','wuxi_apptec','["ESG_REPUTATION"]','["FINANCIAL"]',
        'BRIEF','[]',NULL,'unknown','[]','neutral','minor','neutral',0.5,
        '药明相关报道','[]','[]','[]','[]','[]',20.0,'LOW','none','qwen-plus',0.7,1,'["no_time"]','EVENT','{}',
        'rule','[]')""")
    db.execute("INSERT INTO event_claims VALUES('c1','e1',0,'长鑫募资295亿元','FUND_RAISE','fact',0.95)")
    db.execute("INSERT INTO event_claims VALUES('c2','e2',0,'药明主张','FINANCIAL','fact',0.8)")
    db.execute("""INSERT INTO claim_verdicts VALUES('c1','cxmt','SUPPORTED',NULL,'f1',164,'第七节',
        '本次拟募集资金295亿元。',-44.0,'rule_numeric',0.9,'数字一致','2026-08-27T00:00:00+00:00','q13',
        NULL,'claim-verdict-v2')""")
    db.execute("""INSERT INTO claim_verdicts VALUES('c2','wuxi_apptec','UNVERIFIABLE',NULL,NULL,NULL,
        NULL,NULL,NULL,'no_corpus',1.0,'尚未采集权威披露语料','2026-08-27T00:00:00+00:00',NULL,
        NULL,'claim-verdict-v1')""")
    db.execute("""INSERT INTO inquiry_questions VALUES('q13','cxmt','f1','first','首轮问询回复',13,
        '募投项目','13．关于募投项目根据申请材料：本次拟募集资金295亿元。',164,10,40,11,165,
        '2026-08-31T00:00:00+00:00')""")
    db.execute("""INSERT INTO inquiry_triplets VALUES('t1','q13','cxmt',11,165,'募投项目测算依据如下。',
        'd1','第七节','numeric','["募投","募集"]',0.21,'2026-08-31T00:00:00+00:00')""")
    db.execute("""INSERT INTO disclosure_diffs VALUES('d1','cxmt','申报稿→上会稿','f0','f1','第七节',
        'numeric',NULL,NULL,160,164,'募集资金','290','295',5.0,0.017,NULL)""")
    db.execute("""INSERT INTO issue_ledger VALUES('i1','cxmt','FUND_RAISE','2026-07-01T00:00:00+00:00',
        '2026-08-01T00:00:00+00:00',1,1,1,60.0,'MEDIUM','active','2026-08-01','2026-08-01')""")
    db.execute("""INSERT INTO issue_ledger VALUES('i2','wuxi_apptec','FINANCIAL',
        '2026-07-05T00:00:00+00:00','2026-08-05T00:00:00+00:00',1,1,1,30.0,'LOW','cooling',
        '2026-08-05','2026-08-05')""")
    db.execute("""INSERT INTO evidence_paragraphs VALUES(1,'cxmt','f1','prospectus',
        '注册稿_招股说明书','第七节',164,'本次拟募集资金295亿元。')""")
    # The index stores space-separated character bigrams; a query has to be
    # expanded the same way, which is what claim_index.search does.
    db.execute("INSERT INTO evidence_fts(rowid,body) VALUES(?,?)",
               (1, " ".join(bigrams('本次拟募集资金295亿元。'))))
    db.execute("""INSERT INTO event_timeline VALUES('e1','cxmt','v1','2026-08-01','event_time','day',
        'ISSUANCE','发行与询价',1.3,55.0,71.5,'medium','r1',0,1,1,1.0,'2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO event_timeline VALUES('e2','wuxi_apptec','v1',NULL,'published_at',
        'unknown','UNKNOWN','未知阶段',1.0,20.0,20.0,'low','r1',0,1,0,0.0,
        '2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO timeline_rounds VALUES('r0',0,'2026-08-26T00:00:00+00:00',2,0,1,
        '2026-08-25')""")
    db.execute("""INSERT INTO timeline_rounds VALUES('r1',1,'2026-08-27T00:00:00+00:00',2,1,0,
        '2026-08-26')""")
    db.execute("""INSERT INTO disclosure_phases VALUES('p1','cxmt','ISSUANCE','发行与询价',5,
        '2026-07-09','2026-07-24',1.3,'发行询价期间的口径变化最敏感','f1','注册稿_招股说明书',
        '2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO alerts VALUES('a1','cxmt','FUND_RAISE','i1','e1','v1','2026-08-01',
        '2026-08-01','长鑫科技｜募资：报道与披露口径冲突（1 条核验点）','媒体称募资规模调整',
        '报道称募资规模调整。','["长鑫科技"]','["investors_stock_price"]',
        0.72,'high','{"verdict": 1.0, "verdict_note": "1 条主张可核验", "source_tier": "S2",
        "source": 0.7, "independent_components": 1, "independence": 0.0,
        "production_mode": "BRIEF", "production": 0.6}',
        '[{"event_id": "e1", "claim": "长鑫募资295亿元", "verdict": "SUPPORTED",
        "evidence_page": 164, "evidence_title": "注册稿_招股说明书"}]',
        '{"time_basis": "event_time"}',55.0,71.5,'high','ISSUANCE','发行与询价',1.3,'active',
        '[{"code": "high_adjusted_risk", "label": "阶段修正后风险分达到预警线"}]',1,'["e1"]',0,'r1',
        'open','2026-08-27T00:00:00+00:00','2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO alerts VALUES('a2','wuxi_apptec',NULL,NULL,'e2','v1','2026-08-02',
        '2026-08-02','药明康德｜未归专题：专题升温，本窗口新增 1 篇报道','药明相关报道',
        '摘要。','[]','[]',0.40,'low','{"verdict": 0.3}','[]','{}',
        20.0,20.0,'low','UNKNOWN','未知阶段',1.0,NULL,
        '[{"code": "issue_escalating", "label": "所属议题处于升温状态"}]',1,'["e2"]',0,'r1','open',
        '2026-08-27T00:00:00+00:00','2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO propagation_components VALUES('g1','cxmt','v1',2,2,'2026-08-01',
        '2026-08-02','2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO propagation_components VALUES('g2','cxmt','f1',1,1,'2026-05-27',
        '2026-05-27','2026-08-27T00:00:00+00:00')""")
    db.execute("INSERT INTO propagation_component_members VALUES('g1','v1','cxmt',0)")
    db.execute("INSERT INTO propagation_component_members VALUES('g1','v2','cxmt',1)")
    db.execute("INSERT INTO propagation_component_members VALUES('g2','f1','cxmt',0)")
    db.execute("""INSERT INTO propagation_edges VALUES('x1','cxmt','REPOST','media','v1','v2',
        'FUND_RAISE',0.95,24.0,'标题与正文高度重合','2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO propagation_edges VALUES('x2','cxmt','FROM_DISCLOSURE','disclosure',
        'f1','v1','FUND_RAISE',0.42,NULL,'引用招股说明书','2026-08-27T00:00:00+00:00')""")
    db.execute("""INSERT INTO event_causal_edges VALUES('k1','cxmt','e2','e1','TRIGGERS',0.9,
        '前者的问询直接引出后者的募资安排','问询原话','募资原话','model','shared_elements',3,
        720.0,NULL,NULL,0,'qwen-plus','causal-link-v1','2026-09-06T00:00:00+00:00')""")
    db.execute("""INSERT INTO event_causal_edges VALUES('k2','cxmt','e1','e2','CONTEXT_ONLY',0.6,
        '只是同一批主体','','','model','prior_edge',2,48.0,'FOLLOW_UP',NULL,0,'qwen-plus',
        'causal-link-v1','2026-09-06T00:00:00+00:00')""")
    db.commit()
    return db


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = self.fixture_path = str(Path(self.directory.name) / "fixture.sqlite3")
        build_fixture(path).close()

        def connect():
            db = sqlite3.connect(path, check_same_thread=False)
            db.row_factory = sqlite3.Row
            return db

        # Every route resolves its connection through the events module's
        # db_session, so one patch redirects the whole app at the fixture.
        import api.routes.events as events_module
        self._original = events_module.connect
        events_module.connect = connect

        # Every data endpoint sits behind the session gate, so the fixture client
        # signs in once and then behaves like a logged-in browser.  The generated
        # account it signs in with only exists while no accounts file does, so the
        # path is redirected into the temp directory: otherwise a machine where the
        # platform is actually configured fails this whole suite for no reason.
        # 账号住在库里，所以测试账号要建在 fixture 那个库里——写入路径必须一起
        # 指过去，否则 users_db 会写进正式库。
        import api.auth as auth_module
        import api.review_db as review_db_module
        from api import users_db
        self._accounts_path = auth_module.ACCOUNTS_PATH
        self._write_path = review_db_module.DB_PATH
        auth_module.ACCOUNTS_PATH = Path(self.directory.name) / "no-accounts.json"
        review_db_module.DB_PATH = Path(path)
        # Notification/disposition/issue modules import DB_PATH by value.
        # Redirecting review_db alone leaves their requests touching the real
        # checkout's database and makes a unit test depend on existing users' data.
        from api import dispatch_db, notify_db, issue_db
        self._side_paths = [(module, module.DB_PATH) for module in (dispatch_db, notify_db, issue_db)]
        for module, _ in self._side_paths:
            module.DB_PATH = Path(path)
        self._environment = {key: os.environ.get(key) for key in ("AUTH_USERNAME", "AUTH_PASSWORD")}
        users = users_db.connect()
        try:
            users_db.create(users, "tester", "fixture-password", "reviewer",
                            by="测试", hasher=auth_module.hash_password)
        finally:
            users.close()
        self.client = TestClient(app)
        self.client.post("/api/auth/login",
                         json={"username": "tester", "password": "fixture-password"})

    def become(self, username, role):
        """换一个身份。

        令牌不再单凭签名生效：中间件每次都核对这个账号还在不在、角色是什么。
        所以换身份要先真的有这个人——伪造一个不存在的账号会被挡在 401。
        """
        from api import auth, users_db
        connection = users_db.connect()
        try:
            if not users_db.row_of(connection, username):
                users_db.create(connection, username, f"{username}-fixture-pass", role,
                                by="测试", hasher=auth.hash_password)
            else:
                users_db.set_role(connection, username, role)
        finally:
            connection.close()
        self.client.cookies.set(auth.COOKIE_NAME, auth.issue_token(username, role))

    def tearDown(self):
        import api.routes.events as events_module
        import api.auth as auth_module
        import api.review_db as review_db_module
        events_module.connect = self._original
        auth_module.ACCOUNTS_PATH = self._accounts_path
        review_db_module.DB_PATH = self._write_path
        for module, previous in self._side_paths:
            module.DB_PATH = previous
        for key, value in self._environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.directory.cleanup()


class OverviewTests(ApiTestCase):
    def test_a_company_without_indexed_filings_is_marked_unverifiable(self):
        # The UI must be able to say why, instead of implying a failed check.
        body = self.client.get("/api/overview").json()
        by_company = {row["company_id"]: row for row in body["companies"]}
        self.assertTrue(by_company["cxmt"]["verifiable"])
        self.assertFalse(by_company["wuxi_apptec"]["verifiable"])

    def test_the_review_queue_is_read_not_typed(self):
        # 页面上曾写死「300 条、人工判定 0 条」。两个数都要从表里来。
        body = self.client.get("/api/overview").json()
        self.assertEqual(set(body["review"]), {"open", "decided"})
        self.assertTrue(all(isinstance(value, int) for value in body["review"].values()))

    def test_counts_come_from_the_tables(self):
        body = self.client.get("/api/overview").json()
        self.assertEqual((body["events"], body["claims"], body["verdicts"]), (2, 2, 2))

    def test_new_this_round_is_reported_next_to_the_round_it_counts_against(self):
        body = self.client.get("/api/overview").json()
        self.assertEqual((body["alerts"], body["high_alerts"], body["propagation_chains"]), (2, 1, 1))
        self.assertEqual(body["round"]["ordinal"], 1)
        self.assertEqual(body["round"]["reference_time"], "2026-08-26")


class EvidenceLinkTests(ApiTestCase):
    """证据得能点回原件。

    整条证据路径此前都不返回地址：四个接口都 JOIN 了 document_versions，却只取
    title 与 document_type，于是页面上《文件名》第 N 页只能是一段纯文本，而
    「可核验」正是这一步该兑现的地方。库里 canonical_url 覆盖率是 100%。
    """

    def test_claim_evidence_carries_the_document_address(self):
        body = self.client.get("/api/claims").json()
        cited = [row for row in body["items"] if row["evidence"]]
        self.assertTrue(cited, "fixture 里没有带证据的主张")
        for row in cited:
            self.assertIn("url", row["evidence"], row["claim_id"])

    def test_event_detail_names_the_document_it_cites(self):
        # 事件详情此前连引的是哪份文件都没说，只有页码和章节。
        events = self.client.get("/api/events").json()["items"]
        for event in events:
            detail = self.client.get(f"/api/events/{event['event_id']}").json()
            for claim in detail["claims"]:
                if claim.get("evidence_version_id"):
                    self.assertIn("evidence_title", claim)
                    self.assertIn("evidence_url", claim)

    def test_the_event_page_can_reach_its_duplicate_cluster(self):
        # 这一页此前对同源簇只字不提，从一条事件走不到那份成员名单。
        events = self.client.get("/api/events").json()["items"]
        self.assertTrue(events)
        detail = self.client.get(f"/api/events/{events[0]['event_id']}").json()
        self.assertIn("duplicate_cluster", detail)

    def test_it_survives_a_database_without_the_clustering_stage(self):
        db = sqlite3.connect(self.fixture_path)
        for table in ("duplicate_cluster_members", "duplicate_clusters"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.commit()
        db.close()
        events = self.client.get("/api/events").json()["items"]
        response = self.client.get(f"/api/events/{events[0]['event_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["duplicate_cluster"])


class GuideTests(ApiTestCase):
    """指南页的六步。样例由接口当场挑，所以能出错的地方是「挑不出来」与「缺表」。"""

    def test_every_step_is_listed_even_when_no_sample_can_be_found(self):
        # 六步是这套系统的形状，不依赖任何一条记录。挑不出样例时把整条路径吞掉，
        # 读者就再也读不到「这里一共分几步」——而那正是他打开这一页要问的。
        db = sqlite3.connect(self.fixture_path)
        db.execute("DELETE FROM alerts")
        db.commit()
        db.close()
        body = self.client.get("/api/guide").json()
        self.assertIsNone(body["sample"])
        self.assertEqual(body["candidates"], 0)
        self.assertEqual(len(body["steps"]), 6)
        for step in body["steps"]:
            self.assertFalse(step["available"])
            # 走不通的一步仍然给得出一个能点的整页入口，且说得出为什么走不通。
            self.assertTrue(step["to"].startswith("/"))
            self.assertTrue(step["unavailable_reason"])

    def test_it_survives_a_database_missing_every_optional_stage(self):
        db = sqlite3.connect(self.fixture_path)
        for table in ("event_elements", "duplicate_clusters", "duplicate_cluster_members",
                      "propagation_component_members", "issue_ledger"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.commit()
        db.close()
        response = self.client.get("/api/guide")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["steps"]), 6)

    def test_an_available_step_always_carries_its_filter(self):
        # 深链失败不报错，只是安静地给出另一页内容——`/elements` 与
        # `/elements?event=…` 长得一样，而后者才是这一步在讲的东西。
        # 断言挂在 available 上而不是挂在「样例存在」上：一步可以合法地走不通
        # （那个阶段没跑过），但一步不可以一边说自己走得通、一边指向整页。
        # 第六步此前落在一个筛好的预警列表上（`/alerts?alert=…`），现在直接落在那
        # 一条的研判档案：同一条链路的终点是一份档案，不是一个还要再点一次的列表。
        narrowing = {"identify": "/events/", "extract": "event=", "verify": "event=",
                     "dedupe": "cluster=", "trace": "/issues/", "alert": "/risks/"}
        for step in self.client.get("/api/guide").json()["steps"]:
            if step["available"]:
                self.assertIn(narrowing[step["key"]], step["to"], step["key"])
            else:
                self.assertTrue(step["unavailable_reason"], step["key"])

    def test_the_sample_does_not_move_between_two_identical_requests(self):
        # 排序只用内容决定的列。拿 updated_at 排序时，processor.alerts 重跑一次
        # 就会在演示中途换掉样例。
        first = self.client.get("/api/guide").json()["sample"]
        second = self.client.get("/api/guide").json()["sample"]
        self.assertEqual(first, second)


class EventTests(ApiTestCase):
    def test_time_basis_names_the_clock_used(self):
        items = self.client.get("/api/events").json()["items"]
        by_id = {item["event_id"]: item for item in items}
        self.assertEqual(by_id["e1"]["time_basis"], "event_time")
        self.assertEqual(by_id["e2"]["time_basis"], "published_at")

    def test_a_future_deadline_does_not_head_the_ledger(self):
        # I-04: a 2032 contract deadline, reported 2026-08-01, sorted above everything.
        db = sqlite3.connect(self.fixture_path)
        db.execute("ALTER TABLE event_records ADD COLUMN event_time_role TEXT")
        db.execute("UPDATE event_records SET event_time='2026-09-01', event_time_role='occurred' WHERE event_id='e1'")
        db.execute("UPDATE event_records SET event_time='2032-01-01', event_time_role='scheduled' WHERE event_id='e2'")
        db.commit()
        db.close()
        items = self.client.get("/api/events").json()["items"]
        self.assertEqual([item["event_id"] for item in items], ["e1", "e2"])
        scheduled = items[1]
        self.assertEqual(scheduled["time_basis"], "published_at")
        self.assertEqual(scheduled["target_time"], "2032-01-01")
        self.assertIsNone(items[0]["target_time"])

    def test_company_filter_narrows_the_ledger(self):
        body = self.client.get("/api/events", params={"company": "cxmt"}).json()
        self.assertEqual(body["total"], 1)

    def test_issue_filter_matches_the_whole_code_not_a_prefix(self):
        self.assertEqual(self.client.get("/api/events", params={"issue": "FUND"}).json()["total"], 0)
        self.assertEqual(self.client.get("/api/events", params={"issue": "FUND_RAISE"}).json()["total"], 1)

    def test_detail_carries_claims_with_their_verdicts(self):
        body = self.client.get("/api/events/e1").json()
        self.assertEqual(body["claims"][0]["verdict"], "SUPPORTED")
        self.assertEqual(body["claims"][0]["evidence_page"], 164)

    def test_unknown_event_is_404(self):
        self.assertEqual(self.client.get("/api/events/missing").status_code, 404)

    def test_detail_gathers_the_outputs_the_brief_asks_for_in_one_place(self):
        # Credibility, the timeline entry and the alert rationale live in three
        # analysis tables; the detail page must not have to join them itself.
        body = self.client.get("/api/events/e1").json()
        self.assertEqual(body["timeline"]["phase_label"], "发行与询价")
        self.assertEqual(body["alerts"][0]["alert_id"], "a1")
        self.assertEqual([reason["code"] for reason in body["alerts"][0]["reasons"]],
                         ["high_adjusted_risk"])
        self.assertEqual(body["alerts"][0]["credibility_basis"]["source_tier"], "S2")
        self.assertEqual(body["propagation"][0]["component_id"], "g1")

    def test_detail_says_what_led_here_and_what_followed(self):
        # Before this the page could not answer either question: a reader had to
        # go back to the whole timeline and find the neighbours by eye.
        body = self.client.get("/api/events/e1").json()
        self.assertEqual([(edge["side"], edge["relation"]) for edge in body["causal_edges"]],
                         [("cause", "TRIGGERS")])
        self.assertEqual(body["causal_edges"][0]["other_event_id"], "e2")
        # The other end travels with the edge, so the page renders in one request.
        self.assertEqual(body["causal_edges"][0]["summary"], "药明相关报道")
        self.assertEqual(body["causal_edges"][0]["quote_from"], "问询原话")

    def test_a_refused_pair_is_not_shown_as_a_link(self):
        # CONTEXT_ONLY is how this layer says "these two merely co-occur".  It is
        # kept in the table — it is the measurement — and must never reach a page
        # that presents its rows as the reason a story moved.
        body = self.client.get("/api/events/e2").json()
        self.assertEqual([edge["relation"] for edge in body["causal_edges"]], ["TRIGGERS"])
        self.assertEqual(body["causal_edges"][0]["side"], "effect")

    def test_an_event_outside_every_alert_says_so_rather_than_borrowing_one(self):
        body = self.client.get("/api/events/e2").json()
        self.assertEqual([alert["alert_id"] for alert in body["alerts"]], ["a2"])
        self.assertIsNotNone(body["timeline"])


class ClaimTests(ApiTestCase):
    def test_verdict_filter_selects_one_state(self):
        body = self.client.get("/api/claims", params={"verdict": "SUPPORTED"}).json()
        self.assertEqual([item["claim_id"] for item in body["items"]], ["c1"])

    def test_evidence_is_returned_with_its_page_and_document(self):
        item = self.client.get("/api/claims", params={"verdict": "SUPPORTED"}).json()["items"][0]
        self.assertEqual(item["evidence"]["page"], 164)
        self.assertEqual(item["evidence"]["title"], "注册稿_招股说明书")

    def test_unverifiable_claim_carries_no_evidence_object(self):
        item = self.client.get("/api/claims", params={"company": "wuxi_apptec"}).json()["items"][0]
        self.assertIsNone(item["evidence"])

    def test_stats_keep_no_corpus_out_of_the_attempted_denominator(self):
        body = self.client.get("/api/claims/stats").json()
        self.assertEqual(body["no_corpus"], 1)
        self.assertEqual(body["attempted"], 1)
        self.assertEqual(body["cited_rate"], 1.0)

    def test_stats_report_no_rate_when_nothing_was_attempted(self):
        body = self.client.get("/api/claims/stats", params={"company": "wuxi_apptec"}).json()
        self.assertEqual(body["attempted"], 0)
        self.assertIsNone(body["cited_rate"])


class SubtypeFilterTests(ApiTestCase):
    """Filtering a partial by its subtype.

    交付物14 §3.2 walks the reviewer to ``already_explained`` — the slice where
    the exchange has already asked and the company already answered, which is
    handled differently from an unexplained partial.  The column was returned by
    this endpoint from the start but never filterable, so that beat could not be
    performed in the interface at all.

    The rows are written here rather than into the shared fixture: one more claim
    there moves the overview counts, the vocabulary usage tallies, the review
    seeding mix and the issue verdict summary, and none of those tests are about
    this.
    """

    def partials(self):
        db = sqlite3.connect(self.fixture_path)
        db.execute("INSERT INTO event_claims VALUES('c3','e1',1,'已就募投答复','FUND_RAISE','fact',0.9)")
        db.execute("""INSERT INTO claim_verdicts VALUES('c3','cxmt','PARTIAL','already_explained','f1',
            165,'第七节','募投项目测算依据如下。',-40.0,'llm_adjudicated',0.8,'交易所已问、公司已答',
            '2026-08-27T00:00:00+00:00',NULL,NULL,'claim-verdict-v2')""")
        db.execute("INSERT INTO event_claims VALUES('c4','e1',2,'募投测算口径','FUND_RAISE','fact',0.9)")
        db.execute("""INSERT INTO claim_verdicts VALUES('c4','cxmt','PARTIAL','timing_mismatch','f1',
            166,'第七节','测算基准日为上年末。',-40.0,'llm_adjudicated',0.8,'口径时点不同',
            '2026-08-27T00:00:00+00:00',NULL,NULL,'claim-verdict-v2')""")
        db.commit()
        db.close()

    def test_the_subtype_selects_its_own_slice(self):
        self.partials()
        body = self.client.get("/api/claims", params={"partial_subtype": "already_explained"}).json()
        self.assertEqual([item["claim_id"] for item in body["items"]], ["c3"])
        self.assertEqual(body["total"], 1)

    def test_a_sibling_subtype_is_not_swept_in(self):
        # Both rows are PARTIAL; only the subtype separates them, which is the
        # whole point of the filter.
        self.partials()
        body = self.client.get("/api/claims", params={"partial_subtype": "timing_mismatch"}).json()
        self.assertEqual([item["claim_id"] for item in body["items"]], ["c4"])

    def test_a_subtype_no_row_carries_returns_nothing_rather_than_everything(self):
        self.partials()
        body = self.client.get("/api/claims", params={"partial_subtype": "wrong_entity"}).json()
        self.assertEqual(body["items"], [])

    def test_it_composes_with_the_company_filter(self):
        self.partials()
        params = {"partial_subtype": "already_explained", "company": "wuxi_apptec"}
        self.assertEqual(self.client.get("/api/claims", params=params).json()["items"], [])

    def test_omitting_it_still_returns_everything(self):
        self.partials()
        body = self.client.get("/api/claims").json()
        self.assertEqual(body["total"], 4)


class InquiryChainTests(ApiTestCase):
    def test_a_claim_settled_by_an_inquiry_carries_the_whole_chain(self):
        detail = self.client.get("/api/claims/c1").json()
        chain = detail["inquiry"]
        self.assertEqual((chain["number"], chain["topic"]), (13, "募投项目"))
        self.assertEqual(chain["answer_page"], 165)
        self.assertEqual(chain["changes"][0]["diff_id"], "d1")
        # The shared vocabulary is what the pairing rests on, so it travels with
        # it: a reader who cannot see why two passages were linked cannot argue.
        self.assertEqual(chain["changes"][0]["shared_terms"], ["募投", "募集"])

    def test_a_claim_with_no_inquiry_says_so_rather_than_omitting_the_key(self):
        self.assertIsNone(self.client.get("/api/claims/c2").json()["inquiry"])

    def test_the_event_page_shows_the_chain_on_the_claim_that_has_one(self):
        claims = self.client.get("/api/events/e1").json()["claims"]
        self.assertEqual(claims[0]["inquiry"]["number"], 13)

    def test_a_question_that_produced_no_change_still_returns_its_chain(self):
        # Asked and answered, with the filing left as it was: a real outcome the
        # page has to state, not a reason to hide the chain.
        writer = sqlite3.connect(self.fixture_path)
        writer.execute("DELETE FROM inquiry_triplets")
        writer.commit()
        writer.close()
        chain = self.client.get("/api/claims/c1").json()["inquiry"]
        self.assertEqual(chain["changes"], [])


class DiffTests(ApiTestCase):
    def test_diffs_are_listed_with_their_figures(self):
        item = self.client.get("/api/diffs").json()["items"][0]
        self.assertEqual((item["old_value"], item["new_value"]), ("290", "295"))

    def test_facets_only_offer_values_that_exist(self):
        body = self.client.get("/api/diffs/facets").json()
        self.assertEqual([row["value"] for row in body["kinds"]], ["numeric"])


class IssueTests(ApiTestCase):
    def test_issue_detail_builds_a_timeline_with_its_time_basis(self):
        body = self.client.get("/api/issues/i1").json()
        self.assertEqual(body["timeline"][0]["time_basis"], "event_time")

    def test_issue_detail_summarises_verdicts(self):
        self.assertEqual(self.client.get("/api/issues/i1").json()["verdicts"], {"SUPPORTED": 1})

    def test_the_landing_panel_survives_a_database_missing_every_optional_stage(self):
        # This is the first screen, and it read six tables and one column
        # without checking that any of them existed. The shared fixture's
        # `processed_documents` predates `inclusion_status`, so the panel 500'd
        # on it — and nothing noticed, because the route had no test at all.
        # A card with no number is a state the page renders; a 500 is not.
        db = sqlite3.connect(self.fixture_path)
        for table in ("event_elements", "duplicate_clusters", "duplicate_cluster_members",
                      "event_timeline", "issue_ledger", "alerts"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.commit()
        db.close()
        response = self.client.get("/api/overview/pipeline")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["tasks"]), 6)   # six cards, whatever is missing
        self.assertEqual(body["top_issues"], [])
        for task in body["tasks"]:
            self.assertIsNotNone(task["headline"]["value"])

    def test_the_pipeline_ranks_issues_in_sql_not_in_the_browser(self):
        # The overview fetched a page of issues and sorted it client-side. That
        # is right only while the ledger is smaller than the page size:
        # /api/issues orders by risk score, so past the page limit the busiest
        # issues can fall off before the sort ever sees them. It is also the
        # page's own rule — every aggregate in SQL.
        body = self.client.get("/api/overview/pipeline").json()
        counts = [row["event_count"] for row in body["top_issues"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertLessEqual(len(body["top_issues"]), 10)

    def test_every_card_carries_a_rate_whose_parts_are_the_breakdown(self):
        # 质量率在接口里算好，页面只画。分子分母要能从同一张卡的 breakdown 里对上——
        # 否则同一张卡上的一根条和一张图各说各话。
        body = self.client.get("/api/overview/pipeline").json()
        # 流水线第一个节点「采集文档」与总览的文档数是同一个量。
        self.assertEqual(body["documents"], self.client.get("/api/overview").json()["documents"])
        for task in body["tasks"]:
            rate = task["rate"]
            self.assertTrue(rate["label"] and rate["basis"], task["key"])
            if rate["denominator"]:
                self.assertAlmostEqual(rate["value"], rate["numerator"] / rate["denominator"], places=3)
                self.assertTrue(0 <= rate["value"] <= 1)
            else:
                self.assertIsNone(rate["value"])      # 「还没有数据」不是「比例为 0」
        by_key = {task["key"]: task for task in body["tasks"]}
        verify = by_key["verify"]
        self.assertEqual(verify["rate"]["denominator"], sum(row["count"] for row in verify["breakdown"]))
        self.assertEqual(verify["rate"]["numerator"], sum(row["count"] for row in verify["breakdown"]
                                                          if row["value"] != "UNVERIFIABLE"))

    def test_a_stage_that_never_ran_leaves_a_rate_with_no_value_rather_than_zero(self):
        db = sqlite3.connect(self.fixture_path)
        db.execute("DROP TABLE IF EXISTS event_elements")
        db.commit()
        db.close()
        extract = next(task for task in self.client.get("/api/overview/pipeline").json()["tasks"]
                       if task["key"] == "extract")
        self.assertIsNone(extract["rate"]["value"])

    def test_the_status_trail_is_returned_newest_first_with_its_reason(self):
        # The ledger keeps only the current status word. `issue_status_history`
        # has recorded every change since the timeline stage was written and no
        # endpoint had ever read it, so a reader could see 「降温」 without being
        # able to see that it had been 「升温」 the week before.
        #
        # The rows are written here rather than into the shared fixture: adding
        # a table there reaches seven unrelated assertions.
        db = sqlite3.connect(self.fixture_path)
        db.execute("""CREATE TABLE issue_status_history(
            history_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, company_id TEXT NOT NULL,
            issue_code TEXT NOT NULL, status TEXT NOT NULL, previous_status TEXT,
            recent_event_count INTEGER NOT NULL, previous_event_count INTEGER NOT NULL,
            reason TEXT NOT NULL, round_id TEXT NOT NULL, changed_at TEXT NOT NULL)""")
        db.executemany("INSERT INTO issue_status_history VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            ("h1", "i1", "cxmt", "FUND_RAISE", "escalating", "emerging", 6, 1,
             "近 7 天 6 个事件，较上一轮 1 个", "r1", "2026-07-10"),
            ("h2", "i1", "cxmt", "FUND_RAISE", "cooling", "escalating", 1, 6,
             "近 7 天 1 个事件，较上一轮 6 个", "r2", "2026-07-19"),
        ])
        db.commit()
        db.close()
        trail = self.client.get("/api/issues/i1").json()["status_history"]
        self.assertEqual([row["status"] for row in trail], ["cooling", "escalating"])
        self.assertEqual(trail[0]["previous_status"], "escalating")
        self.assertIn("较上一轮 6 个", trail[0]["reason"])

    def test_the_status_trail_is_empty_rather_than_failing_before_the_stage_runs(self):
        self.assertEqual(self.client.get("/api/issues/i1").json()["status_history"], [])

    def test_unknown_issue_is_404(self):
        self.assertEqual(self.client.get("/api/issues/missing").status_code, 404)

    def test_the_total_counts_the_ledger_not_the_page(self):
        # It read len(rows), so a page of one made the ledger look like one.
        body = self.client.get("/api/issues?limit=1").json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["total"], 2)

    def test_the_total_still_answers_to_the_filter(self):
        self.assertEqual(self.client.get("/api/issues?company=cxmt").json()["total"], 1)


class AlertTests(ApiTestCase):
    def test_alerts_lead_with_the_adjusted_score(self):
        items = self.client.get("/api/alerts").json()["items"]
        self.assertEqual([item["alert_id"] for item in items], ["a1", "a2"])
        self.assertEqual(items[0]["adjusted_risk_score"], 71.5)

    def test_every_alert_carries_its_rules_and_phase_coefficient(self):
        # An alert a reader cannot argue with is not evidence, it is an assertion.
        item = self.client.get("/api/alerts").json()["items"][0]
        self.assertEqual([reason["code"] for reason in item["reasons"]], ["high_adjusted_risk"])
        self.assertEqual((item["risk_score"], item["phase_coefficient"]), (55.0, 1.3))

    def test_reason_filter_selects_one_rule(self):
        body = self.client.get("/api/alerts", params={"reason": "issue_escalating"}).json()
        self.assertEqual([item["alert_id"] for item in body["items"]], ["a2"])

    def test_facets_tally_reasons_across_alerts_not_rows(self):
        # One alert can carry several rules, so the counts come from the arrays.
        facets = self.client.get("/api/alerts/facets").json()
        self.assertEqual({row["value"]: row["count"] for row in facets["reasons"]},
                         {"high_adjusted_risk": 1, "issue_escalating": 1})
        self.assertEqual([row["value"] for row in facets["issues"]], ["FUND_RAISE"])

    def test_detail_carries_its_evidence_and_merged_events(self):
        body = self.client.get("/api/alerts/a1").json()
        self.assertEqual(body["evidence"][0]["evidence_page"], 164)
        self.assertEqual([event["event_id"] for event in body["events"]], ["e1"])
        self.assertEqual(body["credibility_basis"]["source_tier"], "S2")

    def test_unknown_alert_is_404(self):
        self.assertEqual(self.client.get("/api/alerts/missing").status_code, 404)


class TimelineTests(ApiTestCase):
    def test_entries_show_both_scores_and_the_coefficient_between_them(self):
        item = self.client.get("/api/timeline", params={"company": "cxmt"}).json()["items"][0]
        self.assertEqual((item["base_risk_score"], item["phase_coefficient"],
                          item["adjusted_risk_score"]), (55.0, 1.3, 71.5))

    def test_entries_name_the_clock_they_used(self):
        items = self.client.get("/api/timeline").json()["items"]
        self.assertEqual({item["event_id"]: item["time_basis"] for item in items},
                         {"e1": "event_time", "e2": "published_at"})

    def test_new_facts_filter_keeps_only_events_that_added_something(self):
        body = self.client.get("/api/timeline", params={"new_facts_only": True}).json()
        self.assertEqual([item["event_id"] for item in body["items"]], ["e1"])

    def test_a_company_without_phase_windows_is_listed_as_unphased(self):
        # Silently dropping it would make the phase counts disagree with the ledger.
        items = self.client.get("/api/timeline/phases").json()["items"]
        by_phase = {item["phase"]: item for item in items}
        self.assertEqual(by_phase["ISSUANCE"]["events"], 1)
        self.assertEqual(by_phase["UNKNOWN"]["coefficient"], 1.0)
        self.assertIn("没有发行阶段窗口", by_phase["UNKNOWN"]["rationale"])

    def test_rounds_are_newest_first(self):
        items = self.client.get("/api/timeline/rounds").json()["items"]
        self.assertEqual([item["ordinal"] for item in items], sorted((item["ordinal"] for item in items), reverse=True))
        self.assertNotIn("round_id", items[0])
        self.assertTrue(items[-1]["is_baseline"])


class PropagationTests(ApiTestCase):
    def test_single_version_chains_are_excluded_by_default(self):
        body = self.client.get("/api/propagation").json()
        self.assertEqual([item["component_id"] for item in body["items"]], ["g1"])
        self.assertEqual(self.client.get("/api/propagation",
                                         params={"min_members": 1}).json()["total"], 2)

    def test_a_version_resolves_to_the_chain_containing_it(self):
        body = self.client.get("/api/propagation", params={"version": "v2"}).json()
        self.assertEqual([item["component_id"] for item in body["items"]], ["g1"])

    def test_detail_lists_members_by_depth_and_every_edge_touching_them(self):
        body = self.client.get("/api/propagation/g1").json()
        self.assertEqual([(m["version_id"], m["depth"]) for m in body["members"]],
                         [("v1", 0), ("v2", 1)])
        # 源于披露 never merges chains, but it still explains where one started.
        self.assertEqual(sorted(edge["edge_kind"] for edge in body["edges"]),
                         ["FROM_DISCLOSURE", "REPOST"])

    def test_stats_separate_copying_from_mere_citation(self):
        body = self.client.get("/api/propagation/stats").json()
        self.assertEqual({row["value"]: row["count"] for row in body["origins"]},
                         {"media": 1, "disclosure": 1})
        self.assertEqual((body["multi_member"], body["largest"]), (1, 2))

    def test_unknown_component_is_404(self):
        self.assertEqual(self.client.get("/api/propagation/missing").status_code, 404)


class ReviewTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        import api.review_db as review_db
        # One file, two connections — exactly as in production: the analytical
        # side opens it read-only, the review side is the only writer.
        self._original_db_path = review_db.DB_PATH
        review_db.DB_PATH = self.fixture_path
        review_db.connect().close()

    def tearDown(self):
        import api.review_db as review_db
        review_db.DB_PATH = self._original_db_path
        super().tearDown()


class ReviewTests(ReviewTestCase):
    def seed(self, kind="verdict", origin="evaluation_sample", count=10):
        return self.client.post("/api/review/seed",
                                json={"kind": kind, "origin": origin, "count": count})

    def test_seeding_a_verdict_queue_covers_every_verdict_not_just_the_common_one(self):
        # A flat sample of these fixtures would return only UNVERIFIABLE.
        self.assertEqual(self.seed().status_code, 200)
        kinds = self.client.get("/api/review/tasks").json()["items"]
        verdicts = {item["target"]["verdict"] for item in kinds}
        self.assertEqual(verdicts, {"SUPPORTED", "UNVERIFIABLE"})

    def test_seeding_twice_does_not_open_two_tasks_for_one_claim(self):
        self.seed()
        first = self.client.get("/api/review/tasks").json()["total"]
        self.assertEqual(self.seed().json()["created"], 0)
        self.assertEqual(self.client.get("/api/review/tasks").json()["total"], first)

    def test_a_queue_item_carries_what_is_being_judged(self):
        self.seed()
        item = self.client.get("/api/review/tasks").json()["items"][0]
        self.assertIn("text", item["target"])
        self.assertEqual(item["origin_label"], "人工评测抽样")

    def test_a_judgement_is_recorded_without_touching_the_verdict(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        before = self.client.get(f"/api/claims/{task['target_id']}").json()["verdict"]
        response = self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                                    json={"judgement": "wrong", "note": "证据页码指向错误章节"})
        self.assertEqual(response.status_code, 200)
        after = self.client.get(f"/api/claims/{task['target_id']}").json()["verdict"]
        # The pipeline's conclusion must survive being disagreed with.
        self.assertEqual(before, after)
        item = [row for row in self.client.get("/api/review/tasks").json()["items"]
                if row["task_id"] == task["task_id"]][0]
        self.assertEqual(item["status"], "judged")
        self.assertEqual(item["decisions"][0]["judgement"], "wrong")

    def test_an_event_queue_holds_events_whichever_origin_filled_it(self):
        # Origins the event branch did not name fell through to the diff query,
        # so an event queue filled with diff ids that resolve to nothing.
        for origin in ("needs_review", "evaluation_sample"):
            with self.subTest(origin=origin):
                self.assertEqual(self.seed(kind="event", origin=origin).status_code, 200)
        items = self.client.get("/api/review/tasks?kind=event").json()["items"]
        self.assertTrue(items)
        for item in items:
            self.assertIsNotNone(item["target"], item["target_id"])
            self.assertIn("summary", item["target"])

    def test_a_judgement_records_what_the_system_had_said_at_the_time(self):
        # Accuracy per verdict class cannot be reported without it, and a later
        # re-run may change the verdict the judgement was made about.
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                         json={"judgement": "correct"})
        item = [row for row in self.client.get("/api/review/tasks").json()["items"]
                if row["task_id"] == task["task_id"]][0]
        self.assertEqual(item["decisions"][0]["system_verdict"], task["target"]["verdict"])

    def test_open_tasks_are_listed_before_settled_ones(self):
        self.seed()
        tasks = self.client.get("/api/review/tasks").json()["items"]
        self.client.post(f"/api/review/tasks/{tasks[0]['task_id']}/judge",
                         json={"judgement": "correct"})
        self.assertEqual([row["status"] for row in
                          self.client.get("/api/review/tasks").json()["items"]],
                         ["open", "judged"])

    def test_accuracy_is_reported_with_its_sample_size(self):
        self.seed()
        tasks = self.client.get("/api/review/tasks").json()["items"]
        for task, judgement in zip(tasks, ["correct", "wrong"]):
            self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                             json={"judgement": judgement})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        self.assertEqual((body["sample_size"], body["accuracy"], body["judged"]), (2, 0.5, 2))

    def test_an_unsure_judgement_is_counted_but_kept_out_of_the_rate(self):
        self.seed()
        tasks = self.client.get("/api/review/tasks").json()["items"]
        self.client.post(f"/api/review/tasks/{tasks[0]['task_id']}/judge",
                         json={"judgement": "correct"})
        self.client.post(f"/api/review/tasks/{tasks[1]['task_id']}/judge",
                         json={"judgement": "unsure"})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        self.assertEqual((body["judged"], body["sample_size"], body["accuracy"]), (2, 1, 1.0))

    def test_a_revised_judgement_replaces_the_earlier_one_in_the_rate(self):
        # Both decisions stay in the log, but a task counts once, as its latest.
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]["task_id"]
        self.client.post(f"/api/review/tasks/{task}/judge", json={"judgement": "wrong"})
        self.client.post(f"/api/review/tasks/{task}/judge", json={"judgement": "correct"})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        self.assertEqual((body["judged"], body["accuracy"]), (1, 1.0))
        item = [row for row in self.client.get("/api/review/tasks").json()["items"]
                if row["task_id"] == task][0]
        self.assertEqual(len(item["decisions"]), 2)

    def test_a_wrong_judgement_carries_what_the_answer_should_have_been(self):
        # Accuracy needs only "wrong"; a confusion matrix needs the right class.
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        response = self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                                    json={"judgement": "wrong", "correct_label": "CONFLICT"})
        self.assertEqual(response.status_code, 200)
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        said = task["target"]["verdict"]
        self.assertEqual(body["confusion"][said]["CONFLICT"], 1)
        self.assertEqual(body["labelled"], 1)

    def test_macro_f1_averages_the_classes_instead_of_the_samples(self):
        self.seed()
        tasks = self.client.get("/api/review/tasks").json()["items"]
        for task in tasks:
            self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                             json={"judgement": "correct"})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        # Everything the system said was upheld, so every judged class scores 1.
        self.assertEqual(body["macro_f1"], 1.0)
        self.assertTrue(all(row["f1"] == 1.0 for row in body["per_class"].values()))

    def test_a_wrong_without_a_label_is_counted_so_the_gap_is_visible(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        self.client.post(f"/api/review/tasks/{task['task_id']}/judge", json={"judgement": "wrong"})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        self.assertEqual((body["wrong_without_label"], body["labelled"]), (1, 0))
        self.assertIsNone(body["macro_f1"])

    def test_evidence_location_is_measured_apart_from_the_verdict(self):
        # §11.3 states the two targets separately: four-value accuracy and
        # evidence-location accuracy.  One judgement must not answer both.
        self.seed()
        tasks = self.client.get("/api/review/tasks").json()["items"]
        self.client.post(f"/api/review/tasks/{tasks[0]['task_id']}/judge",
                         json={"judgement": "correct", "evidence_ok": True})
        self.client.post(f"/api/review/tasks/{tasks[1]['task_id']}/judge",
                         json={"judgement": "correct", "evidence_ok": False})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        self.assertEqual(body["accuracy"], 1.0)
        located = body["evidence_location"]
        self.assertEqual({key: located[key] for key in ("judged", "correct", "accuracy")},
                         {"judged": 2, "correct": 1, "accuracy": 0.5})

    def test_a_judgement_without_an_evidence_call_stays_out_of_that_rate(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        self.client.post(f"/api/review/tasks/{task['task_id']}/judge", json={"judgement": "correct"})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        self.assertIsNone(body["evidence_location"])

    def test_a_label_from_another_kind_is_refused(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        response = self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                                    json={"judgement": "wrong", "correct_label": "high"})
        self.assertEqual(response.status_code, 400)

    def test_a_right_answer_is_only_accepted_against_a_wrong_one(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        response = self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                                    json={"judgement": "correct", "correct_label": "CONFLICT"})
        self.assertEqual(response.status_code, 400)

    def test_a_partial_subtype_is_accepted_as_the_right_answer(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        response = self.client.post(
            f"/api/review/tasks/{task['task_id']}/judge",
            json={"judgement": "wrong", "correct_label": "PARTIAL/timing_mismatch"})
        self.assertEqual(response.status_code, 200)
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        # The subtype is recorded, but the matrix is stated over the four values.
        self.assertEqual(body["confusion"][task["target"]["verdict"]]["PARTIAL"], 1)

    def test_the_review_vocabulary_matches_the_processor(self):
        # The API process must not import the processor, so the two copies of the
        # subtype list are kept in step by this test rather than by an import.
        from api import review_db
        from processor import claim_verdict
        self.assertEqual(set(review_db.PARTIAL_SUBTYPES), set(claim_verdict.PARTIAL_SUBTYPES))
        self.assertEqual(set(review_db.CORRECT_LABELS["verdict"]), set(claim_verdict.VERDICTS))

    def test_rejecting_a_task_takes_a_reason_and_not_a_judgement(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        response = self.client.post(f"/api/review/tasks/{task['task_id']}/reject",
                                    json={"note": "证据页码指向错误章节"})
        self.assertEqual(response.status_code, 200)
        item = [row for row in self.client.get("/api/review/tasks").json()["items"]
                if row["task_id"] == task["task_id"]][0]
        self.assertEqual(item["status"], "rejected")

    def test_an_unknown_judgement_is_refused(self):
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        response = self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                                    json={"judgement": "maybe"})
        self.assertEqual(response.status_code, 400)

    def test_judging_an_unknown_task_is_404(self):
        response = self.client.post("/api/review/tasks/missing/judge", json={"judgement": "correct"})
        self.assertEqual(response.status_code, 404)


class VocabularyTests(ApiTestCase):
    def test_every_declared_code_carries_its_usage_count(self):
        body = self.client.get("/api/vocabularies").json()
        issues = {term["code"]: term for term in body["issue_code"]["terms"]}
        self.assertEqual(issues["FUND_RAISE"]["used"], 1)
        self.assertEqual(issues["LOCKUP"]["used"], 0)
        self.assertEqual(issues["FUND_RAISE"]["label"], "募资")

    def test_a_json_vocabulary_is_counted_across_its_arrays(self):
        body = self.client.get("/api/vocabularies").json()
        objects = {term["code"]: term["used"] for term in body["impact_object"]["terms"]}
        self.assertEqual(objects["investors_stock_price"], 1)

    def test_a_value_outside_the_vocabulary_is_shown_as_a_defect(self):
        # Either the extractor invented a code or the vocabulary moved under
        # stored rows; both have to be visible rather than rendered as normal.
        writer = sqlite3.connect(self.fixture_path)
        writer.execute("UPDATE event_claims SET issue_code='NOT_A_CODE' WHERE claim_id='c1'")
        writer.commit()
        writer.close()
        undeclared = self.client.get("/api/vocabularies").json()["issue_code"]["undeclared"]
        self.assertEqual(undeclared, [["NOT_A_CODE", 1]])

    def test_the_vocabularies_match_the_processor_that_writes_them(self):
        from api.labels import VOCABULARIES
        from processor.pipeline import (EVENT_TYPES, IMPACT_DIRECTIONS, IMPACT_MAGNITUDES,
                                        IMPACT_OBJECTS, ISSUE_RULES, PRODUCTION_MODES)
        for key, codes in (("event_type", EVENT_TYPES), ("issue_code", ISSUE_RULES),
                           ("impact_object", IMPACT_OBJECTS),
                           ("production_mode", PRODUCTION_MODES),
                           ("impact_direction", IMPACT_DIRECTIONS),
                           ("impact_magnitude", IMPACT_MAGNITUDES)):
            self.assertEqual(set(VOCABULARIES[key]["labels"]), set(codes), key)

    def test_every_term_carries_its_definition_not_only_its_name(self):
        # 「TECH_LEVEL / 技术水平 / 912」 cannot answer 「技术水平和业务拓展怎么分」,
        # and this page is the only one that claims to explain the vocabularies.
        body = self.client.get("/api/vocabularies").json()
        for key in ("event_type", "issue_code", "impact_object", "production_mode",
                    "impact_direction", "impact_magnitude"):
            for term in body[key]["terms"]:
                with self.subTest(vocabulary=key, code=term["code"]):
                    self.assertTrue(term.get("definition"), f'{key}.{term["code"]} 没有定义')

    def test_the_two_vocabularies_with_a_precedence_chain_expose_it(self):
        # The chain is the half that says 「什么情况下不要用这个类」, which is where the
        # measured gain came from (交付物11 §4.6: 0.3997 -> 0.4211).
        # I-06: the judging rules are the labeller's prompt text; definitions stay, rules do not.
        body = self.client.get("/api/vocabularies").json()
        self.assertNotIn("judging_rules", body["event_type"])
        self.assertNotIn("source", body["event_type"])
        self.assertTrue(any(term["definition"] for term in body["issue_code"]["terms"]))

    def test_the_impact_objects_arrive_grouped(self):
        body = self.client.get("/api/vocabularies").json()
        groups = body["impact_object"]["groups"]
        self.assertEqual([group["group"] for group in groups],
                         ["公司内部", "主体关系", "市场关系", "外部关系", "外溢"])
        by_code = {term["code"]: term["group"] for term in body["impact_object"]["terms"]}
        self.assertEqual(by_code["regional_economy"], "外溢")

    def test_company_scope_is_read_from_configuration_with_what_was_collected(self):
        body = self.client.get("/api/companies").json()
        companies = {row["company_id"]: row for row in body["companies"]}
        self.assertIn("cxmt", companies)
        self.assertEqual(companies["cxmt"]["events"], 1)
        self.assertTrue(companies["cxmt"]["sources"])
        self.assertIsNotNone(body["last_reviewed"])


class MarketDataExclusionTests(ApiTestCase):
    """纯行情数据不进风险分布，但排除数要报出来。

    判定不是一个新标签：事件类型层判为「一类都不贴合」就是它——12 类按风险类型立目，
    单日成交额落不进任何一格。事件仍在台账与传播图里（469 条因果边靠它们），
    只是不再算作一条风险。
    """

    def test_a_typeless_event_leaves_the_risk_trend_and_is_counted_aside(self):
        writer = sqlite3.connect(self.fixture_path)
        writer.execute("UPDATE event_records SET event_types_json='[]' WHERE event_id='e1'")
        # fixture 已有 e1 的时间线行；把它挪到窗口内并给一个高档位，这条断言才有东西可画。
        writer.execute("""UPDATE event_timeline SET timeline_at=date('now'),
            adjusted_risk_level='high', adjusted_risk_score=90.0 WHERE event_id='e1'""")
        writer.execute("DELETE FROM event_timeline WHERE event_id<>'e1'")
        writer.commit()
        writer.close()
        body = self.client.get("/api/overview/trend").json()
        self.assertEqual(body["excluded_market_data"], 1)
        drawn = sum(sum(series["points"]) for series in body["series"])
        self.assertEqual(drawn, 0, "无事件类型的事件不应画进风险分布")


class ClassificationBasisTests(ApiTestCase):
    """Why each narrow classifier answered as it did, on the detail page.

    Four labels tables have carried ``reason / confidence / basis / model_name /
    prompt_version`` per event since they were written, and ``/profiling`` shows
    them; the event detail page — the first screen of the whole platform — never
    joined them, so 「疑似宣发」 and 「负面语气」 appeared as bare words.  An outside
    reader called that 「依据太笼统」, and they were right.
    """

    def test_a_judgement_with_a_labels_row_carries_its_reason(self):
        writer = sqlite3.connect(self.fixture_path)
        writer.execute("""CREATE TABLE production_mode_labels(event_id TEXT PRIMARY KEY,
            company_id TEXT, mode TEXT, reason TEXT, confidence REAL, basis TEXT,
            model_name TEXT, prompt_version TEXT, classified_at TEXT)""")
        writer.execute("""INSERT INTO production_mode_labels VALUES('e1','cxmt','SPONSORED',
            '通篇引用公司口径、无独立信源，末尾附联系方式',0.82,'model','qwen-plus',
            'production-mode-v1','2026-09-06T00:00:00+00:00')""")
        writer.commit()
        writer.close()
        basis = self.client.get("/api/events/e1").json()["basis"]["production_mode"]
        self.assertIn("无独立信源", basis["reason"])
        self.assertEqual(basis["confidence"], 0.82)
        self.assertEqual(basis["basis"], "system")
        self.assertNotIn("model_name", basis)
        self.assertNotIn("prompt_version", basis)

    def test_an_unjudged_axis_is_null_rather_than_blank(self):
        # production_mode_labels covers 1,033 of 1,907 events because
        # announcements never enter that pass.  A page that cannot tell 「未判」
        # from 「判了但没给理由」 shows an empty space where a reason belongs.
        basis = self.client.get("/api/events/e1").json()["basis"]
        self.assertEqual(set(basis), {"event_type", "event_impact", "production_mode",
                                      "article_sentiment"})
        self.assertIsNone(basis["event_type"])

    def test_the_risk_score_arrives_with_the_terms_it_is_made_of(self):
        body = self.client.get("/api/events/e1").json()
        parts = body["risk_breakdown"]
        self.assertEqual(round(sum(part["points"] for part in parts), 1), body["risk_score"])
        self.assertEqual(parts[0]["key"], "base")

    def test_whether_a_rule_or_a_model_decided_reaches_the_page(self):
        self.assertEqual(self.client.get("/api/events/e1").json()["label_source"], "system")
        self.assertEqual(self.client.get("/api/events/e2").json()["label_source"], "rule")


class ReportExportTests(ApiTestCase):
    def document(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        return response, archive.read("word/document.xml").decode()

    def test_an_event_report_covers_every_required_output(self):
        # The brief names the outputs a result must carry; a screen layout can
        # satisfy the eye while dropping one, so the structure is asserted.
        _, document = self.document("/api/reports/events/e1.docx")
        for heading in ("一、事件摘要", "二、事件主体", "三、影响对象", "四、影响方向与程度",
                        "五、可信度与信源", "六、证据来源", "七、事件时间线与阶段",
                        "八、预警理由与建议动作"):
            self.assertIn(heading, document)

    def test_it_is_a_word_file_a_browser_will_save(self):
        response, _ = self.document("/api/reports/events/e1.docx")
        self.assertIn("wordprocessingml", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_a_missing_section_says_so_instead_of_disappearing(self):
        _, document = self.document("/api/reports/events/e2.docx")
        self.assertIn("（本条尚无数据）", document)

    def test_cited_evidence_is_tabled_with_its_page(self):
        _, document = self.document("/api/reports/events/e1.docx")
        self.assertIn("<w:tbl>", document)
        self.assertIn("164", document)

    def test_an_inquiry_that_settled_a_claim_is_named_in_the_report(self):
        _, document = self.document("/api/reports/events/e1.docx")
        self.assertIn("问询第 13 问", document)

    def test_an_issue_report_lists_its_events(self):
        _, document = self.document("/api/reports/issues/i1.docx")
        self.assertIn("一、专题状态", document)
        self.assertIn("四、事件清单", document)

    def test_an_issue_report_carries_the_status_trail_or_says_there_is_none(self):
        # The trail is stored history rather than a derived recommendation, so
        # it belongs in a printed record: it will read the same a year from now.
        _, document = self.document("/api/reports/issues/i1.docx")
        self.assertIn("三、状态变化", document)
        self.assertIn("状态自首次判定以来没有变过。", document)

    def test_a_risk_briefing_leads_with_the_conclusion_not_with_the_article(self):
        # 事件报告回答「这一篇稿子讲了什么」，风险简报回答「这条风险现在怎么样了、
        # 谁在管、依据是什么」。两份材料的顺序不同，因为它们回答的不是同一个问题。
        _, document = self.document("/api/reports/risks/a1.docx")
        for heading in ("一、研判结论", "二、处置与责任人", "三、预警理由", "四、证据核验",
                        "五、多源信息", "六、传播脉络", "七、披露变化", "八、处置流水"):
            self.assertIn(heading, document)

    def test_an_untouched_risk_says_it_has_no_trail_rather_than_showing_none(self):
        _, document = self.document("/api/reports/risks/a1.docx")
        self.assertIn("这条事项还没有人处置过，因此没有流水。", document)

    def test_the_briefing_never_folds_the_two_confidences_into_one(self):
        # 事件可信度与模型置信度是两个数——这份材料要跟着这句话一起下发。
        _, document = self.document("/api/reports/risks/a1.docx")
        self.assertIn("不合成一个", document)

    def test_a_downloadable_briefing_never_prints_an_enum_at_the_reader(self):
        # 「风险 high（100.0）」曾经就这么印在一份可下发的材料上。
        _, document = self.document("/api/reports/risks/a1.docx")
        self.assertIn("高风险", document)
        self.assertNotIn("风险 high", document)

    def test_the_propagation_table_says_the_role_in_words(self):
        # 「CORRECTION」曾经就这么出现在传播脉络那一栏里。
        _, document = self.document("/api/reports/risks/a1.docx")
        self.assertNotIn("CORRECTION", document)

    def test_an_unknown_risk_has_no_briefing(self):
        self.assertEqual(self.client.get("/api/reports/risks/nope.docx").status_code, 404)

    def test_an_unknown_event_has_no_report(self):
        self.assertEqual(self.client.get("/api/reports/events/nope.docx").status_code, 404)

    def test_an_export_leaves_a_line_in_the_audit_trail(self):
        # 方案 §7.2：导出或上报行为须记录操作者、时间、版本和目标。一份材料离开平台
        # 是这个只读系统里为数不多的对外动作之一，而在此之前它一行都没留下。
        self.document("/api/reports/risks/a1.docx")
        rows = self.client.get("/api/audit/actions").json()["items"]
        exports = [row for row in rows if row["action"] == "report_export"]
        self.assertEqual(len(exports), 1)
        self.assertEqual((exports[0]["actor"], exports[0]["object_kind"]), ("tester", "risk"))
        # The object is named, not identified by its internal id (I-06).
        self.assertNotIn("object_id", exports[0])
        self.assertTrue(exports[0]["object_label"])
        self.assertEqual(exports[0]["outcome"], "ok")

    def test_three_kinds_of_export_are_told_apart_in_the_trail(self):
        # 「导出过什么」里最要紧的是那个 id 与它是哪一类材料——三份材料回答的不是
        # 同一个问题，记成同一种动作就查不出是谁把哪一条发出去的。
        self.document("/api/reports/events/e1.docx")
        self.document("/api/reports/issues/i1.docx")
        self.document("/api/reports/risks/a1.docx")
        rows = self.client.get("/api/audit/actions").json()["items"]
        kinds = {row["object_kind_label"] for row in rows if row["action"] == "report_export"}
        self.assertEqual(kinds, {"事件报告", "专题报告", "风险事项报告"})

    def test_a_failed_export_does_not_claim_to_have_happened(self):
        # 取不到数的请求在成文之前就抛掉了，不该在流水里留下一条「导出成功」。
        self.assertEqual(self.client.get("/api/reports/risks/nope.docx").status_code, 404)
        rows = self.client.get("/api/audit/actions").json()["items"]
        self.assertEqual([row for row in rows if row["action"] == "report_export"], [])

    def test_a_signed_out_visitor_cannot_export(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/reports/events/e1.docx").status_code, 401)

    def test_every_impact_object_has_a_label_a_reader_can_read(self):
        # An unlabelled code reaches a committee as English shorthand.
        from api.labels import IMPACT_OBJECT_LABELS
        from processor.pipeline import IMPACT_OBJECTS
        self.assertEqual(set(IMPACT_OBJECTS) - set(IMPACT_OBJECT_LABELS), set())


class AuditTests(ReviewTestCase):
    def test_processing_runs_are_listed_newest_first(self):
        items = self.client.get("/api/audit/runs").json()["items"]
        self.assertTrue(items)
        self.assertEqual([row["started_at"] for row in items],
                         sorted((row["started_at"] for row in items), reverse=True))

    def test_a_run_shows_a_category_and_a_reference_not_its_internals(self):
        # I-06: the raw error named the model and the schema; the run id named the prompt.
        row = self.client.get("/api/audit/runs").json()["items"][0]
        self.assertRegex(row["reference"], r"^B-[0-9a-f]{6}$")
        for hidden in ("run_id", "processor_version", "options", "options_json", "error_message"):
            self.assertNotIn(hidden, row)

    def test_a_judgement_appears_in_the_trail_with_what_the_system_had_said(self):
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 1})
        task = self.client.get("/api/review/tasks").json()["items"][0]
        self.client.post(f"/api/review/tasks/{task['task_id']}/judge",
                         json={"judgement": "wrong", "correct_label": "CONFLICT",
                               "note": "证据页码指向错误章节"})
        entry = self.client.get("/api/audit/decisions").json()["items"][0]
        self.assertEqual((entry["judgement"], entry["correct_label"]), ("wrong", "CONFLICT"))
        self.assertEqual(entry["system_verdict"], task["target"]["verdict"])
        self.assertEqual(entry["kind"], "verdict")

    def test_the_model_and_prompt_inventory_is_no_longer_published(self):
        # I-06: which model and prompt produced what stays on the server.
        self.assertNotIn(self.client.get("/api/audit/versions").status_code, (200,))

    def test_the_audit_page_cannot_write(self):
        # An audit trail a page can append to is not an audit trail.
        self.assertEqual(self.client.post("/api/audit/runs").status_code, 405)

    def test_a_signed_out_visitor_sees_no_audit_trail(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/audit/runs").status_code, 401)


class NotificationTests(ReviewTestCase):
    """The inbox mirrors the alerts and the queue; read state is per person."""

    def setUp(self):
        super().setUp()
        import api.notify_db as notify_db
        self._notify_path = notify_db.DB_PATH
        notify_db.DB_PATH = self.fixture_path

    def tearDown(self):
        import api.notify_db as notify_db
        notify_db.DB_PATH = self._notify_path
        super().tearDown()

    def test_a_high_alert_arrives_and_a_low_one_does_not(self):
        # An inbox that carries everything is an inbox nobody reads; low alerts
        # stay on their own page.
        items = self.client.get("/api/notifications").json()["items"]
        refs = {item["ref_id"] for item in items}
        self.assertIn("a1", refs)
        self.assertNotIn("a2", refs)

    def test_syncing_twice_does_not_deliver_the_same_alert_twice(self):
        first = self.client.get("/api/notifications").json()
        second = self.client.get("/api/notifications").json()
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(second["new_since_last_read"], 0)

    def test_an_open_review_task_is_waiting_in_the_inbox(self):
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 2})
        kinds = {item["kind"] for item in self.client.get("/api/notifications").json()["items"]}
        self.assertIn("review_task", kinds)

    def test_a_judged_task_stops_waiting(self):
        # Otherwise the unread count is a number nobody can bring down.
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 2})
        task = self.client.get("/api/review/tasks").json()["items"][0]
        self.client.post(f"/api/review/tasks/{task['task_id']}/judge", json={"judgement": "correct"})
        refs = {item["ref_id"] for item in self.client.get("/api/notifications").json()["items"]}
        self.assertNotIn(task["task_id"], refs)

    def test_reading_is_recorded_for_the_person_who_read_it(self):
        body = self.client.get("/api/notifications").json()
        self.assertEqual(body["unread"], body["total"])
        self.client.post("/api/notifications/read", json={})
        self.assertEqual(self.client.get("/api/notifications").json()["unread"], 0)
        # A second person has read nothing, whatever the first one did.
        self.become("someone-else", "analyst")
        self.assertEqual(self.client.get("/api/notifications").json()["unread"],
                         body["total"])

    def test_one_notification_can_be_marked_without_the_rest(self):
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 2})
        body = self.client.get("/api/notifications").json()
        self.client.post("/api/notifications/read",
                         json={"notification_ids": [body["items"][0]["notification_id"]]})
        after = self.client.get("/api/notifications").json()
        self.assertEqual(after["unread"], body["total"] - 1)

    def test_the_unread_filter_returns_only_what_is_unread(self):
        body = self.client.get("/api/notifications").json()
        self.client.post("/api/notifications/read",
                         json={"notification_ids": [body["items"][0]["notification_id"]]})
        unread = self.client.get("/api/notifications", params={"unread": True}).json()
        self.assertEqual(unread["total"], body["total"] - 1)

    def test_a_signed_out_visitor_has_no_inbox(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/notifications").status_code, 401)


class ReviewRoleTests(ReviewTestCase):
    def test_a_viewer_cannot_write_into_the_review_log(self):
        self.become("looker", "viewer")
        self.assertEqual(self.client.post("/api/review/seed",
                                          json={"kind": "verdict", "origin": "evaluation_sample"}
                                          ).status_code, 403)
        self.assertEqual(self.client.post("/api/review/tasks/any/judge",
                                          json={"judgement": "correct"}).status_code, 403)

    def test_a_viewer_can_still_read_the_queue_and_the_metrics(self):
        self.become("looker", "viewer")
        self.assertEqual(self.client.get("/api/review/tasks").status_code, 200)
        self.assertEqual(self.client.get("/api/review/metrics").status_code, 200)

    def test_an_analyst_may_judge_but_not_approve(self):
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 4})
        task = self.client.get("/api/review/tasks").json()["items"][0]["task_id"]
        self.become("wei", "analyst")
        self.assertEqual(self.client.post(f"/api/review/tasks/{task}/judge",
                                          json={"judgement": "correct"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/review/tasks/{task}/approve").status_code, 403)

    def test_a_reviewer_closes_the_task_and_the_log_names_both_people(self):
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 4})
        task = self.client.get("/api/review/tasks").json()["items"][0]["task_id"]
        self.become("wei", "analyst")
        self.client.post(f"/api/review/tasks/{task}/judge", json={"judgement": "correct"})
        self.become("chen", "reviewer")
        self.assertEqual(self.client.post(f"/api/review/tasks/{task}/approve").status_code, 200)
        item = [row for row in self.client.get("/api/review/tasks").json()["items"]
                if row["task_id"] == task][0]
        self.assertEqual(item["status"], "approved")
        self.assertEqual([(entry["reviewer"], entry["action"]) for entry in item["decisions"]],
                         [("wei", "judge"), ("chen", "approve")])


class SearchTests(ApiTestCase):
    """全局检索：一个不必先猜「这东西属于哪一页」的入口。"""

    def setUp(self):
        super().setUp()
        # 一条真的写着 50% 的主张。放在这里而不是共享 fixture 里：别的测试在数
        # 主张的条数，多一条会让它们无端变红。
        db = sqlite3.connect(self.fixture_path)
        db.execute("""INSERT INTO event_claims VALUES('c3','e1',1,'毛利率下降50%左右',
            'FINANCIAL','fact',0.7)""")
        db.commit()
        db.close()

    def groups(self, **params):
        body = self.client.get("/api/search", params=params).json()
        return {group["kind"]: group for group in body["groups"]}

    def test_every_group_answers_and_the_order_is_fixed(self):
        # 组的顺序是申明的，不是按相关性排的——结构化组是子串命中、没有分数，
        # 披露原文是 bm25，两者折算成一个可比的数就是编数字。
        body = self.client.get("/api/search", params={"q": "募资"}).json()
        self.assertEqual([group["kind"] for group in body["groups"]],
                         ["event", "claim", "issue", "alert", "diff", "document", "evidence"])
        # 组名与它通往的那一页同名：在这里读到「专题」、点进去是「专题跟踪」，
        # 而不是在这里读到「议题」、点进去发现这套系统有两个词表。
        self.assertEqual([group["label"] for group in body["groups"]][:4],
                         ["事件", "核验点", "专题", "风险事项"])

    def test_every_more_link_goes_somewhere_that_still_exists(self):
        # 「查看全部风险事项」此前指向 /alerts——那个页面在业务化重组时退役了，
        # 而检索页是唯一还在用它的地方，改名这一趟才把它照出来。
        body = self.client.get("/api/search", params={"q": "募资"}).json()
        paths = {group["more"]["path"] for group in body["groups"] if group.get("more")}
        self.assertNotIn("/alerts", paths)
        self.assertIn("/risks", paths)

    def test_a_term_finds_the_thing_wherever_the_platform_filed_it(self):
        groups = self.groups(q="募资")
        self.assertEqual([item["event_id"] for item in groups["event"]["items"]], ["e1"])
        self.assertEqual([item["claim_id"] for item in groups["claim"]["items"]], ["c1"])
        self.assertEqual([item["alert_id"] for item in groups["alert"]["items"]], ["a1"])

    def test_the_issue_group_matches_the_chinese_label_nobody_types_the_code(self):
        groups = self.groups(q="募资")
        self.assertEqual([item["issue_code"] for item in groups["issue"]["items"]], ["FUND_RAISE"])
        self.assertEqual(groups["issue"]["items"][0]["issue_label"], "募资")
        self.assertEqual(self.groups(q="FUND_RAISE")["issue"]["items"][0]["issue_id"], "i1")

    def test_each_group_says_how_many_there_are_and_where_to_see_them_all(self):
        # 下拉显示「共 N 条」靠 total；「查看全部」把词交回给那一页自己的 search
        # 参数，全局检索不重做一份分页。
        groups = self.groups(q="长鑫", limit=1)
        self.assertEqual(groups["event"]["more"],
                         {"path": "/events", "query": {"search": "长鑫"}})
        self.assertEqual(groups["claim"]["more"]["path"], "/claims")
        self.assertEqual(groups["document"]["more"]["path"], "/ingestion")
        self.assertGreaterEqual(groups["event"]["total"], len(groups["event"]["items"]))

    def test_the_disclosure_group_reports_no_total_rather_than_a_wrong_one(self):
        # 数 FTS 命中数要再扫一遍倒排，而这个数读者也翻不到；宁可不给。
        self.assertIsNone(self.groups(q="募资")["evidence"]["total"])

    def test_a_missing_index_leaves_the_group_empty_instead_of_failing(self):
        # fixture 没有 evidence_fts——尚未建索引的库应当少一组结果，不是 500。
        self.assertEqual(self.groups(q="募资")["evidence"]["items"], [])

    def test_a_box_holding_only_spaces_is_not_an_error(self):
        body = self.client.get("/api/search", params={"q": "   "}).json()
        self.assertEqual((body["query"], body["groups"]), ("", []))

    def test_a_percent_sign_is_a_character_not_a_wildcard(self):
        # 转义之前，搜「50%」把「50」后面跟任何东西的行全捞出来——正式库上 419 条
        # 主张里只有 16 条真的含这个串。这两条断言要一起看：既要找得到真写着
        # 50% 的那条，又不能把只写了 50 的那些算进来。
        self.assertEqual([item["claim_id"] for item in self.groups(q="50%")["claim"]["items"]],
                         ["c3"])
        self.assertEqual(self.groups(q="295亿%")["claim"]["total"], 0)

    def test_the_disclosure_group_actually_reaches_the_index(self):
        # 这一组是这件事最值钱的部分：93,209 段披露原文此前在页面上没有入口。
        items = self.groups(q="募集资金")["evidence"]["items"]
        self.assertEqual([item["page"] for item in items], [164])
        self.assertIn("本次拟募集资金295亿元", items[0]["text"])
        self.assertEqual(items[0]["company_id"], "cxmt")


class ScriptFoldingTests(ApiTestCase):
    """繁简：药明的港股语料是繁体，而人是照简体打的。"""

    def setUp(self):
        super().setUp()
        db = sqlite3.connect(self.fixture_path)
        db.execute("""INSERT INTO document_versions VALUES('h1','hkex','wuxi_apptec',
            '藥明康德回購H股公告','2026-08-03T00:00:00+00:00','https://example.com/h','filing','S1')""")
        db.execute("""INSERT INTO event_records(event_id,version_id,company_id,event_types_json,
            issue_codes_json,production_mode,event_subjects_json,event_time,event_time_precision,
            affected_objects_json,impact_direction,impact_magnitude,article_sentiment,
            relevance_score,summary,amounts_json,ratios_json,persons_json,organizations_json,
            products_json,risk_score,risk_level,recommended_action,model_name,
            extraction_confidence,needs_review,review_reasons_json,eventhood,raw_label_json)
            VALUES('e3','h1','wuxi_apptec','[]','[]','BRIEF','[]','2026-08-03','day','[]',
                   'neutral','minor','neutral',0.6,'藥明康德回購H股','[]','[]','[]','[]','[]',
                   10.0,'LOW','none','qwen-plus',0.8,0,'[]','EVENT','{}')""")
        db.commit()
        db.close()

    def test_simplified_input_finds_the_traditional_title(self):
        body = self.client.get("/api/search", params={"q": "药明康德"}).json()
        titles = [item["title"] for group in body["groups"]
                  if group["kind"] == "document" for item in group["items"]]
        self.assertIn("藥明康德回購H股公告", titles)

    def test_traditional_input_finds_it_too(self):
        body = self.client.get("/api/search", params={"q": "藥明康德"}).json()
        titles = [item["title"] for group in body["groups"]
                  if group["kind"] == "document" for item in group["items"]]
        self.assertIn("藥明康德回購H股公告", titles)

    def test_the_page_search_boxes_fold_as_well(self):
        # 修的是全站的 LIKE 路径，不是只给新接口开的后门。
        body = self.client.get("/api/events", params={"search": "药明康德"}).json()
        self.assertIn("e3", [item["event_id"] for item in body["items"]])


class RoutingTests(ApiTestCase):
    def test_an_unknown_api_path_is_404_not_the_app_shell(self):
        # The SPA fallback must not swallow a mistyped endpoint.
        self.assertEqual(self.client.get("/api/does-not-exist").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class DanglingReviewTaskTests(unittest.TestCase):
    """A rebuilt analytical table retires ids; the queue must not keep citing them."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        review_db.ensure_schema(self.db)
        self.db.execute("CREATE TABLE disclosure_diffs(diff_id TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE event_records(event_id TEXT PRIMARY KEY)")
        self.db.execute("INSERT INTO disclosure_diffs VALUES('alive')")
        for kind, target in (("diff", "alive"), ("diff", "retired"), ("verdict", "gone")):
            self.db.execute("""INSERT INTO review_tasks(task_id,kind,target_id,company_id,origin,
                status,created_at) VALUES(?,?,?,?,?,?,?)""",
                (review_db.task_id(kind, target), kind, target, "cxmt", "evaluation_sample",
                 "open", "2026-09-01"))
        self.db.commit()

    def test_a_task_whose_target_was_rebuilt_away_is_found(self):
        found = {(row["kind"], row["target_id"]) for row in review_db.dangling_tasks(self.db)}
        self.assertEqual(found, {("diff", "retired"), ("verdict", "gone")})

    def test_dropping_leaves_the_live_tasks_alone(self):
        report = review_db.drop_dangling(self.db)
        self.assertEqual(report["removed"], {"diff": 1, "verdict": 1})
        remaining = [row["target_id"] for row in
                     self.db.execute("SELECT target_id FROM review_tasks")]
        self.assertEqual(remaining, ["alive"])

    def test_a_judged_task_is_never_dropped(self):
        # The judgement is the only thing here a rerun cannot regenerate.
        self.db.execute("""INSERT INTO review_decisions(task_id,reviewer,role,action,judgement,
            decided_at) VALUES(?,?,?,?,?,?)""",
            (review_db.task_id("verdict", "gone"), "analyst", "analyst", "judge", "wrong",
             "2026-09-01"))
        report = review_db.drop_dangling(self.db)
        self.assertEqual(report["kept_because_judged"], 1)
        self.assertEqual(report["removed"], {"diff": 1})

    def test_every_task_kind_knows_where_its_target_lives(self):
        self.assertEqual(set(review_db.TARGET_TABLES), set(review_db.TASK_KINDS))


class SharedMetricTests(unittest.TestCase):
    """macro-F1 has one definition, and both evidence bases use it.

    ``class_scores`` moved to ``api.metrics`` so a processor batch job can import
    it without pulling in FastAPI.  The re-export is what keeps every existing
    caller — and the route module's own ``/metrics`` handler — working.
    """

    def test_the_route_module_still_exposes_class_scores(self):
        from api import metrics
        from api.routes import review as review_routes
        self.assertIs(review_routes.class_scores, metrics.class_scores)

    def test_the_metrics_module_does_not_import_the_web_layer(self):
        import inspect

        from api import metrics
        source = inspect.getsource(metrics)
        self.assertNotIn("fastapi", source)
        self.assertNotIn("sqlite3", source)

    def test_subtypes_fold_into_their_base_class(self):
        from api.metrics import class_scores
        scores = class_scores([("PARTIAL/timing_mismatch", "PARTIAL/scope_mismatch")])
        self.assertEqual(list(scores["confusion"]), ["PARTIAL"])
        self.assertEqual(scores["per_class"]["PARTIAL"]["support"], 1)

    def test_an_unjudged_class_is_left_out_of_the_macro_average(self):
        from api.metrics import class_scores
        scores = class_scores([("SUPPORTED", "SUPPORTED"), ("CONFLICT", "SUPPORTED")])
        # CONFLICT was predicted but never was the truth, so it has no support
        # and averaging a zero for it would understate the sample.
        self.assertEqual(scores["per_class"]["CONFLICT"]["support"], 0)
        self.assertEqual(scores["macro_f1"], scores["per_class"]["SUPPORTED"]["f1"])


class ReviewIntervalTests(ReviewTestCase):
    """A rate without its interval reads the same at n=2 and at n=300."""

    def seed(self):
        return self.client.post("/api/review/seed",
                                json={"kind": "verdict", "origin": "evaluation_sample", "count": 10})

    def test_an_accuracy_carries_a_95_percent_interval(self):
        self.seed()
        tasks = self.client.get("/api/review/tasks").json()["items"]
        self.client.post(f"/api/review/tasks/{tasks[0]['task_id']}/judge", json={"judgement": "correct"})
        body = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]
        interval = body["accuracy_interval"]
        self.assertLessEqual(interval["low"], body["accuracy"])
        self.assertGreaterEqual(interval["high"], body["accuracy"])

    def test_one_judgement_produces_an_interval_wide_enough_to_be_useless(self):
        # Which is the point: the number should look as uncertain as it is.
        self.seed()
        task = self.client.get("/api/review/tasks").json()["items"][0]
        self.client.post(f"/api/review/tasks/{task['task_id']}/judge", json={"judgement": "correct"})
        interval = self.client.get("/api/review/metrics").json()["by_kind"]["verdict"]["accuracy_interval"]
        self.assertGreater(interval["high"] - interval["low"], 0.5)

    def test_the_interval_never_runs_past_the_ends_of_the_scale(self):
        # The normal approximation does exactly that near a rate of 1, which is
        # where every rate in this queue is expected to sit.
        from api.routes.review import wilson
        for total in (1, 5, 30, 300):
            band = wilson(total, total)
            self.assertLessEqual(band["high"], 1.0)
            self.assertGreaterEqual(band["low"], 0.0)

    def test_the_interval_narrows_as_the_sample_grows(self):
        from api.routes.review import wilson
        widths = [wilson(round(0.8 * n), n)["half_width"] for n in (10, 40, 100, 200, 300)]
        self.assertEqual(widths, sorted(widths, reverse=True))
        # The stopping rule this is meant to inform: the first hundred buy most
        # of the precision the whole queue can offer.
        self.assertLess(widths[2], widths[0] / 2)
        self.assertGreater(widths[4], widths[2] / 2)

    def test_no_judgements_means_no_interval_rather_than_a_full_one(self):
        from api.routes.review import wilson
        self.assertIsNone(wilson(0, 0))


class ReviewOrderingTests(ReviewTestCase):
    """Which hundred gets judged first decides what the measurement is worth."""

    def test_the_default_order_leads_with_disagreement(self):
        # A task where the pre-annotating model contradicted the system is one
        # only a person can settle; a confident agreement teaches least. The
        # queue used to be ordered by created_at, which carries no signal.
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 8})
        tasks = self.client.get("/api/review/tasks").json()["items"]
        self.assertGreaterEqual(len(tasks), 2)
        import api.review_db as review_db
        connection = review_db.connect()
        ids = [row["task_id"] for row in connection.execute("SELECT task_id FROM review_tasks")]
        connection.close()
        db = sqlite3.connect(self.fixture_path)
        db.execute("""CREATE TABLE IF NOT EXISTS review_pre_annotations (
            task_id TEXT PRIMARY KEY, kind TEXT, target_id TEXT, company_id TEXT,
            system_label TEXT, model_label TEXT, model_subtype TEXT, evidence_ok INTEGER,
            agrees INTEGER, reason TEXT, confidence REAL, model_name TEXT,
            prompt_version TEXT, annotated_at TEXT)""")
        # The last one disagrees; everything before it agrees confidently.
        for index, task_id in enumerate(ids):
            db.execute("INSERT OR REPLACE INTO review_pre_annotations(task_id,agrees,confidence) VALUES(?,?,?)",
                       (task_id, 0 if index == len(ids) - 1 else 1, 0.99))
        db.commit()
        db.close()
        ordered = self.client.get("/api/review/tasks").json()["items"]
        self.assertEqual(ordered[0]["task_id"], ids[-1])

    def test_ordering_moves_tasks_but_never_drops_them(self):
        # It is a presentation choice, not a sampling one: the denominator has
        # to be identical either way.
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 8})
        informative = self.client.get("/api/review/tasks?limit=200").json()
        chronological = self.client.get("/api/review/tasks?order=created&limit=200").json()
        self.assertEqual(informative["total"], chronological["total"])
        self.assertEqual({row["task_id"] for row in informative["items"]},
                         {row["task_id"] for row in chronological["items"]})

    def test_paging_the_informative_order_does_not_repeat_or_skip(self):
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 8})
        first = self.client.get("/api/review/tasks?limit=3&offset=0").json()["items"]
        second = self.client.get("/api/review/tasks?limit=3&offset=3").json()["items"]
        ids = [row["task_id"] for row in first] + [row["task_id"] for row in second]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_queue_with_no_pre_annotations_still_lists(self):
        # Ordering by a signal that is not there must not empty the page.
        self.client.post("/api/review/seed",
                         json={"kind": "verdict", "origin": "evaluation_sample", "count": 4})
        self.assertGreater(len(self.client.get("/api/review/tasks").json()["items"]), 0)


class FinancialIndicatorTests(ApiTestCase):
    """主要会计数据 over the API — the first thing this system knows about a
    company rather than about a document."""

    def seed(self):
        db = sqlite3.connect(self.fixture_path)
        db.execute("""CREATE TABLE IF NOT EXISTS financial_indicators (
            company_id TEXT NOT NULL, period TEXT NOT NULL, period_type TEXT NOT NULL,
            metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
            version_id TEXT NOT NULL, source_title TEXT, published_at TEXT,
            extracted_at TEXT NOT NULL, PRIMARY KEY(company_id, period, metric))""")
        rows = [
            ("cxmt", "2026H1", "interim", "revenue", 150310076758.0, "CNY",
             "v1", "2026年半年度报告摘要", "2026-08-29"),
            ("cxmt", "2026H1", "interim", "weighted_roe", 81.06, "percent",
             "v1", "2026年半年度报告摘要", "2026-08-29"),
            ("wuxi_apptec", "2025", "annual", "revenue", 45456165774.18, "CNY",
             "v2", "2025年年度报告摘要", "2026-03-24"),
        ]
        db.executemany("""INSERT OR REPLACE INTO financial_indicators
            (company_id,period,period_type,metric,value,unit,version_id,source_title,
             published_at,extracted_at) VALUES(?,?,?,?,?,?,?,?,?,'now')""", rows)
        db.commit()
        db.close()

    def test_figures_are_grouped_by_company_and_period(self):
        self.seed()
        body = self.client.get("/api/financials").json()
        self.assertTrue(body["available"])
        self.assertEqual(body["periods"], 2)
        self.assertEqual(body["figures"], 3)
        first = body["items"][0]
        self.assertEqual((first["company_id"], first["period"]), ("cxmt", "2026H1"))
        self.assertEqual(first["metrics"]["revenue"]["value"], 150310076758.0)

    def test_each_figure_keeps_its_own_unit(self):
        # A percentage and a sum of money in one table; storing both as 「元」
        # would make ROE read as eighty-one yuan.
        self.seed()
        metrics = self.client.get("/api/financials").json()["items"][0]["metrics"]
        self.assertEqual(metrics["revenue"]["unit"], "CNY")
        self.assertEqual(metrics["weighted_roe"]["unit"], "percent")

    def test_every_period_names_the_filing_it_came_from(self):
        # A financial figure with no filing behind it is not checkable, which is
        # the whole reason for reading the reports rather than a data vendor.
        self.seed()
        for row in self.client.get("/api/financials").json()["items"]:
            self.assertTrue(row["source_title"])

    def test_the_display_labels_come_from_the_extractor(self):
        # The page and the parser must not drift about what a stored name means.
        self.seed()
        from processor.financials import METRIC_LABELS
        self.assertEqual(self.client.get("/api/financials").json()["labels"], METRIC_LABELS)

    def test_it_says_so_rather_than_failing_before_the_extractor_has_run(self):
        body = self.client.get("/api/financials").json()
        self.assertFalse(body["available"])
        self.assertTrue(body["reason"])
        self.assertNotIn("processor.", body["reason"])


class WebProcessBoundaryTests(unittest.TestCase):
    """What the web process is allowed to load.

    ``api/review_db.py`` states the rule — the API must not pull the model
    client into the web server — and nothing enforced it.  Two modules do
    import from ``processor`` (``benchmark_db`` for the benchmark path,
    ``routes/meta`` for the financial metric labels), and neither drags a
    gateway client along.  The ones that would are ``ai_label`` and
    ``claim_verdict``.
    """

    IMPORT = re.compile(r"^\s*(?:from|import)\s+processor\.(\w+)", re.M)

    def test_importing_the_api_does_not_load_a_model_client(self):
        import subprocess
        import sys
        # A subprocess, because this suite has already imported half the
        # processor by the time it runs and would be answering about itself.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, api.main; "
             "print(','.join(sorted({m.split('.')[0] for m in sys.modules} "
             "& {'openai','anthropic','httpx'})))"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]), check=True)
        self.assertEqual(result.stdout.strip(), "",
                         "the web process loaded a model client")

    def test_no_module_under_api_imports_a_gateway_backed_processor_module(self):
        # An *import*, not a mention: review_db names claim_verdict in a comment
        # explaining why it copies that vocabulary instead of importing it, and
        # a rule that flagged the comment would be aimed past its own target.
        #
        # 唯一的例外是研判助手（api/assistant.py）：它要在请求里现场调模型。例外收得很窄——
        # 只许在函数内部导入（缩进的 import），模块顶层一条都不许有，于是上面那条
        # 「import api.main 不加载模型客户端」照样成立。
        forbidden = {"ai_label", "claim_verdict", "issue_codes", "production_mode",
                     "event_emotion", "article_sentiment", "pre_annotate"}
        top_level = re.compile(r"^(?:from|import)\s+processor\.(\w+)", re.M)
        offenders = []
        for path in sorted((Path(__file__).resolve().parents[1] / "api").rglob("*.py")):
            pattern = top_level if path.name == "assistant.py" and path.parent.name == "api" else self.IMPORT
            for module in pattern.findall(path.read_text(encoding="utf-8")):
                if module in forbidden:
                    offenders.append(f"{path.name}: processor.{module}")
        self.assertEqual(offenders, [])

    def test_the_assistant_is_the_only_exception_and_imports_lazily(self):
        source = (Path(__file__).resolve().parents[1] / "api" / "assistant.py").read_text(encoding="utf-8")
        lazy = re.findall(r"^\s+from\s+processor\.ai_label\s+import", source, re.M)
        self.assertTrue(lazy, "研判助手应当在函数内部导入网关模块")
        self.assertNotRegex(source, re.compile(r"^(?:from|import)\s+processor\.ai_label", re.M))


class DroppedClaimExposureTests(ApiTestCase):
    """Claims the model proposed and the matcher refused, on the page that has
    to explain why an event carries no evidence."""

    def test_the_refused_quotes_reach_the_detail_page(self):
        # Without them the page can only say 「没有可定位的原子主张」, and a reader
        # cannot tell a model paraphrasing from a matcher that failed.
        body = self.client.get("/api/events/e1").json()
        self.assertEqual(body["dropped_claims"], ["模型提过但定位不到的一句"])

    def test_an_event_with_nothing_refused_reports_an_empty_list(self):
        # Events labelled before this was recorded have no such field; an empty
        # list keeps the page from having to distinguish absent from empty.
        self.assertEqual(self.client.get("/api/events/e2").json()["dropped_claims"], [])


class AgreementTests(unittest.TestCase):
    """κ 而不是原始一致率——两个人都只会说「正确」时，它们给出相反的答案。"""

    def kappa(self, pairs):
        from api.metrics import cohen_kappa
        return cohen_kappa(pairs)

    def test_two_reviewers_who_say_correct_to_everything_have_shown_nothing(self):
        # 原始一致率 100%，但这两个人对任何一条都没有真正表过态。
        both = [("correct", "correct")] * 20
        self.assertEqual(self.kappa(both)["observed"], 1.0)
        # 只用了一个类，机会一致率也是 1，κ 没有分母——报 None 而不是 0：
        # 0 读起来是「完全不一致」，正好是反面。
        self.assertIsNone(self.kappa(both)["kappa"])

    def test_perfect_agreement_across_two_classes_scores_one(self):
        pairs = [("correct", "correct")] * 6 + [("wrong", "wrong")] * 6
        self.assertEqual(self.kappa(pairs)["kappa"], 1.0)

    def test_agreement_no_better_than_their_own_rates_predict_scores_zero(self):
        # 各自一半「正确」一半「错误」，一致的那一半正好是巧合应得的那一半。
        pairs = [("correct", "correct"), ("correct", "wrong"),
                 ("wrong", "correct"), ("wrong", "wrong")]
        self.assertEqual((self.kappa(pairs)["observed"], self.kappa(pairs)["kappa"]), (0.5, 0.0))

    def test_a_pair_with_nothing_in_common_is_undefined_rather_than_zero(self):
        self.assertIsNone(self.kappa([]))


class ReviewAssignmentTests(ReviewTestCase):
    """把队列切给具体的人，以及切完之后统计还对不对。

    三个人共用一个 review 账号时有两个后果，这个类把它们都钉住：队列的排序是确定
    的，所以三个人打开看到的是同一批前几条；而每条判定写的都是同一个用户名，于是
    「谁判的」和「两个人判得一不一致」都无从谈起。
    """

    def seed(self, kinds=("verdict", "event", "diff"), origin="evaluation_sample", count=50):
        """把三类都填进队列——分派本来就是跨类型切的，一类一类地切没有意义。"""
        for kind in kinds:
            self.client.post("/api/review/seed",
                             json={"kind": kind, "origin": origin, "count": count})
        return [row["task_id"] for row in
                self.client.get("/api/review/tasks?limit=200").json()["items"]]

    def team(self, *names, role="analyst"):
        """建好账号，并把身份换回有权分派的 tester。"""
        for name in names:
            self.become(name, role)
        self.become("tester", "reviewer")
        return list(names)

    def assign(self, people, **extra):
        return self.client.post("/api/review/assign",
                                json={"assignees": people, **extra})

    def test_a_split_is_even_and_comes_out_the_same_way_twice(self):
        # 不确定的分派没法复跑，评测集就不能从记录里重建出来。
        tasks = self.seed()
        people = self.team("zhang", "li", "wang")
        first = self.assign(people).json()
        self.assertEqual(sum(first["assigned"].values()), len(tasks))
        self.assertLessEqual(max(first["assigned"].values()) - min(first["assigned"].values()), 1)
        mine = self.client.get("/api/review/tasks?assignee=zhang").json()
        again = self.assign(people).json()
        self.assertEqual(again["assigned"], first["assigned"])
        self.assertEqual([row["task_id"] for row in
                          self.client.get("/api/review/tasks?assignee=zhang").json()["items"]],
                         [row["task_id"] for row in mine["items"]])

    def test_each_person_sees_only_their_own_slice_and_the_slices_do_not_overlap(self):
        tasks = self.seed()
        people = self.team("zhang", "li", "wang")
        self.assign(people)
        held = {}
        for name in people:
            self.become(name, "analyst")
            held[name] = {row["task_id"] for row in
                          self.client.get("/api/review/tasks?assignee=me").json()["items"]}
        self.assertEqual(set().union(*held.values()), set(tasks))
        self.assertEqual(sum(len(slice_) for slice_ in held.values()), len(tasks))

    def test_the_overlap_slice_is_handed_to_everybody(self):
        # 双标那一份：没有它，每条只有一个人判过，一致性根本没有可算的对象。
        tasks = self.seed()
        people = self.team("zhang", "li")
        body = self.assign(people, overlap=2).json()
        self.assertEqual(body["overlap"], 2)
        shared = None
        for name in people:
            self.become(name, "analyst")
            owned = {row["task_id"] for row in
                     self.client.get("/api/review/tasks?assignee=me").json()["items"]}
            shared = owned if shared is None else shared & owned
        self.assertEqual(len(shared), 2)
        self.assertEqual(sum(body["assigned"].values()), len(tasks) + 2)

    def test_a_second_reviewer_on_one_task_no_longer_erases_the_first(self):
        # 这是共用账号真正的代价：按任务取最后一条，后判的人把先判的整条盖掉。
        task = self.seed()[0]
        self.team("zhang", "li")
        for name, judgement in (("zhang", "correct"), ("li", "wrong")):
            self.become(name, "analyst")
            self.client.post(f"/api/review/tasks/{task}/judge", json={"judgement": judgement})
        body = self.client.get("/api/review/metrics").json()
        counted = {row["reviewer"]: row for row in body["by_reviewer"]}
        self.assertEqual(sorted(counted), ["li", "zhang"])
        self.assertEqual((counted["zhang"]["correct"], counted["li"]["wrong"]), (1, 1))
        self.assertEqual((body["double_judged"], body["contested"]), (1, 1))
        # 而总表仍然一条任务只算一次——双标的样本不是两个独立样本。
        self.assertEqual(body["by_kind"]["verdict"]["judged"], 1)

    def test_one_person_changing_their_mind_is_still_one_judgement(self):
        task = self.seed()[0]
        self.team("zhang")
        self.become("zhang", "analyst")
        for judgement in ("wrong", "correct"):
            self.client.post(f"/api/review/tasks/{task}/judge", json={"judgement": judgement})
        body = self.client.get("/api/review/metrics").json()
        row = body["by_reviewer"][0]
        self.assertEqual((row["judged"], row["system_accuracy"]), (1, 1.0))
        self.assertEqual((body["double_judged"], body["contested"]), (0, 0))

    def test_agreement_is_reported_for_the_pair_that_judged_the_same_tasks(self):
        # 一半判得一样，但 li 对什么都说「正确」——所以那一半是巧合，κ = 0。
        # 这正是不能只看原始一致率的理由。
        self.seed()
        tasks = [row["task_id"] for row in
                 self.client.get("/api/review/tasks?kind=verdict").json()["items"]]
        self.team("zhang", "li")
        for name, judgements in (("zhang", ["correct", "wrong"]), ("li", ["correct", "correct"])):
            self.become(name, "analyst")
            for task, judgement in zip(tasks, judgements):
                self.client.post(f"/api/review/tasks/{task}/judge", json={"judgement": judgement})
        pairs = self.client.get("/api/review/metrics").json()["agreement"]["verdict"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["pair"], ["li", "zhang"])
        self.assertEqual((pairs[0]["n"], pairs[0]["observed"], pairs[0]["kappa"]), (2, 0.5, 0.0))

    def test_an_account_that_cannot_judge_is_refused_before_anything_is_dealt(self):
        self.seed()
        self.become("looker", "viewer")
        self.become("tester", "reviewer")
        for name in ("looker", "nobody"):
            with self.subTest(name=name):
                self.assertEqual(self.assign([name]).status_code, 400)
        self.assertEqual(self.client.get("/api/review/tasks?assignee=looker").json()["total"], 0)

    def test_re_dealing_to_a_smaller_team_leaves_nobody_holding_a_stale_slice(self):
        self.seed()
        self.assign(self.team("zhang", "li", "wang"))
        self.assign(["zhang", "li"])
        self.assertEqual(self.client.get("/api/review/tasks?assignee=wang").json()["total"], 0)

    def test_dealing_skips_what_has_already_been_judged(self):
        # 默认只分派 open 的：把判过的任务挪到另一个人名下，判定记录仍写着原来
        # 那个人，统计就对不上了。
        tasks = self.seed()
        self.team("zhang", "li")
        self.become("zhang", "analyst")
        self.client.post(f"/api/review/tasks/{tasks[0]}/judge", json={"judgement": "correct"})
        self.become("tester", "reviewer")
        body = self.assign(["li"]).json()
        self.assertEqual(body["tasks"], len(tasks) - 1)
        self.assertNotIn(tasks[0], [row["task_id"] for row in
                                    self.client.get("/api/review/tasks?assignee=li").json()["items"]])

    def test_a_card_names_who_it_was_dealt_to(self):
        self.seed()
        self.assign(self.team("zhang", "li"))
        items = self.client.get("/api/review/tasks").json()["items"]
        self.assertTrue(all(len(row["assignees"]) == 1 for row in items))
        self.assertEqual({row["assignees"][0] for row in items}, {"zhang", "li"})

    def card(self, task_id, kind="verdict"):
        return [row for row in
                self.client.get(f"/api/review/tasks?kind={kind}&limit=200").json()["items"]
                if row["task_id"] == task_id][0]

    def test_the_second_annotator_cannot_see_the_first_ones_answer(self):
        # 双标要的是两份独立判定。看得见对方的答案，κ 会被锚定效应抬上去——而
        # 抬上去的 κ 恰恰是在说「标注很可靠」，正好是反面。
        self.seed()
        people = self.team("zhang", "li")
        tasks = [row["task_id"] for row in
                 self.client.get("/api/review/tasks?kind=verdict").json()["items"]]
        self.assign(people, kind="verdict", overlap=len(tasks))
        shared = tasks[0]
        self.become("zhang", "analyst")
        self.client.post(f"/api/review/tasks/{shared}/judge", json={"judgement": "wrong"})
        self.become("li", "analyst")
        blind = self.card(shared)
        self.assertEqual(blind["decisions"], [])
        self.assertTrue(blind["blinded"])
        # 判完之后照常全看——那时候分歧正是要看的东西。
        self.client.post(f"/api/review/tasks/{shared}/judge", json={"judgement": "correct"})
        after = self.card(shared)
        self.assertFalse(after["blinded"])
        self.assertEqual({entry["reviewer"] for entry in after["decisions"]}, {"zhang", "li"})

    def test_a_single_annotator_task_still_shows_its_whole_history(self):
        self.seed()
        self.assign(self.team("zhang", "li"))
        self.become("zhang", "analyst")
        mine = self.client.get("/api/review/tasks?assignee=me").json()["items"][0]
        self.client.post(f"/api/review/tasks/{mine['task_id']}/judge",
                         json={"judgement": "correct"})
        again = self.card(mine["task_id"], kind=mine["kind"])
        self.assertFalse(again["blinded"])
        self.assertEqual(len(again["decisions"]), 1)

    def test_somebody_not_on_the_task_sees_both_sides(self):
        # 事后来仲裁的核查负责人必须看得见两份判定，否则无从仲裁。
        self.seed()
        people = self.team("zhang", "li")
        tasks = [row["task_id"] for row in
                 self.client.get("/api/review/tasks?kind=verdict").json()["items"]]
        self.assign(people, kind="verdict", overlap=len(tasks))
        for name, judgement in (("zhang", "wrong"), ("li", "correct")):
            self.become(name, "analyst")
            self.client.post(f"/api/review/tasks/{tasks[0]}/judge", json={"judgement": judgement})
        self.become("tester", "reviewer")
        seen = self.card(tasks[0])
        self.assertFalse(seen["blinded"])
        self.assertEqual({entry["reviewer"] for entry in seen["decisions"]}, {"zhang", "li"})

    def test_the_reviewer_list_offers_only_accounts_that_can_judge(self):
        self.become("looker", "viewer")
        self.become("tester", "reviewer")
        names = {row["username"] for row in
                 self.client.get("/api/review/reviewers").json()["items"]}
        self.assertIn("tester", names)
        self.assertNotIn("looker", names)
