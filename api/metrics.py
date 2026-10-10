"""Classification metrics, shared by the human review queue and the gold benchmark.

Extracted from ``api.routes.review`` so that one definition of macro-F1 serves
both evidence bases.  §11.4 asks for the same metric definition across the
evaluation sets; two implementations would drift, and the first place anyone
would notice is a report that no longer adds up.

Deliberately free of FastAPI and of any database: it is a function of a list of
(what the system said, what it should have been) pairs, so a processor module
can import it without dragging the web layer into a batch job.
"""
from __future__ import annotations

from typing import Any


def class_scores(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Per-class precision, recall and F1 over (what the system said, what the
    reviewer says it should have been) pairs, plus their macro average.

    Accuracy on its own hides the classes the evaluation is actually about:
    CONFLICT is a small share of the pool and could be wrong end to end without
    moving an accuracy figure.  §11.3 asks for macro-F1, which weights every
    class the same, so it is computed where the judgements live rather than
    reassembled in a spreadsheet afterwards.

    Subtypes are folded into their base class here.  The four-value decision is
    what the target is stated over; keeping ``PARTIAL/timing_mismatch`` separate
    would scatter one class across six columns with a handful of samples each.
    """
    folded = [(said.partition("/")[0], truth.partition("/")[0]) for said, truth in pairs]
    labels = sorted({label for pair in folded for label in pair})
    per_class: dict[str, Any] = {}
    for label in labels:
        hit = sum(1 for said, truth in folded if said == label == truth)
        said_it = sum(1 for said, _ in folded if said == label)
        should_have = sum(1 for _, truth in folded if truth == label)
        precision = hit / said_it if said_it else None
        recall = hit / should_have if should_have else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall else 0.0)
        per_class[label] = {"precision": round(precision, 4) if precision is not None else None,
                            "recall": round(recall, 4) if recall is not None else None,
                            "f1": round(f1, 4), "support": should_have, "predicted": said_it}
    # A class nobody has judged yet has no F1 to average; averaging a zero for it
    # would report a worse score than the sample supports.
    scored = [per_class[label]["f1"] for label in labels if per_class[label]["support"]]
    matrix: dict[str, dict[str, int]] = {}
    for said, truth in folded:
        matrix.setdefault(said, {}).setdefault(truth, 0)
        matrix[said][truth] += 1
    return {"per_class": per_class, "confusion": matrix,
            "macro_f1": round(sum(scored) / len(scored), 4) if scored else None,
            "labelled": len(folded)}


def cohen_kappa(pairs: list[tuple[str, str]]) -> dict[str, Any] | None:
    """Agreement between two annotators, net of the agreement chance alone buys.

    Raw agreement cannot be read on its own here, because the classes are nowhere
    near balanced: two reviewers who answer 「正确」 to everything agree on 100%
    of tasks and have demonstrated nothing about either of them.  κ subtracts the
    agreement their own marginal rates already predict, so that pair scores 0 and
    a pair who agree on the hard cases scores high.

    §11 of 总体方案-Pro asks for Cohen's κ, which is defined for exactly two
    raters.  Three reviewers are therefore reported as three pairs rather than
    one number, which would hide *which* two disagree — the only form of the
    answer anybody can act on.

    ``kappa`` comes back None when chance explains everything (both reviewers
    used a single class, and the same one): the statistic has no denominator
    there.  That is a real answer — "this pair has not been tested yet" — and
    reporting 0 for it would read as total disagreement, its opposite.
    """
    if not pairs:
        return None
    total = len(pairs)
    observed = sum(1 for first, second in pairs if first == second) / total
    labels = {label for pair in pairs for label in pair}
    expected = sum((sum(1 for first, _ in pairs if first == label) / total)
                   * (sum(1 for _, second in pairs if second == label) / total)
                   for label in labels)
    return {"n": total, "observed": round(observed, 4), "expected": round(expected, 4),
            "kappa": None if expected >= 1 else round((observed - expected) / (1 - expected), 4)}
