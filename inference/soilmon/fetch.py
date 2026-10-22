"""Data sources: Supabase REST (service/secret key), a CSV export of the table, or the simulator."""
from __future__ import annotations

import time

import pandas as pd
import requests

COLS = ("device_id,boot_id,seq,ts,m1_mv,m2_mv,m3_mv,m1_mad,m2_mad,m3_mad,soil_t_c,air_t_c,rh_pct,lux,"
        "vbat_mv,rssi,flags,fw,created_at")


class FetchError(RuntimeError):
    pass


def _headers(key: str, extra: dict | None = None) -> dict:
    h = {"apikey": key}
    if key.count(".") == 2:                       # legacy JWT keys also go in Authorization; sb_secret_... keys must not
        h["Authorization"] = f"Bearer {key}"
    h.update(extra or {})
    return h


def _get(session, url, headers, params, tries=3):
    last = None
    for k in range(tries):
        try:
            r = session.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            last = e
            time.sleep(2 ** k)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = FetchError(f"HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(2 ** k)
            continue
        return r
    raise FetchError(f"request failed after {tries} tries: {last}")


def fetch_supabase(url: str, key: str, table: str = "readings", device_id: str | None = None,
                   since: pd.Timestamp | None = None, page: int = 1000, session=None) -> tuple[pd.DataFrame, dict]:
    """All rows with a timestamp since `since`, oldest first, plus {'rows_null_ts': N}."""
    if not url or not key:
        raise FetchError("SUPABASE_URL and SUPABASE_SECRET_KEY must be set")
    s = session or requests.Session()
    base = url.rstrip("/") + f"/rest/v1/{table}"
    rows, offset = [], 0
    params = {"select": COLS, "order": "ts.asc,boot_id.asc,seq.asc", "limit": page}
    if device_id:
        params["device_id"] = f"eq.{device_id}"
    if since is not None:
        params["ts"] = f"gte.{since.tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ')}"
    else:
        params["ts"] = "not.is.null"
    while True:
        r = _get(s, base, _headers(key), {**params, "offset": offset})
        if r.status_code in (401, 403):
            raise FetchError(f"HTTP {r.status_code} from Supabase: {r.text[:200]}  "
                             "(check SUPABASE_SECRET_KEY, and run: grant select on public.readings to service_role;)")
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} from Supabase: {r.text[:300]}")
        chunk = r.json()
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    extra = {"rows_null_ts": None}
    try:                                           # count of rows the node uploaded without a timestamp
        p2 = {"select": "seq", "ts": "is.null", "limit": 1}
        if device_id:
            p2["device_id"] = f"eq.{device_id}"
        r = _get(s, base, _headers(key, {"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"}), p2, tries=2)
        cr = r.headers.get("Content-Range", "")
        if "/" in cr and cr.split("/")[-1].isdigit():
            extra["rows_null_ts"] = int(cr.split("/")[-1])
    except Exception:
        pass
    return pd.DataFrame(rows), extra


def load_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path)
