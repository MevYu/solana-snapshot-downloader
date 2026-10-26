"""Small shared HTTP helpers. No third-party dependencies."""

import json
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .models import http_url


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, deadline=None):
        super().__init__()
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            http_url(newurl)
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP redirect exceeded request time budget")
                req.timeout = min(req.timeout, remaining)
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        except Exception:
            fp.close()
            raise
        # urllib otherwise reads the entire redirect response before following
        # it. Closing here avoids downloading an unbounded, irrelevant body.
        if redirected is not None:
            fp.close()
        if redirected is not None and req.get_method() == "HEAD":
            redirected.method = "HEAD"
        return redirected


def open_url(url, *, timeout=10.0, method="GET", headers=None, data=None, deadline=None):
    request_headers = {
        "User-Agent": "solana-snapshot-downloader/" + __version__,
        "Accept-Encoding": "identity",
    }
    request_headers.update(headers or {})
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP request time budget exhausted")
        timeout = min(timeout, remaining)
    request = Request(http_url(url), data=data, headers=request_headers, method=method)
    return build_opener(SafeRedirectHandler(deadline)).open(request, timeout=timeout)


def rpc_call(url, method, *, timeout=10.0, params=None):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method,
        "params": params if params is not None else [],
    }).encode("utf-8")
    with open_url(url, method="POST", data=payload, timeout=timeout,
                  headers={"Content-Type": "application/json"}) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("RPC response exceeds 8 MiB")
    result = json.loads(data)
    if not isinstance(result, dict) or result.get("error") or "result" not in result:
        raise ValueError("RPC " + method + " returned an error or invalid response")
    return result["result"]
