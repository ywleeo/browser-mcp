"""Console entry point for the Browser MCP server and upgrade workflow."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from browser_mcp.config import AppSettings
from browser_mcp.logging_config import configure_logging
from browser_mcp.mcp.server import create_server
from browser_mcp.process_lifecycle import start_owner_watchdog
from browser_mcp.upgrade import (
    UpgradeError,
    apply_upgrade,
    check_upgrade,
    error_report,
    render_upgrade_report,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the stable CLI shared by humans and upgrade-capable Agents."""
    parser = argparse.ArgumentParser(prog="browser-mcp")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("serve", help="run the MCP server over stdio")
    upgrade = subcommands.add_parser("upgrade", help="check or apply a safe source update")
    mode = upgrade.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        dest="upgrade_mode",
        action="store_const",
        const="check",
        help="fetch upstream metadata and report update safety",
    )
    mode.add_argument(
        "--apply",
        dest="upgrade_mode",
        action="store_const",
        const="apply",
        help="fast-forward a clean checkout and sync locked dependencies",
    )
    upgrade.add_argument("--json", action="store_true", help="emit a machine-readable result")
    upgrade.set_defaults(upgrade_mode="check")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch upgrade commands or run the default MCP stdio server."""
    args = build_parser().parse_args(argv)
    if args.command == "upgrade":
        action = args.upgrade_mode
        try:
            report = apply_upgrade() if action == "apply" else check_upgrade()
        except UpgradeError as error:
            report = error_report(action, error)
        print(render_upgrade_report(report, as_json=args.json))
        if not report.ok:
            raise SystemExit(2)
        return

    settings = AppSettings.from_env()
    configure_logging(settings.log_level)
    start_owner_watchdog()
    create_server(settings).run(transport="stdio")
