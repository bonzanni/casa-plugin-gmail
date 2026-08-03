# tests/test_docs.py
"""Pins on the setup and troubleshooting prose.

The README is the only artifact a reader follows before the plugin can run at
all, so two classes of error in it are as breaking as a code bug: prescribing a
console option the reader cannot select, and quoting a diagnostic string the
code no longer emits. Both shipped. These tests are drift guards, not style
checks: the diagnostics are compared against the source that emits them.
"""
import re
from pathlib import Path

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


def test_the_skill_quotes_the_status_the_setup_tool_actually_returns():
    """Same drift guard as the diagnostics above: the status the skill tells the
    agent to recognise must be the one server.py emits."""
    assert '"already_connected"' in _read("server", "server.py")
    assert "`already_connected`" in _read("skills", "gmail", "SKILL.md")
