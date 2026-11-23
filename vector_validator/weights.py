from __future__ import annotations

import base64
import json

import httpx

DEFAULT_API = "https://vector.robotensor.ai"
FORMAT = "vector-weights/2"
MAX_BYTES = 16 << 20


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def fetch(api: str, timeout: float = 30.0, max_bytes: int = MAX_BYTES) -> dict:
    body = bytearray()
    with httpx.stream("GET", api.rstrip("/") + "/v1/weights", timeout=timeout) as r:
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise ApiError(r.status_code, f"the response is larger than {max_bytes} bytes")
    text = body.decode(errors="replace")
    if r.status_code >= 400:
        try:
            detail = json.loads(text).get("detail", text)
        except (ValueError, AttributeError):
            detail = text
        raise ApiError(r.status_code, str(detail)[:2000])
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("the weights response is not JSON") from None
    if not isinstance(doc, dict):
        raise ValueError("the weights response is not a JSON object")
    return doc


def canonical_message(doc: dict) -> bytes:
    body = {k: v for k, v in doc.items() if k not in ("public_key", "signature")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def verify(doc: dict, pubkey_hex: str | None = None, netuid: int | None = None) -> dict:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    if not isinstance(doc, dict):
        raise ValueError("the weights are not a JSON object")
    if doc.get("format") != FORMAT:
        raise ValueError(f"unknown weights format: {doc.get('format')!r} (expected {FORMAT})")
    sig, given = doc.get("signature"), doc.get("public_key")
    if not sig or not given:
        raise ValueError("the weights are not signed")
    if not isinstance(sig, str) or not isinstance(given, str):
        raise ValueError("the signature and public_key must be strings")
    key = (pubkey_hex or given).lower().removeprefix("0x")
    if pubkey_hex and given.lower().removeprefix("0x") != key:
        raise ValueError(f"signed by an unexpected key {given} (pinned {key})")
    try:
        VerifyKey(bytes.fromhex(key)).verify(canonical_message(doc), base64.b64decode(sig))
    except (BadSignatureError, ValueError) as e:
        raise ValueError(f"bad weights signature: {e}") from None
    if not isinstance(doc.get("weights"), dict):
        raise ValueError("weights missing")
    if not isinstance(doc.get("hotkeys"), dict):
        raise ValueError("the uid to hotkey table is missing")
    if not isinstance(doc.get("netuid"), int) or isinstance(doc.get("netuid"), bool):
        raise ValueError("the weights do not say which netuid they are for")
    bad = [k for k in doc["weights"] if not (str(k).isascii() and str(k).isdigit() and len(str(k)) <= 6)]
    if bad:
        raise ValueError(f"weights keyed by something other than a uid: {bad[:3]}")
    bad = [k for k, v in doc["hotkeys"].items() if not isinstance(v, str)]
    if bad:
        raise ValueError(f"hotkeys that are not strings for uids {bad[:3]}")
    if netuid is not None and doc["netuid"] != netuid:
        raise ValueError(f"the weights are for netuid {doc['netuid']}, not {netuid}: refusing them")
    return doc
