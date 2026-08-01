"""Interactive administrator bootstrap commands; secrets never enter argv."""

from __future__ import annotations

import argparse
import asyncio
import getpass

from .service import AdminAuthError, bootstrap_admin


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="create an MFA-protected administrator")
    bootstrap.add_argument("--username", required=True)
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument("--role", default="super_admin")
    return parser


async def _bootstrap(args: argparse.Namespace) -> int:
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.")
        return 2
    try:
        result = await bootstrap_admin(
            username=args.username,
            display_name=args.display_name,
            password=password,
            role_code=args.role,
        )
    except AdminAuthError as exc:
        print(f"Bootstrap failed: {exc.message}")
        return 2
    print(f"Administrator created: {result.admin_id} ({result.username})")
    print("Store the following TOTP secret now; it will not be shown again:")
    print(result.totp_secret)
    print("Provisioning URI:")
    print(result.provisioning_uri)
    return 0


def main() -> None:
    args = _parser().parse_args()
    if args.command == "bootstrap":
        raise SystemExit(asyncio.run(_bootstrap(args)))
    raise SystemExit(2)


__all__ = ["main"]
