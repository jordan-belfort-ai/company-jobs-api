import asyncio
import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

from apify import Actor

from .ats import ATS, Client, parse_target


def _keep(row: dict, f: dict) -> bool:
    if f["titles"] and not any(k in (row["title"] or "").lower() for k in f["titles"]):
        return False
    if f["locations"]:
        hay = " ".join(str(x) for x in [row["location"], row["country"], *(row["allLocations"] or [])] if x).lower()
        if not any(k in hay for k in f["locations"]):
            return False
    if f["remote"] and not (row["remote"] or "remote" in (row["location"] or "").lower()
                            or (row["workplaceType"] or "").lower() == "remote"):
        return False
    if (f["since"] and row.get("_olderThanDays") and row["publishedAt"] is None
            and datetime.now(timezone.utc) - timedelta(days=row["_olderThanDays"]) < f["since"]):
        return False  # Workday only says '30+ days ago' and gives no date
    if f["since"] and row["publishedAt"]:
        try:
            ts = datetime.fromisoformat(re.sub(r"Z$", "+00:00", row["publishedAt"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < f["since"]:
                return False
        except ValueError:
            pass
    return True


_DIR_FILE = Path(__file__).with_name("workday_directory.json")
_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|llc|ltd|limited|plc|gmbh|ag|sa|group|holdings|the)\b")


def _norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"[^a-z0-9]", "", s)


_index: dict | None = None


def workday_board(raw: str) -> dict | None:
    """Bare company name -> its Workday board from the directory (exact name, tenant id or alias only:
    a fuzzy hit could shadow the same company's Greenhouse or Lever board)."""
    global _index
    if _index is None:
        _index = {}
        try:
            entries = json.loads(_DIR_FILE.read_text("utf8"))
        except (OSError, ValueError):
            entries = []
        for e in entries:
            for k in {_norm(e["n"]), _norm(e["t"]), *(_norm(a) for a in e.get("a", []))}:
                if k:
                    _index.setdefault(k, e)
    key = _norm(raw)
    return _index.get(key) if key else None


async def resolve(client: Client, raw: str, only: list[str]) -> tuple[list[dict], str]:
    ats, slugs = parse_target(raw)
    entry = workday_board(raw) if not ats and (not only or "workday" in only) else None
    if entry:
        ats, slugs = parse_target(entry["u"])
    if ats:
        rows = await client.fetch(ats, slugs[0])
        for row in (rows if entry else None) or []:
            row["company"] = entry["n"]  # the board only knows its tenant id (mtb, not M&T Bank)
        return (rows or []), ("" if rows is not None else f"No public {ats} job board for '{slugs[0]}'")
    systems = only or ATS
    for slug in slugs:
        found = await asyncio.gather(*(client.fetch(a, slug) for a in systems), return_exceptions=True)
        hits = [r for r in found if isinstance(r, list) and r]
        # A bare name can collide with another company's slug on some ATS: when several boards
        # answer, keep the ones whose company name matches the slug.
        named = [h for h in hits if slug in re.sub(r"[^a-z0-9]", "", (h[0]["company"] or "").lower())]
        hits = named or hits[:1]
        if hits:
            return [row for h in hits for row in h], ""
    return [], f"No job board with open jobs found for '{raw}' on {', '.join(systems)} (tried slugs: {', '.join(slugs)}). Paste the careers page URL instead."


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        targets = [t.strip() for t in inp.get("companies", []) if isinstance(t, str) and t.strip()]
        if not targets:
            await Actor.fail(status_message="Give at least one company: a careers page URL, a domain or a name.")
            return
        days = inp.get("postedWithinDays") or 0
        f = {"titles": [k.lower() for k in inp.get("titleKeywords") or [] if k.strip()],
             "locations": [k.lower() for k in inp.get("locationKeywords") or [] if k.strip()],
             "remote": inp.get("remoteOnly", False),
             "since": datetime.now(timezone.utc) - timedelta(days=days) if days else None}
        per_company = inp.get("maxJobsPerCompany") or 0
        only_new = inp.get("onlyNewJobs", False)
        monitor = re.sub(r"[^a-zA-Z0-9-]", "-", inp.get("monitorName") or "default")[:40]
        only = [a for a in inp.get("atsSystems") or [] if a in ATS]
        seen_store = await Actor.open_key_value_store(name="ats-jobs-seen") if only_new else None
        client = Client(descriptions=inp.get("includeDescription", True))
        if per_company and not (f["titles"] or f["locations"] or f["remote"] or f["since"]):
            client.wd_limit = per_company  # rows come out in page order, so the first pages already hold the kept jobs
        stats = {"companies": len(targets), "found": 0, "jobs": 0, "filtered": 0, "alreadySeen": 0, "notFound": []}
        sem = asyncio.Semaphore(4)

        async def one(t: str) -> None:
            async with sem:
                try:
                    rows, err = await resolve(client, t, only)
                except Exception as e:  # one bad board never sinks the run
                    rows, err = [], f"{type(e).__name__}: {e}"
            if err:
                stats["notFound"].append(t)
                Actor.log.warning(err)
                await Actor.push_data({"input": t, "error": err})  # no charge event
                return
            stats["found"] += 1
            if f["locations"]:  # Workday lists multi-location postings as '3 Locations': get the real ones first
                await client.workday_details([r for r in rows if r["ats"] == "workday" and (r["location"] or "").endswith("Locations")
                                              and (not f["titles"] or any(k in (r["title"] or "").lower() for k in f["titles"]))])
            kept = [r for r in rows if _keep(r, f)]
            stats["filtered"] += len(rows) - len(kept)
            if per_company:
                kept = kept[:per_company]
            if seen_store:
                by_board: dict = {}
                for r in kept:
                    by_board.setdefault(f"{r['ats']}-{r['boardSlug']}".lower(), []).append(r)
                fresh = []
                for key, rs in by_board.items():
                    key = re.sub(r"[^a-zA-Z0-9!\-_.'()]", "_", f"{monitor}-{key}")[:250]
                    old = set(await seen_store.get_value(key) or [])
                    fresh += [r for r in rs if r["jobId"] not in old]
                    await seen_store.set_value(key, sorted(old | {r["jobId"] for r in rs}))
                stats["alreadySeen"] += len(kept) - len(fresh)
                kept = fresh
            await client.enrich(kept)
            for r in kept:
                r["input"] = t
                r.pop("_olderThanDays", None)
                r.pop("_detailed", None)
                r["scrapedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for i in range(0, len(kept), 500):
                await Actor.push_data(kept[i:i + 500], charged_event_name="job")
            stats["jobs"] += len(kept)
            await Actor.set_status_message(f"{stats['jobs']} jobs from {stats['found']} of {len(targets)} companies")

        try:
            await asyncio.gather(*(one(t) for t in targets))
        finally:
            await client.close()
        await Actor.set_value("STATS", stats)
        await Actor.set_status_message(
            f"Done: {stats['jobs']} jobs from {stats['found']} of {len(targets)} companies"
            + (f", {stats['alreadySeen']} already seen" if only_new else "")
            + (f"; not found: {', '.join(stats['notFound'][:5])}" if stats["notFound"] else ""), is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
