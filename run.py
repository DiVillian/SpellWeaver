#!/usr/bin/env python3
"""Start the spellweaver web UI."""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from spellweaver.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--open", action="store_true",
                    help="open the page in a browser once the server is up")
    args = ap.parse_args()
    serve(args.host, args.port, open_browser=args.open)
