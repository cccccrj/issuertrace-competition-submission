"""The built client bundle, checked for things it must never fetch.

The platform runs on 127.0.0.1 with no network, and the deploy archive now
carries `web/dist` so a target machine needs nothing else.  Both facts are void
if the bundle reaches for a font, a stylesheet or a script at runtime: the page
would render wrong on the machine it was built for, and only there, which is the
worst place to find out.

A component library makes this easy to get wrong — several ship webfonts or icon
fonts by default — so the rule is enforced mechanically rather than remembered.
Skipped when the bundle has not been built; `npm run build` is not this suite's
job, and `test_liveness_and_the_app_shell_stay_public` already fails loudly when
the shell is missing.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "web" / "dist"

# Namespace identifiers and documentation links are inert: they are compared as
# strings or printed in a warning, never fetched.  Everything else is a finding.
ALLOWED = (
    "http://www.w3.org/",
    "https://www.w3.org/",
    "https://vuejs.org/error-reference",
    # Loopback cannot leave the machine, which is the property this whole file
    # exists to protect — so a loopback default in a form (the webhook target
    # placeholder, matching scripts/webhook_receiver.py) is not a finding. Any
    # other host still is.
    "http://127.0.0.1",
    "http://localhost",
)

URL = re.compile(r"https?://[^\s\"'`)\\]+")
# Minifiers keep `/*! ... */` banners because licences require the copyright
# notice to survive distribution -- ZRender's carries a link to its LICENSE.
# Stripping the banner to satisfy this test would trade a compliance obligation
# for a green tick, so the banners are removed from the *scan* instead. That
# makes the check stricter, not weaker: a URL anywhere outside a banner now
# fails, including one a future dependency introduces.
BANNER = re.compile(r"/\*![\s\S]*?\*/")


def assets() -> list[Path]:
    return sorted(p for p in DIST.rglob("*") if p.suffix in {".js", ".css", ".html"})


def scannable(asset: Path) -> str:
    return BANNER.sub(" ", asset.read_text(encoding="utf-8", errors="replace"))


@unittest.skipUnless(DIST.is_dir() and any(DIST.iterdir()),
                     "web/dist 尚未构建：cd web && npm run build")
class OfflineBundleTests(unittest.TestCase):
    def test_no_asset_reaches_for_a_remote_host(self):
        found: dict[str, set[str]] = {}
        for asset in assets():
            remote = {url for url in URL.findall(scannable(asset))
                      if not url.startswith(ALLOWED)}
            if remote:
                found[asset.name] = remote
        self.assertEqual(found, {}, f"构建产物引用了外部地址：{found}")

    def test_no_webfont_is_declared(self):
        # An @font-face with a remote src renders in a fallback face offline,
        # silently, and only on the deployed machine.
        for asset in assets():
            if asset.suffix != ".css":
                continue
            self.assertNotIn("@font-face", asset.read_text(encoding="utf-8", errors="replace"),
                             f"{asset.name} 声明了 @font-face")

    def test_the_licence_banners_are_still_there(self):
        # The counterpart to stripping banners from the scan: they must actually
        # be present. A build configured to drop legal comments would silence
        # the check above by removing a notice the licence requires be kept.
        joined = " ".join(asset.read_text(encoding="utf-8", errors="replace")
                          for asset in assets() if asset.suffix == ".js")
        self.assertIn("Copyright", joined, "构建产物里没有任何版权声明，legal comments 可能被剥掉了")

    def test_the_shell_loads_only_its_own_assets(self):
        html = (DIST / "index.html").read_text(encoding="utf-8")
        for match in re.findall(r'(?:src|href)="([^"]+)"', html):
            self.assertFalse(match.startswith(("http://", "https://", "//")),
                             f"index.html 引用了外部资源：{match}")


if __name__ == "__main__":
    unittest.main()
