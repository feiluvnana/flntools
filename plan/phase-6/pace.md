# pace

Phase 6, with [source](source.md) and [session](session.md), built in step 3. The user asked on 2026-11-12 for the pace to be as small as possible but never below what a site tolerates, chosen from research. A single fixed number per source can't be both. It is either slower than needed most of the time, or too fast at the moments a site counts. So `pace` is three things, from the most exact to the least:
1. **a site's own headers** when it sends them;
2. **a known budget** per kind of request, where one is known;
3. **a learned gap with a floor**: start at the smallest gap, slow down when the site says so, and remember.

## What the sites and the tools say

Researched 2026-11-12:

| Site | What is known | Source |
| --- | --- | --- |
| Instagram | GraphQL: **200 requests per 660 s per query** (`doc_id`), and **275 per 600 s for all GraphQL together**. Other web API calls (`/api/v1/…`): **75 per 660 s**. Instaloader waits only when a window is full, and adds 6 s of margin | Instaloader's `RateController` (`instaloadercontext.py`) |
| Instagram | gallery-dl waits **6–12 s before every request** by default | gallery-dl `docs/configuration.rst`, `sleep-request` |
| Twitter/X | every API response carries `x-rate-limit-remaining` and `x-rate-limit-reset`. gallery-dl runs at full speed, and waits for the reset only when fewer than about 6 requests remain (0–1 always, 2–5 by chance), or on a 429 | gallery-dl `extractor/twitter.py` |
| Facebook | no published or tool-known limit; gallery-dl sets no request interval. Facebook answers too-fast viewing with "You've been temporarily blocked", which gallery-dl treats as a stop, not a wait | gallery-dl `extractor/facebook.py` |
| TikTok | no published or tool-known limit; gallery-dl sets no interval and retries with a fixed timeout. TikTok answers with a captcha | gallery-dl `extractor/tiktok.py` |
| Telegram | the server says exactly how long to wait (`FLOOD_WAIT_<seconds>`) | Telethon's `FloodWaitError` |
| loi | none known; a small site with a JSON API | — |

Two conclusions:
- **Instagram is the only site with a known budget.** It is far more generous than gallery-dl's fixed 6–12 s (275 GraphQL calls per 10 minutes is one every 2.2 s on average), and far tighter for `/api/v1/` (one every 8.8 s). So ins reads posts through GraphQL, not `/api/v1/` ([ins](ins.md#1-read-posts-through-the-api-not-their-pages)).
- **Twitter and Telegram say exactly what they allow**, so for them a fixed pace would only slow them down.

## Design

`Source.pace` is a `Pace`, or a number as shorthand for `Pace(floor=number)`:

```python
@dataclass(frozen=True)
class Pace:
    floor: float = 0.5                                      # the least seconds between two requests, ±40%
    budgets: Mapping[str, tuple[int, float]] = {}           # URL part -> (requests, per seconds)
```

For each request to the source's own `domains` (API calls, in-page calls and page loads; never CDN downloads, which only `concurrency` limits), the session waits for the latest of these:

1. **Headers.** A response's `x-rate-limit-remaining` and `x-rate-limit-reset` (and `x-ratelimit-*`, its other spelling) are read for every source. While more than 5 remain, they add nothing. At 5 or fewer, the next request waits for the reset, plus 1 s. `Retry-After` on a 429 is honored as today. No source declares this; any site that sends the headers gets it.
2. **Budgets.** A sliding window per budget, as Instaloader keeps them. A request whose URL contains the budget's key may go only while fewer than `requests` went in the last `per` seconds, with a 5% margin. Otherwise it waits for the oldest to leave the window. So a run goes at full speed until a window is nearly full, and then exactly as fast as the window allows.
3. **The learned gap**, at least `floor`, ±40%:
   - it starts at the gap this source learned on earlier runs (in its `state`), or at `floor`;
   - after a rate limit, a captcha or a "temporarily blocked" page, it **doubles**, up to 60 s;
   - after 50 requests in a row without one, it shrinks by 20%, never below `floor`;
   - it is saved in the source's `state` at the end of the run, so the next run starts where this one ended.

   So flnd finds the smallest gap each site tolerates for this account and connection, and stays near it.

A wait of more than 15 s shows on the source's row (`ins: pacing, 42 s`), so a slow run explains itself.

## Values

| Source | `pace` | Why |
| --- | --- | --- |
| ins | `Pace(floor=1.0, budgets={"/graphql": (275, 600), "/api/v1/": (75, 660)})` | Instaloader's measured budgets; the floor keeps bursts at a person's speed while a window has room |
| twt | `0.5` | the headers say exactly when to stop; the floor only keeps it from hammering |
| fbk | `1.5` | no known limit, and its block is the harshest of the six (a stop for hours, not a 429), so it starts slower and learns |
| tik | `1.0` | no known limit; a captcha is its signal, which doubles the gap |
| loi | `0.5` | a small JSON API with no known limit |
| tlg | `0` | Telegram's flood waits give the exact wait |

With Instagram's budgets, a 2,000-post tagged tab (about 170 GraphQL pages, and post queries for the videos among them) takes about 10–25 minutes of requests. A fixed 3 s pace takes about 2 hours, and gallery-dl's 6–12 s about 4–8 hours.

**Scrolling** isn't paced by this. It's the fallback, and a page's own requests while scrolling aren't flnd's to space out. A scroll waits for the page's progress rather than a clock, with a learned floor of 0.3 s or more ([session](session.md#scrolling-on-the-pages-own-time)).

## Code

- `flnd/pace.py`, about 70 lines: `Pace`, the sliding windows, the header rule and the learned gap. It has one `wait_turn(url)` and one `record(response)` that the session calls around each request, and `throttled()` calls its `slow_down()`.
- `Source.pace` replaces the `pace` float in [source](source.md); the field count stays 8.

## Tests

`tests/test_pace.py`, on the fake clock:
- with a budget of 275 per 600 s:
  - 275 requests go at the floor's pace;
  - the 276th waits until the first leaves the window, plus the margin;
  - a URL without the key isn't counted;
- `x-rate-limit-remaining: 3` with a reset in 40 s makes the next request wait 41 s; `remaining: 50` adds nothing;
- the learned gap:
  - doubles after a throttle, up to 60 s;
  - shrinks after 50 clean requests, never below the floor;
  - is read from and written to `state`;
- CDN downloads (a URL outside `domains`) never wait.
