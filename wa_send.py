"""Minimal WhatsApp sender over an MCP bridge (tools/call send_whatsapp_text).

Everything private comes from the environment (repository secrets):
  WA_MCP_URL      the bridge endpoint
  WA_MCP_TOKEN    bearer token bound to one WhatsApp system
  WA_MCP_SYSTEM   system id (default "primary")
The bridge only delivers to that system's approved recipients.
"""
import datetime as dt
import hashlib
import json
import os
import time

import requests


class WhatsAppError(RuntimeError):
    """A send did not succeed. Never carries the token."""


def _env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


def _call(tool, arguments, attempts=3):
    url, token = _env("WA_MCP_URL"), _env("WA_MCP_TOKEN")
    if not url or not token:
        raise WhatsAppError("WA_MCP_URL / WA_MCP_TOKEN not set (repository secrets)")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    last = None
    for i in range(attempts):
        try:
            r = requests.post(url, json=body, headers=headers, timeout=30)
        except requests.RequestException as e:
            last = WhatsAppError(f"transport error: {e}")
        else:
            text = (r.text or "")[:500]
            if r.status_code >= 500:
                last = WhatsAppError(f"bridge error {r.status_code}: {text}")
            elif r.status_code >= 300:
                raise WhatsAppError(f"bridge rejected the request ({r.status_code}): {text}")
            else:
                msg = r.json()
                if "error" in msg:
                    raise WhatsAppError(f"MCP error: {str(msg['error'])[:500]}")
                result = msg.get("result") or {}
                out = "".join(c.get("text", "") for c in result.get("content") or []
                              if c.get("type") == "text")
                if result.get("isError"):
                    raise WhatsAppError(f"{tool} failed: {out[:500]}")
                try:
                    return json.loads(out) if out else result
                except ValueError:
                    return out
        if i < attempts - 1:
            time.sleep(2 ** i)
    raise last


def approved_recipients():
    return _call("list_approved_recipients", {"systemId": _env("WA_MCP_SYSTEM", "primary")},
                 attempts=1)


def send_number(message, number, *, kind="alert", allow_personal=True):
    """Send `message` to one E.164 number. The key collapses retries within a minute."""
    if not str(message).strip():
        raise ValueError("refusing to send an empty message")
    if not number:
        raise WhatsAppError("no recipient number set (repository secret)")
    minute = int(dt.datetime.now(dt.timezone.utc).timestamp() // 60)
    key = f"{kind}-{minute}-" + hashlib.sha256(f"{kind}|{number}|{message}".encode()).hexdigest()[:12]
    return _call("send_whatsapp_text", {"systemId": _env("WA_MCP_SYSTEM", "primary"),
                                        "message": message, "idempotencyKey": key,
                                        "phoneNumber": number})


if __name__ == "__main__":          # python wa_send.py  -> token check, sends nothing
    rec = approved_recipients()   # never print the list: this repo's logs are public
    print(f"token OK - {len(rec) if isinstance(rec, list) else '?'} approved recipients")
