#!/usr/bin/env python3
"""Build the client patch into the client, or report what the client has.

The patch is what makes a custom ability visible: without it the client has no
name, no icon and no combat log for anything in the reserved band. Building it
writes it straight into the client's Data folder, because a patch that has been
built but not applied changes nothing.

    python3 tools/build_client_patch.py --client /path/to/WoW
    python3 tools/build_client_patch.py --status
"""
import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from spellweaver import client, clientpatch
from spellweaver.config import load_settings, require_connection
from spellweaver.mysql import connect


def show_chain(title, entries):
    print("  %s" % title)
    for entry in entries:
        print("    %-24s %-16s %7s records %3s fields%s"
              % (entry["archive"], entry["tier"], entry["records"], entry["fields"],
                 "   <-- merged into" if entry["winner"] else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", help="the folder holding Wow.exe and Data")
    ap.add_argument("--locale", help="which locale folder to use, e.g. enUS")
    ap.add_argument("--out", help="write it here instead of into the client")
    ap.add_argument("--status", action="store_true", help="report, do not build")
    ap.add_argument("--exclude", action="append", default=[],
                    help="an archive to keep out of the base; repeatable")
    args = ap.parse_args()

    settings = require_connection(load_settings())
    with connect(settings.db) as db:
        if args.status:
            state = clientpatch.status(db, settings, args.client)
            print("client:    %s" % (state["client"] or "not set"))
            if state["installed"]:
                print("patch:     %s" % state["patch"])
                print("written:   %s" % time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(state["written_at"])))
                print("abilities: %s" % ", ".join(
                    "%s (%d)" % (a["name"] or "unnamed", a["id"])
                    for a in state["spells"]) or "none")
            else:
                print("patch:     not installed")
            for line in state["problems"]:
                print("  PROBLEM: %s" % line)
            if state["ok"]:
                print("  the client is up to date")
            return 0 if state["ok"] else 1

        record = clientpatch.build(db, settings, client_dir=args.client,
                                   locale=args.locale, out_path=args.out,
                                   exclude=args.exclude)

    print("Resolved what this client actually reads:")
    show_chain("Spell.dbc", record["chain"][client.SPELL_DBC])
    show_chain("SkillLineAbility.dbc", record["chain"][client.SKILL_LINE_ABILITY_DBC])
    print()
    print("Applied to %s" % record["patch"])
    print("  %.1f MB, %d spell records (%d ours), %d ability rows (%d ours)"
          % (record["size"] / 1e6, record["spell_records"], len(record["spells"]),
             record["ability_records"], len(record["abilities"])))
    print()
    print("Restart the client to pick it up. The worldserver needs a restart too if")
    print("any ability changed, because spell_dbc is merged at startup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
