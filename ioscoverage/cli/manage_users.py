"""Owner-only platform account recovery commands."""

import argparse
import getpass
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

from ioscoverage.server.deployment import (
    DEFAULT_SERVER_CONFIG_PATH,
    ServerDeploymentConfiguration,
    ServerDeploymentConfigurationError,
)
from ioscoverage.storage.accounts import AccountStore, AccountStoreError


def parse_arguments(arguments: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parses the bounded create-admin command without accepting passwords as arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create_admin = subcommands.add_parser(
        "create-admin", help="create one active administrator account"
    )
    create_admin.add_argument("--username", required=True)
    return parser.parse_args(arguments)


def run(
    arguments: Optional[Sequence[str]] = None,
    config_path: Path = DEFAULT_SERVER_CONFIG_PATH,
    password_reader: Callable[[str], str] = getpass.getpass,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Creates an administrator after two matching no-echo password prompts."""

    options = parse_arguments(arguments)
    try:
        deployment = ServerDeploymentConfiguration.load(config_path)
        store = AccountStore(deployment.runtime_paths.database)
        password = password_reader("Password: ")
        confirmation = password_reader("Confirm password: ")
        if password != confirmation:
            print("password confirmation does not match", file=stderr)
            return 2
        user = store.create_user(options.username, password, "admin")
    except (ServerDeploymentConfigurationError, AccountStoreError) as error:
        print(getattr(error, "message", str(error)), file=stderr)
        return 1
    print(
        "administrator created: {} ({})".format(user["username"], user["id"]),
        file=stdout,
    )
    return 0


def main() -> int:
    """Runs the owner-only account command with production dependencies."""

    return run()
