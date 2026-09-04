"""HTTP client used by the CLI. Credentials live in ``~/.config/relay/credentials.json`` or
``RELAY_TOKEN`` / ``RELAY_URL`` environment variables (env wins)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

CONFIG_DIR = Path(os.environ.get("RELAY_CONFIG_DIR", Path.home() / ".config" / "relay"))
CREDENTIALS = CONFIG_DIR / "credentials.json"


@dataclass
class Credentials:
    url: str
    token: str
    user_id: str = ""

    @classmethod
    def load(cls) -> Credentials:
        url = os.environ.get("RELAY_URL")
        token = os.environ.get("RELAY_TOKEN")
        user_id = os.environ.get("RELAY_USER", "")
        if not (url and token) and CREDENTIALS.exists():
            data = json.loads(CREDENTIALS.read_text())
            url = url or data.get("url")
            token = token or data.get("token")
            user_id = user_id or data.get("user_id", "")
        if not url or not token:
            raise RuntimeError("not logged in: run `relay login` or set RELAY_URL and RELAY_TOKEN")
        return cls(url=url.rstrip("/"), token=token, user_id=user_id)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CREDENTIALS.write_text(
            json.dumps({"url": self.url, "token": self.token, "user_id": self.user_id}, indent=2)
        )
        CREDENTIALS.chmod(0o600)


class RelayClient:
    def __init__(
        self,
        creds: Credentials | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 3700,
    ) -> None:
        self.creds = creds or Credentials.load()
        self.http = httpx.Client(
            base_url=self.creds.url,
            headers={"Authorization": f"Bearer {self.creds.token}"},
            timeout=timeout,
            transport=transport,
        )

    def _raise(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"{r.status_code}: {detail}")

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        r = self.http.post(f"/v1/tools/{name}", json=args)
        self._raise(r)
        return r.json()  # type: ignore[no-any-return]

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        self._raise(r)
        return r.json()

    def post(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        r = self.http.post(path, json=json_body, **kw)
        self._raise(r)
        return r.json()

    def whoami(self) -> dict[str, Any]:
        return self.get("/v1/me")  # type: ignore[no-any-return]
