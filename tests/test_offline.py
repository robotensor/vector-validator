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
    return _sign({"format": "vector-weights/2", "round": 2, "at": at, "netuid": netuid, "king_uid": 0, "burn_uid": 0,
                  "shares": {"vector": 1.0, "expert": 0.3, "contributors": 0.7}, "basis": {"contributors": {"week": 2}},
                  "weights": weights, "hotkeys": hotkeys})


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
    assert ws == pytest.approx([0.2 / 0.6, 0.4 / 0.6])
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
    assert all(src in ("config file", "not set", "default") for src in s.sources.values())


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


def _v7(weights, hotkeys, king_uid, vector=0.3):
    return _sign({"format": "vector-weights/2", "round": 4, "week": 3, "at": NOW, "netuid": 7, "king_uid": king_uid,
                  "burn_uid": king_uid, "shares": {"vector": vector, "expert": 0.3, "contributors": 0.7},
                  "basis": {"king": {"uid": king_uid}}, "weights": weights, "hotkeys": hotkeys})


def _u16(uids, ws):
    top = max(ws)
    return [(u, round(w / top * 65535)) for u, w in zip(uids, ws)]


def test_v7_weights_set_only_the_listed_uids(monkeypatch, capsys):
    w = {"0": 0.7, "1": 0.09 + 0.21 * 0.25, "2": 0.21 * 0.5, "3": 0.21 * 0.25}
    doc, pk = _v7(w, {"0": "5Val", "1": "5Alice", "2": "5Bob", "3": "5Carol"}, king_uid=1)
    assert sum(w.values()) == pytest.approx(1.0)
    calls = _chain(monkeypatch, HK + ["5Dave"])
    assert validator.run_once(_vargs(), lambda: doc, pk)
    (netuid, uids, ws), = calls
    assert netuid == 7 and uids == [0, 1, 2, 3] and 4 not in uids
    assert ws == pytest.approx([w[str(u)] for u in uids])
    assert sum(ws[1:]) == pytest.approx(0.3)
    assert _u16(uids, ws) == _u16(uids, [w[str(u)] for u in uids])
    out = capsys.readouterr().out
    assert "published total 1.000000" in out and "vector share 0.3" in out and "king uid 1" in out


def test_v7_weights_below_one_are_normalised(monkeypatch):
    w = {"1": 0.09, "2": 0.21}
    doc, pk = _v7(w, {"1": "5Alice", "2": "5Bob"}, king_uid=1)
    calls = _chain(monkeypatch, HK)
    assert validator.run_once(_vargs(), lambda: doc, pk)
    (netuid, uids, ws), = calls
    assert uids == [1, 2] and ws == pytest.approx([0.3, 0.7])
    assert _u16(uids, ws) == _u16(uids, [0.09, 0.21])


def test_v7_king_and_listed_rest(monkeypatch):
    doc, pk = _v7({"0": 0.7, "2": 0.3}, {"0": "5Val", "2": "5Bob"}, king_uid=2)
    calls = _chain(monkeypatch, HK)
    assert validator.run_once(_vargs(), lambda: doc, pk)
    (netuid, uids, ws), = calls
    assert netuid == 7 and uids == [0, 2] and ws == pytest.approx([0.7, 0.3])
