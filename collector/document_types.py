#!/usr/bin/env python3
"""One vocabulary for document types, shared by every collector.

The processors select documents by type and nothing tells them when a type is
misspelt -- the document is simply never chosen.  That failure is silent and it
happened three times in one collection round:

* announcements filed as ``announcement`` (singular): 537 filings collected,
  parsed, turned into events, and absent from the evidence index, whose
  ``EVIDENCE_DOCUMENT_TYPES`` names the plural;
* review responses filed as ``review_inquiry_or_response``: ``inquiry_align``
  looks for ``review_responses`` and finds none;
* prospectus drafts recovered from a second host with no hint at all, typed
  ``pdf`` from the response, invisible to both the evidence index and the
  version comparison.

So the vocabulary lives here, the selection sets are stated here, and
``tests/test_document_types.py`` asserts that every type a collector writes is
one a processor recognises.  A type that indexes nothing is a collection bug,
not a naming preference.
"""
from __future__ import annotations

# What the processors actually select on.  Keep in step with
# processor/claim_index.py (EVIDENCE_DOCUMENT_TYPES) and
# processor/disclosure_diff.py (the --document-type default).
EVIDENCE_TYPES = frozenset({"prospectus", "review_responses", "announcements", "attachment"})

# Types a collector may legitimately write that carry no citable evidence:
# regulatory paperwork kept for the record, and audit trail.
SUPPORTING_TYPES = frozenset({
    "listing_documents", "review_inquiries", "periodic_reports", "corrections",
    "company_news", "company_profile", "independent_news",
})
AUDIT_TYPES = frozenset({"search_response", "landing_page", "api_response", "lists"})

KNOWN_TYPES = EVIDENCE_TYPES | SUPPORTING_TYPES | AUDIT_TYPES

# Names individual adapters grew before there was a shared list.  Mapping rather
# than renaming in place: the stored rows keep their history and the mapping
# says, in one place, what each of them was always meant to be.
ALIASES = {
    "announcement": "announcements",
    "review_inquiry_or_response": "review_responses",
    "listing_document": "listing_documents",
    "listing_committee_document": "listing_documents",
    "registration_result": "listing_documents",
    "termination_decision": "listing_documents",
    "periodic_report": "periodic_reports",
    "correction": "corrections",
}


def canonical(document_type: str | None) -> str | None:
    """The name the processors select on, for a type a collector produced."""
    if document_type is None:
        return None
    return ALIASES.get(document_type, document_type)


def is_citable(document_type: str | None) -> bool:
    """Whether a document of this type can end up cited in a verdict."""
    return canonical(document_type) in EVIDENCE_TYPES
