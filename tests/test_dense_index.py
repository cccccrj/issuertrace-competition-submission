import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from processor import dense_index, retrieval_eval
from processor.claim_index import bigrams, ensure_schema
from processor.claim_verdict import retrieve

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("embed_neighbors", ROOT / "scripts" / "gpu" / "embed_neighbors.py")
embed_neighbors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(embed_neighbors)


def corpus_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    db.executescript("""
        CREATE TABLE event_records(event_id TEXT PRIMARY KEY, company_id TEXT, version_id TEXT);
        CREATE TABLE event_claims(claim_id TEXT PRIMARY KEY, event_id TEXT, text TEXT);
        CREATE TABLE claim_verdicts(claim_id TEXT PRIMARY KEY, verdict TEXT);
    """)
    return db


def add_paragraph(db, company, text, version="filing-1", page=1):
    cursor = db.execute("""INSERT INTO evidence_paragraphs
        (company_id,version_id,document_type,title,section,page,text) VALUES(?,?,?,?,?,?,?)""",
        (company, version, "announcements", "公告", "正文", page, text))
    db.execute("INSERT INTO evidence_fts(rowid,body) VALUES(?,?)", (cursor.lastrowid, " ".join(bigrams(text))))
    return cursor.lastrowid


def add_claim(db, claim_id, company, text, source_version="media-1", verdict=None):
    db.execute("INSERT INTO event_records VALUES(?,?,?)", ("e-" + claim_id, company, source_version))
    db.execute("INSERT INTO event_claims VALUES(?,?,?)", (claim_id, "e-" + claim_id, text))
    if verdict:
        db.execute("INSERT INTO claim_verdicts VALUES(?,?)", (claim_id, verdict))


def write_neighbors(directory: Path, rows: dict[str, list[int]], db, *, fake=False, limit=None):
    """Lay down a bundle the way export + the GPU script would."""
    dense_index.export(directory, db)
    with (directory / "neighbors.jsonl").open("w", encoding="utf-8") as handle:
        for claim_id, hits in rows.items():
            handle.write(json.dumps({"id": claim_id, "hits": [[pid, 0.9] for pid in hits]}) + "\n")
    export_info = json.loads((directory / "export.json").read_text())
    (directory / "manifest.json").write_text(json.dumps({
        "model": "test", "fake": fake, "limit": limit, "queries": len(rows),
        "input_sha256": export_info["sha256"],
        "neighbors_sha256": dense_index.sha256_file(directory / "neighbors.jsonl")}))


class FusionTests(unittest.TestCase):
    def test_an_item_both_lists_rank_high_wins(self):
        self.assertEqual(dense_index.rrf([1, 2, 3], [3, 4, 1])[:2], [1, 3])

    def test_items_only_one_list_has_are_kept(self):
        self.assertEqual(set(dense_index.rrf([1], [2])), {1, 2})

    def test_ties_keep_first_seen_order(self):
        self.assertEqual(dense_index.rrf([7], [8]), [7, 8])


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.db = corpus_db()
        self.p_zh = add_paragraph(self.db, "wuxi_apptec", "公司回购A股股份合共20,275,407股。")
        self.p_en = add_paragraph(self.db, "wuxi_apptec", "Revenue for the year was RMB 39,241.1 million.")
        add_claim(self.db, "c1", "wuxi_apptec", "WuXi AppTec bought back 20,275,407 A shares")
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name) / "bundle"
        self.target = Path(self.tmp.name) / "installed"
        dense_index._CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()
        dense_index._CACHE.clear()

    def test_export_writes_ids_company_and_text_only(self):
        summary = dense_index.export(self.bundle, self.db)
        self.assertEqual((summary["paragraphs"], summary["queries"]), (2, 1))
        first = json.loads((self.bundle / "paragraphs.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(set(first), {"id", "company", "text"})

    def test_the_gpu_script_round_trips_with_the_fake_encoder(self):
        dense_index.export(self.bundle, self.db)
        self.assertEqual(embed_neighbors.main(["--bundle", str(self.bundle), "--fake-encoder"]), 0)
        refused = dense_index.install(self.bundle, self.db, self.target)
        self.assertFalse(refused["installed"])
        self.assertTrue(any("fake" in problem for problem in refused["problems"]))
        accepted = dense_index.install(self.bundle, self.db, self.target, allow_fake=True)
        self.assertTrue(accepted["installed"])
        self.assertIn("c1", dense_index.load(self.db, self.target))

    def test_the_gpu_script_refuses_an_altered_bundle(self):
        dense_index.export(self.bundle, self.db)
        with (self.bundle / "queries.jsonl").open("a") as handle:
            handle.write('{"id": "x", "company": "wuxi_apptec", "text": "tampered"}\n')
        self.assertEqual(embed_neighbors.main(["--bundle", str(self.bundle), "--fake-encoder"]), 2)

    def test_install_refuses_a_bundle_built_before_the_paragraphs_changed(self):
        write_neighbors(self.bundle, {"c1": [self.p_zh]}, self.db)
        add_paragraph(self.db, "wuxi_apptec", "新增的一段，段落编号与导出时不再一致。")
        result = dense_index.install(self.bundle, self.db, self.target)
        self.assertFalse(result["installed"])

    def test_install_refuses_a_partial_run(self):
        write_neighbors(self.bundle, {"c1": [self.p_zh]}, self.db, limit=100)
        self.assertFalse(dense_index.install(self.bundle, self.db, self.target)["installed"])

    def test_an_installed_file_goes_quiet_once_paragraphs_change(self):
        write_neighbors(self.bundle, {"c1": [self.p_zh]}, self.db)
        self.assertTrue(dense_index.install(self.bundle, self.db, self.target)["installed"])
        add_paragraph(self.db, "wuxi_apptec", "索引重建后的新段落。")
        dense_index._CACHE.clear()
        self.assertIsNone(dense_index.load(self.db, self.target))
        self.assertFalse(dense_index.status(self.db, self.target)["fresh"])

    def test_hybrid_brings_in_a_paragraph_only_the_vectors_found(self):
        # The English claim shares no term with the Chinese filing; BM25 alone misses it.
        write_neighbors(self.bundle, {"c1": [self.p_zh]}, self.db)
        dense_index.install(self.bundle, self.db, self.target)
        hits = dense_index.hybrid_search(self.db, "wuxi_apptec", "WuXi AppTec bought back shares",
                                         ["c1"], target=self.target)
        found = {hit["paragraph_id"]: hit for hit in hits}
        self.assertIn(self.p_zh, found)
        self.assertIsNone(found[self.p_zh]["score"])   # no BM25 score for a dense-only hit

    def test_a_claim_without_neighbours_gets_plain_bm25(self):
        write_neighbors(self.bundle, {"other": [self.p_zh]}, self.db)
        dense_index.install(self.bundle, self.db, self.target)
        hits = dense_index.hybrid_search(self.db, "wuxi_apptec", "revenue for the year",
                                         ["c1"], target=self.target)
        self.assertEqual([hit["paragraph_id"] for hit in hits], [self.p_en])

    def test_hybrid_never_crosses_companies(self):
        other = add_paragraph(self.db, "cxmt", "长鑫回购股份20,275,407股。")
        write_neighbors(self.bundle, {"c1": [other, self.p_zh]}, self.db)
        dense_index.install(self.bundle, self.db, self.target)
        hits = dense_index.hybrid_search(self.db, "wuxi_apptec", "bought back", ["c1"], target=self.target)
        self.assertNotIn(other, [hit["paragraph_id"] for hit in hits])

    def test_changed_lists_only_claims_whose_evidence_moves(self):
        # c2: BM25 already finds what the vectors find.  c3: only the vectors find it.
        add_claim(self.db, "c2", "wuxi_apptec", "revenue for the year")
        add_claim(self.db, "c3", "wuxi_apptec", "WuXi AppTec bought back shares")
        write_neighbors(self.bundle, {"c2": [self.p_en], "c3": [self.p_zh]}, self.db)
        dense_index.install(self.bundle, self.db, self.target)
        self.assertEqual(dense_index.changed_claims(self.db, target=self.target), ["c3"])

    def test_fallback_keeps_bm25_five_and_only_appends(self):
        extra = [add_paragraph(self.db, "wuxi_apptec", f"Revenue note {i} for the year.") for i in range(6)]
        dense_only = add_paragraph(self.db, "wuxi_apptec", "公司以集中竞价方式回购股份。")
        write_neighbors(self.bundle, {"c9": [extra[0], dense_only, self.p_zh]}, self.db)
        dense_index.install(self.bundle, self.db, self.target)
        from processor.claim_index import search
        base = [hit["paragraph_id"] for hit in search(self.db, "wuxi_apptec", "revenue for the year", 5)]
        hits = dense_index.fallback_search(self.db, "wuxi_apptec", "revenue for the year", ["c9"],
                                           target=self.target)
        ids = [hit["paragraph_id"] for hit in hits]
        self.assertEqual(ids[:5], base)                      # never displaces a BM25 hit
        self.assertLessEqual(len(ids), 5 + dense_index.FALLBACK_EXTRA)
        self.assertIn(dense_only, ids[5:])                   # dense-only evidence is appended
        self.assertIsNone(hits[5]["score"])                  # how a fallback verdict is found later

    def test_fallback_without_vectors_is_exactly_bm25(self):
        dense_index._CACHE[str(dense_index.INSTALL_DIR)] = None
        _, plain = retrieve(self.db, "wuxi_apptec", "revenue for the year")
        _, fallback = retrieve(self.db, "wuxi_apptec", "revenue for the year", ["c1"], "fallback")
        self.assertEqual([c["paragraph_id"] for c in plain], [c["paragraph_id"] for c in fallback])

    def test_fallback_targets_are_unverifiable_claims_with_new_evidence(self):
        add_claim(self.db, "u1", "wuxi_apptec", "WuXi AppTec bought back shares", verdict="UNVERIFIABLE")
        add_claim(self.db, "s1", "wuxi_apptec", "WuXi AppTec bought back shares again", verdict="SUPPORTED")
        write_neighbors(self.bundle, {"u1": [self.p_zh], "s1": [self.p_zh]}, self.db)
        dense_index.install(self.bundle, self.db, self.target)
        self.assertEqual(dense_index.fallback_targets(self.db, target=self.target), ["u1"])

    def test_fallback_only_accepts_supported_resting_on_dense_evidence(self):
        dense = {"via": "dense"}
        bm25 = {"score": -3.2}
        self.assertTrue(dense_index.fallback_accepts({"verdict": "SUPPORTED", "candidate": dense}))
        # A similar paragraph about another buy-back is not a contradiction.
        self.assertFalse(dense_index.fallback_accepts({"verdict": "CONFLICT", "candidate": dense}))
        self.assertFalse(dense_index.fallback_accepts({"verdict": "PARTIAL", "candidate": dense}))
        # Same BM25 evidence, different answer: run-to-run variance, not new evidence.
        self.assertFalse(dense_index.fallback_accepts({"verdict": "SUPPORTED", "candidate": bm25}))
        self.assertFalse(dense_index.fallback_accepts({"verdict": "UNVERIFIABLE", "candidate": None}))

    def test_appended_candidates_are_marked(self):
        dense_only = add_paragraph(self.db, "wuxi_apptec", "公司以集中竞价方式回购股份。")
        write_neighbors(self.bundle, {"c1": [dense_only]}, self.db)
        dense_index.install(self.bundle, self.db, self.target)
        hits = dense_index.fallback_search(self.db, "wuxi_apptec", "revenue for the year", ["c1"],
                                           target=self.target)
        self.assertEqual([hit.get("via") for hit in hits], [None, "dense"])

    def test_the_schema_lets_the_model_cite_an_appended_candidate(self):
        from processor.claim_verdict import verdict_schema
        self.assertEqual(verdict_schema()["properties"]["evidence_index"]["maximum"], 5)
        self.assertEqual(verdict_schema(7)["properties"]["evidence_index"]["maximum"], 7)

    def test_claim_verdict_hybrid_mode_without_vectors_is_bm25(self):
        dense_index._CACHE[str(dense_index.INSTALL_DIR)] = None
        _, plain = retrieve(self.db, "wuxi_apptec", "revenue for the year")
        _, hybrid = retrieve(self.db, "wuxi_apptec", "revenue for the year", ["c1"], "hybrid")
        self.assertEqual([c["paragraph_id"] for c in plain], [c["paragraph_id"] for c in hybrid])


class GoldTests(unittest.TestCase):
    def test_distinctive_figures_only(self):
        self.assertEqual(retrieval_eval.figures("回购20,275,407股，占比0.69%，2024年，金额123.45亿元"),
                         {"20275407", "0.69", "123.45"})

    def test_language(self):
        self.assertEqual(retrieval_eval.language("The Company repurchased shares in the period"), "en")
        self.assertEqual(retrieval_eval.language("公司回购 A 股股份"), "zh")

    def test_gold_sets_from_figures_and_wording(self):
        db = corpus_db()
        zh = add_paragraph(db, "wuxi_apptec", "公司回购A股股份合共20,275,407股，支付总金额为1,000,000,000元。",
                           version="filing-zh")
        en = add_paragraph(db, "wuxi_apptec", "The Company repurchased a total of 20,275,407 A Shares "
                           "for a total of RMB 1,000,000,000 through centralized price bidding.",
                           version="filing-en")
        add_claim(db, "c1", "wuxi_apptec", "The Company repurchased a total of 20,275,407 A Shares "
                  "for RMB 1,000,000,000 in total", verdict="UNVERIFIABLE")
        gold = {row["set"]: row for row in retrieval_eval.build_gold(db)}
        self.assertEqual(gold["cross_lingual"]["gold"], [zh])
        self.assertEqual(gold["numeric"]["gold"], [en])
        self.assertEqual(gold["verbatim_en"]["gold"], [en])

    def test_the_claims_own_source_is_never_gold(self):
        db = corpus_db()
        add_paragraph(db, "wuxi_apptec", "The Company repurchased a total of 20,275,407 A Shares "
                      "for RMB 1,000,000,000.", version="same-doc")
        add_claim(db, "c1", "wuxi_apptec", "The Company repurchased a total of 20,275,407 A Shares "
                  "for RMB 1,000,000,000.", source_version="same-doc")
        self.assertEqual(retrieval_eval.build_gold(db), [])

    def test_score(self):
        self.assertEqual(retrieval_eval.score([5, 6, 7], {7}),
                         {"recall@5": 1.0, "recall@20": 1.0, "recall@shown": 1.0, "mrr@20": 1 / 3})
        self.assertEqual(retrieval_eval.score([1] * 3, {9})["recall@5"], 0.0)

    def test_the_adoption_rule(self):
        table = {"cross_lingual": {"all": {"bm25": {"recall@5": 0.06}, "hybrid": {"recall@5": 0.30}}},
                 "verbatim_en": {"all": {"bm25": {"recall@5": 0.94}, "hybrid": {"recall@5": 0.93}}}}
        self.assertTrue(retrieval_eval.decide(table)["adopt_hybrid"])
        table["verbatim_en"]["all"]["hybrid"]["recall@5"] = 0.90
        self.assertFalse(retrieval_eval.decide(table)["adopt_hybrid"])


if __name__ == "__main__":
    unittest.main()
