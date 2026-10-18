"""
Trading brief automation (v4).

Collects four kinds of data, asks Gemini for a brief, sends it to Telegram (English or Arabic):
  1. Economic calendar   - Forex Factory public feed (USD, EUR, GBP, JPY, CHF, AUD, CAD, NZD)
  2. Prices              - Yahoo Finance via yfinance (7 pairs, Gold, DXY); the key levels are calculated here
  3. Central bank text   - full text (web pages and PDFs) of the latest Fed / ECB / BoE / BoJ / RBA / BoC /
                           SNB / RBNZ statements and speeches
  4. News headlines      - RSS feeds: central banks + forex/economy sites

MODE=weekly -> week-ahead brief      MODE=daily -> pre-London brief      BRIEF_LANGUAGE=en or ar

Secrets (environment variables): GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Optional settings: GEMINI_MODEL, TZ_NAME, CURRENCIES, IMPACTS, FULLTEXT_CHARS, BRIEF_LANGUAGE
"""
import html
import io
import os
import re
import sys
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin
from zoneinfo import ZoneInfo

import feedparser
import requests

try:
    import trafilatura
except ImportError:  # the brief still works without it, minus the central bank full text
    trafilatura = None

try:
    from pypdf import PdfReader
except ImportError:  # PDFs are then skipped
    PdfReader = None

MODE = (os.getenv("MODE") or "weekly").strip().lower()
if MODE not in ("weekly", "daily"):
    MODE = "weekly"

TZ = ZoneInfo(os.getenv("TZ_NAME") or "Africa/Algiers")
CURRENCIES = {c.strip().upper() for c in (os.getenv("CURRENCIES") or "USD,EUR,GBP,JPY,CHF,AUD,CAD,NZD").split(",") if c.strip()}
IMPACTS = {i.strip().title() for i in (os.getenv("IMPACTS") or "High,Medium,Holiday").split(",") if i.strip()}

USER_AGENT = "Mozilla/5.0 (compatible; personal-trading-brief/3.0)"
OLDEST = datetime(1970, 1, 1, tzinfo=timezone.utc)

# ------------------------------------------------------------ language option
# BRIEF_LANGUAGE=ar makes Gemini write the brief in Arabic; these are the fixed phrases the script adds itself.
BRIEF_LANGUAGE = (os.getenv("BRIEF_LANGUAGE") or "en").strip().lower()
if BRIEF_LANGUAGE not in ("en", "ar"):
    BRIEF_LANGUAGE = "en"

_TEXT = {
    "en": {
        "weekly": "WEEKLY BRIEF",
        "daily": "DAILY BRIEF",
        "disclaimer": "AI-generated scenarios for context, not trade signals.",
        "failed": "Sources that failed this run: ",
        "crashed": "Trading brief failed: ",
        "fallback": "AI SUMMARY UNAVAILABLE",
        "raw": "Raw data instead:",
        "levels": "KEY PRICE LEVELS",
        "calendar": "ECONOMIC CALENDAR",
        "headlines": "TOP HEADLINES",
        "scheduled": "scheduled run",
        "manual": "manual run",
        "late": "Note: GitHub started this scheduled run {minutes} minutes late.",
        "prev_day": "previous day",
        "prev_week": "previous week",
        "pos_above": "Price is above the {label} high by {x}",
        "pos_below": "Price is below the {label} low by {x}",
        "pos_inside": "Price is inside the {label} range",
    },
    "ar": {
        "weekly": "الملخص الأسبوعي",
        "daily": "الملخص اليومي",
        "disclaimer": "سيناريوهات مولّدة بالذكاء الاصطناعي للاطلاع فقط، وليست إشارات تداول.",
        "failed": "المصادر التي تعذّر جلبها في هذه المرة: ",
        "crashed": "تعذّر إنشاء الملخص: ",
        "fallback": "ملخص الذكاء الاصطناعي غير متاح",
        "raw": "البيانات الخام بدلاً منه:",
        "levels": "مستويات الأسعار الرئيسية",
        "calendar": "التقويم الاقتصادي",
        "headlines": "أبرز العناوين",
        "scheduled": "تشغيل مجدول",
        "manual": "تشغيل يدوي",
        "late": "ملاحظة: تأخّر GitHub في بدء هذا التشغيل المجدول {minutes} دقيقة.",
        "prev_day": "اليوم السابق",
        "prev_week": "الأسبوع السابق",
        "pos_above": "السعر أعلى من قمة {label} بمقدار {x}",
        "pos_below": "السعر أدنى من قاع {label} بمقدار {x}",
        "pos_inside": "السعر داخل نطاق {label}",
    },
}
TEXT = _TEXT[BRIEF_LANGUAGE]

# ------------------------------------------------------------------ sources

# Each calendar file is tried on two hosts: the feed's CDN sometimes lacks the next-week file on one of them.
CALENDAR_FILES = [
    ("ff_calendar_thisweek.json", [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
    ]),
    ("ff_calendar_nextweek.json", [
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
        "https://cdn-nfs.faireconomy.media/ff_calendar_nextweek.json",
    ]),
]

# display name -> (list of Yahoo tickers to try in order, decimals)
MARKETS = {
    "EUR/USD": (["EURUSD=X"], 5),
    "GBP/USD": (["GBPUSD=X"], 5),
    "USD/JPY": (["JPY=X", "USDJPY=X"], 3),
    "USD/CHF": (["CHF=X", "USDCHF=X"], 5),
    "AUD/USD": (["AUDUSD=X"], 5),
    "USD/CAD": (["CAD=X", "USDCAD=X"], 5),
    "NZD/USD": (["NZDUSD=X"], 5),
    "Gold XAU/USD (futures used as proxy)": (["GC=F", "XAUUSD=X"], 2),
    "US Dollar Index (DXY)": (["DX-Y.NYB"], 3),
}
PAIR_LIST = "EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD, USD/CAD, NZD/USD and Gold (XAU/USD)"

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
    ("Reserve Bank of Australia - media releases", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml", 4, False, True),
    ("Reserve Bank of Australia - speeches", "https://www.rba.gov.au/rss/rss-cb-speeches.xml", 4, False, True),
    ("Bank of Canada - press releases", "https://www.bankofcanada.ca/content_type/press-releases/feed/", 4, False, True),
    ("Bank of Canada - speeches", "https://www.bankofcanada.ca/content_type/speeches/feed/", 4, False, True),
    ("Swiss National Bank - press releases", "https://www.snb.ch/public/rss/en/pressrel", 4, False, True),
    ("Swiss National Bank - speeches", "https://www.snb.ch/public/rss/en/speeches", 4, False, True),
    ("Reserve Bank of New Zealand - news", "https://www.rbnz.govt.nz/feeds/news", 5, False, True),
    # forex-focused news and analysis (FXStreet was removed: it answers 403 to GitHub's servers)
    ("Action Forex", "https://www.actionforex.com/feed/", 12, False, False),
    ("MarketPulse (OANDA)", "https://www.marketpulse.com/feed/", 10, False, False),
    ("Google News - forex", gnews('forex OR "EUR/USD" OR "GBP/USD" OR "USD/JPY"'), 12, False, False),
    ("Google News - gold, Fed, yields", gnews('"gold price" OR "Federal Reserve" OR "Treasury yields"'), 12, False, False),
    ("Google News - AUD, NZD, CAD, CHF", gnews('"Australian dollar" OR "New Zealand dollar" OR "Canadian dollar" OR "Swiss franc"'), 10, False, False),
    # general economy / markets news
    ("CNBC - economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", 8, True, False),
    ("BBC - business", "https://feeds.bbci.co.uk/news/business/rss.xml", 8, True, False),
    ("The Guardian - economics", "https://www.theguardian.com/business/economics/rss", 8, True, False),
    ("MarketWatch - top stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories", 8, True, False),
]

NEWS_HOURS = 168 if MODE == "weekly" else 36
MAX_NEWS_LINES = 150
MAX_FULLTEXT = 6 if MODE == "weekly" else 5          # central bank pages to read per run
FULLTEXT_CHARS = int(os.getenv("FULLTEXT_CHARS") or 6000)  # excerpt size per page

KEYWORDS = re.compile(
    r"\b(inflation|cpi|ppi|gdp|jobs|payrolls?|unemployment|employment|wages?|retail sales|pmi|"
    r"interest rates?|rate (?:cut|cuts|hike|hikes|decision)|central bank|fed|fomc|powell|"
    r"ecb|lagarde|boe|bank of england|bailey|boj|bank of japan|ueda|yen|euro|sterling|pound|"
    r"dollar|treasury|yields?|bonds?|gold|oil|tariffs?|recession|economy|economic|trade|"
    r"currency|forex|stimulus|budget|deficit|debt|aussie|loonie|kiwi|franc|rba|rbnz|snb|boc|"
    r"bank of canada|reserve bank|swiss national bank)\b",
    re.I,
)

# Titles dropped before the per-source cap is applied (routine or off-topic items).
OFFICIAL_NOISE = re.compile(
    r"\b(accounts?|money stock|bonds? held|CV and questionnaire|simulation|banknotes?|"
    r"payment systems?|supervis\w*|discount rate|market operations|tripartite|"
    r"money markets? committee|consultation|penalt\w*|enforcement|AML|CFT|licen[cs]\w*|prudential|insur\w*|"
    r"SONIA|advisory group|working group|chief risk|risk officer|stress tests?)\b",
    re.I,
)
# Cross pairs (neither side is USD, e.g. GBP/JPY or AUD/NZD) are not on the watch list.
CROSS_PAIRS = re.compile(r"\b(?!USD)[A-Z]{3}/(?!USD)[A-Z]{3}\b")
SKIP_BY_SOURCE = {"Action Forex": CROSS_PAIRS}

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
    """Returns (events, failures); a failure reads like
    'calendar ff_calendar_nextweek.json (nfs: HTTPError 404; cdn-nfs: ConnectionError)'."""
    events, failures = [], []
    for name, urls in CALENDAR_FILES:
        errors = []
        for url in urls:
            host = url.split("/")[2].split(".")[0]
            try:
                data = http_get(url, timeout=30).json()
                if isinstance(data, list):
                    events.extend(data)
                    errors = []
                    break
                errors.append(f"{host}: unexpected format")
            except Exception as e:
                errors.append(f"{host}: {describe(e)}")
                print(f"Calendar fetch failed ({name} on {url.split('/')[2]}): {describe(e)}", file=sys.stderr)
        if errors:
            failures.append(f"calendar {name} ({'; '.join(errors)})")
    return events, failures


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


def format_events(chosen, cap=100):
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
    """Where the price sits against a range, worded in the brief's language."""
    if price > high:
        return TEXT["pos_above"].format(label=label, x=f"{price - high:.{dp}f}")
    if price < low:
        return TEXT["pos_below"].format(label=label, x=f"{low - price:.{dp}f}")
    return TEXT["pos_inside"].format(label=label)


def format_levels(name, ticker, daily, weekly, dp, today=None, last_price=None):
    """Key levels worked out here in code (not by the AI): last price, today so far,
    previous day, this week so far and previous week."""
    def f(x):
        return f"{float(x):.{dp}f}"

    tz = daily.index.tz
    today = today or (datetime.now(tz) if tz else datetime.now()).date()
    price = float(last_price) if last_price is not None else float(daily["Close"].iloc[-1])

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
        lines.append(f"    {where(price, prev_day['Low'], prev_day['High'], dp, TEXT['prev_day'])}")
    if this_week is not None:
        lines.append(f"  This week so far: high {f(this_week['High'])} | low {f(this_week['Low'])}")
    if prev_week is not None:
        lines.append(
            f"  Previous week ({prev_week_label}): high {f(prev_week['High'])} | "
            f"low {f(prev_week['Low'])} | close {f(prev_week['Close'])}"
        )
        lines.append(f"    {where(price, prev_week['Low'], prev_week['High'], dp, TEXT['prev_week'])}")
    lines.append("  Last 5 daily closes (oldest to newest): " + ", ".join(f(x) for x in daily["Close"].tail(5)))
    if not weekly.empty:
        lines.append("  Last weekly closes (oldest to newest): " + ", ".join(f(x) for x in weekly["Close"]))
    return "\n".join(lines)


def clean_candles(df):
    """Drop incomplete rows and flat placeholder candles (high equal to low) that Yahoo sometimes adds."""
    df = df.dropna()
    return df[df["High"] > df["Low"]]


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
                daily = clean_candles(t.history(period="1mo", interval="1d")).tail(10)
                if daily.empty:
                    continue
                weekly = clean_candles(t.history(period="3mo", interval="1wk")).tail(4)
                last_price = None
                try:  # the latest hourly close is the last price: it does not depend on how Yahoo cuts daily candles
                    hourly = t.history(period="5d", interval="1h").dropna()
                    if not hourly.empty:
                        last_price = float(hourly["Close"].iloc[-1])
                except Exception as e:
                    print(f"Hourly price fetch failed for {ticker}: {describe(e)}", file=sys.stderr)
                daily_close = float(daily["Close"].iloc[-1])
                if last_price is not None and abs(last_price - daily_close) / daily_close > 0.001:
                    print(f"{name}: last hourly close {last_price} differs from daily close {daily_close}", file=sys.stderr)
                block = format_levels(name, ticker, daily, weekly, dp, last_price=last_price)
                break
            except Exception as e:
                print(f"Price fetch failed for {ticker}: {type(e).__name__}", file=sys.stderr)
        blocks.append(block or f"{name}: price data unavailable.")
        time.sleep(0.5)  # be gentle with Yahoo
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
    """Shrink a long page to `budget` characters: the opening (for context), then the paragraphs
    richest in policy words, kept in their original order."""
    paras = [p.strip() for p in text.split("\n") if len(p.strip()) > 40]
    if sum(len(p) + 1 for p in paras) <= budget:
        return "\n".join(paras)

    def score(p):  # how many different policy words the paragraph uses
        return len({w.lower() for w in POLICY_WORDS.findall(p)})

    chosen, used = set(), 0
    for i, p in enumerate(paras):  # the opening, about 600 characters
        if used >= 600:
            break
        if used + len(p) + 1 <= budget:
            chosen.add(i)
            used += len(p) + 1
    for i in sorted((i for i in range(len(paras)) if i not in chosen), key=lambda i: (-score(paras[i]), i)):
        if score(paras[i]) == 0:
            break
        if used + len(paras[i]) + 1 <= budget:
            chosen.add(i)
            used += len(paras[i]) + 1
    if not chosen:
        return " ".join(paras)[:budget]
    return "\n".join(paras[i] for i in sorted(chosen))


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


def pdf_to_text(content, max_pages=12):
    """Text of a PDF, regrouped into paragraphs of about four sentences (PDF line breaks are not real paragraphs)."""
    if PdfReader is None:
        raise RuntimeError("pypdf not installed")
    reader = PdfReader(io.BytesIO(content))
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:max_pages])
    text = re.sub(r"-\n(?=[a-z])", "", text)   # re-join words split at the end of a line
    text = re.sub(r"\s*\n\s*", " ", text)      # line breaks become spaces
    sentences = re.split(r"""(?<=[.!?])\s+(?=[A-Z"'])""", text)
    return "\n".join(" ".join(sentences[i:i + 4]) for i in range(0, len(sentences), 4))


def find_pdf_link(page_html, page_url, strict):
    """The PDF a short page links to. Bank of Japan speech pages only hold a link to the real text as a PDF.
    strict=True accepts only a PDF whose file name contains the page's own name (ko261015a.htm -> ko261015a.pdf)."""
    urls = []
    for href in re.findall(r"""href=["']([^"'#?]+\.pdf)(?:[?#][^"']*)?["']""", page_html, re.I):
        full = urljoin(page_url, href)
        if full not in urls:
            urls.append(full)
    stem = re.search(r"/([^/]+?)\.html?(?:$|[?#])", page_url)
    if stem:
        for u in urls:
            if stem.group(1) in u:
                return u
    if not strict and len(urls) == 1:
        return urls[0]
    return None


def fetch_page_text(link):
    """Readable text of a web page or PDF; '' if nothing useful could be read."""
    r = http_get(link, timeout=45)
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or link.lower().split("?")[0].endswith(".pdf"):
        return pdf_to_text(r.content)
    text = ""
    if not ctype or "html" in ctype:
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
    pdf_url = find_pdf_link(r.text, link, strict=len(text) >= 1500)
    if pdf_url:  # the page is short, or it has a PDF with its own name: that is the real text
        try:
            alt = pdf_to_text(http_get(pdf_url, timeout=45).content)
            if len(alt) > len(text):
                return alt
        except Exception as e:
            print(f"Linked PDF failed ({pdf_url.rsplit('/', 1)[-1]}): {describe(e)}", file=sys.stderr)
    return text


def fetch_fulltext(candidates, limit):
    docs = []
    for published, name, title, link in candidates:
        if len(docs) >= limit:
            break
        try:
            text = fetch_page_text(link)
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

TRADER = (
    "You are a market analyst writing a briefing for a retail forex and gold trader. "
    f"The watch list is {PAIR_LIST}, with the Dollar Index (DXY) for USD context. "
    "Use general trading concepts: macro drivers and central bank policy, data surprises against "
    "forecast, risk sentiment, yields, currency correlations, and support and resistance taken "
    "from the previous day and previous week highs and lows listed under KEY PRICE LEVELS."
)

RULES = """RULES
- Use ONLY the data below for dates, events, forecasts, statements, headlines and price levels. Never invent numbers.
- KEY PRICE LEVELS were calculated by the script. Copy those numbers exactly, keep their labels (today so far, previous day, previous week) and never work out your own highs or lows. Do not invent any other chart levels.
- If a data block says it is unavailable, say so plainly and do not fill the gap.
- If the ECONOMIC CALENDAR block says it is incomplete or unavailable, tell the reader the calendar could not be checked; never say that no events are scheduled.
- CENTRAL BANK FULL TEXT holds excerpts of official pages; some paragraphs are skipped. Base statements about what officials said on these excerpts only, and never assume what skipped parts said. Quote at most a short phrase (under 15 words).
- Cite news by source name (for example Reuters, ECB, BBC). Headlines are claims made by those sources, not verified facts; never add details beyond the headline and summary. If sources disagree, say so.
- Anything that is not from the data must be labelled "general context".
- Give scenarios, not predictions or trade signals. Never tell the reader to buy or sell.
- Output is plain text for Telegram: no markdown symbols (no asterisks, hashes or backticks). Put each section heading on its own line and use "-" bullets."""

LANGUAGE_RULE_AR = """
- LANGUAGE: write the whole brief in clear Modern Standard Arabic, including the section headings. Keep currency pair names (such as EUR/USD), numbers, clock times, links and source names in Latin characters and digits 0-9. Write day and month names in Arabic. Give each central bank's name in Arabic with its abbreviation in brackets the first time, for example البنك المركزي الأوروبي (ECB). Translate every English phrase in the data into Arabic; only pair names, numbers, clock times, links and source names stay in Latin characters. Write "Bullish" as صعودي and "Bearish" as هبوطي; write "invalidated below X" as يُلغى السيناريو عند الهبوط تحت X, and "invalidated above X" as يُلغى السيناريو عند الصعود فوق X."""

INSTRUMENT_FORMAT = """one block per instrument (EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD, USD/CAD, NZD/USD, Gold), then one line for the Dollar Index. Use exactly this format for each block:
NAME - last price; where it sits versus the previous day and previous week (copy from KEY PRICE LEVELS)
- Bullish: the driver (from the events, central bank text or news); invalidated below LEVEL
- Bearish: the driver; invalidated above LEVEL
- Levels: previous day high / low, previous week high / low
LEVEL must be copied from KEY PRICE LEVELS: LEVEL_RULE
If the data holds no specific driver for a case, write "no specific driver in the data" instead of a generic reason; anything generic must be labelled general context."""

WEEKLY_LEVEL_RULE = "use the previous week low for the bullish case and the previous week high for the bearish case (this is a week-ahead brief)."
DAILY_LEVEL_RULE = "use the previous day low for the bullish case and the previous day high for the bearish case (this is a daily brief)."

WEEKLY_TASK = """TASK: Write the WEEKLY brief for the week ahead. Maximum 950 words. Sections:
1. WEEK AHEAD - the 6 to 8 events that matter most. One line each: day, time, currency, event name, then "forecast X vs previous Y" when the calendar gives them, then why it matters for the watch list (what a result above or below forecast would mean for that currency).
2. CENTRAL BANKS - what officials actually said in the FULL TEXT excerpts: tone (hawkish, dovish or neutral), hints about the rate path, the data they emphasise, anything surprising. Name the institution and add the link for each item you rely on. Central banks with no relevant text: do not give each its own line, list them together in one short line.
3. NEWS THEMES - 2 to 4 market themes from the headlines, naming the sources.
4. INSTRUMENTS - """ + INSTRUMENT_FORMAT.replace("LEVEL_RULE", WEEKLY_LEVEL_RULE) + """
5. NEWS RISK WINDOWS - times to be careful trading, with the event names.
6. ONE-LINE SUMMARY."""

DAILY_TASK = """TASK: Write the DAILY pre-London-open brief for today. Maximum 650 words. Sections:
1. TODAY'S EVENTS - one line each: time, currency, event name, then "forecast X vs previous Y" when the calendar gives them, then why it matters for the watch list (say so if there are none).
2. CENTRAL BANKS AND OVERNIGHT NEWS - what officials said in the FULL TEXT excerpts (with the link for items you rely on) plus the 2 to 4 headlines that matter most, naming sources. Group central banks with nothing relevant into one short line.
3. INSTRUMENTS - """ + INSTRUMENT_FORMAT.replace("LEVEL_RULE", DAILY_LEVEL_RULE) + """ Keep each driver to one short phrase.
4. NEWS RISK WINDOWS.
5. ONE-LINE SUMMARY."""


def build_prompt(now, calendar_text, fulltext_text, news_text, markets_text):
    task = WEEKLY_TASK if MODE == "weekly" else DAILY_TASK
    rules = RULES + (LANGUAGE_RULE_AR if BRIEF_LANGUAGE == "ar" else "")
    return (
        f"{TRADER}\n\n{task}\n\n{rules}\n\n"
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


GEMINI_DEADLINE_S = 420  # total time budget for the AI step, so a slow day cannot hit the workflow's 15-minute limit


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
                    timeout=(15, 120),  # 15 s to connect, 120 s to wait for the answer
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
        f"{TEXT['fallback']} ({str(exc)[:300]})\n"
        f"{TEXT['raw']}\n\n"
        f"{TEXT['levels']}\n{markets_text}\n\n"
        f"{TEXT['calendar']}\n{calendar_text}\n\n"
        f"{TEXT['headlines']}\n" + "\n".join(picked[:30])
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


# ------------------------------------------------------------- run timing

def run_label():
    """'scheduled run' or 'manual run', so the message itself shows how it was started."""
    event = os.getenv("GITHUB_EVENT_NAME")
    return {"schedule": TEXT["scheduled"], "workflow_dispatch": TEXT["manual"]}.get(event, "")


def lateness_note(now):
    """For scheduled runs: how late GitHub started the run (the cron time comes from the workflow file)."""
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return ""
    cron = (os.getenv("CRON") or "").split()
    if len(cron) < 2 or not (cron[0].isdigit() and cron[1].isdigit()):
        return ""
    now_utc = now.astimezone(timezone.utc)
    planned = now_utc.replace(hour=int(cron[1]), minute=int(cron[0]), second=0, microsecond=0)
    late_minutes = int((now_utc - planned).total_seconds() // 60)
    return TEXT["late"].format(minutes=late_minutes) if late_minutes > 45 else ""


# ---------------------------------------------------------------------- main

def main():
    now = datetime.now(TZ)
    problems = []

    raw, calendar_failures = fetch_calendar()
    if raw:
        chosen = select_events(raw, now)
        print(f"Calendar: {len(raw)} events fetched, {len(chosen)} selected.")
        # The next-week file only matters when the window reaches into next week. A daily brief never needs it,
        # and on Sunday the this-week file already holds the whole coming week.
        if MODE == "daily" or (now.weekday() == 6 and chosen):
            calendar_failures = [f for f in calendar_failures if "nextweek" not in f]
        if chosen:
            calendar_text = format_events(chosen)
            if calendar_failures:
                calendar_text += "\nNOTE: part of the calendar feed could not be loaded, so later events may be missing."
        elif calendar_failures:
            calendar_text = (
                "CALENDAR INCOMPLETE: no events were found in this window and part of the calendar feed "
                "could not be loaded. Tell the reader the calendar could not be fully checked; "
                "do not say that no events are scheduled."
            )
        else:
            calendar_text = format_events(chosen)
    else:
        calendar_text = "CALENDAR UNAVAILABLE (fetch failed). Do not guess events."
    problems.extend(calendar_failures)

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

    title = TEXT[MODE]
    date_label = f"{now:%Y-%m-%d}" if BRIEF_LANGUAGE == "ar" else f"{now:%a %d %b %Y}"
    label = run_label()
    header = f"{title} - {date_label} {now:%H:%M}" + (f" ({label})" if label else "")
    late = lateness_note(now)
    if late:
        header += "\n" + late
    footer = TEXT["disclaimer"]
    if problems:
        footer += "\n" + TEXT["failed"] + ", ".join(problems)
    send_telegram(f"{header}\n\n{analysis}\n\n{footer}")
    print("Sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            send_telegram(f"{TEXT['crashed']}{str(exc)[:300]}")
        except Exception:
            pass
        sys.exit(1)
