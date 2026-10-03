"""Mailu admin REST API client (stdlib only: urllib).

Covers every Mailu v1 endpoint observed in core/admin/mailu/api/v1/:
domains (CRUD, DKIM keys, managers, domain users, alternatives),
users, aliases, relays, tokens.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .config import AdminConfig


class AdminError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"Admin API HTTP {status}: {payload}")


def _body(kw: Dict[str, Any], exclude: tuple = ()) -> Dict[str, Any]:
    """PATCH body: path identifiers stay in the URL, never in the JSON
    body (flask-restx validate=True rejects unknown fields)."""
    return {k: v for k, v in kw.items() if k not in exclude}


class AdminClient:
    def __init__(self, cfg: AdminConfig):
        if not cfg.base_url:
            raise ValueError("MAILU_API_BASE is not configured")
        if not cfg.api_token:
            raise ValueError("MAILU_API_TOKEN is not configured")
        self.cfg = cfg

    def _request(self, method: str, path: str,
                 body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.cfg.base_url}/v1/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method.upper())
        req.add_header("Authorization", f"Bearer {self.cfg.api_token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                payload = {"message": str(exc)}
            raise AdminError(exc.code, payload)

    # -- domains ---------------------------------------------------------
    def list_domains(self): return self._request("GET", "domain")

    def get_domain(self, domain: str): return self._request("GET", f"domain/{domain}")

    def create_domain(self, name: str, **kw): return self._request("POST", "domain", {"name": name, **kw})

    def update_domain(self, domain: str, **kw): return self._request("PATCH", f"domain/{domain}", _body(kw, ("domain",)))

    def delete_domain(self, domain: str): return self._request("DELETE", f"domain/{domain}")

    def generate_dkim(self, domain: str): return self._request("POST", f"domain/{domain}/dkim", {})

    def list_managers(self, domain: str): return self._request("GET", f"domain/{domain}/manager")

    def add_manager(self, domain: str, user_email: str):
        return self._request("POST", f"domain/{domain}/manager", {"user_email": user_email})

    def check_manager(self, domain: str, email: str):
        return self._request("GET", f"domain/{domain}/manager/{email}")

    def remove_manager(self, domain: str, email: str):
        return self._request("DELETE", f"domain/{domain}/manager/{email}")

    def list_domain_users(self, domain: str): return self._request("GET", f"domain/{domain}/users")

    # -- alternative domains ----------------------------------------------
    def list_alternatives(self): return self._request("GET", "alternative")

    def get_alternative(self, alt: str): return self._request("GET", f"alternative/{alt}")

    def create_alternative(self, name: str, domain: str):
        return self._request("POST", "alternative", {"name": name, "domain": domain})

    def delete_alternative(self, alt: str): return self._request("DELETE", f"alternative/{alt}")

    # -- users -------------------------------------------------------------
    def list_users(self): return self._request("GET", "user")

    def get_user(self, email: str): return self._request("GET", f"user/{email}")

    def create_user(self, email: str, raw_password: str, **kw):
        return self._request("POST", "user", {"email": email, "raw_password": raw_password, **kw})

    def update_user(self, email: str, **kw): return self._request("PATCH", f"user/{email}", _body(kw, ("email",)))

    def delete_user(self, email: str): return self._request("DELETE", f"user/{email}")

    # -- aliases -----------------------------------------------------------
    def list_aliases(self): return self._request("GET", "alias")

    def get_alias(self, alias: str): return self._request("GET", f"alias/{alias}")

    def create_alias(self, email: str, destination: List[str], **kw):
        return self._request("POST", "alias", {"email": email, "destination": destination, **kw})

    def update_alias(self, alias: str, **kw): return self._request("PATCH", f"alias/{alias}", _body(kw, ("alias",)))

    def delete_alias(self, alias: str): return self._request("DELETE", f"alias/{alias}")

    def aliases_by_domain(self, domain: str): return self._request("GET", f"alias/destination/{domain}")

    # -- relays ------------------------------------------------------------
    def list_relays(self): return self._request("GET", "relay")

    def get_relay(self, name: str): return self._request("GET", f"relay/{name}")

    def create_relay(self, name: str, **kw): return self._request("POST", "relay", {"name": name, **kw})

    def update_relay(self, name: str, **kw): return self._request("PATCH", f"relay/{name}", _body(kw, ("name",)))

    def delete_relay(self, name: str): return self._request("DELETE", f"relay/{name}")

    # -- tokens ------------------------------------------------------------
    def list_tokens(self): return self._request("GET", "token")

    def create_token(self, email: str, **kw):
        return self._request("POST", "token", {"email": email, **kw})

    def tokens_of_user(self, email: str): return self._request("GET", f"token/user/{email}")

    def create_user_token(self, email: str, **kw):
        return self._request("POST", f"token/user/{email}", kw)

    def get_token(self, token_id: str): return self._request("GET", f"token/{token_id}")

    def update_token(self, token_id: str, **kw):
        return self._request("PATCH", f"token/{token_id}", _body(kw, ("token_id",)))

    def delete_token(self, token_id: str): return self._request("DELETE", f"token/{token_id}")
