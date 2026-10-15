"""
Trading brief automation (v3).

Collects four kinds of data, asks Gemini for a brief, sends it to Telegram:
  1. Economic calendar   - Forex Factory public feed (USD, EUR, GBP, JPY)
  2. Prices              - Yahoo Finance via yfinance (EUR/USD, GBP/USD, USD/JPY, Gold, DXY)
  3. Central bank text   - full-text excerpts of the latest Fed / ECB / BoE / BoJ statements and speeches
  4. News headlines      - RSS feeds: central banks + forex/economy sites

MODE=weekly -> week-ahead brief      MODE=daily -> pre-London brief

Secrets (environment variables): GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Optional settings: GEMINI_MODEL, TZ_NAME, CURRENCIES, IMPACTS, FULLTEXT_CHARS
"""
import html
import os
import re
import sys
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests

try:
    import trafilatura
except ImportError:  # the brief still works without it, minus the central bank full text
    trafilatura = None

MODE = (os.getenv("MODE") or "weekly").strip().lower()
if MODE not in ("weekly", "daily"):
    MODE = "weekly"

TZ = ZoneInfo(os.getenv("TZ_NAME") or "Africa/Algiers")
CURRENCIES = {c.strip().upper() for c in (os.getenv("CURRENCIES") or "USD,EUR,GBP,JPY").split(",") if c.strip()}
IMPACTS = {i.strip().title() for i in (os.getenv("IMPACTS") or "High,Medium,Holiday").split(",") if i.strip()}

USER_AGENT = "Mozilla/5.0 (compatible; personal-trading-brief/3.0)"
OLDEST = datetime(1970, 1, 1, tzinfo=timezone.utc)

# ------------------------------------------------------------------ sources

CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# display name -> (list of Yahoo tickers to try in order, decimals)
MARKETS = {
    "EUR/USD": (["EURUSD=X"], 5),
    "GBP/USD": (["GBPUSD=X"], 5),
    "USD/JPY": (["JPY=X", "USDJPY=X"], 3),
    "Gold XAU/USD (futures used as proxy)": (["GC=F", "XAUUSD=X"], 2),
    "US Dollar Index (DXY)": (["DX-Y.NYB"], 3),
}

def gnews(query):
    """Google News RSS search: headlines (with the publisher's name) for a query, newest first."""
    when = "7d" if MODE == "weekly" else "2d"
    return ("https://news.google.com/rss/search?q=" + quote_plus(f"{query} when:{when}")
            + "&hl=en-US&gl=US&ceid=US:en")


# (name, RSS url, max items kept, keyword_filter, official)
# keyword_filter=True keeps only headlines that mention macro/market keywords
#   (used for general news sites so unrelated stories are dropped).
# official=True marks central bank feeds: their newest relevant items get their
#   full page text read (see fetch_fulltext).
NEWS_SOURCES = [
    # central banks
    ("Federal Reserve - monetary policy", "https://www.federalreserve.gov/feeds/press_monetary.xml", 5, False, True),
    ("Federal Reserve - speeches", "https://www.federalreserve.gov/feeds/speeches.xml", 6, False, True),
    ("ECB - press, speeches, interviews", "https://www.ecb.europa.eu/rss/press.xml", 8, False, True),
    ("Bank of England - news", "https://www.bankofengland.co.uk/rss/news", 6, False, True),
    ("Bank of England - speeches", "https://www.bankofengland.co.uk/rss/speeches", 4, False, True),
    ("Bank of Japan - what's new", "https://www.boj.or.jp/en/rss/whatsnew.xml", 6, False, True),
    # forex-focused news and analysis (FXStreet was removed: it answers 403 to GitHub's servers)
    ("Action Forex", "https://www.actionforex.com/feed/", 12, False, False),
    ("MarketPulse (OANDA)", "https://www.marketpulse.com/feed/", 10, False, False),
    ("Google News - forex", gnews('forex OR "EUR/USD" OR "GBP/USD" OR "USD/JPY"'), 12, False, False),
    ("Google News - gold, Fed, yields", gnews('"gold price" OR "Federal Reserve" OR "Treasury yields"'), 12, False, False),
    # general economy / markets news
    ("CNBC - economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", 8, True, False),
    ("BBC - business", "https://feeds.bbci.co.uk/news/business/rss.xml", 8, True, False),
    ("The Guardian - economics", "https://www.theguardian.com/business/economics/rss", 8, True, False),
    ("MarketWatch - top stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories", 8, True, False),
]

NEWS_HOURS = 168 if MODE == "weekly" else 36
MAX_NEWS_LINES = 120
MAX_FULLTEXT = 5 if MODE == "weekly" else 4          # central bank pages to read per run
FULLTEXT_CHARS = int(os.getenv("FULLTEXT_CHARS") or 6000)  # excerpt size per page

KEYWORDS = re.compile(
    r"\b(inflation|cpi|ppi|gdp|jobs|payrolls?|unemployment|employment|wages?|retail sales|pmi|"
    r"interest rates?|rate (?:cut|cuts|hike|hikes|decision)|central bank|fed|fomc|powell|"
    r"ecb|lagarde|boe|bank of england|bailey|boj|bank of japan|ueda|yen|euro|sterling|pound|"
    r"dollar|treasury|yields?|bonds?|gold|oil|tariffs?|recession|economy|economic|trade|"
    r"currency|forex|stimulus|budget|deficit|debt)\b",
    re.I,
)

# Titles dropped before the per-source cap is applied (routine or off-topic items).
OFFICIAL_NOISE = re.compile(
    r"\b(accounts?|money stock|bonds? held|CV and questionnaire|simulation|banknotes?|"
    r"payment systems?|supervis\w*|discount rate|market operations|tripartite|"
    r"money markets? committee|consultation)\b",
    re.I,
)
OTHER_PAIRS = re.compile(r"\b(?:AUD|NZD|CAD|CHF|CNH|SEK|NOK)\b|\b(?:GBP/JPY|EUR/JPY|EUR/GBP)\b", re.I)
SKIP_BY_SOURCE = {"Action Forex": OTHER_PAIRS}

# Which central bank items are worth reading in full (matched against the title).
OFFICIAL_FOCUS = re.compile(
    r"monetary|policy|interest rates?|bank rate|inflation|fomc|mpc|governing council|outlook|"
    r"projections?|minutes|statement|press conference|speech|remarks|testimony|interview|"
    r"economic|economy|meeting",
    re.I,
)

# Paragraphs containing these words are kept first when a long speech has to be cut down.
POLICY_WORDS = re.compile(
    r"\b(inflation|rates?|policy|monetary|labou?r|employment|growth|outlook|projections?|"
    r"expectations?|tightening|easing|cuts?|hikes?|balance sheet|yields?|exchange rate|"
    r"currency|energy|prices?|wages?|risks?)\b",
    re.I,
)


def describe(exc):
    """Short error text for logs and the Telegram footer, e.g. 'HTTPError 403'."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return type(exc).__name__ + (f" {code}" if code else "")


def http_get(url, timeout=30, tries=2):
    """GET with one retry for timeouts, rate limits (429) and server errors (5xx)."""
    for attempt in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(5)
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError):
            if attempt == tries - 1:
                raise
            time.sleep(5)


# ----------------------------------------------------------------- calendar

def fetch_calendar():
    events = []
    for url in CALENDAR_URLS:
        try:
            r = http_get(url, timeout=30)
            data = r.json()
            if isinstance(data, list):
                events.extend(data)
        except Exception as e:
            print(f"Calendar fetch failed ({url.rsplit('/', 1)[-1]}): {describe(e)}", file=sys.stderr)
    return events


def to_local(date_str):
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:  # the feed normally includes an offset; if not, assume US Eastern
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt.astimezone(TZ)


def select_events(raw, now):
    chosen = []
    for e in raw:
        try:
            cur = (e.get("country") or e.get("currency") or "").upper()
            impact = str(e.get("impact", "")).title()
            if cur not in CURRENCIES or impact not in IMPACTS:
                continue
            dt = to_local(e["date"])
        except Exception:
            continue
        if MODE == "daily":
            if dt.date() != now.date():
                continue
        elif not (now - timedelta(hours=1) <= dt <= now + timedelta(days=7)):
            continue
        chosen.append((dt, cur, impact, e))
    chosen.sort(key=lambda x: x[0])
    return chosen


def format_events(chosen, cap=80):
    if not chosen:
        return "No matching events in this window for the selected currencies and impact levels."
    high = [c for c in chosen if c[2] == "High"]
    rest = [c for c in chosen if c[2] != "High"]
    keep = high[:cap] + rest[: max(0, cap - len(high))]  # High-impact events always win a slot
    keep.sort(key=lambda x: x[0])
    lines = []
    for dt, cur, impact, e in keep:
        lines.append(
            f"{dt:%a %d %b %H:%M} | {cur} | {impact} | {e.get('title', '?')} "
            f"| forecast: {e.get('forecast') or '-'} | previous: {e.get('previous') or '-'}"
        )
    return "\n".join(lines)


# -------------------------------------------------------------------- prices

def where(price, low, high, dp, label):
    if price > high:
        return f"above the {label} high by {price - high:.{dp}f}"
    if price < low:
        return f"below the {label} low by {low - price:.{dp}f}"
    return f"inside the {label} range"


def format_levels(name, ticker, daily, weekly, dp, today=None):
    """Key levels worked out here in code (not by the AI): last price, today so far,
    previous day, this week so far and previous week."""
    def f(x):
        return f"{float(x):.{dp}f}"

    tz = daily.index.tz
    today = today or (datetime.now(tz) if tz else datetime.now()).date()
    price = float(daily["Close"].iloc[-1])

    live_day = daily.index[-1].date() == today  # the last candle is today's and still forming
    today_row = daily.iloc[-1] if live_day else None
    pd_pos = -2 if live_day else -1
    prev_day = daily.iloc[pd_pos] if len(daily) >= abs(pd_pos) else None

    this_week = prev_week = None
    prev_week_label = ""
    if not weekly.empty:
        start_day = weekly.index[-1].date()
        live_week = today.weekday() < 5 and 0 <= (today - start_day).days < 5  # Mon-Fri of the week in progress
        this_week = weekly.iloc[-1] if live_week else None
        pw_pos = -2 if live_week else -1
        if len(weekly) >= abs(pw_pos):
            prev_week = weekly.iloc[pw_pos]
            prev_week_label = f"week of {weekly.index[pw_pos]:%d %b}"

    lines = [f"{name} [{ticker}]", f"  Last price: {f(price)}"]
    if today_row is not None:
        lines.append(
            f"  Today so far: open {f(today_row['Open'])} | high {f(today_row['High'])} | low {f(today_row['Low'])}"
        )
    if prev_day is not None:
        lines.append(
            f"  Previous day ({daily.index[pd_pos]:%a %d %b}): high {f(prev_day['High'])} | "
            f"low {f(prev_day['Low'])} | close {f(prev_day['Close'])}"
        )
        lines.append(f"    Price is {where(price, prev_day['Low'], prev_day['High'], dp, 'previous day')}")
    if this_week is not None:
        lines.append(f"  This week so far: high {f(this_week['High'])} | low {f(this_week['Low'])}")
    if prev_week is not None:
        lines.append(
            f"  Previous week ({prev_week_label}): high {f(prev_week['High'])} | "
            f"low {f(prev_week['Low'])} | close {f(prev_week['Close'])}"
        )
        lines.append(f"    Price is {where(price, prev_week['Low'], prev_week['High'], dp, 'previous week')}")
    lines.append("  Last 5 daily closes (oldest to newest): " + ", ".join(f(x) for x in daily["Close"].tail(5)))
    if not weekly.empty:
        lines.append("  Last weekly closes (oldest to newest): " + ", ".join(f(x) for x in weekly["Close"]))
    return "\n".join(lines)


def fetch_markets():
    try:
        import yfinance as yf
    except ImportError:
        return "Price data unavailable (yfinance not installed)."

    blocks = []
    for name, (tickers, dp) in MARKETS.items():
        block = None
        for ticker in tickers:
            try:
                t = yf.Ticker(ticker)
                daily = t.history(period="1mo", interval="1d").dropna().tail(10)
                if daily.empty:
                    continue
                weekly = t.history(period="3mo", interval="1wk").dropna().tail(4)
                block = format_levels(name, ticker, daily, weekly, dp)
                break
            except Exception as e:
                print(f"Price fetch failed for {ticker}: {type(e).__name__}", file=sys.stderr)
        blocks.append(block or f"{name}: price data unavailable.")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------- news

def clean_text(s, limit=220):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def parse_feed(content, max_items, filtered, now_utc, skip=None):
    """Raw RSS/Atom bytes -> [(published_utc_or_None, title, summary, link), ...], newest first."""
    feed = feedparser.parse(content)
    cutoff = now_utc - timedelta(hours=NEWS_HOURS)
    items = []
    for entry in feed.entries:
        title = clean_text(entry.get("title"), 200)
        if not title or (skip and skip.search(title)):
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"), 220)
        core = re.sub(r"\s+[-|]\s+[^-|]+$", "", title)  # headline without a trailing " - Publisher"
        if summary and summary.lower().startswith(core.lower()):
            rest = summary[len(core):].strip(" .:-")
            summary = rest if len(rest) > 25 else ""  # keep only what the summary adds to the headline
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        published = None
        if parsed:
            published = datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)
            if published < cutoff:
                continue
        if filtered and not KEYWORDS.search(f"{title} {summary}"):
            continue
        items.append((published, title, summary, entry.get("link") or ""))
    items.sort(key=lambda x: x[0] or OLDEST, reverse=True)
    return items[:max_items]


def fetch_news(now):
    """Returns (headline_text, failed_feed_names, official_items)."""
    now_utc = now.astimezone(timezone.utc)
    seen, lines, failed, official_items = set(), [], [], []
    for name, url, max_items, filtered, official in NEWS_SOURCES:
        try:
            r = http_get(url, timeout=25)
            skip = OFFICIAL_NOISE if official else SKIP_BY_SOURCE.get(name)
            items = parse_feed(r.content, max_items, filtered, now_utc, skip)
        except Exception as e:
            print(f"News feed failed: {name}: {describe(e)}", file=sys.stderr)
            failed.append(f"{name} ({describe(e)})")
            continue
        print(f"News: {name}: {len(items)} items")
        for published, title, summary, link in items:
            if official:
                official_items.append((published, name, title, link))
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            when = f"{published.astimezone(TZ):%a %d %b %H:%M}" if published else "date n/a"
            line = f"[{name}] {when} | {title}"
            if summary and summary.lower() != title.lower():
                line += f" -- {summary}"
            lines.append(line)
    if len(failed) == len(NEWS_SOURCES):
        return "NEWS UNAVAILABLE (all feeds failed). Do not guess headlines.", failed, official_items
    text = "\n".join(lines[:MAX_NEWS_LINES]) or "No recent headlines found."
    return text, failed, official_items


# ------------------------------------------------- central bank full text

def excerpt(text, budget):
    """Shrink a long page to `budget` characters, keeping the opening and the policy-heavy paragraphs."""
    paras = [p.strip() for p in text.split("\n") if len(p.strip()) > 40]
    if sum(len(p) + 1 for p in paras) <= budget:
        return "\n".join(paras)
    out, used = [], 0
    for p in paras:
        if (used < 800 or POLICY_WORDS.search(p)) and used + len(p) + 1 <= budget:
            out.append(p)
            used += len(p) + 1
    return "\n".join(out) if out else " ".join(paras)[:budget]


def pick_fulltext_candidates(items):
    """Newest relevant official items, at most two per source."""
    per_source, picked = {}, []
    for published, name, title, link in sorted(items, key=lambda x: x[0] or OLDEST, reverse=True):
        if not link or not OFFICIAL_FOCUS.search(title):
            continue
        if per_source.get(name, 0) >= 2:
            continue
        per_source[name] = per_source.get(name, 0) + 1
        picked.append((published, name, title, link))
    return picked


def fetch_fulltext(candidates, limit):
    docs = []
    for published, name, title, link in candidates:
        if len(docs) >= limit:
            break
        if link.lower().split("?")[0].endswith(".pdf"):
            continue  # PDFs are not read
        try:
            r = http_get(link, timeout=30)
            ctype = r.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype:
                continue
            text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
            if len(text) < 300:  # extraction failed or the page is just a stub
                continue
            docs.append((name, title, published, link, excerpt(text, FULLTEXT_CHARS)))
        except Exception as e:
            print(f"Full text failed: {name}: {describe(e)}", file=sys.stderr)
    return docs


def build_fulltext_section(official_items, problems):
    if trafilatura is None:
        problems.append("central bank full text (trafilatura not installed)")
        return "FULL TEXT UNAVAILABLE. Do not guess what officials said."
    candidates = pick_fulltext_candidates(official_items)
    if not candidates:
        return "No recent relevant central bank statements or speeches in this window."
    docs = fetch_fulltext(candidates, MAX_FULLTEXT)
    print(f"Central bank full text: {len(docs)} pages read from {len(candidates)} candidates.")
    if not docs:
        problems.append("central bank full text")
        return "FULL TEXT UNAVAILABLE (pages could not be read). Do not guess what officials said."
    blocks = []
    for name, title, published, link, text in docs:
        when = f"{published.astimezone(TZ):%a %d %b %H:%M}" if published else "date n/a"
        blocks.append(f"--- [{name}] {when} | {title}\n{link}\n{text}")
    return "\n\n".join(blocks)


# -------------------------------------------------------------------- prompt

RULES = """RULES
- Use ONLY the data below for dates, events, forecasts, statements, headlines and price levels. Never invent numbers.
- KEY PRICE LEVELS were calculated by the script. Copy those numbers exactly, keep their labels (today so far, previous day, previous week) and never work out your own highs or lows.
- If a data block says it is unavailable, say so plainly and do not fill the gap.
- CENTRAL BANK FULL TEXT holds excerpts of official pages; some paragraphs are skipped. Base statements about what officials said on these excerpts only, and never assume what skipped parts said. Quote at most a short phrase (under 15 words).
- Cite news by source name (for example FXStreet, ECB, BBC). Headlines are claims made by those sources, not verified facts; never add details beyond the headline and summary. If sources disagree, say so.
- Anything that is not from the data must be labelled "general context".
- Give scenarios, not predictions or trade signals. Never tell the reader to buy or sell.
- Do not draw or invent supply/demand zones; the trader does that. You may point to previous weekly/daily highs and lows from the data as levels worth marking.
- Output is plain text for Telegram: no markdown symbols (no asterisks, hashes or backticks). Use CAPS headings and "-" bullets."""

WEEKLY_TASK = """TASK: Write the WEEKLY brief for the week ahead. Maximum 600 words. Sections:
1. WEEK AHEAD - the 5 to 7 events that matter most (day, time, currency) and why they matter for EUR/USD, GBP/USD, USD/JPY or gold.
2. CENTRAL BANKS - what officials actually said in the FULL TEXT excerpts: tone (hawkish, dovish or neutral), hints about the rate path, the data they emphasise, anything surprising. Name the institution and add the link for each item you rely on. If no full text was provided, say so.
3. NEWS THEMES - 2 to 4 market themes from the headlines, naming the sources.
4. SCENARIOS - for EUR/USD, GBP/USD, USD/JPY and Gold: one line each for the bullish case, the bearish case and what would invalidate each, tied to the events, central bank text, news and price data.
5. PRICE CONTEXT - for each instrument: the last price and where it sits versus the previous day and previous week levels under KEY PRICE LEVELS (copy the numbers exactly), naming the previous week and previous day highs/lows as levels worth marking on weekly and daily charts. Use the Dollar Index for overall USD context.
6. NEWS RISK WINDOWS - times to be careful trading.
7. ONE-LINE SUMMARY."""

DAILY_TASK = """TASK: Write the DAILY pre-London-open brief for today. Maximum 400 words. Sections:
1. TODAY'S EVENTS - with times and currencies, and why they matter for EUR/USD, GBP/USD, USD/JPY or gold (say so if there are none).
2. CENTRAL BANKS AND OVERNIGHT NEWS - what officials said in the FULL TEXT excerpts (with the link for items you rely on) plus the 2 to 4 headlines that matter most, naming sources.
3. SCENARIOS - one short line each for EUR/USD, GBP/USD, USD/JPY and Gold (bullish / bearish / invalidation).
4. LEVELS TO WATCH - previous day and previous week highs/lows from KEY PRICE LEVELS (copy exactly).
5. NEWS RISK WINDOWS.
6. ONE-LINE SUMMARY."""


def build_prompt(now, calendar_text, fulltext_text, news_text, markets_text):
    task = WEEKLY_TASK if MODE == "weekly" else DAILY_TASK
    return (
        "You are a market analyst assisting a Supply & Demand forex trader who focuses on "
        "EUR/USD, GBP/USD, USD/JPY and Gold (XAU/USD), uses the Dollar Index (DXY) for USD context, "
        "and works a weekly > daily > 4H > 1H top-down process.\n\n"
        f"{task}\n\n{RULES}\n\n"
        f"CURRENT TIME: {now:%A %d %B %Y, %H:%M} ({TZ.key}). All times below use this timezone.\n\n"
        f"ECONOMIC CALENDAR (currencies: {', '.join(sorted(CURRENCIES))}; "
        f"impact: {', '.join(sorted(IMPACTS))}):\n{calendar_text}\n\n"
        f"CENTRAL BANK FULL TEXT (official pages, last {NEWS_HOURS} hours):\n{fulltext_text}\n\n"
        f"NEWS HEADLINES (last {NEWS_HOURS} hours, newest first within each source):\n{news_text}\n\n"
        f"KEY PRICE LEVELS (calculated by the script from Yahoo Finance data; copy exactly):\n{markets_text}\n"
    )


# -------------------------------------------------------------------- Gemini

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Google retires and renames models often, so the script asks the API which Flash models
# exist (see candidate_models). This built-in list is only the fallback if that call fails.
STATIC_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite",
                 "gemini-flash-latest", "gemini-2.5-flash"]
MAX_MODELS = 6


def candidate_models(key):
    """Model IDs to try in order: GEMINI_MODEL (if set), the 3 newest stable Flash models,
    the newest Flash-Lite, then the built-in fallbacks."""
    models = []
    pinned = (os.getenv("GEMINI_MODEL") or "").strip()
    if pinned:
        models.append(pinned)
    found = []
    try:
        r = requests.get(f"{API_ROOT}/models", params={"pageSize": 1000},
                         headers={"x-goog-api-key": key}, timeout=30)
        r.raise_for_status()
        flash, lite = [], []
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            # stable IDs only: skips preview / image / tts / live / audio variants
            hit = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?", name)
            if hit:
                version = tuple(int(x) for x in hit.group(1).split("."))
                (lite if hit.group(2) else flash).append((version, name))
        found = [n for _, n in sorted(flash, reverse=True)[:3]] + [n for _, n in sorted(lite, reverse=True)[:1]]
    except Exception as e:
        print(f"Model list unavailable ({type(e).__name__}); using the built-in list.", file=sys.stderr)
    for name in found + STATIC_MODELS:
        if name not in models:
            models.append(name)
    return models[:MAX_MODELS]


GEMINI_DEADLINE_S = 330  # total time budget for the AI step, so a slow day cannot hit the 10-minute workflow limit


def call_gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    deadline = time.monotonic() + GEMINI_DEADLINE_S
    errors = []
    for model in candidate_models(key):
        if time.monotonic() > deadline:
            errors.append("time budget used up")
            break
        url = f"{API_ROOT}/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            # the limit includes the model's internal "thinking" tokens, so it is generous
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
        }
        status, r, no_answer = 0, None, False
        for attempt in range(3):
            try:
                r = requests.post(
                    url,
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body,
                    timeout=(15, 100),  # 15 s to connect, 100 s to wait for the answer
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                print(f"{model}: {describe(e)}, trying the next model", file=sys.stderr)
                no_answer = True
                break
            status = r.status_code
            if status in (429, 500, 502, 503, 504) and attempt < 2 and time.monotonic() < deadline - 40:
                wait = (10, 25)[attempt]
                print(f"{model}: HTTP {status}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            break

        if no_answer:
            errors.append(f"{model}: no answer in time")
            continue
        if status in (403, 404, 429) or status >= 500:
            errors.append(f"{model}: HTTP {status}")
            print(f"{model}: HTTP {status}, trying the next model", file=sys.stderr)
            continue
        if status != 200:
            raise RuntimeError(f"Gemini error {status} on {model}: {r.text[:300]}")

        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError, ValueError):
            text = ""
        if text:
            print(f"Gemini model used: {model}")
            return text
        errors.append(f"{model}: empty response")

    raise RuntimeError(
        "Gemini failed - " + "; ".join(errors or ["no model tried"]) + ". "
        "HTTP 503 or a timeout means Google's servers are busy; run the workflow again later."
    )


def build_fallback(exc, calendar_text, news_text, markets_text):
    """What gets sent when the AI step fails: the data the script collected, so the run is not wasted."""
    per_source, picked = {}, []
    for line in news_text.splitlines():  # at most 3 headlines per source, so every source shows up
        m = re.match(r"\[([^\]]+)\]", line)
        name = m.group(1) if m else "other"
        if per_source.get(name, 0) < 3:
            per_source[name] = per_source.get(name, 0) + 1
            picked.append(line)
    return (
        f"AI SUMMARY UNAVAILABLE ({str(exc)[:300]})\n"
        "Raw data instead:\n\n"
        f"KEY PRICE LEVELS\n{markets_text}\n\n"
        f"ECONOMIC CALENDAR\n{calendar_text}\n\n"
        f"TOP HEADLINES\n" + "\n".join(picked[:30])
    )


# ------------------------------------------------------------------ Telegram

def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)

    for chunk in chunks:
        for i in range(0, len(chunk), 4000):  # hard limit is 4096 characters per message
            r = requests.post(
                url,
                data={"chat_id": chat_id, "text": chunk[i:i + 4000], "disable_web_page_preview": "true"},
                timeout=30,
            )
            if r.status_code != 200:
                raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:200]}")


# ---------------------------------------------------------------------- main

def main():
    now = datetime.now(TZ)
    problems = []

    raw = fetch_calendar()
    if raw:
        chosen = select_events(raw, now)
        print(f"Calendar: {len(raw)} events fetched, {len(chosen)} selected.")
        calendar_text = format_events(chosen)
    else:
        calendar_text = "CALENDAR UNAVAILABLE (fetch failed). Do not guess events."
        problems.append("economic calendar")

    news_text, failed_feeds, official_items = fetch_news(now)
    problems.extend(failed_feeds)

    fulltext_text = build_fulltext_section(official_items, problems)

    markets_text = fetch_markets()
    if "price data unavailable" in markets_text:
        problems.append("some price data")

    prompt = build_prompt(now, calendar_text, fulltext_text, news_text, markets_text)
    print(f"Prompt size: {len(prompt)} characters.")
    try:
        analysis = call_gemini(prompt)
    except Exception as exc:
        print(f"Gemini failed: {exc}", file=sys.stderr)
        problems.append("Gemini AI summary")
        analysis = build_fallback(exc, calendar_text, news_text, markets_text)

    title = "WEEKLY BRIEF" if MODE == "weekly" else "DAILY BRIEF"
    footer = "AI-generated scenarios for context, not trade signals."
    if problems:
        footer += "\nSources that failed this run: " + ", ".join(problems)
    send_telegram(f"{title} - {now:%a %d %b %Y}\n\n{analysis}\n\n{footer}")
    print("Sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            send_telegram(f"Trading brief failed: {str(exc)[:300]}")
        except Exception:
            pass
        sys.exit(1)
