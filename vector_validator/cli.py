from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import chain as ch
from . import config as conf
from .weights import ApiError


def cmd_run(args) -> int:
    from .validator import main as run
    return run(args)


def cmd_config_show(args) -> int:
    s = args.settings
    if args.json:
        print(json.dumps({"config_file": str(s.path), "config_file_found": s.found,
                          "settings": {k: {"value": v, "source": s.sources[k]} for k, v in s.values.items()}}, indent=2))
        return 0
    print(f"config file  {s.path}" + ("" if s.found else "  (not found, using defaults)"))
    rows = [[k, "" if v is None else v, s.sources[k]] for k, v in s.values.items()]
    widths = [max(len(str(r[i])) for r in rows + [["setting", "value", "from"]]) for i in range(3)]
    for r in [["setting", "value", "from"], *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def cmd_config_init(args) -> int:
    path, written = conf.init(getattr(args, "config", None))
    if written:
        print(f"wrote {path}: fill in api, netuid, weights_pubkey and wallet, then check with `vector-validator config show`")
        return 0
    print(f"{path} already exists, left unchanged")
    return 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="settings file (default: $VECTOR_VALIDATOR_CONFIG or ~/.vector-validator/config.yaml)")
    common.add_argument("--api", help="the team's API URL (default: config file `api`)")
    common.add_argument("--wallet.name", dest="wallet_name", help="wallet name")
    common.add_argument("--wallet.hotkey", dest="wallet_hotkey", help="hotkey name (default: default)")
    common.add_argument("--wallet.path", dest="wallet_path", help="wallets folder (default: ~/.bittensor/wallets)")
    common.add_argument("--subtensor.network", dest="network", help="finney, test or a ws:// endpoint (default: finney)")
    common.add_argument("--netuid", type=int, help="the subnet's netuid (default: config file `netuid`)")
    common.add_argument("--pubkey", help="the team's weights key (hex); weights signed by another key are refused")

    p = argparse.ArgumentParser(prog="vector-validator", description="Set weights on the Vector subnet from the "
                                "team's signed weights. Settings come from ~/.vector-validator/config.yaml.")
    p.add_argument("--version", action="version", version=f"vector-validator {__version__}")
    p.add_argument("--config", help="settings file (default: $VECTOR_VALIDATOR_CONFIG or ~/.vector-validator/config.yaml)")
    sp = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sp.add_parser("run", help="fetch the signed weights, check them and set them on chain", parents=[common])
    s.add_argument("--interval", type=float, help="seconds between rounds (default: 1200)")
    s.add_argument("--max-age", type=float, help="refuse weights older than this many seconds, 0: off (default: 3600)")
    s.add_argument("--once", action="store_true", help="one round, then exit")
    s.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None,
                   help="print the weights instead of setting them")
    s.set_defaults(fn=cmd_run)

    c = sp.add_parser("config", help="show or create the settings file")
    csp = c.add_subparsers(dest="config_cmd", required=True, metavar="action")
    s = csp.add_parser("show", help="print the settings in effect and where each comes from", parents=[common])
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(fn=cmd_config_show)
    s = csp.add_parser("init", help="write the example settings file if there is none yet")
    s.add_argument("--config", default=argparse.SUPPRESS,
                   help="where to write it (default: ~/.vector-validator/config.yaml)")
    s.set_defaults(fn=cmd_config_init)
    return p


def _settings(args) -> None:
    flags = {f.attr: getattr(args, f.attr, None) for f in conf.FIELDS}
    conf.load(flags, getattr(args, "config", None)).apply(args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.fn is not cmd_config_init:
            _settings(args)
        return int(args.fn(args) or 0)
    except ApiError as e:
        print(f"error: {e.detail} ({e.status})", file=sys.stderr)
        return 1
    except (ch.ChainUnavailable, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
