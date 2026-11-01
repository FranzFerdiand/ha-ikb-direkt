#!/usr/bin/env python3
"""CLI utility to interact with IKB Direkt Smart Meter portal.

Usage:
  python cli.py test-login
  python cli.py meters
  python cli.py fetch --start 25.10.2026 --end 31.10.2026 --output readings.csv
  python cli.py fetch --start 25.10.2026 --end 31.10.2026 --format json

Credentials can be provided via flags (--username, --password)
or via environment variables IKB_USERNAME and IKB_PASSWORD.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime
import json
import os
import sys

from custom_components.ikb_direkt.api import (
    IKBAuthenticationError,
    IKBConnectionError,
    IKBDirektClient,
    IKBDirektError,
    MeterPoint,
)
from custom_components.ikb_direkt.const import (
    RESOLUTION_15MIN,
    RESOLUTION_DAY,
    RESOLUTION_HOUR,
    RESOLUTION_MONTH,
    RESOLUTION_WEEK,
)


def get_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Retrieve username and password from args or environment."""
    username = args.username or os.getenv("IKB_USERNAME")
    password = args.password or os.getenv("IKB_PASSWORD")

    if not username:
        username = input("IKB Direkt Username / E-Mail: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("IKB Direkt Password: ")

    return username, password


async def cmd_test_login(args: argparse.Namespace) -> int:
    """Test login to IKB Direkt."""
    username, password = get_credentials(args)
    print(f"Connecting to IKB Direkt as '{username}'...")
    client = IKBDirektClient(username, password)
    try:
        await client.login()
        print("Login SUCCESSFUL!")
        return 0
    except IKBAuthenticationError as err:
        print(f"Authentication failed: {err}", file=sys.stderr)
        return 1
    except IKBConnectionError as err:
        print(f"Connection failed: {err}", file=sys.stderr)
        return 2
    except IKBDirektError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 3
    finally:
        await client.close()


async def cmd_meters(args: argparse.Namespace) -> int:
    """List all discoverable Smart Meters on the account."""
    username, password = get_credentials(args)
    client = IKBDirektClient(username, password)
    try:
        await client.login()
        meters = await client.get_meters()
        if not meters:
            print("No smart meters found.")
            return 0

        print(f"Found {len(meters)} meter(s):")
        for i, m in enumerate(meters, 1):
            print(f"\n--- Meter #{i} ---")
            print(f"  Counting Point: {m.counting_point}")
            print(f"  Meter Number:   {m.meter_number}")
            print(f"  Type:           {m.reading_type}")
            print(f"  Period Start:   {m.period_start}")
            print(f"  Period End:     {m.period_end}")
            print(f"  Display Name:   {m.display_name}")
        return 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    finally:
        await client.close()


async def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch load profile readings for a date range."""
    username, password = get_credentials(args)
    client = IKBDirektClient(username, password)
    try:
        await client.login()
        meters = await client.get_meters()
        if not meters:
            print("Error: No meters found.", file=sys.stderr)
            return 1

        # Select meter
        selected_meter: MeterPoint | None = None
        if args.meter:
            for m in meters:
                if args.meter in m.counting_point or args.meter in m.meter_number:
                    selected_meter = m
                    break
            if not selected_meter:
                print(f"Error: Specified meter '{args.meter}' not found.", file=sys.stderr)
                return 1
        else:
            selected_meter = meters[0]

        print(f"Selected meter: {selected_meter.counting_point} ({selected_meter.meter_number})")
        print(f"Fetching from {args.start} to {args.end} (Resolution: {args.resolution})...")

        readings = await client.get_load_profile(
            meter=selected_meter,
            start_date=args.start,
            end_date=args.end,
            resolution=args.resolution,
        )

        print(f"Received {len(readings)} reading(s).")
        if not readings:
            return 0

        total_kwh = sum(r.kwh for r in readings)
        print(f"Total energy in period: {total_kwh:.3f} kWh")

        # Output formatting
        if args.output:
            if args.output.endswith(".json") or args.format == "json":
                out_data = [
                    {
                        "start_time": r.start_datetime.isoformat(),
                        "end_time": r.end_datetime.isoformat(),
                        "start_timestamp": r.start_timestamp,
                        "end_timestamp": r.end_timestamp,
                        "kwh": r.kwh,
                    }
                    for r in readings
                ]
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(out_data, f, indent=2)
                print(f"Saved JSON to {args.output}")
            else:
                with open(args.output, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=";")
                    writer.writerow(["Startzeit", "Endzeit", "Start_Timestamp", "End_Timestamp", "Verbrauch_kWh"])
                    for r in readings:
                        writer.writerow([
                            r.start_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                            r.end_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                            r.start_timestamp,
                            r.end_timestamp,
                            f"{r.kwh:.4f}".replace(".", ","),
                        ])
                print(f"Saved CSV to {args.output}")
        else:
            print("\nPreview of first 5 readings:")
            for r in readings[:5]:
                print(f"  {r.start_datetime.strftime('%Y-%m-%d %H:%M')} - {r.end_datetime.strftime('%H:%M')}: {r.kwh:.4f} kWh")
            if len(readings) > 5:
                print("  ...")
                print("Preview of last 3 readings:")
                for r in readings[-3:]:
                    print(f"  {r.start_datetime.strftime('%Y-%m-%d %H:%M')} - {r.end_datetime.strftime('%H:%M')}: {r.kwh:.4f} kWh")

        return 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    finally:
        await client.close()


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="IKB Direkt Smart Meter CLI tool (Innsbrucker Kommunalbetriebe)",
    )
    parser.add_argument("--username", "-u", help="IKB Direkt Username / E-Mail")
    parser.add_argument("--password", "-p", help="IKB Direkt Password")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # test-login
    subparsers.add_parser("test-login", help="Test credentials and login")

    # meters
    subparsers.add_parser("meters", help="List discoverable meters")

    # fetch
    fetch_parser = subparsers.add_parser("fetch", help="Fetch load profile readings")
    fetch_parser.add_argument("--start", "-s", required=True, help="Start date (DD.MM.YYYY)")
    fetch_parser.add_argument("--end", "-e", required=True, help="End date (DD.MM.YYYY)")
    fetch_parser.add_argument(
        "--resolution",
        "-r",
        default=RESOLUTION_15MIN,
        choices=[RESOLUTION_15MIN, RESOLUTION_HOUR, RESOLUTION_DAY, RESOLUTION_WEEK, RESOLUTION_MONTH],
        help="Resolution (default: ResOpt1 / 15-min)",
    )
    fetch_parser.add_argument("--meter", "-m", help="Counting point or meter number substring filter")
    fetch_parser.add_argument("--output", "-o", help="File path to save CSV or JSON")
    fetch_parser.add_argument("--format", "-f", choices=["csv", "json"], default="csv", help="Output format")

    args = parser.parse_args()

    if args.command == "test-login":
        sys.exit(asyncio.run(cmd_test_login(args)))
    elif args.command == "meters":
        sys.exit(asyncio.run(cmd_meters(args)))
    elif args.command == "fetch":
        sys.exit(asyncio.run(cmd_fetch(args)))


if __name__ == "__main__":
    main()
