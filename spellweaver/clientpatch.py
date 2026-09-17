"""Building the client patch that makes a spell visible.

The server already knows everything about a custom spell; the client knows
nothing. Until it does, the spell has no name, no icon and no description, it
cannot be dragged to an action bar, and nothing it does reaches the combat log,
because the client drops events for a spell it cannot look up.

This builds the archive that fixes that. Two files go into it:

* `Spell.dbc`, the winning copy plus our own rows.
* `SkillLineAbility.dbc`, likewise, carrying the class each spell belongs to so
  it lands in the right tab of the spellbook.

Building and applying are one step. A patch that has been built but not put in
the client changes nothing a player can see, and a status that reports on the
built file rather than the installed one is worse than no status at all: it says
"up to date" while the client has never heard of the ability. Everything here
describes the archive *in the client*, which is the only copy that matters.

The client replaces whole files rather than merging them, so the patch must
carry every row that should exist - all of the base, then ours. That makes the
base a dependency: if the copy we merged from is replaced, our patch is built on
something that is no longer there, and it would silently undo whatever replaced
it. Every build records what it merged from, and `status()` re-resolves and says
so rather than letting a stale patch ship quietly.
"""
import hashlib
import json
import os
import pathlib
import struct
import time

from . import classes, client, dbc, mpq, ranks as ranks_module
from . import store as store_module
from .spell_layout import FIELDS, FLOAT_FIELDS, INDEX

MANIFEST = "spellweaver.json"
SPELL_FIELDS = len(FIELDS)                   # 234
ABILITY_FIELDS = len(classes.COLUMNS)        # 14


class PatchError(Exception):
    pass


def _writable(target):
    """Fail early and in plain words rather than half-way through a write."""
    path = pathlib.Path(target)
    if not path.parent.is_dir():
        raise PatchError(
            "There is no folder at %s to write the patch into. Check the client "
            "path." % path.parent)
    try:
        if path.exists():
            # Opening for append touches the same lock the game holds.
            with open(path, "r+b"):
                pass
        else:
            with open(path, "ab"):
                pass
            path.unlink()
    except PermissionError:
        raise PatchError(
            "Cannot write %s. World of Warcraft locks its archives while it is "
            "running - close the game and try again. If it is already closed, "
            "check the folder is not read-only." % path)
    except OSError as exc:
        raise PatchError("Cannot write %s: %s" % (path, exc))


def _fingerprint(blob):
    head = {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
    if len(blob) >= 12 and blob[:4] == b"WDBC":
        head["records"], head["fields"] = struct.unpack_from("<II", blob, 4)
    return head


def _check_shape(name, parsed, expected, source):
    if parsed.field_count != expected:
        raise PatchError(
            "%s in %s has %d fields, not the %d this client build should have. "
            "Merging into it would write a file the client cannot read."
            % (name, source, parsed.field_count, expected))


def _spell_rows(db, band):
    """Our spells, as they sit in spell_dbc, ready to become DBC records."""
    _, rows = db.query(
        "SELECT %s FROM spell_dbc WHERE ID BETWEEN %d AND %d ORDER BY ID"
        % (", ".join("`%s`" % c for c in FIELDS), band.start, band.end))
    return [dict(zip(FIELDS, row)) for row in rows]


def _merge_spells(base_blob, spells, source):
    """Base Spell.dbc + our rows, with names appended to the string block."""
    base = dbc.Dbc.parse(base_blob, source)
    _check_shape("Spell.dbc", base, SPELL_FIELDS, source)

    strings = bytearray(base.string_block)
    interned = {}

    def intern(text):
        if not text:
            return 0
        if text in interned:
            return interned[text]
        offset = len(strings)
        strings.extend(text.encode("utf-8") + b"\0")
        interned[text] = offset
        return offset

    records = {r[0]: r for r in base.records}
    added = []
    for row in spells:
        values = []
        for column in FIELDS:
            value = row[column]
            if "_Lang_" in column and not column.endswith("_Mask"):
                values.append(intern(value if column.endswith("enUS") else ""))
            elif column in FLOAT_FIELDS:
                values.append(struct.unpack("<i", struct.pack("<f", float(value)))[0])
            else:
                values.append(int(value))
        records[int(row["ID"])] = tuple(values)
        added.append(int(row["ID"]))
    ordered = [records[key] for key in sorted(records)]
    return ordered, bytes(strings), added


def _merge_abilities(base_blob, rows, source):
    """Base SkillLineAbility.dbc + one row per class-restricted spell."""
    base = dbc.Dbc.parse(base_blob, source)
    _check_shape("SkillLineAbility.dbc", base, ABILITY_FIELDS, source)
    records = {r[0]: r for r in base.records}
    for row in rows:
        records[int(row[0])] = tuple(int(v) for v in row)
    return [records[key] for key in sorted(records)], base.string_block


def _write_dbc(rows, string_block):
    """A DBC as bytes, without going through a file on the way."""
    import io
    fields = len(rows[0])
    buf = io.BytesIO()
    buf.write(struct.pack("<4sIIII", b"WDBC", len(rows), fields, fields * 4, len(string_block)))
    packer = struct.Struct("<%di" % fields)
    for row in rows:
        buf.write(packer.pack(*row))
    buf.write(string_block)
    return buf.getvalue()


def installed(client_dir, locale=None):
    """Every patch of ours in this client, with what each says about itself.

    A patch carries its own manifest, so it can be recognised wherever it sits
    and whatever it has been renamed to. That is what lets a rebuild keep its
    own output out of the base, and what lets `status` describe the archive the
    client is actually loading rather than a file left in a staging folder.
    """
    ordered, _locale = client.chain(client_dir, locale)
    found = []
    for path, _tier, rank in ordered:
        try:
            with mpq.Archive(path) as archive:
                if not archive.has(MANIFEST):
                    continue
                try:
                    record = json.loads(archive.read(MANIFEST).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, mpq.MpqError):
                    record = {}
        except (mpq.MpqError, OSError):
            continue
        stat = pathlib.Path(path).stat()
        found.append({"path": path, "rank": rank, "manifest": record,
                      "written_at": int(stat.st_mtime), "size": stat.st_size})
    return found


def our_archives(client_dir, locale=None):
    return [entry["path"] for entry in installed(client_dir, locale)]


def _previous_patch(settings, client_dir):
    """The patch this client was given last time, if there was one."""
    path = manifest_path(settings)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
    except ValueError:
        return None
    if os.path.normcase(str(record.get("client", ""))) != os.path.normcase(str(client_dir)):
        return None
    return record.get("patch")


def manifest_path(settings):
    return pathlib.Path(settings.client_manifest)


def build(db, settings, client_dir=None, locale=None, out_path=None, exclude=()):
    """Build the patch, and record what it was built from."""
    # The same fallback `status` uses, so building and reporting never disagree
    # about which client they mean.
    client_dir = client_dir or settings.client_dir or _remembered_client(settings)
    if not client_dir:
        raise PatchError(
            "No client set. Enter the client folder on the Settings tab.")

    target = out_path or settings.client_patch
    if not target:
        # Straight into the client. Building somewhere else and leaving the copy
        # to the user is how a patch ends up built but not applied.
        here = installed(client_dir, locale)
        if here:
            # Rebuild in place: a new slot each time would leave the old patch
            # behind, still loaded and now wrong.
            target = here[-1]["path"]
        else:
            target, locale = client.free_slot(client_dir, locale)
    target = str(target)

    spells = _spell_rows(db, settings.band)
    if not spells:
        raise PatchError("There are no spells in the reserved band to ship.")
    abilities = classes.rows_in_band(db, settings.band)

    # No patch of ours may become its own base, wherever it happens to sit.
    # `exclude` covers archives we cannot recognise - one built by hand, or by a
    # version of this tool that did not yet stamp its patches.
    exclude = [target] + list(exclude) + our_archives(client_dir, locale)
    spell_winner, spell_blob, spell_chain = client.resolve(
        client_dir, client.SPELL_DBC, locale, exclude=exclude)
    ability_winner, ability_blob, ability_chain = client.resolve(
        client_dir, client.SKILL_LINE_ABILITY_DBC, locale, exclude=exclude)

    rows, strings, added = _merge_spells(spell_blob, spells, spell_winner.name)
    spell_dbc = _write_dbc(rows, strings)
    ability_rows, ability_strings = _merge_abilities(
        ability_blob, abilities, ability_winner.name)
    ability_dbc = _write_dbc(ability_rows, ability_strings)

    record = {
        "built_at": int(time.time()),
        "client": str(client_dir),
        "locale": locale or client.locales(client_dir)[0],
        "patch": target,
        "spells": added,
        "abilities": [r[0] for r in abilities],
        "bases": {
            client.SPELL_DBC: dict(_fingerprint(spell_blob), archive=spell_winner.path),
            client.SKILL_LINE_ABILITY_DBC: dict(_fingerprint(ability_blob),
                                                archive=ability_winner.path),
        },
    }

    _writable(target)
    mpq.write(target, {
        client.SPELL_DBC: spell_dbc,
        client.SKILL_LINE_ABILITY_DBC: ability_dbc,
        # The archive says what it was built from, so a patch that has travelled
        # to another machine can still be identified.
        MANIFEST: json.dumps(record, indent=2).encode("utf-8"),
    })
    manifest_path(settings).parent.mkdir(parents=True, exist_ok=True)
    manifest_path(settings).write_text(json.dumps(record, indent=2))

    record["size"] = pathlib.Path(target).stat().st_size
    record["chain"] = {
        client.SPELL_DBC: [{"archive": c.name, "tier": c.tier, "records": c.records,
                            "fields": c.fields, "winner": c is spell_winner}
                           for c in spell_chain],
        client.SKILL_LINE_ABILITY_DBC: [
            {"archive": c.name, "tier": c.tier, "records": c.records,
             "fields": c.fields, "winner": c is ability_winner}
            for c in ability_chain],
    }
    record["spell_records"] = len(rows)
    record["ability_records"] = len(ability_rows)
    return record


def status(db, settings, client_dir=None):
    """What the client actually has, which is the only thing worth reporting.

    Read from the archive in the client rather than from anything this tool
    wrote down, so a patch that was built but never applied reads as missing
    rather than as up to date.
    """
    client_dir = client_dir or settings.client_dir or _remembered_client(settings)
    out = {"client": client_dir, "installed": False, "problems": [], "ok": False,
           "spells": [], "band": [], "patch": None}
    if not client_dir:
        out["problems"].append(
            "No client set. Point at the folder holding Wow.exe and Data.")
        return out

    try:
        here = installed(client_dir)
    except client.ClientError as exc:
        out["problems"].append(str(exc))
        return out

    current = sorted(int(r["ID"]) for r in _spell_rows(db, settings.band))

    if not here:
        out["problems"].append(
            "The client has no spellweaver patch. Nothing you have made is "
            "visible in game yet.")
        return out

    patch = here[-1]                                  # the one that wins
    record = patch["manifest"]
    shipped = sorted(record.get("spells", []))
    names = _names(db, set(shipped) | set(current))
    out.update(installed=True, patch=patch["path"], written_at=patch["written_at"],
               size=patch["size"], spells=_label(shipped, names),
               band=_label(current, names))

    if len(here) > 1:
        out["problems"].append(
            "%d spellweaver patches are in this client (%s). Only %s is read; "
            "delete the others."
            % (len(here), ", ".join(os.path.basename(e["path"]) for e in here),
               os.path.basename(patch["path"])))

    missing = sorted(set(current) - set(shipped))
    removed = sorted(set(shipped) - set(current))
    if missing:
        out["problems"].append(
            "Not in the patch yet: %s. Build and apply again."
            % _spoken(missing[:5], names))
    if removed:
        out["problems"].append(
            "The patch still carries %s, which no longer exists."
            % _spoken(removed[:5], names))

    for internal, base in (record.get("bases") or {}).items():
        try:
            winner, blob, _chain = client.resolve(
                client_dir, internal, record.get("locale"),
                exclude=[e["path"] for e in here])
        except client.ClientError as exc:
            out["problems"].append(str(exc))
            continue
        if _fingerprint(blob)["sha256"] != base.get("sha256"):
            out["problems"].append(
                "%s now comes from %s, not the copy this patch was built on. "
                "Build and apply again, or the patch will undo it."
                % (internal.rsplit("\\", 1)[-1], winner.name))

    out["ok"] = not out["problems"]
    return out


def _names(db, ids):
    """What each ability is called, so a report can say more than a number."""
    if not ids:
        return {}
    wanted = ", ".join(str(int(i)) for i in ids)
    _, rows = db.query(
        "SELECT spell_id, name FROM %s WHERE spell_id IN (%s)"
        % (store_module.TABLE, wanted))
    names = {int(r[0]): r[1] for r in rows}
    # Every rank past the first is a spell in its own right and has no row of
    # its own in the ability table, so it used to appear in the report as a
    # bare id. It takes its name from the ability it is a rank of.
    #
    # Only where there is more than one rank. The tool keeps a rank row even for
    # an ability that has just the one, to account for the id, and calling that
    # "Rank 1" would say something the game does not: an unranked spell has no
    # rank under its name.
    _, rows = db.query(
        "SELECT r.rank_spell_id, r.`rank`, s.name FROM %s r "
        "JOIN %s s ON s.spell_id = r.spell_id "
        "JOIN (SELECT spell_id, COUNT(*) AS n FROM %s GROUP BY spell_id) c "
        "  ON c.spell_id = r.spell_id "
        "WHERE c.n > 1 AND r.rank_spell_id IN (%s)"
        % (ranks_module.TABLE, store_module.TABLE, ranks_module.TABLE, wanted))
    for rank_spell_id, rank, name in rows:
        names[int(rank_spell_id)] = "%s Rank %d" % (name, int(rank))
    return names


def _label(ids, names):
    return [{"id": i, "name": names.get(i, "")} for i in ids]


def _spoken(ids, names):
    return ", ".join(names.get(i) or str(i) for i in ids)


def _remembered_client(settings):
    """The client the last build used, when none is configured."""
    path = manifest_path(settings)
    if not path.is_file():
        return ""
    try:
        return json.loads(path.read_text()).get("client", "")
    except (OSError, ValueError):
        return ""
