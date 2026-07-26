#!/usr/bin/env python3
"""Issue an access token to a user. This is the admin side of access control:
run it to mint a token, then hand the plaintext to the user out of band. Only the
hash is stored, so the plaintext is shown exactly once, here.

    # Install the tablestore SDK first (it is not in the local dev venv):
    uv pip install tablestore

    # Point at the deployed table (its name is a Terraform output) and run.
    # Needs OTS_ENDPOINT, OTS_INSTANCE, and Aliyun credentials in the environment:
    ARENA_TABLE=$(terraform -chdir=terraform output -raw ots_table) \
    OTS_ENDPOINT="https://<instance>.<region>.ots.aliyuncs.com" \
    OTS_INSTANCE="<instance>" \
    ALIBABA_CLOUD_ACCESS_KEY_ID="..." \
    ALIBABA_CLOUD_ACCESS_KEY_SECRET="..." \
        python scripts/issue-token.py --user alice --name "Alice"

    # Revoke later by deleting the TOKEN# row, or set revoked=true, in Tablestore.

Requires the tablestore SDK and Aliyun credentials with write access to the arena table.
"""

import argparse
import secrets
import sys
from pathlib import Path

# Make the backend package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from arena.store import OTSStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Mint an arena access token.")
    ap.add_argument("--user", required=True, help="stable user id (e.g. github handle)")
    ap.add_argument("--name", help="display name (defaults to --user)")
    ap.add_argument("--label", default="cli", help="token label, for your own records")
    ap.add_argument("--admin", action="store_true",
                    help="grant admin rights (e.g. resetting any match)")
    ap.add_argument("--table", help="Tablestore table (default: $ARENA_TABLE, then "
                                    "'arena'; the deployed name is "
                                    "`terraform output -raw ots_table`)")
    args = ap.parse_args()

    store = OTSStore(table_name=args.table)
    token = "arena_" + secrets.token_urlsafe(32)
    store.put_user(args.user, args.name or args.user)
    store.put_token(token, args.user, args.label, admin=args.admin)

    print(f"user:  {args.user}")
    print(f"token: {token}")
    if args.admin:
        print("admin: yes (can reset matches)")
    print("\nHand this token to the user. It is not recoverable -- only its hash is stored.")


if __name__ == "__main__":
    main()
