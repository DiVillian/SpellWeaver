#!/usr/bin/env python3
"""Pull the client data files this tool reads out of a WoW client.

    python3 tools/extract_dbc.py --client /path/to/WoW

Run once after cloning. The files land in data/dbc/ and are not tracked: they
are Blizzard's, and they come from your own client.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from spellweaver import clientdata
from spellweaver.config import load_settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", help="the folder holding Wow.exe and Data")
    ap.add_argument("--locale", help="which locale folder to use, e.g. enUS")
    ap.add_argument("--into", default=str(clientdata.DATA))
    args = ap.parse_args()

    where = args.client or load_settings().client_dir
    if not where:
        raise SystemExit(
            "No client given. Pass --client, or set one on the Settings tab first.")

    try:
        written, locale = clientdata.extract(where, args.into, args.locale)
    except clientdata.DataError as exc:
        raise SystemExit(str(exc))

    print("From %s (%s):" % (where, locale))
    for row in written:
        print("  %-24s %-22s %8.1f KB"
              % (row["name"], row["archive"], row["bytes"] / 1024.0))
    print()
    print("%d files in %s." % (len(written), args.into))
    return 0


if __name__ == "__main__":
    sys.exit(main())
