"""Supabase Storage as a scoped user, never with a service key.

The worker signs in as its own Auth user; storage policies let that user read
`inputs/` and create (never overwrite) objects under `outputs/`."""
import time

import httpx

from . import config

_session = {"token": None, "exp": 0.0}


def _token():
    if _session["token"] and _session["exp"] - time.time() > 60:
        return _session["token"]
    r = httpx.post(f"{config.SUPABASE_URL}/auth/v1/token?grant_type=password",
                   headers={"apikey": config.SUPABASE_ANON_KEY},
                   json={"email": config.STORAGE_EMAIL, "password": config.STORAGE_PASSWORD},
                   timeout=20)
    r.raise_for_status()
    body = r.json()
    _session.update(token=body["access_token"], exp=time.time() + body.get("expires_in", 3600))
    return _session["token"]


def _h(extra=None):
    h = {"apikey": config.SUPABASE_ANON_KEY, "Authorization": f"Bearer {_token()}"}
    h.update(extra or {})
    return h


def signed_url(path, expires_s=300):
    """Signed URL minted when the attempt starts, not when the order was paid."""
    r = httpx.post(f"{config.SUPABASE_URL}/storage/v1/object/sign/{config.BUCKET}/{path}",
                   headers=_h(), json={"expiresIn": expires_s}, timeout=20)
    r.raise_for_status()
    return f"{config.SUPABASE_URL}/storage/v1{r.json()['signedURL']}"


def download_signed(url, dest):
    with httpx.stream("GET", url, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)


def upload_new(path, data: bytes, content_type):
    """Create an immutable object. x-upsert false: an existing path is an error."""
    r = httpx.post(f"{config.SUPABASE_URL}/storage/v1/object/{config.BUCKET}/{path}",
                   headers=_h({"content-type": content_type, "x-upsert": "false"}),
                   content=data, timeout=60)
    r.raise_for_status()
