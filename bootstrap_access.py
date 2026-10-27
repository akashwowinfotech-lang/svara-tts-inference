"""Create local, private admin and owner keys on the first manual start."""

import os
import secrets
from pathlib import Path

from api.access import AccessStore


ROOT = Path(__file__).resolve().parent
secrets_dir = ROOT / ".secrets"
secrets_dir.mkdir(mode=0o700, exist_ok=True)
os.chmod(secrets_dir, 0o700)

admin_file = Path(os.getenv("ADMIN_API_KEY_FILE", str(secrets_dir / "admin.key")))
if not os.getenv("ADMIN_API_KEY") and not admin_file.exists():
    fd = os.open(admin_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secrets.token_urlsafe(48) + "\n")

admin_secret = os.getenv("ADMIN_API_KEY") or admin_file.read_text().strip()
store = AccessStore(os.getenv("ACCESS_DB_PATH", str(ROOT / ".data/access.sqlite3")), admin_secret)

owner_file = secrets_dir / "owner-client.key"
if not owner_file.exists():
    issued = store.create_key("Owner playground", ["127.0.0.1/32", "::1/128"],
                              expires_days=3650, max_concurrent=2, requests_per_minute=60)
    fd = os.open(owner_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(issued["key"] + "\n")

print(f"Dashboard login file: {secrets_dir / 'admin-login.json'}")
print(f"Local playground key file: {owner_file}")
