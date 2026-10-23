"""Jobs from the public job-board APIs that ATS vendors publish for career pages.

Every fetcher returns normalized rows, or None when the company has no board on that ATS.
"""
import asyncio
import html
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

ATS = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "personio", "workday"]

log = logging.getLogger("ats-jobs")

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
    # Workday: slug is 'tenant.wdN/site'; an optional locale segment such as /en-US/ is skipped
    (r"([\w-]+\.wd\d+)\.myworkdayjobs\.com(?:/[a-z]{2}(?:-[A-Za-z]{2,4})?(?=/|$))?/?([\w.%-]*)", "workday"),
    # The same boards on wdN.myworkdaysite.com/recruiting/{tenant}/{site}; tenant.wdN.myworkdayjobs.com serves them too
    (r"(wd\d+)\.myworkdaysite\.com(?:/[a-z]{2}(?:-[A-Za-z]{2,4})?)?/recruiting/([\w-]+)/([\w.%-]+)", "workday-site"),
]


def parse_target(raw: str) -> tuple[str | None, list[str]]:
    """'https://jobs.lever.co/spotify' -> ('lever', ['spotify']); 'Acme Corp' -> (None, candidates)."""
    s = raw.strip()
    for pat, ats in URL_PATTERNS:
        m = re.search(pat, s, re.I)
        if m and m.group(1).lower() not in {"www", "apply", "jobs", "careers", "api"}:
            if ats == "workday":  # a bare slug can't identify a Workday board: tenant, wd host and site are all needed
                return ats, [f"{m.group(1).lower()}/{m.group(2)}"]
            if ats == "workday-site":
                return "workday", [f"{m.group(2).lower()}.{m.group(1).lower()}/{m.group(3)}"]
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
        self.wd_limit = 0  # >0: stop paging a Workday board once this many postings are in (no filters, capped run)
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=True,
                                      headers={"User-Agent": "ats-jobs-apify-actor/1.0 (+https://apify.com/jordan-belfort)",
                                               "Accept-Encoding": "gzip, deflate"})
        self.sem = asyncio.Semaphore(8)
        self.wd_sems: dict[str, asyncio.Semaphore] = {}  # Workday: at most 4 requests in flight per tenant
        self.wd_robots: dict[str, RobotFileParser | None] = {}

    async def close(self):
        await self.http.aclose()

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def request(self, method, url, host_sem: asyncio.Semaphore | None = None, **kw):
        async with host_sem or self.sem:
            for attempt in range(4):
                try:
                    r = await self.http.request(method, url, **kw)
                except httpx.TransportError:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    wait = r.headers.get("Retry-After", "")
                    await asyncio.sleep(min(int(wait), 60) if wait.isdigit() else 2 * (attempt + 1))
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
        out, jobs = [], None
        for host in ("api.lever.co", "api.eu.lever.co"):  # EU-hosted boards answer [] or 404 on the US API
            r = await self.get(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
            if r.status_code == 200 and isinstance(r.json(), list):
                jobs = r.json() if not jobs else jobs
                if jobs:
                    break
        if jobs is None:
            return None
        for j in jobs:
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

    # ---- Workday ----------------------------------------------------------------------------------------------
    # Career sites call https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs themselves. Workday
    # returns `total` only on the first page (0 afterwards), serves at most 2000 postings (offsets past the end
    # answer with the first page again) and rejects limit > 20 and requests without `Accept: application/json`.
    WD_PAGE = 20
    WD_CAP = 2000

    @staticmethod
    def _wd_split(slug: str) -> tuple[str, str, str]:
        hostpart, _, site = slug.partition("/")
        return hostpart.split(".")[0], site, f"https://{hostpart}.myworkdayjobs.com"

    def _wd_sem(self, base: str) -> asyncio.Semaphore:
        return self.wd_sems.setdefault(base, asyncio.Semaphore(4))

    async def _wd_allowed(self, base: str, site: str) -> bool:
        """The tenant's robots.txt must allow the public career site path (its sitemap lists the same jobs)."""
        if base not in self.wd_robots:
            rp = None
            try:
                r = await self.get(f"{base}/robots.txt")
                if r.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                pass
            self.wd_robots[base] = rp
        rp = self.wd_robots[base]
        return rp is None or (rp.can_fetch("*", f"/{site}/") and rp.can_fetch("*", f"/wday/cxs/{site}/jobs"))

    @staticmethod
    def _wd_posted(text: str | None, today: datetime) -> tuple[str | None, int | None]:
        """'Posted 3 Days Ago' -> ('2026-10-15', None); 'Posted 30+ Days Ago' -> (None, 30): only a lower bound."""
        t = (text or "").lower()
        if "today" in t:
            return today.date().isoformat(), None
        if "yesterday" in t:
            return (today - timedelta(days=1)).date().isoformat(), None
        m = re.search(r"(\d+)(\+?)\s*day", t)
        if m:
            if m.group(2):
                return None, int(m.group(1))
            return (today - timedelta(days=int(m.group(1)))).date().isoformat(), None
        return None, None

    async def workday(self, slug):
        tenant, site, base = self._wd_split(slug)
        if not site:
            return None
        if not await self._wd_allowed(base, site):
            log.warning("workday %s: robots.txt disallows /%s/, skipped", tenant, site)
            return None
        api = f"{base}/wday/cxs/{tenant}/{site}"
        today = datetime.now(timezone.utc)
        items, seen, offset, total = [], set(), 0, None
        while True:
            r = await self.request("POST", f"{api}/jobs", host_sem=self._wd_sem(base),
                                   headers={"Accept": "application/json"},
                                   json={"appliedFacets": {}, "limit": self.WD_PAGE, "offset": offset, "searchText": ""})
            if r.status_code != 200:
                if offset == 0:
                    return None
                log.warning("workday %s/%s: HTTP %s at offset %s, keeping %s postings", tenant, site, r.status_code, offset, len(items))
                break
            d = r.json()
            if total is None:
                total = d.get("total") or 0
            page = [p for p in d.get("jobPostings") or [] if p.get("externalPath") and p["externalPath"] not in seen]
            if not page:
                break
            seen.update(p["externalPath"] for p in page)
            items += page
            offset += self.WD_PAGE
            if offset >= min(total, self.WD_CAP):
                break
            if self.wd_limit and len(items) >= self.wd_limit:
                break
        if self.wd_limit and len(items) >= self.wd_limit:
            pass  # stopped early on purpose: the run only wants the first wd_limit jobs
        elif total and total >= self.WD_CAP:
            log.warning("workday %s/%s: reports %s postings; Workday serves at most %s per query, returning %s",
                        tenant, site, total, self.WD_CAP, len(items))
        elif total and len(items) < total:
            log.warning("workday %s/%s: got %s of %s postings", tenant, site, len(items), total)
        out = []
        for p in items:
            path = p["externalPath"]
            pub, older = self._wd_posted(p.get("postedOn"), today)
            url = f"{base}/{site}{path}"
            row = _row(company=tenant, ats="workday", boardSlug=slug, title=p.get("title"), location=p.get("locationsText"),
                       jobId=(p.get("bulletFields") or [None])[0] or path.rsplit("_", 1)[-1],
                       url=url, applyUrl=url, publishedAt=pub)
            row["_olderThanDays"] = older  # postedOn only says '30+ days': lets postedWithinDays still drop it
            out.append(row)
        return out

    async def _wd_detail(self, r: dict) -> None:
        tenant, site, base = self._wd_split(r["boardSlug"])
        path = r["url"].removeprefix(f"{base}/{site}")
        x = await self.request("GET", f"{base}/wday/cxs/{tenant}/{site}{path}", host_sem=self._wd_sem(base),
                               headers={"Accept": "application/json"})
        if x.status_code != 200:
            return
        i = x.json().get("jobPostingInfo") or {}
        body = i.get("jobDescription")
        locs = [l for l in [i.get("location"), *(i.get("additionalLocations") or [])] if l]
        country = (i.get("jobRequisitionLocation") or {}).get("country") or i.get("country") or {}
        remote_type = i.get("remoteType")
        r.update(title=i.get("title") or r["title"], jobId=i.get("jobReqId") or r["jobId"], location=locs[0] if locs else r["location"],
                 allLocations=locs or None, country=country.get("alpha2Code") or country.get("descriptor"),
                 employmentType=i.get("timeType"), workplaceType=remote_type,
                 remote=("remote" in remote_type.lower()) if remote_type else None,
                 publishedAt=i.get("startDate") or r["publishedAt"], url=i.get("externalUrl") or r["url"])
        r["applyUrl"] = r["url"]
        r["_olderThanDays"] = None
        if self.d and body:
            r["descriptionHtml"], r["descriptionText"] = body, _text(body)
        r["_detailed"] = True

    async def workday_details(self, rows: list[dict]) -> None:
        """Postings are listed without text or full locations; one GET per posting that survived the filters."""
        await asyncio.gather(*(self._wd_detail(r) for r in rows if r["ats"] == "workday" and not r.get("_detailed")))

    async def enrich(self, rows: list[dict]) -> None:
        """SmartRecruiters lists postings without text; fetch it only for the rows that survived the filters."""
        if not self.d:
            return
        await self.workday_details(rows)

        async def one(r):
            x = await self.get(f"https://api.smartrecruiters.com/v1/companies/{r['boardSlug']}/postings/{r['jobId']}")
            if x.status_code == 200:
                sec = (x.json().get("jobAd") or {}).get("sections") or {}
                h = "".join(f"<h3>{v.get('title')}</h3>{v.get('text')}" for v in sec.values() if v.get("text"))
                r["descriptionHtml"], r["descriptionText"] = h, _text(h)
        await asyncio.gather(*(one(r) for r in rows if r["ats"] == "smartrecruiters" and not r["descriptionHtml"]))
