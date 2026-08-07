# tests/test_docs.py
"""Pins on the setup and troubleshooting prose.

The README is the only artifact a reader follows before the plugin can run at
all, so two classes of error in it are as breaking as a code bug: prescribing a
console option the reader cannot select, and quoting a diagnostic string the
code no longer emits. Both shipped. These tests are drift guards, not style
checks: the diagnostics are compared against the source that emits them.
"""
import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _read(*parts):
    return (_ROOT.joinpath(*parts)).read_text()


README = "README.md"

_CASA_SECTION = "### 3. Configure the casa deployment"


def _casa_steps():
    """The top-level numbered items of "3. Configure the casa deployment",
    indexed from 1 the way the README's own "Step 3.N" pointers count them."""
    section = _read(README).split(_CASA_SECTION)[1].split("\n####")[0]
    # Each item runs to the next top-level number, so its indented sub-bullets
    # count as part of it.
    return [part.strip() for part in re.split(r"^\d+\. ", section, flags=re.M)[1:]]


def _step_number(marker):
    hits = [i for i, step in enumerate(_casa_steps(), 1) if marker in step]
    assert len(hits) == 1, f"{marker!r} identifies {len(hits)} steps, need exactly 1"
    return hits[0]


# ── Fix D: the OAuth client type a personal account can actually create ────

def test_readme_does_not_prescribe_an_internal_only_client():
    """"Internal" is selectable only inside a Workspace organization. Requiring
    it blocks a personal-account reader before authorization is even reached."""
    text = _read(README)
    assert "An Internal OAuth 2.0 client" not in text
    assert "set the User Type to **Internal**" not in text


def test_readme_explains_both_consent_screen_user_types():
    text = _read(README)
    assert "**External**" in text
    assert "Test users" in text
    # ...without ever contradicting the Web-application requirement.
    assert "must still be **Web application**" in text


# ── Fix E: the redirect URI must be discoverable, not guessed ──────────────

def test_readme_gives_a_concrete_way_to_obtain_the_redirect_uri():
    """The old text said to read `/data/callbacks/<plugin-name>/ready.json`
    while warning that a scoped install has a different effective name — i.e.
    it asked the reader to guess the very path it told them not to guess."""
    text = _read(README)
    assert "/data/callbacks/<plugin-name>/ready.json" not in text
    assert "grep -o '\"redirect_uri\":[^,}]*' /data/callbacks/*/ready.json" in text
    assert "the tool returns a `redirect_uri` field" in text


# ── Fix F: every quoted diagnostic must be a string the code emits ─────────

def test_readme_no_longer_quotes_the_obsolete_token_diagnostic():
    assert "stored token invalid or expired" not in _read(README)


def test_every_quoted_diagnostic_matches_its_source():
    """Each fragment must appear BOTH in the README and in the module that
    prints it — so renaming a message without touching the docs fails here."""
    pairs = [
        ("server/auth.py", "stored token is dead — re-auth needed"),
        ("server/auth.py", "the stored credential authorizes "),
        ("server/auth.py", "could not refresh right now "),
        ("server/server.py", "Gmail is not authenticated. Call gmail_auth_start"),
        ("server/auth_flow.py",
         "Authorization was not granted ({error}). Nothing has changed."),
        ("server/auth_flow.py", "That authorization was granted by "),
        ("server/auth_flow.py", "No authorization result was waiting"),
    ]
    readme = _read(README)
    for source, fragment in pairs:
        assert fragment in _read(source), f"{fragment!r} is not emitted by {source}"
        readme_fragment = fragment.replace("{error}", "access_denied")
        assert readme_fragment in readme, f"{readme_fragment!r} missing from README"


# ── Round 2: a step cannot depend on configuration a later step performs ───

def test_the_env_vars_are_configured_before_the_tool_that_needs_them():
    """The redirect URI is discovered by calling `gmail_auth_start`, and the
    server exits in `read_env()` unless all three variables are set. A reader
    following the numbered steps in order must have set them first."""
    env_step = _step_number("GMAIL_CLIENT_SECRET")
    discovery_step = _step_number("gmail_auth_start")
    assert env_step < discovery_step, (
        "the tool-based redirect-URI discovery route cannot run before the "
        "step that sets the environment it needs"
    )


def test_the_env_vars_the_readme_names_are_the_ones_the_server_demands():
    """The claim above is only load-bearing if these are really the variables
    whose absence exits the process."""
    required = re.search(r"_REQUIRED_ENV_VARS = \[(.*?)\]",
                         _read("server", "auth.py"), re.S).group(1)
    step = _casa_steps()[_step_number("GMAIL_CLIENT_SECRET") - 1]
    for name in re.findall(r'"([^"]+)"', required):
        assert f"`{name}`" in step, f"{name} is required but the step omits it"
    assert "sys.exit(1)" in _read("server", "auth.py")


def test_the_host_side_route_is_the_one_that_needs_no_running_server():
    step = _casa_steps()[_step_number("gmail_auth_start") - 1]
    assert "without a running server" in step


def test_consent_is_approved_only_after_the_environment_is_configured():
    """Approving the consent DM is precisely what makes casa dispatch
    `setup_gmail`, and casa dispatches it ONCE: it treats an accepted agent turn
    as dispatched without correlating whether the tool executed, so there is no
    retry. With the three variables still unset the server exits in
    `read_env()`, the dispatched call fails, and the setup episode is spent —
    the promised automatic link never arrives. This is not hypothetical: on the
    live host the callback-consent ack is timestamped 10:53:27Z and the
    variables were wired at 10:54:21Z."""
    env_step = _step_number("GMAIL_CLIENT_SECRET")
    consent_step = _step_number("callback-consent DM")
    assert env_step < consent_step, (
        "the README has the operator approve consent — which triggers the "
        "one-shot setup dispatch — before configuring the environment that "
        "dispatch depends on"
    )


def test_redirect_uri_registration_follows_consent_because_casa_forces_it():
    """Consent cannot simply be moved to the very end. Casa publishes a
    plugin's `ready.json`/`.index` only for a ROUTED callback, and an unacked
    callback is never routed (callback_reconcile.py: an "unacked" plugin's
    published pair is retired as an orphan) — so before consent the redirect
    URI does not exist to be read, by either documented route. Registration
    therefore has to come after consent, and the README must say why rather
    than leave a reader to "fix" the order back into a dead end."""
    consent_step = _step_number("callback-consent DM")
    register_step = _step_number("Authorized redirect URIs")
    assert consent_step < register_step
    step = _casa_steps()[register_step - 1]
    assert "only once its callback is *routed*" in step
    assert "the value does not exist to be read" in step


def test_the_consent_step_warns_that_approval_is_what_dispatches_setup():
    """The reader has to know WHY the order matters, or they will reorder it."""
    step = _casa_steps()[_step_number("callback-consent DM") - 1]
    assert "MCP server is healthy" in step
    assert "no automatic retry" in step
    assert "dispatches it **once**" in step
    # ...and that the link it produces must not be opened before 3.5.
    assert "finish Step 3.5 before opening it" in step


def test_the_missing_link_advice_bounds_the_wait_and_names_an_unhealthy_server():
    """"If no link arrives" gave no wait bound — leaving the reader to guess how
    long "automatic" takes — and omitted the very failure the old step order
    encouraged: a server that exited at startup for want of its env vars."""
    text = _read(README)
    assert "within about two minutes" in text
    assert "MCP server is not running or not healthy" in text
    assert "Step 3.3" in text          # points at the env step by number


def test_every_step_3_pointer_names_the_step_it_means():
    """A renumbering that leaves a stale `Step 3.N` behind is the same class of
    bug as the ordering it fixes — it sends the reader to the wrong place."""
    text = _read(README)
    for marker, pointer in [
        ("`public_url`", "`public_url` (Step 3.{n})"),
        ("assigned role", "assigned role (Step 3.{n})"),
        ("callback-consent DM", "Consent DM not approved (Step 3.{n})"),
        ("Authorized redirect URIs", "re-check Step 3.{n} above"),
        ("Authorized redirect URIs", "discovery command in Step 3.{n}"),
    ]:
        expected = pointer.format(n=_step_number(marker))
        assert expected in text, f"stale cross-reference: expected {expected!r}"

    numbered = len(_casa_steps())
    for ref in re.findall(r"Step 3\.(\d+)", text):
        assert 1 <= int(ref) <= numbered, f"Step 3.{ref} does not exist"


# ── Round 2: External + Testing expires the connection weekly ──────────────

def test_readme_documents_the_testing_status_refresh_token_expiry():
    """Round 1 told personal-account readers to use External and leave the app
    unpublished. Google expires refresh tokens issued by an External app in
    Testing after 7 days, so that advice breaks the connection weekly."""
    text = _read(README)
    assert "7 days" in text
    assert "publishing status" in text
    assert "In production" in text
    # ...and no longer claims the unpublished Testing path is equivalent.
    assert "which works the same here" not in text
    assert "while the app stays unpublished" not in text


def test_readme_is_honest_about_what_production_entails():
    """These scopes are sensitive/restricted, so "just publish it" must not
    read as a formality — nor as though verification were mandatory."""
    text = _read(README)
    assert "restricted" in text and "sensitive" in text
    assert "Google hasn't verified this app" in text
    assert "security assessment" in text
    assert "does **not** require passing Google verification first" in text


# ── Fix C's companion: the skill must relay an empty result honestly ───────

def test_skill_tells_the_agent_an_empty_collect_is_not_a_success():
    text = _read("skills", "gmail", "SKILL.md")
    assert "Nothing waiting" in text
    assert "never\n  report `status: \"ok\"` on its own as confirmation" in text


# ── v0.5.1: casa auto-runs the declared setup tool after consent ───────────

def test_docs_name_the_setup_tool_the_manifest_declares():
    """Casa dispatches whatever `casa.setupTool` names, and nothing else. Docs
    naming a different tool would describe a flow that never happens; a name the
    server does not define would fail the episode at the agent."""
    import json
    name = json.loads(_read(".claude-plugin", "plugin.json"))["casa"]["setupTool"]
    assert f"`{name}`" in _read(README), f"README never names {name}"
    assert f"`{name}`" in _read("skills", "gmail", "SKILL.md"), \
        f"SKILL.md never names {name}"
    assert f"def {name}(" in _read("server", "server.py"), \
        "the manifest declares a setup tool the server does not define"


def test_readme_documents_the_automatic_setup_dispatch():
    """The v0.5.0 walkthrough ended with the operator asking the agent to connect
    Gmail — the manual step the consent gate was supposed to replace."""
    text = _read(README)
    assert "dispatches `setup_gmail` automatically" in text
    assert "without being asked" in text
    assert "gmail_auth_start" in text          # manual fallback still documented


def test_skill_tells_the_agent_already_connected_is_not_a_new_authorization():
    text = _read("skills", "gmail", "SKILL.md")
    assert "casa may dispatch `setup_gmail`" in text
    assert "do not report it as a new authorization" in text


# ── Google refuses OAuth in an embedded (in-app) browser ──────────────────
#
# Confirmed by a controlled bisect on the operator's phone: same device, same
# Google account, same URL. Tapping the link in the chat client fails with
# Google's "Something went wrong" after sign-in; long-pressing the SAME link
# and opening it in Chrome reaches the consent screen and completes. The only
# variable that changed is the browser context. Casa's callback endpoint
# received zero requests from the failing attempts, so nothing is redirected
# back and the flow stops silently — which is why the instruction has to be in
# the skill: it is the only thing standing between the operator and a dead end.

def test_the_skill_tells_the_agent_to_send_the_operator_to_a_real_browser():
    """The hands-free flow delivers the link on the one surface where tapping
    it cannot work, so this is the highest-value line in the file."""
    text = _read("skills", "gmail", "SKILL.md")
    assert "open it in a real browser" in text
    assert "Google refuses OAuth sign-in" in text


def test_the_skill_puts_the_browser_warning_before_the_link_not_after():
    """On a phone the operator taps the link before reading whatever follows
    it, so the instruction only works if it comes first. This pins the
    placement rule, not the rationale already pinned above."""
    text = _read("skills", "gmail", "SKILL.md")
    assert "before the link" in text
    assert "never after" in text


def test_the_skill_names_the_signature_and_the_remedy():
    """"Something went wrong" AFTER sign-in is the tell; retrying the tap is
    not the remedy, and neither is a fresh link."""
    text = _read("skills", "gmail", "SKILL.md")
    assert "Something went wrong" in text
    assert "after signing in" in text
    assert "reopen the *same* link" in text


def test_the_readme_records_the_embedded_browser_constraint_in_both_places():
    """Once in the walkthrough (where it prevents the failure) and once in
    troubleshooting (where it is looked up after the failure)."""
    text = _read(README)
    walkthrough = text.split("### 4. Run the authorization flow")[1].split("\n## ")[0]
    troubleshooting = text.split("## Troubleshooting")[1]
    claim = "Google refuses to run OAuth sign-in in an embedded browser"
    assert claim in walkthrough, "the walkthrough does not warn about it"
    assert claim in troubleshooting, "troubleshooting does not explain it"
    assert "Something went wrong" in troubleshooting


def test_the_embedded_browser_claim_is_not_overstated():
    """We observed one chat client's in-app browser and never captured Google's
    underlying error code, so the docs must name neither — and must not imply
    the plugin can detect a browser it cannot see."""
    for text in (_read(README), _read("skills", "gmail", "SKILL.md")):
        assert "Telegram" not in text
        for code in ("disallowed_useragent", "invalid_request", "403"):
            assert code not in text, f"names a Google error code we never saw: {code}"
    assert "cannot see which browser" in _read(README)
    assert "cannot detect" in _read(README)


def test_the_skill_no_longer_calls_an_unavailable_setup_a_non_failure():
    """`unavailable` means automatic setup did NOT complete — actionable and
    retryable. And the skill must not attribute a cause: casa dispatches only
    after consent is approved AND a live route check, so "usually the consent
    DM or the plugin's role" is wrong on its face. Relay the returned reason."""
    text = _read("skills", "gmail", "SKILL.md")
    assert "automatic setup did not complete" in text
    assert "neither is a failure" not in text
    assert "usually the consent" not in text
    assert "Do not guess at the cause" in text


def test_the_skill_covers_every_status_the_setup_tool_can_return():
    """Drift guard: a status the server emits but the skill never mentions is a
    result the agent has no instruction for."""
    source = _read("server", "server.py")
    skill = _read("skills", "gmail", "SKILL.md")
    emitted = set(re.findall(r'"status": "(\w+)"', source))
    emitted |= set(re.findall(r'result\["status"\] = "(\w+)"', source))
    for status in emitted:
        assert f"`{status}`" in skill, \
            f"server.py returns {status!r} but SKILL.md never mentions it"


# ── Sol round 2: the README must not promise what the code cannot keep ────

def test_the_readme_no_longer_calls_re_running_setup_always_safe():
    """The old sentence promised re-running `setup_gmail` was "always safe"
    because it would report an outstanding link instead of minting a second
    one. The check behind it read only `attempts/`, which casa materializes up
    to five minutes AFTER the mint — so for those minutes it minted twice. The
    promise now has to name what the check actually consults."""
    text = _read(README)
    assert "Re-running `setup_gmail` is always safe" not in text
    assert "`pending/`" in text and "`attempts/`" in text
    # ...and the code must really consult both.
    assert "pending_mint_times" in _read("server", "casa_callback.py")
    assert "_cb.pending_mint_times()" in _read("server", "server.py")


def test_the_redirect_uri_step_warns_that_asking_for_a_link_mints_another():
    """Step 3.5's preferred route asks the agent to call `gmail_auth_start`, which
    answers a direct request and always mints — so following it after automatic
    setup has already posted a link leaves two live authorizations. The
    setup-tool guard cannot prevent that; the reader has to know."""
    step = _casa_steps()[_step_number("gmail_auth_start") - 1]
    assert "always mints a fresh link" in step


def test_the_skill_quotes_the_status_the_setup_tool_actually_returns():
    """Same drift guard as the diagnostics above: the status the skill tells the
    agent to recognise must be the one server.py emits."""
    assert '"already_connected"' in _read("server", "server.py")
    assert "`already_connected`" in _read("skills", "gmail", "SKILL.md")


# ── configuration_error recovery must not promise same-session success ────
#
# GMAIL_CLIENT_ID/SECRET are read once into process memory by read_env() and
# cached there; casa's env reload only re-sources plugin-env.conf into casa's
# own process, never the live MCP server subprocess. So re-running
# `setup_gmail` in the same session probes with the stale secret forever. The
# MCP server is per-session, so a new session (lighter than a full restart)
# is what actually picks up the fix.

def _configuration_error_entry():
    return _read(README).split("the OAuth client configuration was rejected")[1] \
                        .split("\n\n**`")[0]


def test_the_configuration_error_recovery_does_not_promise_same_session_fix():
    """The troubleshooting entry for `configuration_error` must not tell the
    reader that re-running `setup_gmail` alone recovers — that only works
    after a new session (or a restart) re-reads the corrected env vars.

    Banning the one phrase that shipped is not enough on its own: any
    rewording of it is the same lie. `_same_session_promises` is what holds
    that line; the literal ban below stays only as a regression pin."""
    entry = _configuration_error_entry()
    assert "brings it straight back into service without a restart" not in entry
    assert "same" in entry and "session" in entry
    assert "new session" in entry
    assert "restart" in entry
    assert _same_session_promises(entry) == []


# ── The relayed payload is documentation too ──────────────────────────────
#
# SKILL.md tells the agent to relay a `configuration_error`'s `instructions`
# VERBATIM ("Relay the `instructions` verbatim (they name what to check)"), so
# that string is operator-facing prose that merely happens to live in code.
# Every guard above reads only docs, and that is precisely how the last fix
# went half-done: the README's same-session promise was corrected while the
# identical promise survived in server.py — the one the operator actually
# hears. So these two guards run over the docs AND over the strings the docs
# mandate relaying, and they are written as conditions on a SHAPE of claim
# rather than on any one wording, so a future string making the same promise
# somewhere else is caught wherever it lives.

_RELAYED_SOURCES = ["server/server.py", "server/auth_flow.py"]


def _relayed_strings(rel):
    """Every string literal `rel` can put in front of the operator.

    Docstrings are excluded — they address maintainers. Adjacent literal
    concatenation is folded by the parser into ONE node, so a multi-line
    `instructions` value arrives whole; checking its fragments separately
    would let a qualifier in one line excuse a promise in another. f-string
    placeholders collapse to "…" — their values are runtime detail.
    """
    tree = ast.parse(_read(*rel.split("/")))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found = []

    class Collect(ast.NodeVisitor):
        def visit_Constant(self, node):
            if isinstance(node.value, str) and id(node) not in docstrings:
                found.append(node.value)

        def visit_JoinedStr(self, node):    # deliberately does not descend
            found.append("".join(
                part.value if isinstance(part, ast.Constant) else "…"
                for part in node.values))

    Collect().visit(tree)
    return found


def _troubleshooting_entries():
    """The README's troubleshooting entries, one per symptom — checked
    individually, since the file as a whole trivially satisfies any
    vocabulary test."""
    section = _read(README).split("## Troubleshooting")[1].split("\n## ")[0]
    return [e for e in re.split(r"\n\n(?=\*\*)", section) if e.strip()]


def _operator_texts():
    """(where, passage) for everything an operator is told about recovery."""
    texts = [(README, e) for e in _troubleshooting_entries()]
    for rel in _RELAYED_SOURCES:
        texts += [(rel, s) for s in _relayed_strings(rel)]
    return texts


# "re-run setup_gmail", "run setup_gmail again", "call setup_gmail" — the
# instruction, however it is phrased. `[^.;:]` keeps a match inside one clause.
_RERUN = re.compile(r"\b(?:re-?run|run|call)\w*\b[^.;:]{0,60}?setup_gmail"
                    r"|setup_gmail\b[^.;:]{0,60}?\bagain", re.I)

# The claim that no session change is needed — the load-bearing falsehood.
_NO_SESSION_CHANGE = re.compile(
    r"without (?:a |any )?(?:full |plugin )?(?:restart|restarting|new session)"
    r"|no (?:restart|new session) (?:is )?(?:needed|required)"
    r"|(?:in|from|within) the same session"
    r"|\bimmediately\b|right away|straight away|\bon its own\b|\balone\b", re.I)

# ...unless the passage is DENYING it, which is what the correct prose does.
_DENIED = re.compile(r"\bnot\b|\bnever\b|\bno matter\b|\bcannot\b|can't|won't"
                     r"|\bkeeps? reporting\b|\bstill\b|\buntil\b", re.I)

# A denial only excuses the claim it actually denies. Exempting a whole
# sentence because a negation appears anywhere in it is how this guard was
# defeated: "Do not start a new authorization, but run setup_gmail again in
# the same session and it will recover Gmail." negates *starting an
# authorization*, not the recovery, and the promise walked straight through.
# So the denial is looked for only over the span a negation can actually
# reach the claim from:
#
#   to the left  — negation scopes rightward, so a "not" standing before the
#                  claim reaches it only from inside the same clause ("do NOT
#                  re-run setup_gmail in the same session"). Punctuation ends
#                  a clause, and so do "but" and "then", which hand the
#                  following clause back to positive polarity ("do not start
#                  one, but run it again…", "…, then run it again"). "and" /
#                  "or" are deliberately NOT breaks: a negation genuinely
#                  distributes over them ("never correct the values and
#                  re-run setup_gmail in the same session").
#   to the right — the outcome carries the polarity and is often stated later
#                  in the passage ("…will keep reporting this", "…does not
#                  restart it"), so everything after the claim counts.
#
# Residual, accepted: a negation planted *after* the claim on an unrelated
# verb ("run setup_gmail again immediately, and do not wait for an
# administrator") still exempts. Telling that apart from a genuine negative
# outcome ("…will not help") needs to know WHICH verb is negated, which no
# regex here can do; dropping the rightward reach instead would fire on the
# `denies-the-outcome` case below, and a guard that fires on correct prose is
# one the next author deletes.
_CLAUSE_BREAK = re.compile(r"[,;:(\n—–]|\bbut\b|\bthen\b", re.I)


def _denies_the_claim(passage, claim_start):
    """Whether `passage` denies the recovery claim starting at `claim_start`,
    as opposed to merely containing a negation somewhere."""
    breaks = [m.end() for m in _CLAUSE_BREAK.finditer(passage[:claim_start])]
    return bool(_DENIED.search(passage[breaks[-1] if breaks else 0:]))


def _promises_same_session(passage):
    """A re-run instruction and a no-session-change claim in one passage, with
    nothing denying the claim."""
    rerun = _RERUN.search(passage)
    claim = _NO_SESSION_CHANGE.search(passage)
    if not (rerun and claim):
        return False
    return not _denies_the_claim(passage, min(rerun.start(), claim.start()))


def _same_session_promises(text):
    """Passages telling the operator to re-run `setup_gmail` while asserting
    that no new session is needed. Markdown emphasis is stripped first, so
    `*same*` cannot hide a claim from the match.

    Sentences are checked one at a time and then in adjacent pairs: putting
    the instruction and the immediate-effect claim in two sentences ("…then
    run setup_gmail again. It comes straight back into service immediately.")
    makes exactly the same promise while neither half carries it alone. The
    pair window stops at two because that is where the corpus stays quiet —
    no legitimate passage in it pairs a re-run instruction with a
    no-session-change claim — and a wider window would start joining
    unrelated advice into a promise nobody made.
    """
    flat = re.sub(r"[`*]", "", text)
    sentences = re.split(r"(?<=[.;])\s+", flat)
    offenders = [s for s in sentences if _promises_same_session(s)]
    split_across = [f"{a} {b}" for a, b in zip(sentences, sentences[1:])
                    if a not in offenders and b not in offenders
                    and _promises_same_session(f"{a} {b}")]
    return offenders + split_across


def test_no_operator_facing_text_promises_recovery_without_a_session_change():
    """The general form of the bug, over docs and relayed strings alike."""
    for where, text in _operator_texts():
        offenders = _same_session_promises(text)
        assert offenders == [], \
            f"{where} promises a same-session recovery: {offenders}"


# ── What the detector itself is pinned against ────────────────────────────
#
# The corpus test above can only ever prove that TODAY's prose is clean; it
# says nothing about whether the detector would still see the promise in
# tomorrow's. These two tables are the detector's own tests, and they are the
# reason it can be changed safely: every wording that has actually got past
# it, or been proposed to, is kept here verbatim.

_MUST_BE_CAUGHT = [
    # The wording that shipped, and that the literal ban in the entry test
    # was written for.
    ("shipped-wording",
     "Check `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in casa's plugin "
     "environment against the Google OAuth client, then run `setup_gmail` "
     "again — it re-checks the stored credential and, if the fix worked, "
     "brings it straight back into service without a restart or a "
     "re-authorization (the credential is tied to the client **ID**, so "
     "rotating only the secret costs nothing)."),
    # A rewording of it that the literal ban cannot see.
    ("reworded",
     "Just re-run `setup_gmail` in the *same* session — it re-checks the "
     "stored credential and, if the fix worked, brings it straight back into "
     "service without a re-authorization (the credential is tied to the "
     "client **ID**, so rotating only the secret costs nothing)."),
    # The same promise relocated into a different runtime string.
    ("relocated",
     "run setup_gmail again in the same session and it will work."),
    # Terra: an unrelated negation ("do not WAIT") in the same sentence used
    # to disarm a sentence-wide denial exemption.
    ("terra-unrelated-negation",
     "Do not wait for an administrator: correct your OAuth client settings, "
     "then run setup_gmail again immediately."),
    # Sol: the same trick, and the promise made twice over — "in the same
    # session" AND "it will recover".
    ("sol-unrelated-negation",
     "Do not start a new authorization, but run setup_gmail again in the "
     "same session and it will recover Gmail."),
    # Terra: the instruction and the immediate-effect claim split across a
    # full stop, so that neither sentence carries the promise alone.
    ("split-across-sentences",
     "Correct `GMAIL_CLIENT_SECRET` in casa's plugin environment, then run "
     "`setup_gmail` again. It brings the stored credential back into service "
     "immediately."),
]

# The other half of the trade-off. A guard that fires on correct prose gets
# weakened or deleted by the next person to touch it, so these pin that the
# scoping above did not simply become "any negation is ignored".
_MUST_NOT_BE_CAUGHT = [
    # The negation governs the re-run itself.
    ("denies-the-rerun",
     "Do not re-run `setup_gmail` in the *same* session."),
    # The negation is the outcome, stated after the claim.
    ("denies-the-outcome",
     "Re-running `setup_gmail` in the same session, however, will not help."),
    # The shape the corrected README uses: claim, then its refutation, in one
    # sentence, across a colon.
    ("refutes-in-the-same-sentence",
     "Check `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in casa's plugin "
     "environment against the Google OAuth client — but re-running "
     "`setup_gmail` in the *same* session will keep reporting "
     "`configuration_error` no matter how many times you call it: the "
     "running MCP server read those variables once at startup."),
]


@pytest.mark.parametrize("passage", [p for _, p in _MUST_BE_CAUGHT],
                         ids=[i for i, _ in _MUST_BE_CAUGHT])
def test_the_detector_catches_every_wording_that_has_beaten_it(passage):
    """Each of these promises a same-session recovery. Two of them were
    written by reviewers specifically to slip past this detector, by putting a
    negation that denies something else into the sentence; a third splits the
    promise over two sentences. If one of them stops failing here, the guard
    has a hole of exactly the kind it exists to close."""
    assert _same_session_promises(passage) != [], \
        f"the detector no longer sees the promise in: {passage!r}"


@pytest.mark.parametrize("passage", [p for _, p in _MUST_NOT_BE_CAUGHT],
                         ids=[i for i, _ in _MUST_NOT_BE_CAUGHT])
def test_the_detector_leaves_prose_that_denies_the_promise_alone(passage):
    """None of these promises anything — they deny it. A detector that fires
    on them is one the next author has to disable to say something true."""
    assert _same_session_promises(passage) == [], \
        f"the detector fired on prose that denies the promise: {passage!r}"


def test_advice_to_fix_a_startup_cached_env_var_names_the_session_requirement():
    """`read_env()` copies these variables into process memory once — it is
    reached only through `validate_and_init()`, which only `_startup()` calls —
    and every later probe uses `self._client_secret`, the cached copy. So
    correcting them in casa's plugin environment changes nothing for the
    running server, and any text that tells the operator to fix one AND to
    re-run `setup_gmail` is a dead end unless it also says a new session (or a
    restart) has to come first.

    Stated as a condition on the pair, not on a wording, so it holds for a
    variable or a string that does not exist yet."""
    required = re.search(r"_REQUIRED_ENV_VARS = \[(.*?)\]",
                         _read("server", "auth.py"), re.S).group(1)
    cached = re.findall(r'"([^"]+)"', required)
    assert cached, "could not read the startup-cached variables from auth.py"
    assert "self.read_env()" in _read("server", "auth.py")

    checked = 0
    for where, text in _operator_texts():
        if not any(name in text for name in cached):
            continue
        if not _RERUN.search(re.sub(r"[`*]", "", text)):
            continue
        checked += 1
        lowered = text.lower()
        assert "new session" in lowered and "restart" in lowered, (
            f"{where} tells the operator to correct a variable this process "
            f"cached at startup and then re-run setup_gmail, without saying a "
            f"new session is needed first: {text!r}"
        )
    assert checked, "the guard matched nothing — it has stopped guarding"


# ── 2026-08-07: an update must acknowledge the authorization it did not touch
#
# Observed on the N150 (session 6e80ce7f, 07:59Z): the configurator handed back
# "The integration is not live until `setup_gmail` runs", the resident relayed
# it as fact and asked the operator whether to run setup — while Gmail was
# connected and serving. One minute later `search_emails` returned the inbox.
#
# The deadness claim is casa's generic doctrine (recipes/plugin/update.md,
# written for a webhook-repointing plugin that must re-publish its URL and
# key). It is not a fact about this plugin: the refresh token lives in
# CLAUDE_PLUGIN_DATA, outside the versioned artifact, and this plugin declares
# a callback and no triggers, so an update re-mints no secret that could reach
# the stored grant. Only the store knows, and `setup_gmail` is what asks it.

_SETUP_BLOCK_MARKER = "**When something else asks for Gmail setup"


def _setup_block():
    """The SKILL.md block covering setup the operator did not ask for — casa's
    post-consent dispatch AND a turn that merely reports an update. Extracted
    so the assertions below hold of that passage and not of the file as a
    whole, which trivially satisfies any vocabulary test."""
    text = _read("skills", "gmail", "SKILL.md")
    assert _SETUP_BLOCK_MARKER in text, "SKILL.md has no unprompted-setup block"
    return text.split(_SETUP_BLOCK_MARKER)[1].split("\n## ")[0]


def test_the_skill_description_triggers_on_a_plugin_update_report():
    """The load-bearing half of the fix. Casa runs `skills="all"` — ordinary
    description-gated loading — and grepping both N150 transcripts for a
    `Skill` invocation naming `gmail` returns nothing: the body was not in
    context on the turn that relayed the false claim, nor even on the turn that
    called `setup_gmail`. A description covering only reading, searching,
    sending and managing email never matches a turn that reports a DEPLOYMENT,
    so guidance in the body below is unreachable without this."""
    description = re.search(r"^description:\s*(.+?)\n(?=\w+:|---)",
                            _read("skills", "gmail", "SKILL.md"),
                            re.S | re.M).group(1).lower()
    assert "updated" in description, \
        "the description never mentions an update, so the body will not load on one"
    assert "reload" in description or "restart" in description
    assert "authorization" in description or "setup" in description


def test_the_skill_tells_the_agent_an_update_cannot_revoke_the_authorization():
    """The invariant, scoped. For a role-assigned install the data directory is
    on casa's persistent volume; for an executor engagement it is scratch. This
    plugin requires a role assignment, so the scoped claim is the true one and
    the unscoped claim would be the same class of overstatement as the bug."""
    block = _setup_block()
    assert "role-assigned install" in block
    assert "never revokes it" in block
    assert "data directory" in block


def test_the_skill_forbids_relaying_someone_elses_verdict_on_the_connection():
    """The observed failure in one assertion. Stated as a class, not as casa's
    sentence: the issue filed against casa asks it to reword that sentence, and
    an instruction pinned to the old wording would stop matching what the agent
    sees."""
    block = _setup_block()
    assert "do not relay" in block.lower()
    assert "is not evidence" in block


def test_the_skill_does_not_ask_the_operator_whether_to_run_setup():
    """What the resident actually did wrong — "Want me to kick that off?" —
    asked about a tool that is argument-free, idempotent and needs no approval.
    The question also can't be answered honestly: nobody knows yet whether
    anything needs doing, which is what the call determines."""
    block = _setup_block()
    flat = re.sub(r"[`*]", "", block)
    offenders = re.findall(
        r"ask (?:the )?(?:user|operator)[^.;]{0,40}whether|want me to",
        flat, re.I)
    # Prohibitions are allowed to quote the shape they forbid; an instruction
    # to perform it is not. Only a match that is not being denied counts.
    offenders = [m for m in offenders
                 if not re.search(r"\bdo not\b|\bdon't\b|\bnever\b|\bwithout\b",
                                  flat[:flat.index(m)].rsplit(".", 1)[-1], re.I)]
    assert offenders == [], \
        f"SKILL.md tells the agent to ask instead of checking: {offenders}"
    assert "no arguments" in block


_README_UPDATE_HEADING = "### Updating an already-connected install"


def test_the_readme_tells_a_working_operator_an_update_changes_nothing():
    """Placed with "Rotating credentials" — both answer "what happens to the
    stored credential when X changes" — and deliberately NOT in
    Troubleshooting: nothing is broken, so an operator with a working install
    never looks there."""
    text = _read(README)
    assert _README_UPDATE_HEADING in text, "the README never covers an update"
    section = text.split(_README_UPDATE_HEADING)[1].split("\n### ")[0].split("\n## ")[0]
    assert "setup_gmail" in section, \
        "the section never names the tool that answers the question"
    assert "no triggers" in section, \
        "the section does not say why no secret an update re-mints can reach the grant"
    assert "already_connected" in section
    # The claim being contradicted, named once so the operator recognises it.
    assert "not live" in section


def test_the_readme_update_section_sits_outside_troubleshooting():
    """Guard on the placement, not the prose: inside `## Troubleshooting` the
    section would join the `_same_session_promises` corpus, where a passage
    pairing "run `setup_gmail`" with a no-session-change word fails a detector
    written for an unrelated falsehood."""
    troubleshooting = _read(README).split("## Troubleshooting")[1]
    assert _README_UPDATE_HEADING not in troubleshooting
