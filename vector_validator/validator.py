from __future__ import annotations

import datetime as dt
import math
import sys
import time

from . import chain as ch
from . import weights as wt
from .config import open_wallet


def pinned_pubkey(key: str | None) -> str | None:
    return key.lower().removeprefix("0x") if key else None


MAX_SKEW_S = 300.0


def fetch_verified(fetch, pubkey: str | None, max_age_s: float, netuid: int | None = None) -> dict:
    doc = wt.verify(fetch(), pubkey, netuid)
    if not isinstance(doc.get("at"), str):
        raise ValueError("the weights do not say when they were made (at)")
    try:
        at = dt.datetime.fromisoformat(doc["at"])
    except ValueError:
        raise ValueError(f"the weights' time {doc['at'][:64]!r} is not an ISO time") from None
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - at).total_seconds()
    if age < -MAX_SKEW_S:
        raise ValueError(f"the weights are dated {-age:.0f}s in the future (more than {MAX_SKEW_S:.0f}s of clock "
                         "skew): refusing them")
    if max_age_s and age > max_age_s:
        raise ValueError(f"the weights are {age:.0f}s old (limit {max_age_s:.0f}s)")
    return doc


def _weight(w) -> float:
    try:
        x = float(w)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) and x > 0 else 0.0


def _normalise(pairs: list[tuple[int, float]]) -> tuple[list[int], list[float]]:
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return [], []
    return [u for u, _ in pairs], [w / total for _, w in pairs]


def published_total(doc: dict) -> float:
    return sum(_weight(x) for x in doc["weights"].values())


def vector_share(doc: dict):
    sh = doc.get("shares")
    return sh.get("vector", "?") if isinstance(sh, dict) else "?"


def by_uid(weights: dict[str, float], listed: dict[str, str], hotkeys: list[str]) -> tuple[list[int], list[float], list[str]]:
    pairs, dropped, seen = [], [], set()
    for k, w in weights.items():
        uid, hk = int(k), listed.get(str(k))
        if uid in seen:
            dropped.append(f"uid {uid}: listed twice")
        elif not hk:
            dropped.append(f"uid {uid}: no hotkey listed")
        elif uid >= len(hotkeys):
            dropped.append(f"uid {uid}: not on the subnet")
        elif hotkeys[uid] != hk:
            dropped.append(f"uid {uid}: now held by {hotkeys[uid]}, scored for {hk}")
        elif _weight(w) > 0:
            pairs.append((uid, _weight(w)))
        seen.add(uid)
    uids, ws = _normalise(pairs)
    return uids, ws, dropped


def select(doc: dict, hotkeys: list[str]) -> tuple[list[int], list[float], list[str]]:
    return by_uid(doc["weights"], doc["hotkeys"], hotkeys)


def _print_unchecked(doc: dict) -> None:
    w = doc["weights"]
    if not w:
        print("  (no miner earned points in the window)")
    for k, x in sorted(w.items(), key=lambda kv: -_weight(kv[1])):
        print(f"  {int(k):4d}  {_weight(x):.6f}  {doc['hotkeys'].get(k, '?')}")


def run_once(args, fetch, pubkey: str | None) -> bool:
    doc = fetch_verified(fetch, pubkey, args.max_age, args.netuid)
    w = doc["weights"]
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] weights at {doc['at']} "
          f"({len(w)} uids, round {doc.get('round', '?')}, {doc['format']}, key {doc['public_key'][:16]}...)")
    print(f"  published total {published_total(doc):.6f} (vector share {vector_share(doc)}), king uid "
          f"{doc.get('king_uid', '?')}: only these uids get weight, in these proportions")
    try:
        bt = ch.bittensor()
    except ch.ChainUnavailable:
        if args.dry_run:
            print("bittensor is not installed: showing the weights as published, not checked against the metagraph")
            _print_unchecked(doc)
            return True
        raise
    sub = bt.Subtensor(network=args.network)
    hotkeys = ch.metagraph_hotkeys(sub, args.netuid)
    uids, ws, dropped = select(doc, hotkeys)
    for note in dropped:
        print(f"  skipped {note}")
    if args.dry_run:
        print("dry run: uid  weight")
        for u, x in zip(uids, ws):
            print(f"  {u:4d}  {x:.6f}  {hotkeys[u]}")
        return True
    if not uids:
        print("no weights to set this round")
        return True
    wallet = open_wallet(args)
    if wallet.hotkey.ss58_address not in hotkeys:
        print(f"warning: validator hotkey {wallet.hotkey.ss58_address} is not registered on netuid {args.netuid}")
    ok, msg = ch.set_weights(sub, wallet, args.netuid, uids, ws)
    print(("set weights for " if ok else "setting weights failed for ") + f"{len(uids)} uids" + (f": {msg}" if msg else ""))
    return ok


def main(args) -> int:
    if args.netuid is None:
        print("refusing to run: no netuid set (netuid in the config file, or --netuid)", file=sys.stderr)
        return 2
    pubkey = pinned_pubkey(args.pubkey)
    if not pubkey:
        if not args.dry_run:
            print("refusing to run: no team public key pinned (weights_pubkey in the config file, --pubkey or "
                  "VECTOR_VALIDATOR_WEIGHTS_PUBKEY)", file=sys.stderr)
            return 2
        print("warning: no team public key pinned, checking the signature against the key the server sends", file=sys.stderr)
    def fetch():
        return wt.fetch(args.api)
    while True:
        try:
            ok = run_once(args, fetch, pubkey)
        except (wt.ApiError, ValueError, ch.ChainUnavailable, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            ok = False
        except Exception as e:
            print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
            ok = False
        if args.once:
            return 0 if ok else 1
        time.sleep(args.interval)
