"""Remove the test data and access from the Supabase project: empty and delete the
bucket, delete the four Auth users, then run db/teardown.sql.

    SUPABASE_URL=... SUPABASE_REF=... SUPABASE_ADMIN_KEY_FILE=... \
        python infra/supabase_teardown.py <creds file>
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

URL, REF = os.environ["SUPABASE_URL"], os.environ["SUPABASE_REF"]
KEYS = json.load(open(os.environ["SUPABASE_ADMIN_KEY_FILE"]))
ADMIN = next(k["api_key"] for k in KEYS if k["name"] == "service_role")
H = {"apikey": ADMIN, "Authorization": f"Bearer {ADMIN}"}
creds = json.load(open(sys.argv[1]))

r = httpx.post(f"{URL}/storage/v1/bucket/prepress-files/empty", headers=H, timeout=60)
print("empty bucket:", r.status_code)
for _ in range(10):   # emptying is asynchronous; deletion succeeds once it is done
    r = httpx.delete(f"{URL}/storage/v1/bucket/prepress-files", headers=H, timeout=60)
    if r.status_code == 200:
        break
    time.sleep(10)
print("delete bucket:", r.status_code)
for name, u in creds["users"].items():
    r = httpx.delete(f"{URL}/auth/v1/admin/users/{u['id']}", headers=H, timeout=30)
    print(f"delete user {name}:", r.status_code)
sql = Path(__file__).resolve().parent.parent / "db" / "teardown.sql"
r = subprocess.run(["supabase", "db", "query", "--linked", "--project-ref", REF, "-f", str(sql)],
                   capture_output=True, text=True)
print("schema, policies and roles dropped:", r.returncode == 0 and '"_tag":"Error"' not in r.stdout)
