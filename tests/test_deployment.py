"""The container definition, checked against the application it ships.

Docker is not installed on the machine this was written on, so these are not a
substitute for building the image — they are the part that can be checked
without a daemon: that the image copies every package the app imports, that the
corpus is mounted rather than baked in, and that the port is not published to
the world.  A missing COPY here is a container that starts and 500s on import.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICE = COMPOSE["services"]["platform"]


class ImageTests(unittest.TestCase):
    def test_every_package_the_app_imports_is_copied_in(self):
        packages = {path.name for path in ROOT.iterdir()
                    if path.is_dir() and (path / "__init__.py").exists()
                    and path.name not in {"tests"}}
        copied = set(re.findall(r"^COPY (\w+) ", DOCKERFILE, re.M))
        self.assertEqual(packages - copied, set())

    def test_the_client_bundle_is_built_and_carried_over(self):
        # api/main.py serves web/dist; without it every page is a 404.
        self.assertIn("npm run build", DOCKERFILE)
        self.assertIn("COPY --from=web /build/web/dist ./web/dist", DOCKERFILE)

    def test_the_corpus_is_not_baked_into_the_image(self):
        # 150 MB of evidence inside an image is a copy nobody can update.
        self.assertNotRegex(DOCKERFILE, r"^COPY data", )
        self.assertIn("data/", (ROOT / ".dockerignore").read_text())

    def test_it_does_not_run_as_root(self):
        self.assertIn("USER platform", DOCKERFILE)

    def test_the_dependency_list_is_the_one_in_the_repository(self):
        self.assertIn("pip install --no-cache-dir -r requirements.txt", DOCKERFILE)


class ComposeTests(unittest.TestCase):
    def test_the_database_and_raw_files_are_mounted_from_the_host(self):
        self.assertIn("./data:/app/data", SERVICE["volumes"])

    def test_configuration_is_mounted_read_only(self):
        # The company list and source scope are rules, not state.
        self.assertIn("./config:/app/config:ro", SERVICE["volumes"])

    def test_the_port_is_published_to_loopback_only(self):
        for mapping in SERVICE["ports"]:
            self.assertTrue(str(mapping).startswith("127.0.0.1:"), mapping)

    def test_credentials_come_from_the_environment_not_the_file(self):
        for key, value in SERVICE["environment"].items():
            self.assertRegex(str(value), r"^\$\{[A-Z_]+:-\}$", f"{key} looks hardcoded")


if __name__ == "__main__":
    unittest.main()


class ProfileTests(unittest.TestCase):
    """The two profiles the plan asks for, and the one thing that separates them."""

    SERVICES = COMPOSE["services"]

    def test_both_profiles_exist_and_the_platform_is_in_both(self):
        # minimal is the read-only platform; full adds the toolchain beside it,
        # so the platform has to answer to both names.
        self.assertEqual(set(self.SERVICES["platform"]["profiles"]), {"minimal", "full"})
        self.assertEqual(self.SERVICES["toolchain"]["profiles"], ["full"])

    def test_minimal_never_mounts_the_raw_evidence_or_the_secrets(self):
        # It reads its conclusions out of SQLite and the exports; opening the
        # raw files or the model key would make the 50MB archive a lie.
        for mount in self.SERVICES["platform"]["volumes"]:
            self.assertNotIn("/app/.env", mount)
        self.assertNotIn("LLM_API_KEY", self.SERVICES["platform"].get("environment", {}))

    def test_only_the_toolchain_is_given_the_gateway_key(self):
        # Collection and processing call the model; the read-only platform has
        # no reason to hold a credential it never uses.
        self.assertIn("LLM_API_KEY", self.SERVICES["toolchain"]["environment"])
        self.assertRegex(str(self.SERVICES["toolchain"]["environment"]["LLM_API_KEY"]),
                         r"^\$\{[A-Z_]+:-\}$")

    def test_the_toolchain_publishes_no_port(self):
        # It is something you run, not something that listens.
        self.assertIsNone(self.SERVICES["toolchain"].get("ports"))

    def test_the_toolchain_does_not_restart_itself(self):
        # A one-shot runtime with restart:unless-stopped is a crash loop.
        self.assertEqual(str(self.SERVICES["toolchain"]["restart"]), "no")

    def test_both_services_are_the_one_image(self):
        self.assertEqual(self.SERVICES["platform"]["image"], self.SERVICES["toolchain"]["image"])
