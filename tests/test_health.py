import sqlite3
import unittest

from collector.health import (
    api_jobs_by_class, bis_search_health, csrc_search_job_health, einteractive_health,
    cnstock_search_health, environment_search_health, maintenance_failure_reasons, miit_search_health,
    jiemian_search_health, nbd_search_health, openfda_enforcement_health,
    stcn_search_health,
    yicai_search_health,
    twenty_first_search_health,
)


class HealthTests(unittest.TestCase):
    def test_failed_storage_maintenance_makes_health_unhealthy(self):
        self.assertEqual(maintenance_failure_reasons({
            "verify_storage": {"last_run_at": "2026-08-24T22:00:00+00:00",
                               "status": "storage_verification_error"},
        }), ["maintenance job verify_storage last run failed"])
        self.assertEqual(maintenance_failure_reasons({
            "verify_storage": {"last_run_at": "2026-08-24T22:00:00+00:00",
                               "status": "storage_verification_success"},
        }), [])

    def test_api_job_health_separates_backfill_and_incremental(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE api_jobs(job_id TEXT,status TEXT)")
        db.executemany("INSERT INTO api_jobs VALUES(?,?)", [
            ("cxmt:2024-01-01:2024-01-31:1", "done"),
            ("wuxi:2024-01-01:2024-01-31:1", "pending"),
            ("incremental:cxmt:2026-08-25:1", "pending"),
        ])
        self.assertEqual(api_jobs_by_class(db), {
            "backfill": {"done": 1, "pending": 1},
            "incremental": {"pending": 1},
        })
        db.close()

    def test_csrc_search_job_health_keeps_company_watermarks(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE csrc_search_jobs(
            company_id TEXT,status TEXT,attempts INTEGER,last_run_at TEXT,last_error TEXT)""")
        db.execute("INSERT INTO csrc_search_jobs VALUES('cxmt','done',1,'2026-08-24T23:00:00+00:00',NULL)")
        self.assertEqual(csrc_search_job_health(db), {
            "cxmt": {"status": "done", "attempts": 1,
                     "last_run_at": "2026-08-24T23:00:00+00:00", "last_error": None},
        })
        db.close()

    def test_einteractive_health_exposes_partial_history(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE sse_einteractive_state(
            company_id TEXT,stock_code TEXT,uid TEXT,backfill_coverage TEXT,
            coverage_start_at TEXT,last_incremental_date TEXT,last_error TEXT)""")
        db.execute("""CREATE TABLE sse_einteractive_items(
            item_id TEXT,company_id TEXT)""")
        db.execute("""INSERT INTO sse_einteractive_state VALUES(
            'wuxi_apptec','603259','121066','partial_recent_window',
            '2026-07-28T18:18+08:00','2026-08-25',NULL)""")
        db.execute("INSERT INTO sse_einteractive_items VALUES('1769302','wuxi_apptec')")
        self.assertEqual(einteractive_health(db)["wuxi_apptec"], {
            "stock_code": "603259", "uid": "121066",
            "backfill_coverage": "partial_recent_window",
            "coverage_start_at": "2026-07-28T18:18+08:00",
            "last_incremental_date": "2026-08-25", "items": 1,
            "last_error": None,
        })
        db.close()

    def test_environment_search_health_keeps_term_watermarks(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE local_environment_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,last_error TEXT)""")
        db.execute("""INSERT INTO local_environment_search_state VALUES(
            'cxmt_storage','cxmt','长鑫存储技术有限公司','2026-08-25T00:00:00+00:00',NULL)""")
        self.assertEqual(environment_search_health(db)["cxmt_storage"], {
            "company_id": "cxmt", "search_term": "长鑫存储技术有限公司",
            "last_completed_at": "2026-08-25T00:00:00+00:00", "last_error": None,
        })
        db.close()

    def test_miit_search_health_keeps_zero_result_watermark(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE miit_search_state(
            term_id TEXT,search_term TEXT,last_completed_at TEXT,total_results INTEGER,
            last_error TEXT)""")
        db.execute("""INSERT INTO miit_search_state VALUES(
            'cxmt_english','CXMT','2026-08-25T00:00:00+00:00',0,NULL)""")
        self.assertEqual(miit_search_health(db)["cxmt_english"], {
            "search_term": "CXMT", "last_completed_at": "2026-08-25T00:00:00+00:00",
            "total_results": 0, "last_error": None,
        })
        db.close()

    def test_bis_search_health_keeps_company_and_zero_result_watermark(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE bis_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,
            total_results INTEGER,last_error TEXT)""")
        db.execute("""INSERT INTO bis_search_state VALUES(
            'wuxi_legal','wuxi_apptec','WuXi AppTec',
            '2026-08-25T00:00:00+00:00',0,NULL)""")
        self.assertEqual(bis_search_health(db)["wuxi_legal"], {
            "company_id": "wuxi_apptec", "search_term": "WuXi AppTec",
            "last_completed_at": "2026-08-25T00:00:00+00:00",
            "total_results": 0, "last_error": None,
        })
        db.close()

    def test_openfda_health_keeps_endpoint_and_zero_result_watermark(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE openfda_enforcement_state(
            term_id TEXT,endpoint_type TEXT,company_id TEXT,search_term TEXT,
            last_completed_at TEXT,total_results INTEGER,last_error TEXT)""")
        db.execute("""INSERT INTO openfda_enforcement_state VALUES(
            'drug:wuxi_apptec','drug','wuxi_apptec','WuXi AppTec',
            '2026-08-25T00:00:00+00:00',0,NULL)""")
        self.assertEqual(openfda_enforcement_health(db)["drug:wuxi_apptec"], {
            "endpoint_type": "drug", "company_id": "wuxi_apptec",
            "search_term": "WuXi AppTec",
            "last_completed_at": "2026-08-25T00:00:00+00:00",
            "total_results": 0, "last_error": None,
        })
        db.close()

    def test_nbd_health_keeps_exact_result_watermark(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE nbd_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,
            total_results INTEGER,exact_results INTEGER,last_error TEXT)""")
        db.execute("""INSERT INTO nbd_search_state VALUES(
            'cxmt_legal','cxmt','长鑫科技集团股份有限公司',
            '2026-08-25T02:00:00+00:00',63,61,NULL)""")
        self.assertEqual(nbd_search_health(db)["cxmt_legal"]["exact_results"], 61)
        db.close()

    def test_twenty_first_health_keeps_page_and_skip_watermarks(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE twenty_first_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,
            pages_scanned INTEGER,exact_results INTEGER,skipped_before_window INTEGER,
            last_error TEXT)""")
        db.execute("""INSERT INTO twenty_first_search_state VALUES(
            'wuxi_legal','wuxi_apptec','无锡药明康德新药开发股份有限公司',
            '2026-08-25T02:00:00+00:00',3,7,12,NULL)""")
        state = twenty_first_search_health(db)["wuxi_legal"]
        self.assertEqual(state["pages_scanned"], 3)
        self.assertEqual(state["skipped_before_window"], 12)
        db.close()

    def test_jiemian_health_exposes_coverage_boundary(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE jiemian_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,
            pages_scanned INTEGER,exact_results INTEGER,skipped_before_window INTEGER,
            coverage_note TEXT,last_error TEXT)""")
        db.execute("""INSERT INTO jiemian_search_state VALUES(
            'cxmt','cxmt','长鑫科技','2026-08-25T04:00:00+00:00',12,83,4,
            'date_boundary_reached',NULL)""")
        state = jiemian_search_health(db)["cxmt"]
        self.assertEqual(state["exact_results"], 83)
        self.assertEqual(state["coverage_note"], "date_boundary_reached")
        db.close()

    def test_yicai_health_exposes_coverage_boundary(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE yicai_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,
            pages_scanned INTEGER,exact_results INTEGER,skipped_before_window INTEGER,
            coverage_note TEXT,last_error TEXT)""")
        db.execute("""INSERT INTO yicai_search_state VALUES(
            'wuxi_apptec','wuxi_apptec','药明康德',NULL,3,9,0,'in_progress',NULL)""")
        state = yicai_search_health(db)["wuxi_apptec"]
        self.assertEqual(state["exact_results"], 9)
        self.assertEqual(state["coverage_note"], "in_progress")
        db.close()

    def test_stcn_health_exposes_coverage_boundary(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE stcn_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,
            pages_scanned INTEGER,exact_results INTEGER,skipped_before_window INTEGER,
            coverage_note TEXT,last_error TEXT)""")
        db.execute("""INSERT INTO stcn_search_state VALUES(
            'cxmt','cxmt','长鑫科技',NULL,1,17,0,'in_progress',NULL)""")
        state = stcn_search_health(db)["cxmt"]
        self.assertEqual(state["exact_results"], 17)
        self.assertEqual(state["coverage_note"], "in_progress")
        db.close()

    def test_cnstock_health_exposes_total_and_coverage_boundary(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE cnstock_search_state(
            term_id TEXT,company_id TEXT,search_term TEXT,last_completed_at TEXT,
            total_results INTEGER,pages_scanned INTEGER,exact_results INTEGER,
            skipped_before_window INTEGER,coverage_note TEXT,last_error TEXT)""")
        db.execute("""INSERT INTO cnstock_search_state VALUES(
            'cxmt','cxmt','长鑫科技',NULL,297,1,4,0,'in_progress',NULL)""")
        state = cnstock_search_health(db)["cxmt"]
        self.assertEqual(state["total_results"], 297)
        self.assertEqual(state["exact_results"], 4)
        self.assertEqual(state["coverage_note"], "in_progress")
        db.close()


if __name__ == "__main__":
    unittest.main()


class SchemaFreshnessTests(unittest.TestCase):
    """「新代码 + 旧库」必须在部署时说话，而不是在某个页面上 500。

    平台以 ``mode=ro`` 打开数据库，所以它永远不会、也不该自己建列建表。代价是这件事
    不会在启动时报错：2026-09-07 实测，拿改动前的库跑新代码，事件详情页直接抛
    ``IndexError``（缺 ``risk_breakdown_json`` 一列），而健康检查一切正常。
    """

    def _db(self, *, fresh: bool):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        extra = ", risk_breakdown_json TEXT, label_source TEXT" if fresh else ""
        db.execute(f"CREATE TABLE event_records(event_id TEXT PRIMARY KEY{extra})")
        bearing = ", risk_bearing_event_count INTEGER" if fresh else ""
        db.execute(f"CREATE TABLE issue_ledger(issue_id TEXT PRIMARY KEY{bearing})")
        return db

    def test_a_database_older_than_the_code_names_every_gap(self):
        from api.db import schema_gaps
        gaps = schema_gaps(self._db(fresh=False))
        self.assertIn("event_records 缺列 risk_breakdown_json", gaps)
        self.assertIn("issue_ledger 缺列 risk_bearing_event_count", gaps)

    def test_a_current_database_reports_none(self):
        from api.db import schema_gaps
        self.assertEqual(schema_gaps(self._db(fresh=True)), [])

    def test_the_expectations_list_covers_what_the_routes_read(self):
        # 加了新列却忘了登记，这条守卫就白设了：把 API 真正读的两列钉在这里。
        from api.db import SCHEMA_EXPECTATIONS
        self.assertIn("risk_breakdown_json", SCHEMA_EXPECTATIONS["event_records"])
        self.assertIn("risk_bearing_event_count", SCHEMA_EXPECTATIONS["issue_ledger"])
