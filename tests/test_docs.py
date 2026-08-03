# tests/test_docs.py
"""Pins on the setup and troubleshooting prose.

The README is the only artifact a reader follows before the plugin can run at
all, so two classes of error in it are as breaking as a code bug: prescribing a
console option the reader cannot select, and quoting a diagnostic string the
code no longer emits. Both shipped. These tests are drift guards, not style
checks: the diagnostics are compared against the source that emits them.
"""
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _read(*parts):
    return (_ROOT.joinpath(*parts)).read_text()


README = "README.md"


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


# ── Fix C's companion: the skill must relay an empty result honestly ───────

def test_skill_tells_the_agent_an_empty_collect_is_not_a_success():
    text = _read("skills", "gmail", "SKILL.md")
    assert "Nothing waiting" in text
    assert "never\n  report `status: \"ok\"` on its own as confirmation" in text
