#!/usr/bin/env python3
"""CLI tool for managing GC Flow Analyzer org profiles.

Usage:
  python manage_orgs.py generate-key
  python manage_orgs.py hash-password
  python manage_orgs.py list
  python manage_orgs.py add --name "Customer A" --environment mypurecloud.ie \\
      --client-id CLIENT_ID --client-secret CLIENT_SECRET
  python manage_orgs.py delete --name "Customer A"

Environment:
  FC_ENCRYPTION_KEY   Fernet encryption key (also read from .env.prod)
  ORGS_FILE           Path to orgs.yaml (default: ./data/orgs.yaml)
"""

import argparse, base64, getpass, os, sys
from pathlib import Path

# Load .env.prod if it exists
env_file = Path(".env.prod")
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from cryptography.fernet import Fernet
import bcrypt
import yaml


def _get_fernet() -> Fernet:
    key = os.environ.get("FC_ENCRYPTION_KEY", "")
    if not key:
        print("ERROR: FC_ENCRYPTION_KEY not set. Run 'python manage_orgs.py generate-key' first.")
        sys.exit(1)
    return Fernet(key.encode())


def _orgs_file() -> Path:
    return Path(os.environ.get("ORGS_FILE", "./data/orgs.yaml"))


def _load_raw() -> dict:
    f = _orgs_file()
    if not f.exists():
        return {"orgs": []}
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {"orgs": []}


def _save(data: dict):
    f = _orgs_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def cmd_generate_key(_args):
    key = Fernet.generate_key().decode()
    print(f"\nFC_ENCRYPTION_KEY={key}\n")
    print("Add this to .env.prod  — keep it safe, losing it means re-entering all credentials.")


def cmd_hash_password(_args):
    pw = getpass.getpass("Enter password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("ERROR: Passwords do not match.")
        sys.exit(1)
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt())
    h_b64 = base64.b64encode(h).decode()
    print(f"\nAPP_PASSWORD_HASH={h_b64}\n")
    print("Add this to .env.prod")


def cmd_list(_args):
    data = _load_raw()
    orgs = data.get("orgs", [])
    if not orgs:
        print("No org profiles configured.")
        return
    print(f"\n{'#':<4} {'Name':<40} {'Environment'}")
    print("-" * 70)
    for i, org in enumerate(orgs, 1):
        print(f"{i:<4} {org.get('name','?'):<40} {org.get('environment','?')}")
    print()


def cmd_add(args):
    fernet = _get_fernet()
    data = _load_raw()
    orgs = data.setdefault("orgs", [])

    # Remove existing entry with same name
    orgs[:] = [o for o in orgs if o.get("name") != args.name]

    orgs.append({
        "name": args.name,
        "environment": args.environment,
        "client_id":     fernet.encrypt(args.client_id.encode()).decode(),
        "client_secret": fernet.encrypt(args.client_secret.encode()).decode(),
    })
    _save(data)
    print(f"Org '{args.name}' ({args.environment}) saved to {_orgs_file()}")


def cmd_delete(args):
    data = _load_raw()
    before = len(data.get("orgs", []))
    data["orgs"] = [o for o in data.get("orgs", []) if o.get("name") != args.name]
    if len(data["orgs"]) == before:
        print(f"Org '{args.name}' not found.")
        sys.exit(1)
    _save(data)
    print(f"Org '{args.name}' deleted.")


def main():
    parser = argparse.ArgumentParser(description="GC Flow Analyzer org management")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-key", help="Generate a new Fernet encryption key")
    sub.add_parser("hash-password", help="Generate bcrypt hash for APP_PASSWORD_HASH")
    sub.add_parser("list", help="List configured orgs")

    p_add = sub.add_parser("add", help="Add or update an org profile")
    p_add.add_argument("--name",          required=True, help="Org display name")
    p_add.add_argument("--environment",   required=True, help="GC environment domain (e.g. mypurecloud.ie)")
    p_add.add_argument("--client-id",     required=True, dest="client_id")
    p_add.add_argument("--client-secret", required=True, dest="client_secret")

    p_del = sub.add_parser("delete", help="Delete an org profile")
    p_del.add_argument("--name", required=True)

    args = parser.parse_args()
    {"generate-key": cmd_generate_key, "hash-password": cmd_hash_password,
     "list": cmd_list, "add": cmd_add, "delete": cmd_delete}[args.command](args)


if __name__ == "__main__":
    main()
