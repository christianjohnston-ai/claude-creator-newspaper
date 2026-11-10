"""A small Apify API client (standard library only).

Rules this file enforces:
- The token is sent only in the Authorization: Bearer header, never in a URL.
- The token is sent only to https://api.apify.com, never to any other host
  (video links from scrapers point at other hosts and must not get it).
- Errors come back as ApifyError with a plain-words explanation.

Endpoints used (checked against docs.apify.com on 2026-11-10):
  GET  /v2/users/me                          who the token belongs to
  GET  /v2/users/me/limits                   this month's usage and limit
  POST /v2/acts/{user}~{actor}/runs          start a run (waitForFinish <= 60 s)
  GET  /v2/actor-runs/{runId}                poll a run (waitForFinish <= 60 s)
  GET  /v2/datasets/{datasetId}/items        read the results
The one-call endpoint run-sync-get-dataset-items exists too, but it gives up
after 300 seconds (HTTP 408), and a first run over many creators can take
longer, so this client starts the run and polls it instead.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

API_HOST = "api.apify.com"
API_BASE = "https://api.apify.com/v2"
USER_AGENT = "creator-newspaper-skill/1.0"
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


class ApifyError(Exception):
    def __init__(self, status: int, err_type: str, message: str):
        self.status = status
        self.err_type = err_type
        self.message = message
        super().__init__(self.plain())

    def plain(self) -> str:
        t, s = self.err_type, self.status
        if t in ("token-not-provided", "x402-payment-required", "missing-api-token"):
            why = ("Apify got a request with no token. Your token is missing from .env "
                   "(APIFY_TOKEN). Ask Claude to set it up again.")
        elif s == 401 or t in ("user-or-token-not-found", "invalid-token"):
            why = ("Apify did not accept your token. It may be mistyped or revoked. Make a new one in "
                   "Apify Console > Settings > API & Integrations, save it in a text file, and ask Claude "
                   "to set it up again.")
        elif s == 402:
            why = ("Apify says this account cannot pay for this run (out of monthly credit, or the run "
                   "needs a paid plan). Check Billing in Apify Console.")
        elif s == 403:
            why = "Apify says this token is not allowed to do that (permissions)."
        elif s == 404:
            why = ("Apify could not find that actor, run or dataset. The actor may have been renamed; "
                   "check the Apify Store.")
        elif s == 408:
            why = "The Apify run took too long and timed out."
        elif s == 429:
            why = "Apify is rate limiting us. Wait a minute and try again."
        elif s >= 500:
            why = "Apify had a server problem. Try again in a few minutes."
        elif s == 0:
            why = "Could not reach api.apify.com. Check the internet connection."
        else:
            why = "Apify refused the request."
        return "%s (HTTP %s, %s: %s)" % (why, s, t or "error", self.message)


def actor_path_id(actor: str) -> str:
    """'apify/instagram-reel-scraper' -> 'apify~instagram-reel-scraper' (Apify's URL form)."""
    return actor.strip().replace("/", "~")


def build_url(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    if not path.startswith("/"):
        path = "/" + path
    url = API_BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if "token" in clean:
            raise ValueError("The token never goes in a URL.")
        url += "?" + urllib.parse.urlencode(clean)
    return url


class Apify:
    def __init__(self, token: Optional[str], retries: int = 3):
        self._token = token
        self.retries = retries

    # The token is never part of repr or logs.
    def __repr__(self) -> str:
        return "Apify(token=%s)" % ("set" if self._token else "missing")

    def _headers(self, url: str, has_body: bool) -> Dict[str, str]:
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if has_body:
            h["Content-Type"] = "application/json"
        host = urllib.parse.urlparse(url).hostname or ""
        if self._token and host == API_HOST:
            h["Authorization"] = "Bearer " + self._token
        return h

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                body: Any = None, timeout: float = 90.0) -> Any:
        url = build_url(path, params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last: Optional[ApifyError] = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=self._headers(url, data is not None))
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as exc:
                err = _parse_error(exc.code, exc.read())
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    last = err
                    time.sleep(min(60, 5 * (2 ** attempt)))
                    continue
                raise err
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = ApifyError(0, "network", str(getattr(exc, "reason", exc)))
                if attempt < self.retries:
                    time.sleep(min(60, 5 * (2 ** attempt)))
                    continue
                raise last
        raise last or ApifyError(0, "unknown", "no response")

    # ------------------------------------------------------------ account
    def me(self) -> Dict[str, Any]:
        return (self.request("GET", "/users/me") or {}).get("data", {})

    def limits(self) -> Dict[str, Any]:
        return (self.request("GET", "/users/me/limits") or {}).get("data", {})

    # ------------------------------------------------------------- actors
    def start_run(self, actor: str, actor_input: Dict[str, Any],
                  max_total_charge_usd: Optional[float] = None, wait: int = 60) -> Dict[str, Any]:
        params = run_params(max_total_charge_usd, wait)
        return (self.request("POST", "/acts/%s/runs" % actor_path_id(actor), params=params,
                             body=actor_input, timeout=wait + 60) or {}).get("data", {})

    def get_run(self, run_id: str, wait: int = 60) -> Dict[str, Any]:
        return (self.request("GET", "/actor-runs/%s" % urllib.parse.quote(run_id),
                             params={"waitForFinish": wait}, timeout=wait + 60) or {}).get("data", {})

    def dataset_items(self, dataset_id: str, page: int = 1000) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            chunk = self.request("GET", "/datasets/%s/items" % urllib.parse.quote(dataset_id),
                                 params={"format": "json", "clean": "true", "offset": offset, "limit": page})
            chunk = chunk or []
            items.extend(chunk)
            if len(chunk) < page:
                return items
            offset += page

    def run_actor(self, actor: str, actor_input: Dict[str, Any], max_total_charge_usd: Optional[float],
                  max_minutes: float = 45.0, on_wait=None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        run = self.start_run(actor, actor_input, max_total_charge_usd)
        deadline = time.time() + max_minutes * 60
        while run.get("status") not in TERMINAL:
            if time.time() > deadline:
                raise ApifyError(408, "client-timeout",
                                 "run %s still %s after %d minutes" % (run.get("id"), run.get("status"), max_minutes))
            if on_wait:
                on_wait(run)
            run = self.get_run(run["id"])
        items = self.dataset_items(run["defaultDatasetId"]) if run.get("defaultDatasetId") else []
        return run, items


def run_params(max_total_charge_usd: Optional[float], wait: int = 60) -> Dict[str, Any]:
    params: Dict[str, Any] = {"waitForFinish": max(0, min(60, int(wait)))}
    if max_total_charge_usd is not None:
        # Apify itself stops charging past this amount, a second guard behind our own estimate.
        params["maxTotalChargeUsd"] = "%.4f" % max(0.0, max_total_charge_usd)
    return params


def _parse_error(status: int, raw: bytes) -> ApifyError:
    try:
        err = json.loads(raw.decode("utf-8")).get("error", {})
        return ApifyError(status, str(err.get("type", "")), str(err.get("message", "")))
    except Exception:
        return ApifyError(status, "http-error", raw[:200].decode("utf-8", "replace"))
