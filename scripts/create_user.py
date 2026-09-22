"""Create a user (or rotate their key) and print the API key exactly once.

    python scripts/create_user.py alice@example.com
    python scripts/create_user.py alice@example.com --rotate

Only the key's SHA-256 is stored; a lost key can be replaced, not recovered.
"""

import argparse
import sys

from sqlalchemy import select

from api_service.config import get_database_settings
from api_service.db.models import User
from api_service.db.session import make_sessionmaker
from api_service.services.auth import generate_api_key, hash_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("email")
    parser.add_argument("--rotate", action="store_true", help="replace an existing user's key")
    args = parser.parse_args()
    email = args.email.strip().lower()

    api_key = generate_api_key()
    with make_sessionmaker(get_database_settings().database_url)() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None and args.rotate:
            print(f"no user {email!r} to rotate", file=sys.stderr)
            return 1
        if user is not None and not args.rotate:
            print(f"user {email!r} exists; pass --rotate to issue a new key", file=sys.stderr)
            return 1
        if user is None:
            user = User(email=email, api_key_hash=hash_api_key(api_key))
            db.add(user)
        else:
            user.api_key_hash = hash_api_key(api_key)
        db.commit()
        user_id = user.id

    print(f"user_id: {user_id}")
    print(f"api_key: {api_key}")
    print("Store this key now; it cannot be shown again.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
