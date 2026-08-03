import hashlib
import json
import os
import pytest
from pathlib import Path


def write_index(spool_root: Path, plugin_root: Path, payload: dict) -> None:
    key = hashlib.sha256(os.path.realpath(str(plugin_root)).encode()).hexdigest()
    index = spool_root / ".index"
    index.mkdir(parents=True, exist_ok=True)
    (index / f"{key}.json").write_text(json.dumps(payload))


def good_payload(effective="plg-gmail--oauth"):
    return {
        "v": 1,
        "base_url": "https://casa.example.com",
        "plugin_dir": "gmail",
        "callbacks": {
            "oauth": {
                "effective": effective,
                "redirect_uri": f"https://casa.example.com/callback/{effective}",
            }
        },
    }


def make_cb(spool_root, plugin_root):
    from casa_callback import CasaCallback
    return CasaCallback(str(plugin_root), spool_root=str(spool_root))


def test_resolve_returns_route_from_index(tmp_path):
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload())

    route = make_cb(spool, root).resolve()
    assert route.effective == "plg-gmail--oauth"
    assert route.redirect_uri == "https://casa.example.com/callback/plg-gmail--oauth"
    assert route.spool_dir == spool / "gmail"


def test_resolve_reads_scoped_effective_name_verbatim(tmp_path):
    """The redirect URI is READ, never derived from the plugin name."""
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload("plg-fin.gmail--oauth"))

    route = make_cb(spool, root).resolve()
    assert route.effective == "plg-fin.gmail--oauth"
    assert route.redirect_uri.endswith("/callback/plg-fin.gmail--oauth")


def test_resolve_missing_index_raises_unavailable(tmp_path):
    from casa_callback import CallbackUnavailable
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    with pytest.raises(CallbackUnavailable):
        make_cb(spool, root).resolve()


def test_resolve_unavailable_message_is_neutral(tmp_path):
    """Must not guess a cause — casa has five, and the plugin cannot tell them apart."""
    from casa_callback import CallbackUnavailable
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    with pytest.raises(CallbackUnavailable) as exc:
        make_cb(spool, root).resolve()
    msg = str(exc.value)
    for reason in ("callback_pending_ack", "callback_base_url_invalid",
                   "callback_no_target", "callback_invalid", "callback_spool_error"):
        assert reason in msg


@pytest.mark.parametrize("payload", [
    {"v": 2, "plugin_dir": "gmail", "callbacks": {}},
    {"v": 1, "plugin_dir": "gmail", "callbacks": {}},
    {"v": 1, "callbacks": {"oauth": {"effective": "e", "redirect_uri": "u"}}},
    {"not": "a payload"},
])
def test_resolve_malformed_index_raises_unavailable(tmp_path, payload):
    from casa_callback import CallbackUnavailable
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, payload)
    with pytest.raises(CallbackUnavailable):
        make_cb(spool, root).resolve()
