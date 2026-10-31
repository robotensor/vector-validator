from __future__ import annotations


class ChainUnavailable(RuntimeError):
    pass


def bittensor():
    try:
        import bittensor as bt
    except ImportError:
        raise ChainUnavailable('this needs the bittensor SDK: pip install "vector-validator[chain]"') from None
    return bt


def _field(row, key: str):
    return row.get(key) if isinstance(row, dict) else getattr(row, key)


def metagraph_hotkeys(sub, netuid: int) -> list[str]:
    if hasattr(sub, "read") and not hasattr(sub, "metagraph"):
        rows = sub.read("neurons", netuid=netuid, lite=True)
        by = {int(_field(r, "uid")): str(_field(r, "hotkey")) for r in rows}
        return [by.get(u, "") for u in range(max(by) + 1)] if by else []
    mg = sub.metagraph(netuid=netuid, lite=True)
    return [str(h) for h in mg.hotkeys]


def set_weights(sub, wallet, netuid: int, uids: list[int], weights: list[float]) -> tuple[bool, str]:
    if not hasattr(sub, "set_weights"):
        bt = bittensor()
        signer = getattr(wallet, "hotkey", wallet)
        res = sub.execute(bt.intents.SetWeights(netuid=netuid, uids=list(uids), weights=list(weights)), signer)
        ok = getattr(res, "success", None)
        return (bool(res) if ok is None else bool(ok)), str(getattr(res, "message", "") or getattr(res, "error", "") or "")
    res = sub.set_weights(wallet=wallet, netuid=netuid, uids=uids, weights=weights,
                          wait_for_inclusion=True, wait_for_finalization=False)
    if isinstance(res, tuple):
        return bool(res[0]), str(res[1] if len(res) > 1 else "")
    ok = getattr(res, "success", None)
    return (bool(res) if ok is None else bool(ok)), str(getattr(res, "message", "") or "")
