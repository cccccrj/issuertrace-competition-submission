"""Every versioned prompt in the processor, pinned to its version label.

A version label the code does not enforce is a label, not a version.  The cost
is on the record: extracting ``issue_code_rules`` into ``pipeline`` on
2026-09-05 dropped one judging line and rewrapped another while
``PROMPT_VERSION`` stayed at ``issue-codes-v2``.  Two runs an hour apart carry
the same label and different prompts, and E1's train micro-F1 fell 0.4429 ->
0.4286 -- seven times that task's noise floor -- with nothing in the suite to
say so.  ``benchmark_runs`` keys on the label, so every number recorded against
it silently became incomparable.

``tests.test_issue_codes.PromptVersionIsLoadBearing`` pinned two of the prompts
after that incident.  Eight modules carry a version label and ten prompts hang
off them, so the other eight were still unpinned -- including the four whose
guides are about to move into ``pipeline`` so ``/master-data`` can display them.
Pinning them *first* is what turns "moving a constant" into a checkable claim:
if the digests still match after the move, the move changed nothing.

The discovery test is the part that keeps this file honest.  Adding a pin by
hand is something the next sidecar's author can forget; failing the suite when
an unpinned prompt appears is not.
"""

import hashlib
import importlib
import pkgutil
import unittest

import processor


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# module -> (version attribute, version label, {prompt attribute: digest}).
#
# The version label is spelled out rather than read from the module so that
# renaming a version fails here too: a digest alone would happily follow a
# prompt into a version number nobody meant to publish.
PINNED: dict[str, tuple[str, str, dict[str, str]]] = {
    "processor.ai_label": ("PROMPT_VERSION", "event-label-v8", {
        "SYSTEM_PROMPT": "eaa5bc8b0687205e"}),
    "processor.event_types": ("PROMPT_VERSION", "event-types-v2", {
        "SYSTEM_PROMPT": "a40c9055cdb0fc61"}),
    "processor.event_impact": ("PROMPT_VERSION", "event-impact-v3", {
        "SYSTEM_PROMPT": "7d4c37850214710e"}),
    "processor.issue_codes": ("PROMPT_VERSION", "issue-codes-v2", {
        "SYSTEM_PROMPT": "d6867eb9fe8eafd2"}),
    "processor.production_mode": ("PROMPT_VERSION", "production-mode-v1", {
        "SYSTEM_PROMPT": "aa6038c1b00e244f"}),
    "processor.article_sentiment": ("PROMPT_VERSION", "article-sentiment-v1", {
        "SYSTEM_PROMPT": "2d0d2dae09deaf10"}),
    "processor.event_emotion": ("PROMPT_VERSION", "event-emotion-v1", {
        "SYSTEM_PROMPT": "76e6d7ae200b8b57"}),
    "processor.causal_link": ("PROMPT_VERSION", "causal-link-v1", {
        "SYSTEM_PROMPT": "0c1a3ed1622d09e7"}),
    "processor.claim_verdict": ("VERDICT_PROMPT_VERSION", "claim-verdict-v3", {
        "VERDICT_SYSTEM_PROMPT": "847d6fe7aabbd431"}),
    # One version label over three prompts: the pre-annotation queue judges
    # verdicts, diffs and events with the same annotator identity.
    "processor.pre_annotate": ("PROMPT_VERSION", "pre-annotate-v1", {
        "VERDICT_PROMPT": "d2feb7617f295295",
        "DIFF_PROMPT": "0aacee7e42cb7581",
        "EVENT_PROMPT": "1b7c27c21bb2181a"}),
}

# Below this length a ``*_PROMPT`` constant is a fragment (a fence marker, a
# format template), not a prompt whose wording carries a measurement.
PROMPT_MIN_CHARS = 200


def versioned_prompts(module) -> tuple[list[str], list[str]]:
    """The version attributes and prompt attributes a module exposes."""
    versions = [name for name in dir(module) if name.endswith("PROMPT_VERSION")
                and isinstance(getattr(module, name), str)]
    prompts = [name for name in dir(module) if name.endswith("PROMPT")
               and isinstance(getattr(module, name), str)
               and len(getattr(module, name)) >= PROMPT_MIN_CHARS]
    return versions, prompts


class PromptsMatchTheirVersions(unittest.TestCase):

    def test_every_pinned_prompt_still_hashes_to_its_pin(self):
        for name, (version_attr, version, prompts) in PINNED.items():
            module = importlib.import_module(name)
            with self.subTest(module=name):
                self.assertEqual(getattr(module, version_attr), version,
                                 f"{name}.{version_attr} 变了——改版本号要同时更新本文件的 PINNED")
            for prompt_attr, expected in prompts.items():
                with self.subTest(module=name, prompt=prompt_attr):
                    self.assertEqual(digest(getattr(module, prompt_attr)), expected,
                                     f"{version} 的 {prompt_attr} 变了——"
                                     "改提示词就要同时改版本号（并更新此处的 digest）")


class EveryVersionedPromptIsPinned(unittest.TestCase):
    """The guard that makes the pins above complete rather than a sample.

    Written as discovery instead of a list because the failure it prevents is an
    omission: the next narrow sidecar will define ``PROMPT_VERSION`` and
    ``SYSTEM_PROMPT`` like the six before it, and nothing else in the suite
    would notice that its wording is free to drift under a fixed label.
    """

    def test_no_processor_prompt_is_unpinned(self):
        missing: list[str] = []
        for found in pkgutil.iter_modules(processor.__path__):
            name = f"processor.{found.name}"
            module = importlib.import_module(name)
            versions, prompts = versioned_prompts(module)
            if not versions and not prompts:
                continue
            if name not in PINNED:
                missing.append(f"{name}（{', '.join(versions + prompts)}）")
                continue
            version_attr, _, pinned_prompts = PINNED[name]
            for attr in versions:
                if attr != version_attr:
                    missing.append(f"{name}.{attr}")
            for attr in prompts:
                if attr not in pinned_prompts:
                    missing.append(f"{name}.{attr}")
        self.assertEqual([], missing,
                         "这些带版本号的提示词没有被钉住，加进 PINNED："
                         + "、".join(missing))

    def test_a_module_carrying_a_prompt_carries_a_version_for_it(self):
        # A prompt without a version cannot be recorded in ``benchmark_runs`` or
        # a labels table, which is how a number becomes unattributable.
        for found in pkgutil.iter_modules(processor.__path__):
            module = importlib.import_module(f"processor.{found.name}")
            versions, prompts = versioned_prompts(module)
            if prompts:
                with self.subTest(module=found.name):
                    self.assertTrue(versions,
                                    f"processor.{found.name} 有提示词却没有版本号常量")


if __name__ == "__main__":
    unittest.main()
