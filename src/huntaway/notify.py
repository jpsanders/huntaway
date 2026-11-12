"""
Tell the user what happened.

A desktop notification, and optionally a webhook (ntfy, Slack, Discord or JSON). Sending happens on a worker thread,
so a slow network never delays an action.
"""

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

from huntaway import osinfo
from huntaway.config import Config


def webhook_request(url: str, title: str, body: str, severity: str, extra: dict[str, Any]) -> urllib.request.Request:
    """
    Build the webhook request in the shape the receiving service expects.

    :param url: The webhook URL
    :param title: The title
    :param body: The text
    :param severity: info to critical
    :param extra: More fields for generic JSON receivers
    :returns: The request
    """
    host = urlparse(url).hostname or ""
    headers = {"User-Agent": "huntaway"}
    if "ntfy" in host:
        headers.update(
            {
                "Title": title.encode("ascii", "replace").decode(),
                "Tags": "dog",
                "Priority": "high" if severity in ("high", "critical") else "default",
            }
        )
        return urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    if host.endswith("slack.com"):
        payload: dict[str, Any] = {"text": f"*{title}*\n{body}"}
    elif host.endswith(("discord.com", "discordapp.com")):
        payload = {"content": f"**{title}**\n{body}"[:1900]}
    else:
        payload = {"title": title, "body": body, "severity": severity, **extra}
    headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=json.dumps(payload, default=str).encode(), headers=headers, method="POST")


class Notifier:
    """Sends notifications without blocking the caller."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="huntaway-notify")

    def send(self, title: str, body: str, severity: str = "info", extra: dict[str, Any] | None = None) -> None:
        """
        Queue a notification.

        :param title: The title
        :param body: The text
        :param severity: info, low, medium, high or critical
        :param extra: More fields for generic webhooks
        """
        self._pool.submit(self._deliver, title, body, severity, extra or {})

    def _deliver(self, title: str, body: str, severity: str, extra: dict[str, Any]) -> None:
        if self.cfg["general.notify"]:
            osinfo.desktop_notify(title, body, urgent=severity in ("high", "critical"))
        url = str(self.cfg["general.webhook"]).strip()
        if url.startswith(("http://", "https://")):
            try:
                request = webhook_request(url, title, body, severity, extra)
                with urllib.request.urlopen(request, timeout=10):  # nosec B310  # scheme checked above
                    pass
            except urllib.error.URLError, OSError, ValueError:
                pass

    def close(self) -> None:
        """Stop the worker thread after queued notifications are sent."""
        self._pool.shutdown(wait=True, cancel_futures=False)
