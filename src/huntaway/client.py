"""
Reach the running service over HTTP, or do the work in this process.

When it is not running.
"""

import json
import urllib.error
import urllib.request
from typing import Any

from huntaway import config, paths
from huntaway.core import METHODS, ApiError, Core, call


class Client:
    """Calls API methods on the daemon, falling back to an in process core."""

    def __init__(self, cfg: config.Config | None = None, local_only: bool = False) -> None:
        self.cfg = cfg or config.load()
        host = str(self.cfg["server.host"])
        if host in ("0.0.0.0", "::", ""):  # nosec B104  # picks where to connect, never binds
            host = "127.0.0.1"
        self.base = f"http://{'[' + host + ']' if ':' in host else host}:{self.cfg['server.port']}"
        self.local_only = local_only or not self.cfg["server.enabled"]
        self._core: Core | None = None
        self._remote: bool | None = None

    def token(self) -> str:
        """
        Return the API token, if one exists.

        :returns: The token or an empty string
        """
        try:
            return paths.token_file().read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def remote(self) -> bool:
        """
        Whether the daemon answers.

        :returns: True when it does
        """
        if self._remote is None:
            if self.local_only:
                self._remote = False
            else:
                try:
                    with urllib.request.urlopen(self.base + "/healthz", timeout=1.5) as r:  # nosec B310  # http only
                        self._remote = r.status == 200
                except urllib.error.URLError, OSError, ValueError:
                    self._remote = False
        return self._remote

    def core(self) -> Core:
        """
        Return the in process core, created on first use.

        :returns: The core
        """
        if self._core is None:
            self._core = Core(self.cfg)
        return self._core

    def call(self, name: str, **params: Any) -> Any:
        """
        Run an API method.

        :param name: The method
        :param params: Its parameters
        :returns: The result
        :raises ApiError: When the method fails
        """
        if not self.remote():
            return call(self.core(), name, params)
        method = METHODS.get(name)
        data = json.dumps(params).encode()
        request = urllib.request.Request(
            f"{self.base}/api/{name}",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token()}"},
            method="POST",
        )
        timeout = 600 if method is not None and not method.readonly else 60
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310  # http to our own daemon
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode()).get("error", str(exc))
            except ValueError:
                message = str(exc)
            raise ApiError(message, exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(f"the service did not answer: {exc}", 503) from None
