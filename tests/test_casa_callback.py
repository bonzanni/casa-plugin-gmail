import hashlib
import json
import os
import pytest
import sys
import types
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


def install_stub_protocol(monkeypatch, tmp_path, calls):
    """Stand in for casa's callback_spool / callback_attempts under lib_dir."""
    spool = types.ModuleType("callback_spool")
    attempts = types.ModuleType("callback_attempts")

    def _validate(obj, expect_hash=None):
        if not isinstance(obj, dict):
            return None
        if obj.get("state_hash") != expect_hash:
            return None
        if obj.get("status") not in ("awaiting_redirect", "result_ready", "done"):
            return None
        if (obj.get("outcome") is not None) != (obj.get("status") == "done"):
            return None
        return obj

    attempts.validate_attempt = _validate
    spool.state_hash = lambda s: hashlib.sha256(s.encode()).hexdigest()
    spool.mint = lambda d, state, meta=None: calls.append(("mint", str(d), state, meta))
    spool.ack = lambda d, h: calls.append(("ack", str(d), h))

    def _collect(d, h):
        calls.append(("collect", str(d), h))
        path = Path(d) / "results" / f"{h}.json"
        rec = json.loads(path.read_text())
        held = Path(d) / "results" / f".collect-{h}-deadbeef"
        path.rename(held)
        return rec, held

    spool.collect = _collect
    monkeypatch.setitem(sys.modules, "callback_spool", spool)
    monkeypatch.setitem(sys.modules, "callback_attempts", attempts)


def write_attempt(spool_dir: Path, h: str, **over):
    rec = {"state_hash": h, "minted_ts": 100.0, "status": "result_ready",
           "outcome": None, "claimed": False, "meta": None}
    rec.update(over)
    d = spool_dir / "attempts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{h}.json").write_text(json.dumps(rec))


H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64


def test_attempts_skips_ack_tokens_and_invalid(tmp_path, monkeypatch):
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload())
    install_stub_protocol(monkeypatch, tmp_path, [])
    sd = spool / "gmail"
    write_attempt(sd, H1)
    (sd / "attempts" / f".ack-{H2}").write_text("")
    (sd / "attempts" / f"{H3}.json").write_text("{not json")

    got = make_cb(spool, root).attempts()
    assert [r["state_hash"] for r in got] == [H1]


def test_attempts_skips_schema_invalid_record(tmp_path, monkeypatch):
    """Parseable is not valid: hash must bind to the filename."""
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload())
    install_stub_protocol(monkeypatch, tmp_path, [])
    write_attempt(spool / "gmail", H1, state_hash=H2)   # mismatched hash

    assert make_cb(spool, root).attempts() == []


def test_attempts_orders_newest_first_null_oldest_hash_breaks_ties(tmp_path, monkeypatch):
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload())
    install_stub_protocol(monkeypatch, tmp_path, [])
    sd = spool / "gmail"
    write_attempt(sd, H1, minted_ts=None)
    write_attempt(sd, H2, minted_ts=500.0)
    write_attempt(sd, H3, minted_ts=500.0)

    got = [r["state_hash"] for r in make_cb(spool, root).attempts()]
    assert got == [H3, H2, H1]      # 500/H3 > 500/H2 > None(0.0)/H1


def test_mint_and_ack_delegate_to_casa(tmp_path, monkeypatch):
    calls = []
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload())
    install_stub_protocol(monkeypatch, tmp_path, calls)

    cb = make_cb(spool, root)
    cb.mint("state-xyz", {"kind": "gmail-oauth", "v": 1})
    cb.ack(H1)
    assert ("mint", str(spool / "gmail"), "state-xyz", {"kind": "gmail-oauth", "v": 1}) in calls
    assert ("ack", str(spool / "gmail"), H1) in calls


def test_held_reads_surviving_collect_journal(tmp_path, monkeypatch):
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload())
    install_stub_protocol(monkeypatch, tmp_path, [])
    results = spool / "gmail" / "results"
    results.mkdir(parents=True)
    (results / f".collect-{H1}-abc").write_text(json.dumps({"query": [["code", "c1"]]}))

    assert make_cb(spool, root).held(H1) == {"query": [["code", "c1"]]}


def test_held_returns_none_when_absent(tmp_path, monkeypatch):
    spool, root = tmp_path / "spool", tmp_path / "plugin"
    root.mkdir()
    write_index(spool, root, good_payload())
    install_stub_protocol(monkeypatch, tmp_path, [])
    (spool / "gmail" / "results").mkdir(parents=True)

    assert make_cb(spool, root).held(H1) is None
