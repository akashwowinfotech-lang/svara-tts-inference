"""Set or rotate dashboard credentials without placing the password in shell history."""

import argparse
import getpass
import os
import sqlite3
from pathlib import Path

from api.admin_auth import create_credentials


parser = argparse.ArgumentParser()
parser.add_argument("--username", required=True)
args = parser.parse_args()
password = getpass.getpass("New dashboard password: ")
confirm = getpass.getpass("Confirm password: ")
if password != confirm:
    raise SystemExit("Passwords did not match")
path = Path(__file__).resolve().parent / ".secrets" / "admin-login.json"
create_credentials(path, args.username, password)
db_path = Path(os.getenv("ACCESS_DB_PATH", str(path.parent.parent / ".data" / "access.sqlite3")))
if db_path.is_file():
    with sqlite3.connect(db_path) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_sessions'").fetchone()
        if exists:
            db.execute("DELETE FROM admin_sessions")
print("Dashboard credentials saved. Restart the server to use them.")
