"""
Fetches latest news for all tracked SET stocks from two sources:
  1. yfinance   — English news tied directly to each ticker
  2. gnews       — Thai-language news per ticker + Thai macro queries

Applies rule-based sentiment analysis (English + Thai keywords) and
writes frontend/public/news_data.json.

Run:  python news_pipeline.py
Schedule 3x daily (08:00 / 12:00 / 18:00) via Windows Task Scheduler.
"""
import json
import re
import uuid
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("Missing dependency — run: pip install yfinance")

try:
    from gnews import GNews
    _GNEWS_OK = True
except ImportError:
    _GNEWS_OK = False
    print("[INFO] gnews not installed — Thai news disabled. Run: pip install gnews")

RELATION_DIR = Path(__file__).parent / "Relation"
OUTPUT       = Path(__file__).parent / "frontend" / "public" / "news_data.json"

# ── Thai macro search queries (lang, query) ───────────────────────────────────

THAI_MACRO_QUERIES = [
    ("th", "ตลาดหุ้น SET index"),
    ("th", "กนง อัตราดอกเบี้ย นโยบาย"),
    ("th", "เศรษฐกิจไทย GDP"),
    ("th", "ราคาน้ำมัน ไทย"),
    ("th", "ค่าเงินบาท ดอลลาร์"),
    ("th", "นักท่องเที่ยว ไทย"),
    ("th", "หุ้นไทย วันนี้"),
    ("en", "SET index Thailand stock market"),
    ("en", "Thailand economy baht interest rate"),
    ("en", "Thailand stocks today"),
]

# ── English sentiment keywords ────────────────────────────────────────────────

POSITIVE_WORDS = frozenset({
    "surge", "surges", "surged", "surging",
    "rise", "rises", "rose", "risen", "rising",
    "gain", "gains", "gained", "gaining",
    "profit", "profits", "profitable",
    "growth", "grew", "grow", "growing",
    "beat", "beats", "beating",
    "record", "records",
    "increase", "increases", "increased", "increasing",
    "boost", "boosts", "boosted", "boosting",
    "strong", "stronger", "strength",
    "rally", "rallied", "rallying",
    "approve", "approved", "approval", "approves",
    "positive", "upgrade", "upgraded", "upgrading",
    "expand", "expansion", "expanding",
    "jump", "jumped", "jumping",
    "soar", "soared", "soaring",
    "outperform", "outperformed",
    "exceed", "exceeded", "exceeding",
    "recovery", "recover", "recovered", "recovering",
    "optimistic", "opportunity", "upside",
    "dividend", "dividends",
    "inflow", "investment",
})

NEGATIVE_WORDS = frozenset({
    "fall", "falls", "fell", "fallen", "falling",
    "drop", "drops", "dropped", "dropping",
    "loss", "losses", "losing",
    "decline", "declines", "declined", "declining",
    "miss", "misses", "missed", "missing",
    "decrease", "decreases", "decreased", "decreasing",
    "cut", "cuts", "cutting",
    "weak", "weakness", "weaker",
    "crash", "crashes", "crashed", "crashing",
    "slump", "slumped", "slumping",
    "warning", "warnings", "warn",
    "negative", "downgrade", "downgraded", "downgrading",
    "risk", "risks", "risky",
    "concern", "concerns",
    "debt", "default", "defaults",
    "sink", "sank", "sinking",
    "plunge", "plunged", "plunging",
    "layoff", "layoffs",
    "underperform", "underperformed",
    "disappoint", "disappointed", "disappointing",
    "pressure", "pressured",
    "headwind", "headwinds",
    "selloff", "sell-off",
    "slowing", "slowdown",
})

# ── Thai sentiment keywords (substring match — Thai has no word spaces) ───────

THAI_POSITIVE = frozenset({
    "เพิ่มขึ้น", "กำไร", "บวก", "ฟื้นตัว", "แข็งค่า",
    "ขยายตัว", "เติบโต", "แข็งแกร่ง", "ดีขึ้น", "ปรับตัวขึ้น",
    "สูงขึ้น", "กระโดด", "ทะลุ", "ทำสถิติ", "ลงทุน",
    "เชิงบวก", "ฟื้น", "แรลลี่", "พุ่ง", "ขาขึ้น",
})

THAI_NEGATIVE = frozenset({
    "ลดลง", "ขาดทุน", "ลบ", "อ่อนค่า", "หดตัว",
    "ต่ำลง", "ร่วง", "ดิ่ง", "เสี่ยง", "กังวล",
    "ปรับตัวลง", "ขาลง", "เทขาย", "ตลาดหมี", "วิกฤต",
    "กดดัน", "อ่อนแอ", "ขาดทุน", "หนี้", "ผิดนัด",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    """Lowercase + strip punctuation — used as a secondary dedup key."""
    return re.sub(r"\W+", "", title.lower())


def analyze_sentiment(text: str) -> tuple[str, float]:
    """English word-split + Thai substring voting."""
    words = [w.strip(".,!?:;()[]\"'") for w in text.lower().split()]
    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)

    # Thai keywords: substring match
    pos += sum(1 for kw in THAI_POSITIVE if kw in text)
    neg += sum(1 for kw in THAI_NEGATIVE if kw in text)

    total = pos + neg
    if total == 0:
        return "NEUTRAL", 0.5
    ratio = pos / total
    if ratio >= 0.60:
        return "POSITIVE", round(ratio, 2)
    if ratio <= 0.40:
        return "NEGATIVE", round(1 - ratio, 2)
    return "NEUTRAL", 0.5


def _parse_rfc2822(date_str: str) -> int:
    """Parse RFC 2822 date string ('Mon, 24 Apr 2026 08:00:00 GMT') to Unix ts."""
    if not date_str:
        return 0
    try:
        return int(parsedate_to_datetime(date_str).timestamp())
    except Exception:
        return 0


def load_stock_data() -> dict[str, dict]:
    stocks: dict[str, dict] = {}
    for path in sorted(RELATION_DIR.glob("*.json")):
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for entity in data.get("entities", []):
            ticker = entity.get("ticker", "")
            if not ticker:
                continue
            stocks[ticker] = {"name": entity.get("name", ticker)}
    return stocks


# ── yfinance parser (handles v0.1 flat + v0.2 nested 'content') ──────────────

def _parse_yf_item(raw: dict) -> dict | None:
    content = raw.get("content") or raw

    title   = content.get("title", "")
    summary = content.get("summary") or content.get("description", "")

    url = ""
    for key in ("canonicalUrl", "clickThroughUrl"):
        obj = content.get(key)
        if isinstance(obj, dict):
            url = obj.get("url", "")
        if url:
            break
    if not url:
        url = content.get("link") or content.get("url", "")
    if not url or not title:
        return None

    pub_ts = 0
    pub_raw = (content.get("pubDate") or content.get("displayTime") or
               raw.get("providerPublishTime") or raw.get("published"))
    if pub_raw:
        if isinstance(pub_raw, (int, float)):
            pub_ts = int(pub_raw)
        elif isinstance(pub_raw, str):
            try:
                pub_ts = int(datetime.fromisoformat(
                    pub_raw.replace("Z", "+00:00")
                ).timestamp())
            except ValueError:
                pass

    source = ""
    provider = content.get("provider")
    if isinstance(provider, dict):
        source = provider.get("displayName") or provider.get("name", "")
    if not source:
        pub = raw.get("publisher")
        if isinstance(pub, dict):
            source = pub.get("name") or pub.get("title", "")
        elif isinstance(pub, str):
            source = pub

    thumbnail = ""
    thumb = content.get("thumbnail") or raw.get("thumbnail")
    if isinstance(thumb, dict):
        res = thumb.get("resolutions", [])
        if res and isinstance(res[0], dict):
            thumbnail = res[0].get("url", "")
        if not thumbnail:
            thumbnail = thumb.get("originalUrl", "")

    return {
        "title": title, "summary": summary, "url": url,
        "pub_ts": pub_ts, "source": source, "thumbnail": thumbnail,
    }


# ── Affected-stocks builder ───────────────────────────────────────────────────

def build_affected_stocks(
    primary: str | None,
    related: list[str],
    sentiment: str,
    known: set[str],
) -> list[dict]:
    direction = ("POSITIVE" if sentiment == "POSITIVE" else
                 "NEGATIVE" if sentiment == "NEGATIVE" else "NEUTRAL")
    result = []
    seen: set[str] = set()

    if primary:
        result.append({
            "ticker":           primary,
            "impact_direction": direction,
            "impact_weight":    1.0,
            "impact_reason":    "Directly covered by this article",
        })
        seen.add(primary)

    for t in related:
        if t in seen or t not in known:
            continue
        seen.add(t)
        result.append({
            "ticker":           t,
            "impact_direction": direction,
            "impact_weight":    0.5,
            "impact_reason":    "Related ticker",
        })
    return result


# ── Source 1: yfinance ────────────────────────────────────────────────────────

def fetch_yfinance_news(
    stock_data: dict,
    seen_urls: set[str],
    seen_titles: set[str],
) -> list[dict]:
    known = set(stock_data.keys())
    tickers = list(stock_data.keys())
    articles: list[dict] = []

    print(f"[yfinance] Fetching {len(tickers)} tickers...")
    for i, ticker in enumerate(tickers):
        try:
            raw_news = yf.Ticker(ticker).news or []
        except Exception as exc:
            print(f"  [WARN] {ticker}: {exc}")
            raw_news = []

        for raw in raw_news:
            parsed = _parse_yf_item(raw)
            if not parsed:
                continue
            url   = parsed["url"]
            ntitle = _normalize_title(parsed["title"])
            if url in seen_urls or ntitle in seen_titles:
                continue
            seen_urls.add(url)
            seen_titles.add(ntitle)

            text = f"{parsed['title']} {parsed['summary']}"
            sentiment, score = analyze_sentiment(text)
            related = [
                t for t in raw.get("relatedTickers", [])
                if isinstance(t, str) and t in known and t != ticker
            ]

            articles.append({
                "id":              str(uuid.uuid4()),
                "ticker_source":   ticker,
                "title":           parsed["title"],
                "summary":         parsed["summary"],
                "url":             url,
                "published_at":    parsed["pub_ts"],
                "source":          parsed["source"],
                "thumbnail":       parsed["thumbnail"],
                "lang":            "en",
                "sentiment":       sentiment,
                "sentiment_score": score,
                "affected_stocks": build_affected_stocks(ticker, related, sentiment, known),
            })

        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(tickers)} processed...")
        time.sleep(0.08)

    print(f"  -> {len(articles)} articles from yfinance")
    return articles


# ── Source 2: gnews ───────────────────────────────────────────────────────────

def _gnews_instance(lang: str) -> "GNews":
    return GNews(language=lang, country="TH", max_results=5)


def _parse_gnews_item(item: dict, ticker: str | None, sentiment: str, known: set[str]) -> dict:
    pub = item.get("publisher", {})
    source = pub.get("title", "") if isinstance(pub, dict) else ""
    pub_ts = _parse_rfc2822(item.get("published date", ""))
    title  = item.get("title", "")
    desc   = item.get("description", "")

    return {
        "id":              str(uuid.uuid4()),
        "ticker_source":   ticker or "MACRO",
        "title":           title,
        "summary":         desc,
        "url":             item.get("url", ""),
        "published_at":    pub_ts,
        "source":          source,
        "thumbnail":       "",
        "lang":            "th",
        "sentiment":       sentiment,
        "sentiment_score": 0.5,
        "affected_stocks": build_affected_stocks(ticker, [], sentiment, known),
    }


def fetch_gnews_news(
    stock_data: dict,
    seen_urls: set[str],
    seen_titles: set[str],
) -> list[dict]:
    if not _GNEWS_OK:
        return []

    known = set(stock_data.keys())
    articles: list[dict] = []
    gn_cache: dict[str, "GNews"] = {}

    def get_gn(lang: str) -> "GNews":
        if lang not in gn_cache:
            gn_cache[lang] = _gnews_instance(lang)
        return gn_cache[lang]

    def add_items(items: list[dict], ticker: str | None) -> None:
        for item in items:
            url   = item.get("url", "")
            ntitle = _normalize_title(item.get("title", ""))
            if not url or not ntitle:
                continue
            if url in seen_urls or ntitle in seen_titles:
                continue
            seen_urls.add(url)
            seen_titles.add(ntitle)

            text = f"{item.get('title', '')} {item.get('description', '')}"
            sentiment, _ = analyze_sentiment(text)
            articles.append(_parse_gnews_item(item, ticker, sentiment, known))

    # ── Per-ticker Thai news ──────────────────────────────────────────────────
    tickers = list(stock_data.keys())
    print(f"[gnews] Per-ticker Thai search ({len(tickers)} tickers)...")
    for i, ticker in enumerate(tickers):
        base = ticker.replace(".BK", "").replace(".TH", "")
        try:
            items = get_gn("th").get_news(base) or []
        except Exception as exc:
            print(f"  [gnews WARN] {ticker}: {exc}")
            items = []
        add_items(items, ticker)

        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(tickers)} processed...")
        time.sleep(0.6)

    # ── Macro / market-wide Thai + English queries ────────────────────────────
    print(f"[gnews] Macro queries ({len(THAI_MACRO_QUERIES)} queries)...")
    for lang, query in THAI_MACRO_QUERIES:
        try:
            items = get_gn(lang).get_news(query) or []
        except Exception as exc:
            print(f"  [gnews macro WARN] '{query}': {exc}")
            items = []
        add_items(items, None)  # no specific ticker for macro news
        time.sleep(0.6)

    print(f"  -> {len(articles)} articles from gnews")
    return articles


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    stock_data = load_stock_data()
    print(f"Loaded {len(stock_data)} stocks from Relation/\n")

    seen_urls:   set[str] = set()
    seen_titles: set[str] = set()

    yf_articles    = fetch_yfinance_news(stock_data, seen_urls, seen_titles)
    gnews_articles = fetch_gnews_news(stock_data, seen_urls, seen_titles)

    all_articles = yf_articles + gnews_articles
    all_articles.sort(key=lambda x: x["published_at"], reverse=True)

    print(f"\nTotal unique articles: {len(all_articles)}")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "news": all_articles,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    counts: dict[str, int] = {}
    langs:  dict[str, int] = {}
    for a in all_articles:
        counts[a["sentiment"]] = counts.get(a["sentiment"], 0) + 1
        langs[a["lang"]]       = langs.get(a["lang"], 0) + 1

    print(f"Done -> {OUTPUT}")
    print(f"  + Positive: {counts.get('POSITIVE', 0)}")
    print(f"  - Negative: {counts.get('NEGATIVE', 0)}")
    print(f"  ~ Neutral:  {counts.get('NEUTRAL', 0)}")
    print(f"  EN: {langs.get('en', 0)}  TH: {langs.get('th', 0)}")


if __name__ == "__main__":
    main()
