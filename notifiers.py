"""
Alert channels. Each notifier exposes send(text) and never raises out of send();
delivery failures are logged so the watcher loop keeps running.

Configure via environment variables (see .env.example):

  LINE_CHANNEL_ACCESS_TOKEN  Messaging API channel access token (long-lived)
  LINE_TO                    user ID (starts with U...), or a group/room ID, to push to
  DISCORD_WEBHOOK_URL        https://discord.com/api/webhooks/...
  NTFY_TOPIC                 optional: ntfy.sh topic for phone push with zero setup
  ALERT_STDOUT               "1" to also print alerts to the console (default on)
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("crystal.notify")


def _chunks(text: str, limit: int) -> list[str]:
    """Split on line boundaries so a long alert never exceeds the channel's max length."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit:
            out.append(cur.rstrip("\n"))
            cur = ""
        while len(line) > limit:  # pathological single long line
            out.append(line[:limit])
            line = line[limit:]
        cur += line
    if cur.strip():
        out.append(cur.rstrip("\n"))
    return out


class Notifier:
    name = "base"

    def send(self, text: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class StdoutNotifier(Notifier):
    name = "stdout"

    def send(self, text: str) -> bool:
        print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60 + "\n", flush=True)
        return True


class LineNotifier(Notifier):
    """LINE Official Account via the Messaging API push endpoint."""
    name = "line"
    PUSH_URL = "https://api.line.me/v2/bot/message/push"
    MAX_TEXT = 5000  # LINE text message limit

    def __init__(self, token: str, to: str):
        self.token = token
        self.to = to

    def send(self, text: str) -> bool:
        ok = True
        for chunk in _chunks(text, self.MAX_TEXT):
            try:
                r = requests.post(
                    self.PUSH_URL,
                    headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                    json={"to": self.to, "messages": [{"type": "text", "text": chunk}]},
                    timeout=15,
                )
                if r.status_code >= 300:
                    log.error("LINE push failed %s: %s", r.status_code, r.text[:300])
                    ok = False
            except Exception as e:  # noqa: BLE001
                log.error("LINE push error: %s", e)
                ok = False
        return ok


class DiscordNotifier(Notifier):
    name = "discord"
    MAX_TEXT = 1900  # Discord content limit is 2000

    def __init__(self, webhook_url: str):
        self.url = webhook_url

    def send(self, text: str) -> bool:
        ok = True
        for chunk in _chunks(text, self.MAX_TEXT):
            try:
                r = requests.post(self.url, json={"content": chunk}, timeout=15)
                if r.status_code == 429:
                    wait = float(r.json().get("retry_after", 2))
                    time.sleep(wait)
                    r = requests.post(self.url, json={"content": chunk}, timeout=15)
                if r.status_code >= 300:
                    log.error("Discord webhook failed %s: %s", r.status_code, r.text[:300])
                    ok = False
            except Exception as e:  # noqa: BLE001
                log.error("Discord webhook error: %s", e)
                ok = False
        return ok


class NtfyNotifier(Notifier):
    """ntfy.sh: install the ntfy app, subscribe to a topic, done. Handy as a stopgap."""
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.url = f"{server.rstrip('/')}/{topic}"

    def send(self, text: str) -> bool:
        try:
            r = requests.post(
                self.url,
                data=text.encode("utf-8"),
                headers={"Title": "Tennis court free", "Priority": "high", "Tags": "tennis"},
                timeout=15,
            )
            if r.status_code >= 300:
                log.error("ntfy failed %s: %s", r.status_code, r.text[:300])
                return False
            return True
        except Exception as e:  # noqa: BLE001
            log.error("ntfy error: %s", e)
            return False


def build_notifiers_from_env() -> list[Notifier]:
    ns: list[Notifier] = []
    if os.getenv("ALERT_STDOUT", "1") not in ("0", "false", "no"):
        ns.append(StdoutNotifier())
    tok, to = os.getenv("LINE_CHANNEL_ACCESS_TOKEN"), os.getenv("LINE_TO")
    if tok and to:
        ns.append(LineNotifier(tok, to))
    if os.getenv("DISCORD_WEBHOOK_URL"):
        ns.append(DiscordNotifier(os.environ["DISCORD_WEBHOOK_URL"]))
    if os.getenv("NTFY_TOPIC"):
        ns.append(NtfyNotifier(os.environ["NTFY_TOPIC"], os.getenv("NTFY_SERVER", "https://ntfy.sh")))
    return ns


def broadcast(notifiers: list[Notifier], text: str) -> None:
    for n in notifiers:
        try:
            n.send(text)
        except Exception as e:  # noqa: BLE001  (belt and braces: never kill the loop)
            log.error("notifier %s crashed: %s", n.name, e)
