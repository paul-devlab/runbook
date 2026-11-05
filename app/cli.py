"""Admin commands, run from a shell (there's no self-signup):

    python -m app.cli create-user --email you@example.com --name "Your Name"

On staging, run it inside the app's pod:

    kubectl -n runbook exec -it deploy/runbook -- \\
      python -m app.cli create-user --email you@example.com --name "Your Name"

The password is typed at a prompt (twice), never given on the command line,
where it would end up in shell history and be visible to other users via `ps`.
"""

import argparse
import getpass
import sys

from sqlalchemy.orm import Session

from app.db import get_engine
from app.security import password_problem
from app.users import UserError, create_user


def prompt_for_password() -> str:
    """Ask for a new password twice. Raises UserError if they differ or it's too weak."""
    password = getpass.getpass("Password: ")
    problem = password_problem(password)
    if problem:
        raise UserError(problem)
    if getpass.getpass("Password (again): ") != password:
        raise UserError("Passwords don't match.")
    return password


def cmd_create_user(args: argparse.Namespace) -> int:
    password = prompt_for_password()
    with Session(get_engine()) as session:
        user = create_user(session, email=args.email, display_name=args.name, password=password)
        session.commit()
        print(f"Created user {user.id}: {user.email} ({user.display_name})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Runbook admin commands")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user", help="create a user who can log in")
    create.add_argument("--email", required=True, help="login email address")
    create.add_argument("--name", required=True, help="display name, e.g. 'Paul Coulter'")
    create.set_defaults(handler=cmd_create_user)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except UserError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
