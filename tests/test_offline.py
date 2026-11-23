import base64
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from nacl.signing import SigningKey

from vector_validator import chain as ch
from vector_validator import cli
from vector_validator import config as conf
from vector_validator import validator
from vector_validator.weights import verify

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime.now(dt.timezone.utc).isoformat()
HK = ["5Val", "5Alice", "5Bob", "5Carol"]


def _sign(body, sk=None):
    sk = sk or SigningKey.generate()
    msg = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    doc = {**body, "public_key": sk.verify_key.encode().hex(), "signature": base64.b64encode(sk.sign(msg).signature).decode()}
    return json.loads(json.dumps(doc)), doc["public_key"]


def _doc(weights, hotkeys, netuid=7, at=NOW):
    return _sign({"format": "vector-weights/2", "round": 2, "at": at, "netuid": netuid, "king_uid": 1,
                  "basis": {"contributors": {"week": 2}}, "weights": weights, "hotkeys": hotkeys})


def test_roundtrip_and_tamper():
    doc, pk = _doc({"1": 0.25, "10": 0.75}, {"1": "5Alice", "10": "5Ten"})
    assert verify(doc, pk, netuid=7)["weights"]["10"] == 0.75
    for field, value in (("hotkeys", {"1": "5Mallory", "10": "5Ten"}), ("netuid", 8), ("weights", {"1": 1.0})):
        forged = {**doc, field: value}
        with pytest.raises(ValueError, match="signature"):
            verify(forged, pk)
    with pytest.raises(ValueError, match="unexpected key"):
        verify(doc, "ab" * 32)


def test_unsigned_refused():
    with pytest.raises(ValueError, match="not signed"):
        verify({"format": "vector-weights/2", "weights": {}, "hotkeys": {}, "netuid": 7})


def test_netuid_mismatch_refused():
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"}, netuid=7)
    with pytest.raises(ValueError, match="netuid 7, not 8"):
        verify(doc, pk, netuid=8)


def test_malformed_refused():
    doc, pk = _sign({"format": "vector-weights/2", "round": 2, "at": NOW, "netuid": 7, "weights": {"5Alice": 1.0},
                     "hotkeys": {}})
    with pytest.raises(ValueError, match="uid"):
        verify(doc, pk)
    doc, pk = _sign({"format": "vector-weights/2", "round": 2, "at": NOW, "weights": {"1": 1.0}, "hotkeys": {"1": "x"}})
    with pytest.raises(ValueError, match="netuid"):
        verify(doc, pk)
    for fmt in ("vector-weights/1", "vector-weights/3"):
        doc, pk = _sign({"format": fmt, "weights": {}})
        with pytest.raises(ValueError, match="format"):
            verify(doc, pk)


def test_drops_uids_held_by_another_hotkey():
    doc, _ = _doc({"1": 0.2, "2": 0.3, "3": 0.4, "9": 0.1}, {"1": "5Alice", "2": "5OldBob", "3": "5Carol", "9": "5Gone"})
    uids, ws, dropped = validator.select(doc, HK)
    assert uids == [1, 3]
    assert ws == pytest.approx([0.5 / 0.9, 0.4 / 0.9])
    assert any("uid 2" in d and "5OldBob" in d for d in dropped) and any("uid 9" in d for d in dropped)


def test_missing_hotkey_and_bad_weights_dropped():
    uids, ws, dropped = validator.by_uid({"1": 1.0, "2": 0.5, "3": "nan", "0": -1}, {"1": "5Alice", "3": "5Carol", "0": "5Val"}, HK)
    assert uids == [1] and ws == [1.0] and any("uid 2" in d for d in dropped)


def _chain(monkeypatch, hotkeys):
    calls = []
    monkeypatch.setattr(ch, "bittensor", lambda: SimpleNamespace(Subtensor=lambda network: SimpleNamespace(network=network)))
    monkeypatch.setattr(ch, "metagraph_hotkeys", lambda sub, netuid: list(hotkeys))
    monkeypatch.setattr(ch, "set_weights", lambda sub, w, netuid, uids, ws: calls.append((netuid, uids, ws)) or (True, ""))
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="5Val"))
    monkeypatch.setattr(validator, "open_wallet", lambda args: wallet)
    return calls


def _vargs(**kw):
    return SimpleNamespace(**{"netuid": 7, "max_age": 3600.0, "dry_run": False, "network": "test", **kw})


def test_run_once_sets_weights_by_uid(monkeypatch, capsys):
    calls = _chain(monkeypatch, HK)
    doc, pk = _doc({"1": 0.5, "2": 0.5}, {"1": "5Alice", "2": "5Replaced"})
    assert validator.run_once(_vargs(), lambda: doc, pk)
    assert calls == [(7, [1], [1.0])]
    assert "uid 2" in capsys.readouterr().out


def test_run_once_dry_run_sets_nothing(monkeypatch, capsys):
    calls = _chain(monkeypatch, HK)
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"})
    assert validator.run_once(_vargs(dry_run=True), lambda: doc, pk)
    assert calls == [] and "5Alice" in capsys.readouterr().out


def test_run_once_refuses_other_netuid(monkeypatch):
    calls = _chain(monkeypatch, HK)
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"}, netuid=7)
    with pytest.raises(ValueError, match="netuid"):
        validator.run_once(_vargs(netuid=12), lambda: doc, pk)
    assert calls == []


def test_run_once_refuses_stale_weights(monkeypatch):
    calls = _chain(monkeypatch, HK)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"}, at=old)
    with pytest.raises(ValueError, match="old"):
        validator.run_once(_vargs(), lambda: doc, pk)
    assert calls == []


PK1, PK2 = "aa" * 32, "bb" * 32


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_defaults_without_a_file():
    s = conf.load()
    assert not s.found and s["network"] == "finney" and s["netuid"] is None and s["wallet.hotkey"] == "default"
    assert s["validator.interval_s"] == 1200 and s["validator.dry_run"] is False and s["weights_pubkey"] is None


def test_precedence_flag_env_file_default(vv_home, monkeypatch):
    _write(vv_home / "config.yaml", f"""
api: https://file.example/
netuid: 3
network: test
weights_pubkey: "0x{PK1}"
wallet: {{name: filewallet, hotkey: hk}}
validator: {{interval_s: 60, dry_run: true}}
""")
    s = conf.load()
    assert s.found and s["api"] == "https://file.example" and s["netuid"] == 3 and s["network"] == "test"
    assert s["weights_pubkey"] == PK1 and s["wallet.name"] == "filewallet" and s["validator.interval_s"] == 60
    assert s["validator.dry_run"] is True and s["validator.max_age_s"] == 3600
    assert s.sources["netuid"] == "config file" and s.sources["validator.max_age_s"] == "default"

    monkeypatch.setenv("VECTOR_VALIDATOR_NETUID", "4")
    monkeypatch.setenv("VECTOR_VALIDATOR_WEIGHTS_PUBKEY", PK2)
    monkeypatch.setenv("VECTOR_VALIDATOR_DRY_RUN", "no")
    s = conf.load()
    assert s["netuid"] == 4 and s["weights_pubkey"] == PK2 and s["validator.dry_run"] is False
    assert s.sources["netuid"] == "env VECTOR_VALIDATOR_NETUID"

    s = conf.load({"netuid": 5, "pubkey": PK1, "dry_run": True, "wallet_name": "flagwallet", "api": None})
    assert s["netuid"] == 5 and s["weights_pubkey"] == PK1 and s["validator.dry_run"] is True
    assert s["wallet.name"] == "flagwallet" and s.sources["wallet.name"] == "flag"
    assert s["api"] == "https://file.example"


def test_config_file_choice(tmp_path, vv_home, monkeypatch):
    _write(vv_home / "config.yaml", "netuid: 1\n")
    other = _write(tmp_path / "other.yaml", "netuid: 2\n")
    third = _write(tmp_path / "third.yaml", "netuid: 3\n")
    assert conf.load()["netuid"] == 1
    monkeypatch.setenv("VECTOR_VALIDATOR_CONFIG", str(other))
    assert conf.load()["netuid"] == 2
    assert conf.load(config=str(third))["netuid"] == 3
    monkeypatch.setenv("VECTOR_VALIDATOR_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(conf.ConfigError, match="not found"):
        conf.load()


def test_team_pubkey_is_only_the_last_fallback(monkeypatch, vv_home):
    fields = tuple(conf.Field(f.key, f.env, f.attr, f.parse, PK2 if f.key == "weights_pubkey" else f.default)
                   for f in conf.FIELDS)
    monkeypatch.setattr(conf, "FIELDS", fields)
    s = conf.load()
    assert s["weights_pubkey"] == PK2 and s.sources["weights_pubkey"] == "built in"
    _write(vv_home / "config.yaml", f"weights_pubkey: {PK1}\n")
    assert conf.load()["weights_pubkey"] == PK1


@pytest.mark.parametrize("text, match", [
    ("netuid: seven\n", "netuid"),
    ("weights_pubkey: abc\n", "weights_pubkey"),
    ("api: arena.example\n", "api"),
    ("validator: {dry_run: maybe}\n", "dry_run"),
    ("wallet: alice\n", "section"),
    ("- a\n- b\n", "mapping"),
])
def test_bad_values_refused(vv_home, text, match):
    _write(vv_home / "config.yaml", text)
    with pytest.raises(conf.ConfigError, match=match):
        conf.load()


def test_unknown_key_warns(vv_home, capsys):
    _write(vv_home / "config.yaml", "netuid: 1\nnetuidd: 2\nwallet: {nmae: x}\n")
    assert conf.load()["netuid"] == 1
    err = capsys.readouterr().err
    assert "netuidd" in err and "wallet.nmae" in err


def test_example_file(vv_home):
    assert [p.relative_to(ROOT) for p in ROOT.rglob("config.example.yaml") if ".venv" not in p.parts] == \
        [conf.EXAMPLE.relative_to(ROOT)]
    assert str(conf.EXAMPLE.relative_to(ROOT)) in (ROOT / "scripts" / "setup.sh").read_text()
    _write(vv_home / "config.yaml", conf.EXAMPLE.read_text())
    s = conf.load()
    assert set(s.values) == {f.key for f in conf.FIELDS}
    assert all(src in ("config file", "not set", "default", conf.WALLET_PATH_DEFAULT) for src in s.sources.values())
    assert s["validator.retry_s"] == 60 and s.sources["wallet.path"] == "bittensor default (~/.bittensor/wallets)"


def test_cli_config_init_and_show(vv_home, capsys):
    assert cli.main(["config", "init"]) == 0
    assert (vv_home / "config.yaml").read_text() == conf.EXAMPLE.read_text()
    assert cli.main(["config", "init"]) == 1
    capsys.readouterr()
    assert cli.main(["config", "show", "--netuid", "9"]) == 0
    assert "netuid" in capsys.readouterr().out
    assert cli.main(["config", "show", "--json", "--pubkey", PK1]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["settings"]["weights_pubkey"] == {"value": PK1, "source": "flag"}


def test_cli_global_config_flag(tmp_path, capsys):
    f = _write(tmp_path / "c.yaml", "netuid: 42\n")
    assert cli.main(["--config", str(f), "config", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["settings"]["netuid"]["value"] == 42
    assert cli.main(["config", "show", "--config", str(tmp_path / "nope.yaml")]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_run_needs_netuid_and_key(capsys):
    assert cli.main(["run", "--once"]) == 2
    assert "netuid" in capsys.readouterr().err
    assert cli.main(["run", "--once", "--netuid", "1"]) == 2
    assert "public key" in capsys.readouterr().err


def test_chain_calls_on_bittensor_11(monkeypatch):
    sent = {}

    class Intent:
        def __init__(self, **kw):
            self.kw = kw

    class Sub:
        def read(self, name, **kw):
            assert name == "neurons" and kw == {"netuid": 7, "lite": True}
            return [{"uid": 2, "hotkey": "5Bob"}, SimpleNamespace(uid=0, hotkey="5Val"), {"uid": 3, "hotkey": "5Carol"}]

        def execute(self, intent, wallet):
            sent.update(intent=intent.kw, wallet=wallet)
            return SimpleNamespace(success=True, message="ok")

    monkeypatch.setattr(ch, "bittensor", lambda: SimpleNamespace(intents=SimpleNamespace(SetWeights=Intent)))
    assert ch.metagraph_hotkeys(Sub(), 7) == ["5Val", "", "5Bob", "5Carol"]
    wallet = SimpleNamespace(hotkey="hotkey-keypair")
    assert ch.set_weights(Sub(), wallet, 7, [0, 2], [0.5, 0.5]) == (True, "ok")
    assert sent == {"intent": {"netuid": 7, "uids": [0, 2], "weights": [0.5, 0.5]}, "wallet": "hotkey-keypair"}


def _published(weights, hotkeys, king_uid):
    return _sign({"format": "vector-weights/2", "round": 4, "week": 3, "at": NOW, "netuid": 7, "king_uid": king_uid,
                  "basis": {"king": {"uid": king_uid}}, "weights": weights, "hotkeys": hotkeys})


def _u16(uids, ws):
    top = max(ws)
    return [(u, round(w / top * 65535)) for u, w in zip(uids, ws)]


def test_sets_only_the_listed_uids(monkeypatch, capsys):
    w = {"1": 0.4, "2": 0.35, "3": 0.25}
    doc, pk = _published(w, {"1": "5Alice", "2": "5Bob", "3": "5Carol"}, king_uid=1)
    assert sum(w.values()) == pytest.approx(1.0)
    calls = _chain(monkeypatch, HK + ["5Dave"])
    assert validator.run_once(_vargs(), lambda: doc, pk)
    (netuid, uids, ws), = calls
    assert netuid == 7 and uids == [1, 2, 3] and 0 not in uids and 4 not in uids
    assert ws == pytest.approx([w[str(u)] for u in uids])
    assert _u16(uids, ws) == _u16(uids, [w[str(u)] for u in uids])
    out = capsys.readouterr().out
    assert "published total 1.000000" in out and "king uid 1" in out and "share" not in out


def test_weights_below_one_are_normalised(monkeypatch):
    w = {"1": 0.2, "2": 0.3}
    doc, pk = _published(w, {"1": "5Alice", "2": "5Bob"}, king_uid=1)
    calls = _chain(monkeypatch, HK)
    assert validator.run_once(_vargs(), lambda: doc, pk)
    (netuid, uids, ws), = calls
    assert uids == [1, 2] and ws == pytest.approx([0.4, 0.6])
    assert _u16(uids, ws) == _u16(uids, [0.2, 0.3])


def test_king_and_one_listed_uid(monkeypatch):
    doc, pk = _published({"1": 0.45, "3": 0.55}, {"1": "5Alice", "3": "5Carol"}, king_uid=3)
    calls = _chain(monkeypatch, HK)
    assert validator.run_once(_vargs(), lambda: doc, pk)
    (netuid, uids, ws), = calls
    assert netuid == 7 and uids == [1, 3] and ws == pytest.approx([0.45, 0.55])


def test_a_share_whose_uid_changed_hands_goes_to_the_king(monkeypatch, capsys):
    w = {"1": 0.4, "2": 0.35, "3": 0.25}
    listed = {"1": "5Alice", "2": "5OldBob", "3": "5Carol"}
    doc, pk = _published(w, listed, king_uid=1)
    calls = _chain(monkeypatch, HK)
    assert validator.run_once(_vargs(), lambda: doc, pk)
    (netuid, uids, ws), = calls
    assert uids == [1, 3] and ws == pytest.approx([0.75, 0.25])
    assert "its share goes to the king uid 1" in capsys.readouterr().out
    uids, ws, dropped = validator.select({**doc, "hotkeys": {**listed, "9": "5Gone"}, "weights": {**w, "9": 0.1}}, HK)
    assert uids == [1, 3] and ws == pytest.approx([0.75 / 1.0, 0.25 / 1.0])
    assert any("uid 9: not on the subnet" == d for d in dropped)


def test_dropped_share_is_renormalised_when_the_king_fails_the_check(monkeypatch):
    w = {"1": 0.4, "2": 0.35, "3": 0.25}
    for king, listed in ((1, {"1": "5OldAlice", "2": "5OldBob", "3": "5Carol"}),
                         (2, {"1": "5Alice", "2": "5OldBob", "3": "5Carol"}),
                         (7, {"1": "5Alice", "2": "5OldBob", "3": "5Carol"}),
                         (None, {"1": "5Alice", "2": "5OldBob", "3": "5Carol"})):
        doc, _ = _published(w, listed, king_uid=king)
        uids, ws, dropped = validator.select(doc, HK)
        kept = {u: w[str(u)] for u in (1, 3) if listed[str(u)] == HK[u]}
        assert uids == sorted(kept) and ws == pytest.approx([kept[u] / sum(kept.values()) for u in uids])
        assert not any("king" in d for d in dropped)


def test_run_once_refuses_weights_from_the_future(monkeypatch):
    calls = _chain(monkeypatch, HK)
    ahead = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat()
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"}, at=ahead)
    with pytest.raises(ValueError, match="future"):
        validator.run_once(_vargs(), lambda: doc, pk)
    with pytest.raises(ValueError, match="future"):
        validator.run_once(_vargs(max_age=0), lambda: doc, pk)
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60)).isoformat()
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"}, at=soon)
    assert validator.run_once(_vargs(), lambda: doc, pk)
    assert calls == [(7, [1], [1.0])]


@pytest.mark.parametrize("at", ["yesterday", 12345, None])
def test_a_bad_time_is_refused(monkeypatch, at):
    _chain(monkeypatch, HK)
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"}, at=at)
    with pytest.raises(ValueError, match="time|when"):
        validator.run_once(_vargs(), lambda: doc, pk)


def test_odd_uids_and_hotkeys_are_refused():
    for weights, hotkeys, msg in (({"²": 1.0}, {"²": "5Alice"}, "uid"), ({"1" * 12: 1.0}, {}, "uid"),
                                  ({"1": 1.0}, {"1": {"x": 1}}, "not strings")):
        doc, pk = _doc(weights, hotkeys)
        with pytest.raises(ValueError, match=msg):
            verify(doc, pk)
    with pytest.raises(ValueError, match="JSON object"):
        verify([1, 2])
    doc, pk = _doc({"1": 1.0}, {"1": "5Alice"})
    with pytest.raises(ValueError, match="strings"):
        verify({**doc, "signature": 5}, pk)


def test_fetch_bounds_and_checks_the_response(monkeypatch):
    import httpx

    from vector_validator import weights as wt
    bodies = {"big": b"{" + b" " * 5000 + b"}", "list": b"[1]", "text": b"<html>", "ok": b'{"format": "x"}'}

    def stream(method, url, timeout=None, kind="ok"):
        tr = httpx.MockTransport(lambda req: httpx.Response(200, content=bodies[kind]))
        return httpx.Client(transport=tr).stream(method, url)
    for kind, msg in (("big", "larger than 4000"), ("list", "JSON object"), ("text", "not JSON")):
        monkeypatch.setattr(httpx, "stream", lambda m, u, timeout=None, k=kind: stream(m, u, timeout, k))
        with pytest.raises((ValueError, wt.ApiError), match=msg):
            wt.fetch("http://api", max_bytes=4000)
    monkeypatch.setattr(httpx, "stream", lambda m, u, timeout=None: stream(m, u, timeout, "ok"))
    assert wt.fetch("http://api") == {"format": "x"}


DATA = Path(__file__).parent / "data"


def test_contract_with_a_document_from_the_server(monkeypatch, capsys):
    raw = (DATA / "weights_v2.json").read_bytes()
    meta = json.loads((DATA / "weights_v2.meta.json").read_text())
    doc = json.loads(raw)
    assert set(doc) == {"format", "round", "week", "weights", "hotkeys", "king_uid", "basis", "at", "netuid",
                        "public_key", "signature"}
    assert verify(json.loads(raw), meta["public_key"], meta["netuid"]) == doc
    assert sum(doc["weights"].values()) == pytest.approx(1.0)
    calls = _chain(monkeypatch, meta["metagraph"])
    args = _vargs(netuid=meta["netuid"], max_age=0.0)
    assert validator.run_once(args, lambda: json.loads(raw), meta["public_key"])
    (netuid, uids, ws), = calls
    assert netuid == meta["netuid"] and uids == sorted(int(k) for k in doc["weights"])
    assert ws == pytest.approx([doc["weights"][str(u)] for u in uids])
    assert f"king uid {doc['king_uid']}" in capsys.readouterr().out
    with pytest.raises(ValueError, match="old"):
        validator.run_once(_vargs(netuid=meta["netuid"], max_age=1.0), lambda: json.loads(raw), meta["public_key"])
    for field, value in (("weights", {**doc["weights"], str(doc["king_uid"]): 0.9}), ("king_uid", 2), ("netuid", 8)):
        with pytest.raises(ValueError, match="signature"):
            verify({**json.loads(raw), field: value}, meta["public_key"])
    moved = list(meta["metagraph"])
    other = next(u for u in uids if u != doc["king_uid"])
    moved[other] = "5Someone"
    calls.clear()
    monkeypatch.setattr(ch, "metagraph_hotkeys", lambda sub, netuid: list(moved))
    assert validator.run_once(args, lambda: json.loads(raw), meta["public_key"])
    (_, uids2, ws2), = calls
    assert other not in uids2 and sum(ws2) == pytest.approx(1.0)
    king = uids2.index(doc["king_uid"])
    assert ws2[king] == pytest.approx(doc["weights"][str(doc["king_uid"])] + doc["weights"][str(other)])


def _serve(monkeypatch, status, body):
    import httpx

    def stream(method, url, timeout=None):
        tr = httpx.MockTransport(lambda req: httpx.Response(status, content=body))
        return httpx.Client(transport=tr).stream(method, url)
    monkeypatch.setattr(httpx, "stream", stream)


@pytest.mark.parametrize("status, body, detail", [
    (503, b'{"detail": "the weights are unavailable"}', "the weights are unavailable"),
    (500, b"Internal Server Error", "Internal Server Error"),
    (404, b'{"detail": "Not Found"}', "Not Found"),
    (429, b"[1]", "[1]"),
])
def test_http_errors_are_api_errors(monkeypatch, status, body, detail):
    from vector_validator import weights as wt
    _serve(monkeypatch, status, body)
    with pytest.raises(wt.ApiError) as e:
        wt.fetch("http://api")
    assert e.value.status == status and e.value.detail == detail


def test_a_refused_pass_sets_nothing(monkeypatch, capsys):
    calls = _chain(monkeypatch, HK)
    _serve(monkeypatch, 503, b'{"detail": "the weights are unavailable"}')
    assert cli.main(["run", "--once", "--api", "http://api", "--netuid", "7", "--pubkey", PK1]) == 1
    assert calls == [] and "error: 503: the weights are unavailable" in capsys.readouterr().err
    doc, _ = _doc({"1": 1.0}, {"1": "5Alice"})
    _serve(monkeypatch, 200, json.dumps(doc).encode())
    assert cli.main(["run", "--once", "--api", "http://api", "--netuid", "7", "--pubkey", PK1]) == 1
    assert calls == [] and "unexpected key" in capsys.readouterr().err


class _Stop(BaseException):
    pass


def test_loop_retries_sooner_after_a_failed_pass(monkeypatch):
    from vector_validator import weights as wt
    calls = _chain(monkeypatch, HK)
    doc, pk = _doc({"1": 0.5, "3": 0.5}, {"1": "5Alice", "3": "5Carol"})
    answers = [wt.ApiError(503, "the weights are unavailable"), OSError("connection refused"), doc]

    def fetch(api):
        a = answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a
    sleeps = []

    def sleep(s):
        sleeps.append((s, len(calls)))
        if len(sleeps) == 3:
            raise _Stop
    monkeypatch.setattr(wt, "fetch", fetch)
    monkeypatch.setattr(validator.time, "sleep", sleep)
    with pytest.raises(_Stop):
        cli.main(["run", "--api", "http://api", "--netuid", "7", "--pubkey", pk, "--retry", "45"])
    assert sleeps == [(45.0, 0), (45.0, 0), (1200.0, 1)]
    assert calls == [(7, [1, 3], [0.5, 0.5])]


def test_retry_setting(vv_home, monkeypatch):
    assert conf.load()["validator.retry_s"] == 60
    _write(vv_home / "config.yaml", "validator: {retry_s: 30}\n")
    assert conf.load()["validator.retry_s"] == 30
    monkeypatch.setenv("VECTOR_VALIDATOR_RETRY_S", "15")
    assert conf.load()["validator.retry_s"] == 15
    assert conf.load({"retry": 5})["validator.retry_s"] == 5


def test_chain_calls_on_bittensor_9_and_10(monkeypatch):
    sent = []

    class Sub:
        def __init__(self, result):
            self.result = result

        def metagraph(self, netuid, lite):
            assert netuid == 7 and lite is True
            return SimpleNamespace(hotkeys=["5Val", "5Alice", "5Bob"])

        def set_weights(self, **kw):
            sent.append(kw)
            return self.result

    def boom():
        raise AssertionError("the 9 / 10 path needs no bittensor import")
    monkeypatch.setattr(ch, "bittensor", boom)
    wallet = SimpleNamespace(hotkey="hotkey-keypair")
    assert ch.metagraph_hotkeys(Sub(None), 7) == ["5Val", "5Alice", "5Bob"]
    assert ch.set_weights(Sub((True, "included")), wallet, 7, [1, 2], [0.25, 0.75]) == (True, "included")
    assert ch.set_weights(Sub((False,)), wallet, 7, [1], [1.0]) == (False, "")
    assert ch.set_weights(Sub(SimpleNamespace(success=False, message="rate limited")), wallet, 7, [1], [1.0]) == \
        (False, "rate limited")
    assert ch.set_weights(Sub(True), wallet, 7, [1], [1.0]) == (True, "")
    assert sent[0] == {"wallet": wallet, "netuid": 7, "uids": [1, 2], "weights": [0.25, 0.75],
                       "wait_for_inclusion": True, "wait_for_finalization": False}


def test_help_says_pass_and_the_wallet_default(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "round" not in out and "seconds between passes" in out and "--retry" in out
    assert "bittensor default (~/.bittensor/wallets)" in out
