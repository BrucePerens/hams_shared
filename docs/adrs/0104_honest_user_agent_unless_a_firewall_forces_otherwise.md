# ADR 0104: Honest User-Agent Unless a Firewall Forces Otherwise

## Status
Accepted (Bruce, 2026-10-03: "We are honest unless a firewall forces us to masquerade as a browser. We
still have reverse DNS identifying us as a crawler.")

Related: [ADR 0080](0080_good_bot_compliance_and_scraping_ethics.md) (status Proposed) lists browser spoofing, headless browsers and IP rotation as "Exception Mode" techniques; decision 2(e) below excludes everything beyond the `User-Agent` string.

This replaces the 2026-10-02 hold ("never a browser string, stop and report a 403") that the
`sync-daemons-honest-user-agent` branches carried. The hold was stricter than Bruce's final decision: it
allowed no exception at all. This ADR allows one narrow, documented, verified exception.

## Context

hams.com runs many daemons that fetch from servers we do not own: government callsign databases (FCC ULS, the Universal Licensing System;
Ofcom, ACMA, ANATEL, ISED, NZ RSM, BNetzA, the regulators of the other countries), QRZ (the QRZ.com callsign site), POTA/SOTA (Parks on the Air and Summits on the Air, portable-operating award programs), contest and hamfest calendars, club web pages,
and so on. Until 2026-10-02 several of them sent a Chrome `User-Agent` string by default, and the FCC
daemon also sent fake `Sec-Fetch-*` navigation headers and a Chrome TLS fingerprint (`curl_cffi`
`impersonate="chrome"`). The reason given in `project-experience` was "government CDNs block default Python
`requests` User-Agents". That is a statement about the *library default* UA, not about an honest,
self-identifying one, and nobody had measured the honest one.

Disguising a crawler as a person is dishonest to the site operator, it is the exact behaviour bot
protection exists to ban, and it puts our egress IPs (several are Bruce's home connection) at risk of a
ban that would also take down legitimate use. The only real evidence of a block we have is the FCC case:
`data.fcc.gov` (Akamai) answered 403 to hams1. Later, controlled testing showed that block was **by
IP/ASN (autonomous system number, the network operator the address belongs to: here the Vultr datacenter), identical for every User-Agent and every TLS impersonation profile**, and a
residential IP got a clean 200. So the browser string never helped there. The fix that worked was an
egress path (pi500-1, `FCC_ULS_PROXY_URL`).

## Decision

**1. Default: an honest User-Agent.** Every daemon, script or module that fetches from a server we do not
operate sends a User-Agent that names hams.com and gives a contact URL. The form is a product token and a
`+URL`, nothing else:

    HamsComSyncDaemon/1.0 (+https://crawler.hams.com)

- `SYSTEM_USER_AGENT` (set for every daemon by `core.env`, provisioned in
  `hams_shared/tools/infrastructure.py`) is the value actually used in production. Code reads it with
  `os.environ.get("SYSTEM_USER_AGENT", <fallback>)`.
- The fallback is the string above. New hams_com daemon code takes it from `daemons/hams_config.py`
  (`HONEST_USER_AGENT` / `honest_user_agent()`); existing daemons carry the same literal as their fallback,
  and the lint test (decision 6) checks every one. A daemon with a more specific identity (the club-page
  crawler's `HamsComCrawler/1.0 (+https://crawler.hams.com)`) may keep it: the requirements are a product
  token, hams.com in a `+https://` contact URL, and no e-mail address or phone number.
- The identity string carries **no personal phone number and no e-mail address**: it is sent to every
  third-party server we fetch from, and those logs are not ours. The contact page at
  `https://crawler.hams.com` carries the contact details. That page (or at least the name resolving) must
  exist for the URL to be a real contact; it is operational work outside this ADR.
- A daemon honours `robots.txt` where it crawls pages (as `page_cleaning.py` does), regardless of UA.

**2. Exception: a browser-like User-Agent, for one named source, only after the honest one is verified
blocked.** A daemon may send a browser-like UA to a source only if all of these hold:

- (a) The source's bot protection (Akamai, Cloudflare, ...) actually refuses the honest UA and accepts
  the browser one. "Probably blocks us", "the library default was blocked once", or "a similar site does"
  does not count.
- (b) The block is a **User-Agent block**, not an IP/ASN block. If the same request with a browser UA
  from the same egress is still refused, the exception does not apply; the remedy is an egress path or
  asking the operator for access, never a disguise. (This is why FCC is not an exception.)
- (c) Verification is one deliberate pair of requests (honest UA, then browser UA, same URL, same
  egress) run on the host that actually runs the daemon in production, or on a host Bruce names. Never
  from a test or development machine (Bruce's standing rule: those never fetch from third-party servers,
  and the home IP must not be banned), and never as a repeated probe loop.
- (d) The exception is **scoped to that source**: the browser UA is used only for that source's host(s),
  not as the daemon's default for other hosts it also contacts.
- (e) It covers the `User-Agent` string only (and ordinary content headers such as `Accept`). It does
  **not** cover a forged TLS fingerprint (`impersonate=`), forged `Sec-Fetch-*` / navigation headers,
  solving or bypassing a challenge or CAPTCHA, or rotating IPs to evade a block. Any of those needs a
  new decision by Bruce recorded as an amendment to this ADR.

**3. The exception is declared next to the code, in a machine-checked tag.** On the line of the browser
string, or in the comment block immediately above it (within 8 lines), write:

    # honest-ua-exception: <source host> | <YYYY-MM-DD verified> | <evidence: honest UA got <status>, browser UA got <status>, from <host>>

For example (illustrative; no such exception exists today):

    # honest-ua-exception: example.gov | 2026-11-02 | honest UA got 403, browser UA got 200, from hams1

The date is when (c) was run. The tag must have all three fields. Re-verify when the source or our
egress changes, and delete the exception when the honest UA works again. The test in decision 6 fails a
browser-like UA without a well-formed tag.

**Registry of current exceptions: none.** Every source was audited on 2026-10-03:
- FCC ULS: IP/ASN block, not a UA block (see Context). Not an exception.
- Ofcom, ACMA, ANATEL, ISED, NZ RSM, QRZ and the rest: no recorded evidence of any block of an honest UA.
  They use the honest UA. If one is later blocked, follow decisions 2 and 3.
Add each future exception to the list here, with the tag text, in the same change that adds the tag.

**4. Reverse DNS identifies our crawler hosts regardless.** The `+https://crawler.hams.com` in the UA is
the claim; forward-confirmed reverse DNS is what a site operator can check without trusting us. Egress
hosts we control (hams1 and any other production sync host with a static address) carry a PTR record under
`crawler.hams.com` that resolves forward to the same address. Where we cannot set a PTR (a residential
connection such as pi500-1's, where the ISP owns the reverse zone), the UA's contact URL is the identity.
The browser-UA exception never changes this: the site can still see who we are, and decision 2's
requirement to be blocked first means we mask only the UA string, never the host.

**5. What this does not allow.** No browser UA to avoid rate limits or robots.txt, no browser UA "to be
safe", no browser UA for a source nobody has measured. A 403/429 against an honest UA is reported (and may
become a verification under decision 2), not silently bypassed. Daemons that fetch only from our own
hosts need no change.

**6. Test.** `hams_com/daemons/test_no_browser_user_agent.py` walks every Python source under `daemons/`
and fails if a browser-like UA string, an `impersonate=` argument, or a forged `Sec-Fetch-*` header
appears without a well-formed `honest-ua-exception` tag in range, and also fails if the tag's fields are
missing, its date is not `YYYY-MM-DD`, or any `SYSTEM_USER_AGENT` fallback literal lacks hams.com with a `+https://` URL or contains an `@`, or the scan finds none of the known sync daemons (a broken scan
must not pass). Each fetching daemon's own tests check what is actually sent on the wire where the daemon
already has a local HTTP stand-in (`uk_ofcom_sync`, `fcc_uls_sync`). In hams_open, the same browser-UA
pattern is rejected in `pager_duty` by its synthetic-spooler test. These tests never contact a third-party
server.

## Consequences

- Several sources may start returning 403 to the honest UA. That is information, not a failure to
  paper over: it shows up as a failed run, and decision 2 says how to document a justified exception.
- The earlier belief that "FCC needs a browser" is retired; the FCC path stays on its egress fix
  (`FCC_ULS_PROXY_URL`, pi500-1).
- Existing scripts outside `daemons/` that still send browser strings (one-off scripts under
  `docs/Clubs/scripts/` and `ingest/` in hams_com, `binary_downloader` in hams_open) are outside the
  automated test's scope and tracked as follow-up; they should be converted when next touched.

## Verification

- `python3 -m pytest daemons/test_no_browser_user_agent.py` in hams_com, covering the tag grammar with
  positive and negative fixtures written to a temporary directory.
- `grep -rnE "Mozilla/|AppleWebKit|Chrome/" daemons/` returns only lines that carry an exception tag
  (today: none).
