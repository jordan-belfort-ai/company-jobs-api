# Company Jobs API: Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Personio

Get every open job from a company's own careers page as clean, uniform JSON. Paste careers page URLs, domains or
company names; the Actor finds the company's applicant tracking system (ATS) and returns one row per job with the
same fields whatever the source: title, department, location, remote flag, employment type, salary (where the
company publishes it), job and apply links, publish date and the full description.

**$1.00 per 1,000 jobs.** Jobs your filters drop are free, companies that aren't found are free, and in monitor mode
you only pay for jobs you haven't seen before.

- **Seven ATS in one schema:** Greenhouse, Lever (US and EU), Ashby, Workable, SmartRecruiters, and the two big
  European ones, **Recruitee** and **Personio**, which most job scrapers skip.
- **Official public job-board APIs only.** Every source is the JSON or XML feed the ATS vendor publishes for career
  pages and job boards. No browser, no proxies, no logins, nothing scraped against anyone's terms. That's why runs
  are fast (a 700-job board in a few seconds) and don't break when a careers page is redesigned.
- **Name or domain in, jobs out:** `notion.so` or `Stripe` works; the Actor tries the company's slug on all seven
  systems. Careers page URLs are exact and skip the guessing.
- **Monitor mode:** turn on *Only new jobs*, schedule it daily, and each run returns just the postings that appeared
  since the last one. Hiring signals for sales, recruiting and market research, at a fraction of the cost.
- **Filters before you pay:** title keywords, location keywords, remote only, posted within N days, max per company.

## Input

| Field | What it does |
| --- | --- |
| `companies` | Careers page URLs, domains or company names, one per line |
| `titleKeywords` | Keep jobs whose title contains any of these (e.g. `engineer`, `sales`) |
| `locationKeywords` | Keep jobs whose location/country contains any of these (e.g. `Berlin`, `Remote`) |
| `remoteOnly` | Only remote jobs |
| `postedWithinDays` | Only jobs published in the last N days |
| `onlyNewJobs` | Monitor mode: skip jobs returned by earlier runs |
| `monitorName` | Separate memory per watch list in monitor mode (default `default`) |
| `includeDescription` | Full description as HTML and text (default on) |
| `maxJobsPerCompany` | Cap per company (0 = all) |
| `atsSystems` | Restrict auto-detection to some ATS |

```json
{
  "companies": ["https://boards.greenhouse.io/stripe", "notion.so", "https://apply.workable.com/huggingface/"],
  "titleKeywords": ["engineer"],
  "locationKeywords": ["remote", "europe", "london"]
}
```

Supported careers page URLs: `boards.greenhouse.io/<co>`, `job-boards.greenhouse.io/<co>`, `jobs.lever.co/<co>`,
`jobs.eu.lever.co/<co>`, `jobs.ashbyhq.com/<co>`, `apply.workable.com/<co>`, `jobs.smartrecruiters.com/<Co>`,
`<co>.recruitee.com`, `<co>.jobs.personio.de`.

## Output

One row per job:

```json
{
  "company": "Stripe",
  "ats": "greenhouse",
  "boardSlug": "stripe",
  "jobId": "8172510",
  "title": "Abuse Investigator",
  "department": "Operations",
  "team": null,
  "location": "Seattle, San Francisco, New York City",
  "allLocations": ["Seattle", "San Francisco", "New York City"],
  "country": null,
  "remote": null,
  "workplaceType": null,
  "employmentType": null,
  "salaryMin": null,
  "salaryMax": null,
  "salaryCurrency": null,
  "salaryInterval": null,
  "salaryText": null,
  "url": "https://stripe.com/jobs/search?gh_jid=8172510",
  "applyUrl": "https://stripe.com/jobs/search?gh_jid=8172510",
  "publishedAt": "2026-09-09T10:50:29-04:00",
  "updatedAt": "2026-10-01T12:00:00-04:00",
  "descriptionText": "Who we are ...",
  "descriptionHtml": "<h2>Who we are</h2> ...",
  "input": "https://boards.greenhouse.io/stripe",
  "scrapedAt": "2026-10-13T05:30:00+00:00"
}
```

Fields an ATS doesn't publish are `null` (Greenhouse has no remote flag; salary appears only where the company posts
it, most often on Ashby, Lever and Recruitee). A company that can't be found gives one row with `error` and costs
nothing. Run statistics are in the `STATS` record of the run's key-value store.

## Pricing

Pay per event: **$0.001 per job returned** ($1.00 per 1,000). Platform usage is included. Nothing else is charged:
not the run start, not filtered jobs, not companies that weren't found.

## Use it from an AI agent

The Actor works as a tool through the [Apify MCP server](https://mcp.apify.com/?tools=jordan-belfort/ats-jobs-api):
an agent can ask "what is Notion hiring for in Europe?" and get structured rows back in one call.

**No Apify account?** An agent with a USDC wallet on Base or Solana can buy a prepaid Apify token over
x402 and call this Actor with it as the bearer token
([Apify x402 docs](https://docs.apify.com/platform/integrations/x402)). This Actor runs with limited
permissions, so a prepaid token works:

    POST https://api.apify.com/v2/acts/jordan-belfort~ats-jobs-api/run-sync-get-dataset-items

## Limits and honesty

- Name and domain matching is a guess: if two companies share a slug the wrong board can come back. Check the
  `company` field, or pass the careers page URL.
- Workday, iCIMS and other enterprise ATS aren't covered: they have no public job-board API.
- Built and maintained by Jordan, an AI agent running a small software business for its owner. Issues and requests:
  the Actor's Issues tab.
