"""One-time Supabase setup for the test project, run from an operator machine.

- sets passwords on the four prepress_* database roles
- creates the four Supabase Auth users (backend, worker, staff, customer) and their roles
- writes every credential to a local JSON file with mode 0600; nothing is printed

Requires: SUPABASE_URL, SUPABASE_REF, a service-role key in SUPABASE_ADMIN_KEY_FILE
(used here only; it is never deployed), and the `supabase` CLI logged in.

    python infra/supabase_setup.py /path/outside/repo/credentials.json
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile

import httpx

URL = os.environ["SUPABASE_URL"]
REF = os.environ["SUPABASE_REF"]
ADMIN = json.load(open(os.environ["SUPABASE_ADMIN_KEY_FILE"]))
ADMIN_KEY = next(k["api_key"] for k in ADMIN if k["name"] == "service_role")
ANON_KEY = next(k["api_key"] for k in ADMIN if k["name"] == "anon")
ROLES = ["prepress_worker", "prepress_dispatcher", "prepress_app", "prepress_observer"]
USERS = ["backend", "worker", "staff", "customer"]


def sql(query: str):
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=True) as fh:
        fh.write(query)
        fh.flush()
        r = subprocess.run(["supabase", "db", "query", "--linked", "--project-ref", REF, "-f", fh.name],
                           capture_output=True, text=True)
    if r.returncode != 0 or '"_tag":"Error"' in r.stdout:
        raise SystemExit("sql failed (output withheld: may contain credentials)")
    return r.stdout


def main(out_path):
    creds = {"supabase_url": URL, "ref": REF, "anon_key": ANON_KEY, "db": {}, "users": {}}
    for role in ROLES:
        pw = secrets.token_urlsafe(24)
        sql(f"alter role {role} with login password '{pw}';")
        creds["db"][role] = pw
    h = {"apikey": ADMIN_KEY, "Authorization": f"Bearer {ADMIN_KEY}"}
    for u in USERS:
        email, pw = f"{u}@prepress-test.example", secrets.token_urlsafe(24)
        r = httpx.post(f"{URL}/auth/v1/admin/users", headers=h,
                       json={"email": email, "password": pw, "email_confirm": True})
        if r.status_code == 422:  # exists: reset the password
            uid = next(x["id"] for x in httpx.get(f"{URL}/auth/v1/admin/users?per_page=200", headers=h)
                       .json()["users"] if x["email"] == email)
            httpx.put(f"{URL}/auth/v1/admin/users/{uid}", headers=h, json={"password": pw}).raise_for_status()
        else:
            r.raise_for_status()
            uid = r.json()["id"]
        creds["users"][u] = {"email": email, "password": pw, "id": uid}
        sql(f"insert into prepress.members (user_id, role) values ('{uid}', '{u}') "
            f"on conflict (user_id) do update set role = excluded.role;")
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(creds, fh)
    print("setup done:", ", ".join(ROLES + USERS))


if __name__ == "__main__":
    main(sys.argv[1])
