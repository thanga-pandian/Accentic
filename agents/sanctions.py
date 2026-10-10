"""
Official sanctions data: the UN Security Council Consolidated List.

RBI KYC Directions para 56 require the bank to hold no account for anyone on the
UNSC ISIL (Da'esh) & Al-Qaida and Taliban lists, and para 58 to verify the UNSCR
1718 list every day. This module syncs the list from the UN's own XML feed, keeps a
fingerprinted snapshot of every sync, and records which designations were added or
removed since the last one. It never edits the list: what is stored is what the UN
published, identified by the UN's own generation timestamp and the file's SHA-256.

Usage:
    python -m agents.sanctions          # sync now (schedule daily, as para 58 requires)
"""

import hashlib
import json
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from db.connection import get_connection

SOURCE_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

# UN_LIST_TYPE in the feed -> the list as RBI names it, and the paragraph requiring it.
REGIMES = {
    "Al-Qaida": ("ISIL (Da'esh) and Al-Qaida Sanctions List", "56(1)(i)"),
    "Taliban": ("Taliban Sanctions List", "56(1)(ii)"),
    "DPRK": ("UNSCR 1718 Sanctions List", "58"),
}
OTHER = ("Other UNSC sanctions lists", "59(1)")
MAX_AGE_HOURS = 24


def _name(node) -> str:
    parts = (node.findtext(tag) for tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME"))
    return " ".join(p.strip() for p in parts if p and p.strip())


def parse(xml_bytes: bytes) -> tuple[str, list[dict]]:
    """Return (dateGenerated, entries). Raises ValueError if this is not the UN list."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"not XML: {exc}") from exc
    if root.tag != "CONSOLIDATED_LIST" or not root.get("dateGenerated"):
        raise ValueError(f"not the UN consolidated list (root element {root.tag!r})")
    entries = []
    for group, item, kind, alias in (("INDIVIDUALS", "INDIVIDUAL", "individual", "INDIVIDUAL_ALIAS"),
                                     ("ENTITIES", "ENTITY", "entity", "ENTITY_ALIAS")):
        for node in root.find(group).findall(item):
            entries.append({
                "reference_number": (node.findtext("REFERENCE_NUMBER") or "").strip(),
                "kind": kind,
                "name": _name(node),
                "list_type": (node.findtext("UN_LIST_TYPE") or "").strip(),
                "listed_on": (node.findtext("LISTED_ON") or "").strip(),
                "aliases": sorted({a.findtext("ALIAS_NAME").strip() for a in node.findall(alias)
                                   if (a.findtext("ALIAS_NAME") or "").strip()}),
            })
    return root.get("dateGenerated"), entries


def regime(list_type: str) -> tuple[str, str]:
    return REGIMES.get(list_type, OTHER)


def _fetch() -> bytes:
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Accentic compliance sync"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _after(latest) -> datetime:
    """Now in UTC, strictly after the latest stored sync. Snapshots are ordered by this time,
    and a coarse clock (Windows before Python 3.13) can return the same value twice."""
    now = datetime.now(timezone.utc)
    if latest is not None:
        last = latest if isinstance(latest, datetime) else datetime.fromisoformat(str(latest))
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        if now <= last:
            now = last + timedelta(microseconds=1)
    return now


def sync(conn, xml_bytes: bytes | None = None) -> dict:
    """Download (or accept) the list, diff it against the stored one, and record a snapshot."""
    xml_bytes = xml_bytes if xml_bytes is not None else _fetch()
    generated, entries = parse(xml_bytes)
    sha = hashlib.sha256(xml_bytes).hexdigest()

    with conn.cursor() as cur:
        cur.execute("SELECT reference_number, name, list_type FROM sanctions_entry")
        before = {ref: (name, lt) for ref, name, lt in cur.fetchall()}
        cur.execute("SELECT COUNT(*), MAX(retrieved_at) FROM sanctions_snapshot")
        count, latest = cur.fetchone()
        first = count == 0
    now = {e["reference_number"]: (e["name"], e["list_type"]) for e in entries}
    added = [] if first else sorted(set(now) - set(before))
    removed = [] if first else sorted(set(before) - set(now))
    changes = {"added": [{"ref": r, "name": now[r][0], "list": regime(now[r][1])[0]} for r in added],
               "removed": [{"ref": r, "name": before[r][0], "list": regime(before[r][1])[0]}
                           for r in removed]}

    snap_id = str(uuid.uuid4())
    retrieved = _after(latest).strftime("%Y-%m-%d %H:%M:%S.%f")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sanctions_entry")
        for e in entries:
            cur.execute(
                """
                INSERT INTO sanctions_entry
                    (id, reference_number, kind, name, list_type, listed_on, aliases)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (str(uuid.uuid4()), e["reference_number"], e["kind"], e["name"],
                 e["list_type"], e["listed_on"], json.dumps(e["aliases"])),
            )
        cur.execute(
            """
            INSERT INTO sanctions_snapshot
                (id, source_url, date_generated, retrieved_at, sha256, individuals, entities,
                 added, removed, changes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (snap_id, SOURCE_URL, generated, retrieved, sha,
             sum(e["kind"] == "individual" for e in entries),
             sum(e["kind"] == "entity" for e in entries),
             len(added), len(removed), json.dumps(changes)),
        )
    conn.commit()
    return {"date_generated": generated, "retrieved_at": retrieved, "sha256": sha,
            "designations": len(entries), "added": len(added), "removed": len(removed),
            "first_sync": first}


def status(conn) -> dict | None:
    """Latest snapshot with per-regime counts, or None if the list was never synced."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_url, date_generated, retrieved_at, sha256, individuals, entities,
                   added, removed, changes
            FROM sanctions_snapshot ORDER BY retrieved_at DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("SELECT list_type, kind, COUNT(*) FROM sanctions_entry GROUP BY list_type, kind")
        counts = cur.fetchall()
    keys = ("source_url", "date_generated", "retrieved_at", "sha256", "individuals",
            "entities", "added", "removed", "changes")
    snap = dict(zip(keys, row))
    snap["changes"] = json.loads(snap["changes"] or "{}")
    retrieved = datetime.fromisoformat(str(snap["retrieved_at"])).replace(tzinfo=timezone.utc)
    snap["age_hours"] = round((datetime.now(timezone.utc) - retrieved).total_seconds() / 3600, 1)
    snap["stale"] = snap["age_hours"] > MAX_AGE_HOURS

    regimes = {}
    for list_type, kind, n in counts:
        label, para = regime(list_type)
        r = regimes.setdefault(label, {"list": label, "rbi_para": para, "individuals": 0,
                                       "entities": 0, "un_list_types": []})
        r["individuals" if kind == "individual" else "entities"] += n
        if list_type not in r["un_list_types"]:
            r["un_list_types"].append(list_type)
    order = [v[0] for v in REGIMES.values()] + [OTHER[0]]
    snap["regimes"] = sorted(regimes.values(), key=lambda r: order.index(r["list"]))
    return snap


if __name__ == "__main__":
    conn = get_connection()
    try:
        print(json.dumps(sync(conn), indent=1))
    finally:
        conn.close()
