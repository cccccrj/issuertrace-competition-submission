import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient

from api import auth, platform_audit, review_db, users_db
from api.main import app, CLIENT_DIST

PASSWORD = "gate-password"


class AuthTestCase(unittest.TestCase):
    """账号住在库里，所以每个用例给自己一份临时库。

    此前这批用例测的是「没有账号文件时生成的一次性口令」那条兜底——它已经去掉了：
    账号是一张表，第一个管理员由 `python -m api.users create` 建。
    """

    def setUp(self):
        self._environment = {key: os.environ.get(key) for key in ("AUTH_SECRET",)}
        os.environ["AUTH_SECRET"] = "test-secret"
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "users.sqlite3"
        from tests.test_api import build_fixture
        from api.routes import events
        build_fixture(path).close()
        self._read_connect = events.connect
        def fixture_connect():
            db=sqlite3.connect(path,check_same_thread=False)
            db.row_factory=sqlite3.Row
            return db
        events.connect=fixture_connect
        # Authentication tests must not borrow a real analytical database just
        # to exercise health/overview. Both reads and account writes use fixture.
        self._paths = (review_db.DB_PATH, auth.ACCOUNTS_PATH)
        review_db.DB_PATH = path
        # 不要让正式的 accounts.json 被当成历史账号导进临时库。
        auth.ACCOUNTS_PATH = Path(self.directory.name) / "no-accounts.json"
        connection = users_db.connect()
        try:
            users_db.create(connection, "analyst", PASSWORD, "analyst",
                            by="测试", hasher=auth.hash_password)
        finally:
            connection.close()
        # Throttle state is process-wide, so each test starts from a clean count.
        auth._attempts.clear()
        self.client = TestClient(app)

    def add_user(self, username, password=PASSWORD, role="viewer"):
        connection = users_db.connect()
        try:
            users_db.create(connection, username, password, role,
                            by="测试", hasher=auth.hash_password)
        finally:
            connection.close()

    def tearDown(self):
        for key, value in self._environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        review_db.DB_PATH, auth.ACCOUNTS_PATH = self._paths
        from api.routes import events
        events.connect = self._read_connect
        self.directory.cleanup()
        auth._attempts.clear()

    def sign_in(self, password=PASSWORD, username="analyst"):
        return self.client.post("/api/auth/login",
                                json={"username": username, "password": password})


class GateTests(AuthTestCase):
    def test_data_endpoints_refuse_an_anonymous_caller(self):
        for path in ("/api/overview", "/api/events", "/api/alerts", "/api/claims/stats"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_liveness_and_the_app_shell_stay_public(self):
        # The shell has to load in order to render the sign-in page at all.
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        shell=self.client.get('/login')
        self.assertEqual(shell.status_code, 200 if CLIENT_DIST.is_dir() else 404)

    def test_a_new_endpoint_is_protected_without_being_listed(self):
        # The gate matches on prefix, so protection is the default rather than
        # something each route has to remember to ask for.
        self.assertNotIn("/api/does-not-exist", auth.PUBLIC_PATHS)
        self.assertEqual(self.client.get("/api/does-not-exist").status_code, 401)

    def test_signing_in_opens_the_data_endpoints(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])
        self.assertIn(auth.COOKIE_NAME, self.client.cookies)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_signing_out_closes_them_again(self):
        self.sign_in()
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/overview").status_code, 401)


class CredentialTests(AuthTestCase):
    def test_a_wrong_password_neither_signs_in_nor_sets_a_cookie(self):
        response = self.sign_in(password="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(auth.COOKIE_NAME, self.client.cookies)

    def test_the_message_does_not_say_which_half_was_wrong(self):
        wrong_user = self.sign_in(username="nobody").json()["detail"]
        wrong_password = self.sign_in(password="wrong").json()["detail"]
        self.assertEqual(wrong_user, wrong_password)

    def test_repeated_failures_are_throttled(self):
        for _ in range(auth.MAX_ATTEMPTS):
            self.assertEqual(self.sign_in(password="wrong").status_code, 401)
        self.assertEqual(self.sign_in(password="wrong").status_code, 429)
        # The correct password is refused too while the window is open: a
        # throttle that the right password walks past is not a throttle.
        self.assertEqual(self.sign_in().status_code, 429)

    def test_no_accounts_means_nobody_gets_in_and_the_log_says_how_to_fix_it(self):
        # 此前这里是「没有账号文件时生成一次性口令」——一个人一把口令，看起来像
        # 有用户体系。现在没有任何账号时谁也登不进，启动日志给出建管理员的命令。
        connection = users_db.connect()
        try:
            connection.execute("DELETE FROM platform_users")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.sign_in().status_code, 401)
        self.assertIn("api.users create", auth.startup_notice())

    def test_a_deactivated_account_cannot_sign_in(self):
        self.add_user("gone", role="analyst")
        connection = users_db.connect()
        try:
            users_db.set_active(connection, "gone", False)
        finally:
            connection.close()
        self.assertEqual(self.sign_in(username="gone").status_code, 401)


class UserTableTests(AuthTestCase):
    """账号住在库里之后，这些性质要守住。"""

    def test_an_account_signs_in_with_its_own_role(self):
        self.add_user("wei", "s3cret-pass", "analyst")
        response = self.client.post("/api/auth/login",
                                    json={"username": "wei", "password": "s3cret-pass"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "analyst")
        self.assertEqual(self.client.get("/api/auth/session").json()["role"], "analyst")

    def test_a_role_change_takes_effect_without_signing_in_again(self):
        # 令牌里带着签发时的角色。只验签名的话，降权要等 12 小时才生效。
        self.add_user("wei", "s3cret-pass", "reviewer")
        self.client.post("/api/auth/login", json={"username": "wei", "password": "s3cret-pass"})
        self.assertEqual(self.client.get("/api/auth/session").json()["role"], "reviewer")
        connection = users_db.connect()
        try:
            users_db.set_role(connection, "wei", "viewer")
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/auth/session").json()["role"], "viewer")

    def test_deactivating_an_account_ends_its_live_session(self):
        self.add_user("wei", "s3cret-pass", "analyst")
        self.client.post("/api/auth/login", json={"username": "wei", "password": "s3cret-pass"})
        self.assertEqual(self.client.get("/api/overview").status_code, 200)
        connection = users_db.connect()
        try:
            users_db.set_active(connection, "wei", False)
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/overview").status_code, 401)

    def test_an_unknown_role_is_refused_rather_than_downgraded(self):
        connection = users_db.connect()
        try:
            with self.assertRaises(ValueError):
                users_db.create(connection, "wei", "s3cret-pass", "superuser",
                                by="测试", hasher=auth.hash_password)
        finally:
            connection.close()

    def test_the_legacy_accounts_file_is_imported_once_and_only_when_empty(self):
        # 部署过的机器要能平滑过渡：老的 accounts.json 连哈希一起搬进表里，
        # 原口令照常能登录；但只在表为空时搬，之后它不再是账号来源。
        auth.ACCOUNTS_PATH.write_text(json.dumps({"accounts": [
            {"username": "old", "role": "viewer",
             "password_hash": auth.hash_password("legacy-pass")}]}), encoding="utf-8")
        connection = users_db.connect()
        try:
            connection.execute("DELETE FROM platform_users")
            connection.commit()
            self.assertEqual(users_db.adopt_legacy_accounts(connection), 1)
            # 表里已经有人之后不再导入
            self.assertEqual(users_db.adopt_legacy_accounts(connection), 0)
        finally:
            connection.close()
        self.assertEqual(self.sign_in(username="old", password="legacy-pass").status_code, 200)

    def test_a_stored_password_is_not_recoverable_from_the_file(self):
        stored = auth.hash_password("s3cret")
        self.assertNotIn("s3cret", stored)
        self.assertTrue(auth.verify_password("s3cret", stored))
        self.assertFalse(auth.verify_password("wrong", stored))
        self.assertFalse(auth.verify_password("s3cret", "plaintext-not-a-hash"))

    def test_two_accounts_with_one_password_do_not_share_a_hash(self):
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_an_unknown_username_costs_what_a_wrong_password_costs(self):
        # Timing is the leak, and derivation rounds are what timing measures.
        # Deriving a decoy per attempt made an unknown username cost a hash plus
        # a verify — more than the single verify it was imitating — so the clock
        # answered "does this account exist", just backwards.
        self.add_user("wei", "s3cret-pass", "analyst")

        def rounds(username, password):
            with mock.patch.object(auth.hashlib, "pbkdf2_hmac",
                                   side_effect=hashlib.pbkdf2_hmac) as derive:
                auth.check(username, password)
            return derive.call_count

        self.assertEqual(rounds("wei", "wrong"), rounds("ghost", "wrong"))
        self.assertEqual(rounds("wei", "s3cret-pass"), rounds("ghost", "wrong"))


class RoleTests(AuthTestCase):
    def test_authority_accumulates_up_the_ladder(self):
        self.assertTrue(auth.allows("reviewer", "analyst"))
        self.assertTrue(auth.allows("analyst", "analyst"))
        self.assertFalse(auth.allows("viewer", "analyst"))
        self.assertFalse(auth.allows("analyst", "reviewer"))

    def test_an_unknown_role_carries_no_authority(self):
        self.assertFalse(auth.allows("superuser", "viewer"))
        self.assertFalse(auth.allows(None, "viewer"))


class TokenTests(AuthTestCase):
    def test_a_token_names_the_user_and_role_it_was_issued_to(self):
        self.assertEqual(auth.read_token(auth.issue_token("wei", "analyst")),
                         {"username": "wei", "role": "analyst"})

    def test_a_username_containing_the_delimiter_stays_one_username(self):
        # A delimited payload would have parsed "a:reviewer" into another account.
        session = auth.read_token(auth.issue_token("a:reviewer", "viewer"))
        self.assertEqual(session, {"username": "a:reviewer", "role": "viewer"})

    def test_a_token_naming_an_unknown_role_is_refused(self):
        forged = auth.issue_token("wei", "superuser")
        self.assertIsNone(auth.read_token(forged))

    def test_an_expired_token_proves_nothing(self):
        old = auth.issue_token("analyst", now=time.time() - auth.SESSION_TTL_SECONDS - 1)
        self.assertIsNone(auth.read_token(old))

    def test_a_tampered_payload_is_rejected(self):
        encoded, signature = auth.issue_token("analyst").rsplit(".", 1)
        forged = auth.issue_token("intruder").rsplit(".", 1)[0]
        self.assertIsNone(auth.read_token(f"{forged}.{signature}"))
        self.assertIsNone(auth.read_token(f"{encoded}.{signature[:-2]}xy"))
        self.assertIsNone(auth.read_token("not-a-token"))

    def test_a_token_signed_with_another_secret_is_rejected(self):
        token = auth.issue_token("analyst")
        os.environ["AUTH_SECRET"] = "a-different-secret"
        self.assertIsNone(auth.read_token(token))

    def test_the_cookie_is_not_readable_by_scripts(self):
        header = self.sign_in().headers["set-cookie"]
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=lax", header)


if __name__ == "__main__":
    unittest.main()


class UserManagementTests(AuthTestCase):
    """账号管理接口：改自己的口令谁都可以，其余一律要 admin。"""

    def setUp(self):
        super().setUp()
        self.add_user("boss", "boss-pass-1", "admin")

    def as_admin(self):
        self.client.post("/api/auth/login", json={"username": "boss", "password": "boss-pass-1"})

    def as_analyst(self):
        self.client.post("/api/auth/login", json={"username": "analyst", "password": PASSWORD})

    def test_only_an_admin_sees_the_account_list(self):
        self.as_analyst()
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.as_admin()
        names = [row["username"] for row in self.client.get("/api/users").json()["items"]]
        self.assertIn("analyst", names)

    def test_an_admin_can_create_an_account_that_then_signs_in(self):
        self.as_admin()
        created = self.client.post("/api/users", json={
            "username": "wei", "password": "brand-new-pass", "role": "analyst"})
        self.assertEqual(created.status_code, 200)
        self.client.post("/api/auth/logout")
        response = self.client.post("/api/auth/login",
                                    json={"username": "wei", "password": "brand-new-pass"})
        self.assertEqual((response.status_code, response.json()["role"]), (200, "analyst"))

    def test_a_short_password_is_refused_with_a_reason(self):
        self.as_admin()
        response = self.client.post("/api/users", json={
            "username": "wei", "password": "short", "role": "viewer"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("至少", response.json()["detail"])

    def test_anyone_can_change_their_own_password_but_must_prove_the_old_one(self):
        self.as_analyst()
        wrong = self.client.post("/api/users/me/password",
                                 json={"current": "not-it", "password": "another-pass"})
        self.assertEqual(wrong.status_code, 403)
        ok = self.client.post("/api/users/me/password",
                              json={"current": PASSWORD, "password": "another-pass"})
        self.assertEqual(ok.status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": "analyst", "password": "another-pass"}).status_code, 200)

    def test_an_analyst_cannot_reset_someone_elses_password(self):
        self.as_analyst()
        self.assertEqual(self.client.post("/api/users/boss/password",
                                          json={"password": "hijacked-pass"}).status_code, 403)

    def test_the_last_admin_cannot_lock_everyone_out(self):
        # 把自己降下去或停用自己，这台机器就再没有人能发账号——正是这次改动
        # 要消掉的那种死角。
        self.as_admin()
        demote = self.client.post("/api/users/boss/role", json={"role": "viewer"})
        self.assertEqual(demote.status_code, 400)
        self.assertIn("最后一个管理员", demote.json()["detail"])
        disable = self.client.post("/api/users/boss/active", json={"active": False})
        self.assertEqual(disable.status_code, 400)

    def test_a_reset_password_is_stored_hashed_not_in_the_clear(self):
        self.as_admin()
        self.client.post("/api/users/analyst/password", json={"password": "reset-by-admin"})
        connection = users_db.connect()
        try:
            row = users_db.row_of(connection, "analyst")
        finally:
            connection.close()
        self.assertNotIn("reset-by-admin", row["password_hash"])
        self.assertTrue(auth.verify_password("reset-by-admin", row["password_hash"]))


class PlatformAuditTests(AuthTestCase):
    """登录、导出、权限变更此前一行都没留下。

    另外六类记录（处理轮次、复核判定、采集日志、模型版本、投递、处置流水）都有自己的表，
    因为留痕不是它们存在的理由——它们本来就要记下这件事。这三类没有这样的本体记录：
    登录成功不改变任何业务数据，改一次角色只在账号表上留下一个当前值。
    """

    def setUp(self):
        super().setUp()
        self.add_user("boss", "boss-pass-1", "admin")

    def rows(self, action=None):
        db = platform_audit.connect()
        try:
            return platform_audit.recent(db, action=action, limit=100)
        finally:
            db.close()

    def test_a_successful_sign_in_leaves_one_line(self):
        self.sign_in()
        rows = self.rows("login")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["actor"], rows[0]["role"], rows[0]["outcome"]),
                         ("analyst", "analyst", "ok"))
        self.assertTrue(rows[0]["client"], "来源要记下来——「谁在试」之外还有「从哪里试」")

    def test_a_refused_sign_in_leaves_one_too(self):
        # 失败的那一半才是事后要查的：只记成功的，撞口令这件事就只存在于当时那个屏幕上。
        self.sign_in(password="wrong-password")
        rows = self.rows("login")
        self.assertEqual([(row["actor"], row["outcome"]) for row in rows],
                         [("analyst", "refused")])

    def test_the_password_never_reaches_the_log(self):
        # 一份记下了口令的审计日志，本身就是那个口令的第二个存放处。
        secret = "a-very-distinctive-password"
        self.sign_in(password=secret)
        self.sign_in()
        for row in self.rows():
            self.assertNotIn(secret, json.dumps(row, ensure_ascii=False))
            self.assertNotIn(PASSWORD, json.dumps(row, ensure_ascii=False))

    def test_being_throttled_is_itself_recorded(self):
        # 限流那一条不能省：它正是「有人在撞口令」的那个信号。
        for _ in range(12):
            self.sign_in(password="wrong-password")
        outcomes = [row["outcome"] for row in self.rows("login")]
        self.assertTrue(all(outcome == "refused" for outcome in outcomes))
        details = {row["detail"] for row in self.rows("login")}
        self.assertIn("尝试次数过多，已限流", details)

    def test_a_role_change_says_what_it_changed_from(self):
        # 账号表只存当前值。它答不出「谁把他改上去的、什么时候」，而权限这件事
        # 被问到的恰恰是这个。
        self.client.post("/api/auth/login", json={"username": "boss", "password": "boss-pass-1"})
        self.client.post("/api/users/analyst/role", json={"role": "reviewer"})
        rows = self.rows("role_change")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["actor"], rows[0]["object_id"], rows[0]["outcome"]),
                         ("boss", "analyst", "ok"))
        self.assertEqual(rows[0]["detail"], "分析师 → 核查负责人")

    def test_a_refused_permission_change_is_recorded_as_refused(self):
        self.client.post("/api/auth/login", json={"username": "boss", "password": "boss-pass-1"})
        self.assertEqual(self.client.post("/api/users/boss/role",
                                          json={"role": "viewer"}).status_code, 400)
        rows = self.rows("role_change")
        self.assertEqual([(row["outcome"], row["detail"]) for row in rows],
                         [("refused", "最后一个管理员不能降自己")])

    def test_deactivating_an_account_is_recorded(self):
        self.client.post("/api/auth/login", json={"username": "boss", "password": "boss-pass-1"})
        self.client.post("/api/users/analyst/active", json={"active": False})
        rows = self.rows("active_change")
        self.assertEqual([(row["object_id"], row["detail"]) for row in rows], [("analyst", "停用")])

    def test_the_trail_only_grows(self):
        """这个模块没有 UPDATE 也没有 DELETE——一份能被改的审计记录不是审计记录。"""
        self.assertFalse([name for name in dir(platform_audit)
                          if name.startswith(("update", "delete", "remove", "purge"))])
        self.sign_in()
        first = self.rows()
        self.sign_in()
        self.assertEqual(len(self.rows()), len(first) + 1)

    def test_a_failure_to_record_never_fails_the_action_it_records(self):
        """留痕写不进去的代价是少一行；写崩了的代价是这个动作从此不能用。"""
        with mock.patch.object(platform_audit, "connect", side_effect=OSError("disk gone")):
            response = self.sign_in()
        self.assertEqual(response.status_code, 200)


class WordingTests(AuthTestCase):
    """面向读者的文案统一用「密码」。

    登录页的标签写「密码」而错误提示写「口令」，是两套词并存——`login.test.js`
    早就钉过一次「它一直是账号 + 密码，标签却写着口令，读起来像全公司共用一句
    暗号」。这次把服务端返回的文案也收进同一套词。
    """

    def test_the_error_says_password_not_passphrase(self):
        detail = self.sign_in(password="wrong").json()["detail"]
        self.assertIn("密码", detail)
        self.assertNotIn("口令", detail)

    def test_the_password_rules_say_password_too(self):
        self.assertIn("密码", users_db.password_problem("wei", "short"))
        self.assertIn("密码", users_db.password_problem("wei", "wei"))
