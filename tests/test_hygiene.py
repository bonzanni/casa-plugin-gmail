import pytest

from hygiene import (NEEDS, build_selector, filter_description, label_search_term,
                     normalize_action, normalize_criteria)


# --- build_selector ---

def test_selector_keeps_top_level_or_scoped():
    assert build_selector("from:a OR from:b", ["in:inbox"]) == "(from:a OR from:b) in:inbox"


def test_selector_without_needs_is_just_the_query():
    assert build_selector("from:x", [""]) == "(from:x)"
    assert build_selector("from:x", []) == "(from:x)"


def test_selector_rejects_blank_query():
    with pytest.raises(ValueError):
        build_selector("   ", ["in:inbox"])


def test_selector_several_needs_form_one_or_group():
    # Brace OR form, proven live in Task 0 (equals the union, negated terms included).
    assert build_selector("from:a OR from:b", ["in:inbox", "is:unread", "-is:starred"]) \
        == "(from:a OR from:b) {in:inbox is:unread -is:starred}"


def test_selector_drops_empty_and_duplicate_needs():
    assert build_selector("from:x", ["in:inbox", "", "in:inbox"]) == "(from:x) in:inbox"


def test_needs_table():
    assert NEEDS == {"archive": "in:inbox", "skip_inbox": "in:inbox", "mark_read": "is:unread",
                     "mark_unread": "is:read", "star": "-is:starred", "trash": ""}


# --- label_search_term (live form from Task 0) ---

def test_label_search_term_matches_gmail_form():
    assert label_search_term("Receipts/2026") == "receipts-2026"
    assert label_search_term("Travel Docs") == "travel-docs"
    assert label_search_term("Lesina Holding B.V./Bills") == "lesina-holding-b.v.-bills"


# --- normalisation ---

def test_normalize_treats_absent_and_default_as_equal():
    assert normalize_criteria({"query": "x", "hasAttachment": False}) == normalize_criteria({"query": "x"})
    assert normalize_criteria({"query": "x", "hasAttachment": True}) != normalize_criteria({"query": "x"})
    assert normalize_action({"addLabelIds": ["B", "A"]}) == normalize_action({"addLabelIds": ["A", "B"]})
    assert normalize_criteria({"query": "x", "sizeComparison": "unspecified"}) == normalize_criteria({"query": "x"})
    assert normalize_criteria({"query": "x", "size": 0}) == normalize_criteria({"query": "x"})
    assert normalize_criteria({"query": "x", "size": 10, "sizeComparison": "larger"}) \
        != normalize_criteria({"query": "x"})


# --- filter_description ---

def test_description_is_deterministic_and_complete():
    d = filter_description({"query": "from:x", "hasAttachment": True},
                           {"addLabelIds": ["Label_1", "STARRED"], "removeLabelIds": ["INBOX", "UNREAD"]},
                           {"Label_1": "News"})
    assert d == "query=from:x; hasAttachment=true → label News, star, skip inbox, mark read"


def test_description_never_omits_a_destructive_or_unknown_action():
    d = filter_description({"from": "x@y"}, {"addLabelIds": ["TRASH", "Label_404"],
                                             "removeLabelIds": ["Label_405"], "forward": "a@b"}, {})
    assert "trash" in d and "label Label_404" in d and "remove label Label_405" in d and "forward a@b" in d


def test_description_orders_criteria_fixed():
    d = filter_description({"subject": "s", "from": "f", "size": 5, "sizeComparison": "larger"},
                           {"removeLabelIds": ["INBOX"]}, {})
    assert d == "from=f; subject=s; size=5; sizeComparison=larger → skip inbox"


def test_normalize_keeps_an_explicit_zero_size_comparison():
    assert normalize_criteria({"query": "x", "size": 0, "sizeComparison": "smaller"}) \
        != normalize_criteria({"query": "x"})
    d = filter_description({"query": "x", "size": 0, "sizeComparison": "smaller"}, {"removeLabelIds": ["INBOX"]}, {})
    assert "size=0" in d and "sizeComparison=smaller" in d
