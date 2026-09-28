"""Credential management CLI.

Usage::

    python -m travel_api.cli create-key --owner alice --label laptop
    python -m travel_api.cli list-keys
    python -m travel_api.cli revoke-key <key-id>

Kept as a module ``__main__`` entry point so it works inside the same container
as the API without a separate install step, and so it reuses the exact
``AuthService`` the API uses rather than duplicating hashing or validation.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from travel_api.config import get_settings
from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.services.auth import ALL_SCOPES, AuthService


def _service() -> AuthService:
    settings = get_settings()
    database = Database(settings.database_url)
    return AuthService(
        lambda: UnitOfWork(database.session_factory),
        enabled=True,
        anonymous_owner=settings.auth_anonymous_owner,
    )


def _create_key(args: argparse.Namespace) -> int:
    service = _service()
    scopes = tuple(args.scope or ())
    try:
        key, key_id = service.mint(args.owner, label=args.label, scopes=scopes)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"owner      : {args.owner}")
    print(f"key id     : {key_id}")
    print(f"label      : {args.label or '-'}")
    print(f"scopes     : {', '.join(scopes) if scopes else ', '.join(ALL_SCOPES) + ' (all)'}")
    print()
    print("API key (shown once, store it now):")
    print(f"  {key}")
    print()
    print("Use it as:  Authorization: Bearer <key>")
    return 0


def _list_keys(args: argparse.Namespace) -> int:
    service = _service()
    records = service.list_keys(args.owner)
    if not records:
        print("no API keys")
        return 0
    print(f"{'id':<38} {'prefix':<14} {'owner':<16} {'active':<7} {'label'}")
    for record in records:
        print(
            f"{record['id']:<38} {record['key_prefix']:<14} "
            f"{record['owner_id']:<16} {str(record['active']):<7} {record['label']}"
        )
    return 0


def _revoke_key(args: argparse.Namespace) -> int:
    service = _service()
    try:
        key_id = UUID(args.key_id)
    except ValueError:
        print(f"error: {args.key_id!r} is not a valid key id", file=sys.stderr)
        return 2
    if service.revoke(key_id):
        print(f"revoked {key_id}")
        return 0
    print(f"error: no revocable key with id {key_id}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="travel_api.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-key", help="mint a new API key")
    create.add_argument("--owner", required=True, help="owner identity the key acts as")
    create.add_argument("--label", default="", help="human label, e.g. 'laptop'")
    create.add_argument(
        "--scope",
        action="append",
        choices=list(ALL_SCOPES),
        help="restrict to a scope; repeatable. Omit for full access.",
    )
    create.set_defaults(func=_create_key)

    listing = sub.add_parser("list-keys", help="list keys (hashes are never shown)")
    listing.add_argument("--owner", default=None, help="filter by owner")
    listing.set_defaults(func=_list_keys)

    revoke = sub.add_parser("revoke-key", help="revoke a key by id")
    revoke.add_argument("key_id")
    revoke.set_defaults(func=_revoke_key)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
