from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import TEAM_PUBKEY
from .weights import DEFAULT_API

EXAMPLE = Path(__file__).with_name("config.example.yaml")
WALLET_PATH_DEFAULT = "bittensor default (~/.bittensor/wallets)"


def home() -> Path:
    return Path(os.environ.get("VECTOR_VALIDATOR_HOME") or Path.home() / ".vector-validator")


class ConfigError(ValueError):
    pass


def _str(v: Any) -> str:
    if isinstance(v, (dict, list)):
        raise ValueError("expected a single value")
    return str(v).strip()


def _int(v: Any) -> int:
    if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
        raise ValueError("expected a whole number")
    return int(str(v).strip())


def _float(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("expected a number")
    x = float(str(v).strip())
    if x < 0 or x != x:
        raise ValueError("expected a number >= 0")
    return x


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError("expected true or false")


def _pubkey(v: Any) -> str:
    s = _str(v).lower().removeprefix("0x")
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("expected a 32 byte ed25519 public key in hex (64 characters)")
    return s


def _api(v: Any) -> str:
    s = _str(v).rstrip("/")
    if not s.startswith(("http://", "https://")):
        raise ValueError("expected an http:// or https:// URL")
    return s


@dataclass(frozen=True)
class Field:
    key: str
    env: str
    attr: str
    parse: Callable[[Any], Any]
    default: Any = None


FIELDS: tuple[Field, ...] = (
    Field("api", "VECTOR_VALIDATOR_API", "api", _api, DEFAULT_API),
    Field("network", "VECTOR_VALIDATOR_NETWORK", "network", _str, "finney"),
    Field("netuid", "VECTOR_VALIDATOR_NETUID", "netuid", _int),
    Field("weights_pubkey", "VECTOR_VALIDATOR_WEIGHTS_PUBKEY", "pubkey", _pubkey, TEAM_PUBKEY or None),
    Field("wallet.name", "VECTOR_VALIDATOR_WALLET_NAME", "wallet_name", _str),
    Field("wallet.hotkey", "VECTOR_VALIDATOR_WALLET_HOTKEY", "wallet_hotkey", _str, "default"),
    Field("wallet.path", "VECTOR_VALIDATOR_WALLET_PATH", "wallet_path", _str),
    Field("validator.interval_s", "VECTOR_VALIDATOR_INTERVAL_S", "interval", _float, 1200.0),
    Field("validator.retry_s", "VECTOR_VALIDATOR_RETRY_S", "retry", _float, 60.0),
    Field("validator.max_age_s", "VECTOR_VALIDATOR_MAX_AGE_S", "max_age", _float, 3600.0),
    Field("validator.dry_run", "VECTOR_VALIDATOR_DRY_RUN", "dry_run", _bool, False),
)
SECTIONS = {"wallet", "validator"}


@dataclass
class Settings:
    path: Path
    found: bool
    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def apply(self, args) -> None:
        for f in FIELDS:
            setattr(args, f.attr, self.values[f.key])
        args.settings = self


def config_file(flag: str | None = None) -> tuple[Path, bool]:
    if flag:
        return Path(flag).expanduser(), True
    if os.environ.get("VECTOR_VALIDATOR_CONFIG"):
        return Path(os.environ["VECTOR_VALIDATOR_CONFIG"]).expanduser(), True
    return home() / "config.yaml", False


def read_file(path: Path) -> dict:
    import yaml

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: not valid YAML: {e}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping of settings at the top level")
    flat: dict[str, Any] = {}
    known = {f.key for f in FIELDS}
    for k, v in data.items():
        k = str(k)
        if k in SECTIONS:
            if v is None:
                continue
            if not isinstance(v, dict):
                raise ConfigError(f"{path}: `{k}` should be a section with keys under it")
            for k2, v2 in v.items():
                flat[f"{k}.{k2}"] = v2
        else:
            flat[k] = v
    for k in flat:
        if k not in known:
            print(f"warning: {path}: unknown setting `{k}` ignored", file=sys.stderr)
    return {k: v for k, v in flat.items() if k in known}


def load(flags: dict[str, Any] | None = None, config: str | None = None) -> Settings:
    flags = flags or {}
    path, explicit = config_file(config)
    found = path.is_file()
    if explicit and not found:
        raise ConfigError(f"config file not found: {path}")
    file_values = read_file(path) if found else {}
    s = Settings(path=path, found=found)
    for f in FIELDS:
        layers = [(flags.get(f.attr), "flag"), (os.environ.get(f.env), f"env {f.env}"),
                  (file_values.get(f.key), "config file")]
        value, source = f.default, "default"
        for raw, src in layers:
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            try:
                value, source = f.parse(raw), src
            except (TypeError, ValueError) as e:
                where = {"flag": "command line", "config file": str(path)}.get(src, src)
                raise ConfigError(f"{f.key} from {where}: {e} (got {raw!r})") from None
            break
        if source == "default" and value is None:
            source = WALLET_PATH_DEFAULT if f.key == "wallet.path" else "not set"
        elif source == "default" and f.key == "weights_pubkey":
            source = "built in"
        s.values[f.key], s.sources[f.key] = value, source
    return s


def init(config: str | None = None) -> tuple[Path, bool]:
    path, _ = config_file(config)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(EXAMPLE.read_text())
    return path, True


def open_wallet(args):
    from bittensor_wallet import Wallet

    if not getattr(args, "wallet_name", None):
        raise SystemExit("no wallet set: add wallet.name to the config file or pass --wallet.name")
    kw = {"name": args.wallet_name, "hotkey": args.wallet_hotkey or "default"}
    if getattr(args, "wallet_path", None):
        kw["path"] = args.wallet_path
    return Wallet(**kw)
