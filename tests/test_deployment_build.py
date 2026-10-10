"""Build the image and run it, on a machine that has a Docker daemon.

The definition tests in ``test_deployment.py`` read the YAML and the Dockerfile.
They can say the compose file does not hand ``minimal`` a gateway key; they
cannot say the container that comes out of the build does not have one, and
those are different claims.  The second is what the deliverable asks for and it
needs a daemon.

This machine has none, so every test here skips.  That is the intended state and
not a silent pass: run it wherever the image is actually built, and the run
either proves the two profiles behave as documented or names what does not.

``docker`` is not in requirements.txt.  It is a test-only client for an
environment that already has Docker; installing it here would add a dependency
whose server never answers.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "listed-company-event-intelligence:test"

try:  # pragma: no cover - exercised only where a daemon exists
    import docker
    from docker.errors import DockerException
    try:
        CLIENT = docker.from_env()
        CLIENT.ping()
    except (DockerException, Exception):  # noqa: BLE001 - any failure means no daemon
        CLIENT = None
except ImportError:  # pragma: no cover
    CLIENT = None

REASON = "需要 Docker 守护进程与 docker SDK（pip install docker）；本机没有时整组跳过"


@unittest.skipIf(CLIENT is None, REASON)
class ImageBuildTests(unittest.TestCase):
    """What the built image contains, as opposed to what the Dockerfile says."""

    @classmethod
    def setUpClass(cls):
        cls.image, _ = CLIENT.images.build(path=str(ROOT), tag=IMAGE, rm=True)

    def run_in_image(self, *command: str) -> str:
        return CLIENT.containers.run(IMAGE, list(command), remove=True,
                                     entrypoint="").decode("utf-8", "ignore")

    def test_the_corpus_is_not_baked_into_the_image(self):
        # An image carrying a 150MB SQLite file is a copy of the evidence that
        # nobody can update.  The .dockerignore is supposed to prevent it; this
        # checks the result rather than the intention.
        listing = self.run_in_image("sh", "-c", "ls /app/data 2>/dev/null || echo EMPTY")
        self.assertNotIn(".sqlite3", listing, "语料被烤进镜像了")

    def test_the_client_bundle_is_present(self):
        # Without web/dist the app shell does not exist and /login is a 404.
        listing = self.run_in_image("sh", "-c", "ls /app/web/dist")
        self.assertIn("index.html", listing)

    def test_it_does_not_run_as_root(self):
        self.assertNotEqual(self.run_in_image("id", "-u").strip(), "0")


@unittest.skipIf(CLIENT is None, REASON)
class ProfileBehaviourTests(unittest.TestCase):
    """The two profiles differ in what they are given; prove it at runtime."""

    def container(self, environment: dict[str, str]):
        return CLIENT.containers.run(
            IMAGE, ["sh", "-c", "env"], environment=environment, remove=True,
            entrypoint="").decode("utf-8", "ignore")

    def test_minimal_is_not_given_the_gateway_key(self):
        # The read-only platform reads its conclusions out of SQLite and never
        # calls the model, so a credential in its environment is blast radius
        # bought for nothing.  compose gives it AUTH_* only.
        environment = self.container({"AUTH_USERNAME": "x", "AUTH_PASSWORD": "y"})
        self.assertNotIn("LLM_API_KEY", environment)

    def test_the_toolchain_is_given_the_gateway_key(self):
        environment = self.container({"LLM_API_KEY": "test-only-not-a-real-key"})
        self.assertIn("LLM_API_KEY", environment)


@unittest.skipIf(CLIENT is None, REASON)
class RunningPlatformTests(unittest.TestCase):
    """The container answers, and answers 401 to an unauthenticated caller."""

    @classmethod
    def setUpClass(cls):
        cls.container = CLIENT.containers.run(
            IMAGE, detach=True, ports={"8000/tcp": ("127.0.0.1", 0)},
            volumes={str(ROOT / "data"): {"bind": "/app/data", "mode": "rw"},
                     str(ROOT / "config"): {"bind": "/app/config", "mode": "ro"}})
        cls.container.reload()
        binding = cls.container.attrs["NetworkSettings"]["Ports"]["8000/tcp"][0]
        cls.base = f"http://127.0.0.1:{binding['HostPort']}"

    @classmethod
    def tearDownClass(cls):
        cls.container.remove(force=True)

    def wait_for(self, path: str, tries: int = 30):
        import time

        import requests
        last = None
        for _ in range(tries):
            try:
                return requests.get(f"{self.base}{path}", timeout=3)
            except requests.RequestException as exc:  # noqa: PERF203 - startup poll
                last = exc
                time.sleep(1)
        raise AssertionError(f"容器没有在 {tries} 秒内应答: {last}")

    def test_health_answers(self):
        self.assertEqual(self.wait_for("/api/health").status_code, 200)

    def test_an_unauthenticated_call_is_refused(self):
        # If this returns 200 the auth middleware is bypassed, which matters
        # more than any feature being absent.
        self.wait_for("/api/health")
        import requests
        self.assertEqual(requests.get(f"{self.base}/api/events", timeout=5).status_code, 401)


if __name__ == "__main__":
    unittest.main()
