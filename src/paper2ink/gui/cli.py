"""``paper2ink-gui`` - start the local web UI."""

from __future__ import annotations

import argparse
import logging
import sys


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="paper2ink-gui",
        description="Start the paper2ink web UI on this machine.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: localhost only)")
    parser.add_argument("--port", type=int, default=8731,
                        help="port to listen on (default: 8731)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        from .server import run as serve
    except ImportError as exc:  # pragma: no cover - depends on the install
        print(
            "The web UI needs its extra dependencies:\n"
            '    pip install "paper2ink[gui]"\n'
            f"({exc})",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Warning: binding to {args.host} exposes the UI to your network. "
            "It has no authentication.",
            file=sys.stderr,
        )

    try:
        serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    except KeyboardInterrupt:  # pragma: no cover
        print("\nstopped")


if __name__ == "__main__":  # pragma: no cover
    run()
