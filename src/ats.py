"""Jobs from the public job-board APIs that ATS vendors publish for career pages.

Every fetcher returns normalized rows, or None when the company has no board on that ATS.
"""
import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

ATS = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "personio"]

# Career-page URL patterns -> (ats, slug)
URL_PATTERNS = [
    (r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w.-]+)", "greenhouse"),
    (r"boards-api\.greenhouse\.io/v1/boards/([\w.-]+)", "greenhouse"),
    (r"jobs(?:\.eu)?\.lever\.co/([\w.-]+)", "lever"),
    (r"jobs\.ashbyhq\.com/([\w.%-]+)", "ashby"),
    (r"apply\.workable\.com/([\w.-]+)", "workable"),
    (r"([\w-]+)\.workable\.com", "workable"),
    (r"(?:careers|jobs)\.smartrecruiters\.com/([\w.-]+)", "smartrecruiters"),
    (r"([\w-]+)\.recruitee\.com", "recruitee"),
    (r"([\w-]+)\.jobs\.personio\.(?:de|com)", "personio"),
]


def parse_target(raw: str) -> tuple[str | None, list[str]]:
    """'https://jobs.lever.co/spotify' -> ('lever', ['spotify']); 'Acme Corp' -> (None, candidates)."""
    s = raw.strip()
    for pat, ats in URL_PATTERNS:
        m = re.search(pat, s, re.I)
        if m and m.group(1).lower() not in {"www", "apply", "jobs", "careers", "api"}:
            return ats, [m.group(1)]
    host = urlparse(s if "://" in s else "https://" + s).hostname or s
    if "." in host and " " not in s:
        parts = host.lower().removeprefix("www.").split(".")
        base = parts[-2] if len(parts) >= 2 else parts[0]
        names = [base]
    else:
        names = [s]
    cands: list[str] = []
    for n in names:
        low = n.lower()
        for c in (re.sub(r"[^a-z0-9]", "", low), re.sub(r"[^a-z0-9]+", "-", low).strip("-"),
                  re.sub(r"[^a-z0-9]", "", re.sub(r"\b(inc|llc|ltd|gmbh|ag|se|corp|co|company|group)\b", "", low))):
            if c and c not in cands:
                cands.append(c)
    return None, cands


def _text(h: str | None) -> str | None:
    if not h:
        return None
    t = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", html.unescape(h), flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\xa0]+", " ", t)).strip() or None


def _ms(v) -> str | None:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat()
    return v or None


def _row(**kw) -> dict:
    base = dict.fromkeys(["company", "ats", "boardSlug", "jobId", "title", "department", "team", "location",
                          "allLocations", "country", "remote", "workplaceType", "employmentType",
                          "salaryMin", "salaryMax", "salaryCurrency", "salaryInterval", "salaryText",
                          "url", "applyUrl", "publishedAt", "updatedAt", "descriptionText", "descriptionHtml"])
    base.update({k: v for k, v in kw.items() if k in base})
    return base


class Client:
    def __init__(self, descriptions: bool = True):
        self.d = descriptions
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=True,
                                      headers={"User-Agent": "ats-jobs-apify-actor/1.0 (+https://apify.com/jordan-belfort)"})
        self.sem = asyncio.Semaphore(8)

    async def close(self):
        await self.http.aclose()

    async def get(self, url, **kw):
        async with self.sem:
            for attempt in range(4):
                try:
                    r = await self.http.get(url, **kw)
                except httpx.TransportError:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return r

    async def fetch(self, ats: str, slug: str) -> list[dict] | None:
        return await getattr(self, ats)(slug)

    async def greenhouse(self, slug):
        r = await self.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        if r.status_code != 200:
            return None
        name = slug
        b = await self.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if b.status_code == 200:
            name = b.json().get("name") or slug
        out = []
        for j in r.json().get("jobs", []):
            body = html.unescape(j.get("content") or "")
            pay = [m for m in (j.get("pay_input_ranges") or [])]
            p = pay[0] if pay else {}
            out.append(_row(company=name, ats="greenhouse", boardSlug=slug, jobId=str(j.get("id")), title=j.get("title"),
                            department=", ".join(d["name"] for d in j.get("departments") or []) or None,
                            location=(j.get("location") or {}).get("name"),
                            allLocations=[o["name"] for o in j.get("offices") or []] or None,
                            salaryMin=(p.get("min_cents") or 0) / 100 or None, salaryMax=(p.get("max_cents") or 0) / 100 or None,
                            salaryCurrency=p.get("currency_type"), url=j.get("absolute_url"), applyUrl=j.get("absolute_url"),
                            publishedAt=j.get("first_published"), updatedAt=j.get("updated_at"),
                            descriptionHtml=body if self.d else None, descriptionText=_text(body) if self.d else None))
        return out

    async def lever(self, slug):
        out = []
        for host in ("api.lever.co", "api.eu.lever.co"):
            r = await self.get(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
            if r.status_code == 200 and isinstance(r.json(), list):
                break
        else:
            return None
        for j in r.json():
            c = j.get("categories") or {}
            s = j.get("salaryRange") or {}
            desc = (j.get("description") or "") + "".join(
                f"<h3>{l.get('text')}</h3><ul>{l.get('content')}</ul>" for l in j.get("lists") or []) + (j.get("additional") or "")
            out.append(_row(company=slug, ats="lever", boardSlug=slug, jobId=j.get("id"), title=j.get("text"),
                            department=c.get("department"), team=c.get("team"), location=c.get("location"),
                            allLocations=c.get("allLocations"), country=j.get("country"),
                            remote=j.get("workplaceType") == "remote", workplaceType=j.get("workplaceType"),
                            employmentType=c.get("commitment"), salaryMin=s.get("min"), salaryMax=s.get("max"),
                            salaryCurrency=s.get("currency"), salaryInterval=s.get("interval"),
                            url=j.get("hostedUrl"), applyUrl=j.get("applyUrl"), publishedAt=_ms(j.get("createdAt")),
                            descriptionHtml=desc if self.d else None, descriptionText=_text(desc) if self.d else None))
        return out

    async def ashby(self, slug):
        r = await self.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
        if r.status_code != 200:
            return None
        out = []
        for j in r.json().get("jobs", []):
            if j.get("isListed") is False:
                continue
            comp = j.get("compensation") or {}
            sal = next((c for c in comp.get("summaryComponents") or [] if c.get("compensationType") == "Salary"), {})
            out.append(_row(company=slug, ats="ashby", boardSlug=slug, jobId=j.get("id"), title=j.get("title"),
                            department=j.get("department"), team=j.get("team"), location=j.get("location"),
                            allLocations=[j.get("location")] + [l.get("location") for l in j.get("secondaryLocations") or []],
                            country=((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry"),
                            remote=j.get("isRemote"), workplaceType=j.get("workplaceType"),
                            employmentType=j.get("employmentType"), salaryMin=sal.get("minValue"), salaryMax=sal.get("maxValue"),
                            salaryCurrency=sal.get("currencyCode"), salaryInterval=sal.get("interval"),
                            salaryText=comp.get("compensationTierSummary"), url=j.get("jobUrl"), applyUrl=j.get("applyUrl"),
                            publishedAt=j.get("publishedAt"),
                            descriptionHtml=j.get("descriptionHtml") if self.d else None,
                            descriptionText=j.get("descriptionPlain") if self.d else None))
        return out

    async def workable(self, slug):
        r = await self.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
        if r.status_code != 200:
            return None
        data = r.json()
        out = []
        for j in data.get("jobs", []):
            locs = [", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) for l in j.get("locations") or []]
            out.append(_row(company=data.get("name") or slug, ats="workable", boardSlug=slug, jobId=j.get("shortcode"),
                            title=j.get("title"), department=j.get("department") or None,
                            location=locs[0] if locs else ", ".join(x for x in (j.get("city"), j.get("country")) if x) or None,
                            allLocations=locs or None, country=j.get("country"), remote=j.get("telecommuting"),
                            employmentType=j.get("employment_type"), url=j.get("url") or j.get("shortlink"),
                            applyUrl=j.get("application_url"), publishedAt=j.get("published_on"), updatedAt=j.get("created_at"),
                            descriptionHtml=j.get("description") if self.d else None,
                            descriptionText=_text(j.get("description")) if self.d else None))
        return out

    async def smartrecruiters(self, slug):
        items, offset = [], 0
        while True:
            r = await self.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                               params={"limit": 100, "offset": offset})
            if r.status_code != 200:
                return None
            d = r.json()
            items += d.get("content", [])
            offset += 100
            if offset >= d.get("totalFound", 0) or offset >= 5000:
                break
        if not items:
            return None
        out = []
        for j in items:
            l = j.get("location") or {}
            comp = (j.get("company") or {})
            out.append(_row(company=comp.get("name") or slug, ats="smartrecruiters", boardSlug=slug, jobId=j.get("id"),
                            title=j.get("name"), department=(j.get("department") or {}).get("label"),
                            location=l.get("fullLocation") or ", ".join(x for x in (l.get("city"), l.get("country")) if x),
                            country=l.get("country"), remote=l.get("remote"),
                            employmentType=(j.get("typeOfEmployment") or {}).get("label"),
                            url=f"https://jobs.smartrecruiters.com/{comp.get('identifier') or slug}/{j.get('id')}",
                            applyUrl=f"https://jobs.smartrecruiters.com/{comp.get('identifier') or slug}/{j.get('id')}",
                            publishedAt=j.get("releasedDate")))
        return out

    async def recruitee(self, slug):
        r = await self.get(f"https://{slug}.recruitee.com/api/offers/")
        if r.status_code != 200 or "offers" not in r.text[:50]:
            return None
        out = []
        for j in r.json().get("offers", []):
            s = j.get("salary") or {}
            body = (j.get("description") or "") + (j.get("requirements") or "")
            out.append(_row(company=j.get("company_name") or slug, ats="recruitee", boardSlug=slug, jobId=str(j.get("id")),
                            title=j.get("title"), department=j.get("department"), location=j.get("location"),
                            allLocations=[l.get("name") for l in j.get("locations") or []] or None,
                            country=j.get("country_code") or j.get("country"), remote=j.get("remote"),
                            workplaceType="remote" if j.get("remote") else "hybrid" if j.get("hybrid") else "onsite" if j.get("on_site") else None,
                            employmentType=j.get("employment_type_code"), salaryMin=s.get("min"), salaryMax=s.get("max"),
                            salaryCurrency=s.get("currency"), salaryInterval=s.get("period"),
                            url=j.get("careers_url"), applyUrl=j.get("careers_apply_url"), publishedAt=j.get("published_at"),
                            updatedAt=j.get("updated_at"),
                            descriptionHtml=body if self.d else None, descriptionText=_text(body) if self.d else None))
        return out

    async def personio(self, slug):
        for tld in ("de", "com"):
            r = await self.get(f"https://{slug}.jobs.personio.{tld}/xml", params={"language": "en"})
            if r.status_code == 200 and "<workzag-jobs" in r.text[:300]:
                break
        else:
            return None
        root = ET.fromstring(r.content)
        out = []
        for p in root.findall("position"):
            g = lambda k: (p.findtext(k) or "").strip() or None
            body = "".join(f"<h3>{html.escape(d.findtext('name') or '')}</h3>{d.findtext('value') or ''}"
                           for d in p.findall("jobDescriptions/jobDescription"))
            offices = [g("office")] + [o.text for o in p.findall("additionalOffices/office") if o.text]
            out.append(_row(company=g("subcompany") or slug, ats="personio", boardSlug=slug, jobId=g("id"), title=g("name"),
                            department=g("department"), team=g("recruitingCategory"), location=g("office"),
                            allLocations=[o for o in offices if o] or None, employmentType=g("employmentType"),
                            workplaceType=g("schedule"), url=f"https://{slug}.jobs.personio.{tld}/job/{g('id')}",
                            applyUrl=f"https://{slug}.jobs.personio.{tld}/job/{g('id')}#apply", publishedAt=g("createdAt"),
                            descriptionHtml=body if self.d else None, descriptionText=_text(body) if self.d else None))
        return out

    async def enrich(self, rows: list[dict]) -> None:
        """SmartRecruiters lists postings without text; fetch it only for the rows that survived the filters."""
        if not self.d:
            return

        async def one(r):
            x = await self.get(f"https://api.smartrecruiters.com/v1/companies/{r['boardSlug']}/postings/{r['jobId']}")
            if x.status_code == 200:
                sec = (x.json().get("jobAd") or {}).get("sections") or {}
                h = "".join(f"<h3>{v.get('title')}</h3>{v.get('text')}" for v in sec.values() if v.get("text"))
                r["descriptionHtml"], r["descriptionText"] = h, _text(h)
        await asyncio.gather(*(one(r) for r in rows if r["ats"] == "smartrecruiters" and not r["descriptionHtml"]))
