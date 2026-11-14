"""Pure helpers for the inbox-hygiene tools: search selectors, label search
terms, and filter normalisation/description. No Gmail calls here.

Search syntax facts below were checked against a live Workspace inbox
(2026-11-14, ID sets, pagination exhausted): the brace group `{a -b}` equals
the union of the separate searches, negated terms included; `label:` takes
the name lowercased with spaces and "/" turned into "-"; quoted label names
match nothing.
"""

# The term that selects mail still needing a change. Label adds and removes
# are built from the label's name with label_search_term().
NEEDS = {
    "archive": "in:inbox",
    "skip_inbox": "in:inbox",
    "mark_read": "is:unread",
    "mark_unread": "is:read",
    "star": "-is:starred",
    "trash": "",  # the default search already leaves out Trash
}

_CRITERIA_ORDER = ("from", "to", "subject", "query", "negatedQuery", "hasAttachment",
                   "excludeChats", "size", "sizeComparison")

_ADDED = {"STARRED": "star", "IMPORTANT": "important", "TRASH": "trash", "SPAM": "spam",
          "INBOX": "add to inbox", "UNREAD": "mark unread"}
_REMOVED = {"INBOX": "skip inbox", "UNREAD": "mark read", "SPAM": "never spam",
            "IMPORTANT": "never important"}


def label_search_term(name: str) -> str:
    """The operand Gmail's `label:` search operator takes for a label name."""
    return name.lower().replace(" ", "-").replace("/", "-")


def build_selector(query: str, needs: list[str]) -> str:
    """The caller's query, parenthesised, narrowed to mail that still needs at
    least one of `needs` (several needs form one OR group)."""
    if not query or not query.strip():
        raise ValueError("A search query is required (use in:inbox to mean the whole inbox).")
    terms = list(dict.fromkeys(n for n in needs if n))
    base = f"({query.strip()})"
    if not terms:
        return base
    if len(terms) == 1:
        return f"{base} {terms[0]}"
    return f"{base} {{{' '.join(terms)}}}"


def normalize_criteria(criteria: dict) -> dict:
    """Criteria with Gmail's defaults removed, so equal behaviour compares equal."""
    out = {k: v for k, v in (criteria or {}).items() if v not in (None, False, "", [], 0)}
    if out.get("sizeComparison") == "unspecified":
        out.pop("sizeComparison")
    if "size" not in out:
        out.pop("sizeComparison", None)
    return out


def normalize_action(action: dict) -> dict:
    out = {}
    for k, v in (action or {}).items():
        if v in (None, False, "", []):
            continue
        out[k] = sorted(v) if isinstance(v, list) else v
    return out


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def filter_description(criteria: dict, action: dict, label_names: dict[str, str]) -> str:
    """One deterministic line naming every criterion and every action."""
    crit = normalize_criteria(criteria)
    keys = [k for k in _CRITERIA_ORDER if k in crit] + sorted(k for k in crit if k not in _CRITERIA_ORDER)
    left = "; ".join(f"{k}={_value(crit[k])}" for k in keys)

    def name(lid):
        return label_names.get(lid, lid)

    act = normalize_action(action)
    parts = [_ADDED.get(lid, f"label {name(lid)}") for lid in act.get("addLabelIds", [])]
    parts += [_REMOVED.get(lid, f"remove label {name(lid)}") for lid in act.get("removeLabelIds", [])]
    if act.get("forward"):
        parts.append(f"forward {act['forward']}")
    return f"{left} → {', '.join(parts) if parts else '(no action)'}"
