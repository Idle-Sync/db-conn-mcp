"""Command-line entry point: argparse wiring for the server and the setup wizard.

Two responsibilities, no DB knowledge:
  * ``db-conn-mcp [--transport stdio|http] [--config PATH]`` — launch the server.
  * ``db-conn-mcp setup`` — interactive wizard to register the first DB and
    (optionally) inject the server into Claude Desktop / Cursor configs.
"""

import argparse


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser (server flags + ``setup`` subcommand)."""
    parser = argparse.ArgumentParser(
        prog="db-conn-mcp",
        description="A dead-simple, self-hosted MCP server for querying databases.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport to launch (default: stdio).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Explicit path to connections.json (overrides repo/global lookup).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("setup", help="Interactive wizard to register a database.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (declared in ``pyproject.toml`` as ``db-conn-mcp``)."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
