#!/usr/bin/env python3
"""Get information about the FranklinHW installation."""

import argparse
import asyncio
import logging
import os
import shlex
import sys

from franklinwh import Client, TokenFetcher, WorkMode
import jsonpickle

modes: dict[str, WorkMode] = {
    "tou": WorkMode.TIME_OF_USE,
    "self": WorkMode.SELF_CONSUMPTION,
    "emergency": WorkMode.EMERGENCY_BACKUP,
    "backup": WorkMode.EMERGENCY_BACKUP,
    "vpp": WorkMode.VPP_MODE,
}
for v in WorkMode:
    modes[v.title] = v
    modes[v.title.lower()] = v
    modes[str(v.id)] = v


def mode(value: str) -> str:
    """Convert a modes value to a proper title."""
    return modes.get(value).title


class ranged[cls: type]:
    """An argparse type converter bounded by [min, max)."""

    def __init__(self, min: cls, max: cls) -> None:
        """."""
        self.min = min
        self.max = max

    def __call__(self):
        """Create the converter."""

        def check(value: str) -> cls:
            """Convert and check the value."""
            try:
                # TODO: would prefer 'cls(value)' but type erasure requires this hack.
                #       hopefully Python generics formalize a better way.
                value = self.__orig_class__.__args__[0](value)
            except ValueError as err:
                raise argparse.ArgumentTypeError(
                    f"{value} must convert to {cls.__name__}"
                ) from err
            if value < self.min or value >= self.max:
                raise argparse.ArgumentTypeError(
                    f"'{value}' must be within [{self.min}, {self.max})"
                )
            return value

        return check


async def main():
    """Do all the work."""
    parser = argparse.ArgumentParser(description="Get FranklinWH installation info.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "username",
        type=str,
        help="The username for the installation.",
    )
    parser.add_argument(
        "password",
        type=str,
        help="The password for the installation.",
    )
    parser.add_argument(
        "gateway",
        type=str,
        help="The gateway / serial number to query.",
    )
    parser.add_argument(
        "--mode",
        choices=modes.keys(),
        type=mode,
        help="The mode to set.",
    )
    parser.add_argument(
        "--reserve",
        type=ranged[int](5, 101)(),
        help="The reserve percentage to set.",
    )

    if "USED_VSCODE_COMMAND_PICKARGS" in os.environ:
        sys.argv = shlex.split(" ".join(sys.argv))
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig()
        logging.getLogger("franklinwh").setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)

    fetcher = TokenFetcher(args.username, args.password)
    client = Client(fetcher, args.gateway)
    if args.mode is not None:
        await client.set_mode(await client.get_mode_by_name(args.mode))
    if args.reserve is not None:
        await client.set_backup_reserve(args.reserve)

    print(  # noqa: T201
        jsonpickle.dumps(await client.get_mode(), indent=2, unpicklable=False)
    )

    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
