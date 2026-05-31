"""Bloomberg Television (@markets) — video show fetcher.
Target shows:
  - Bloomberg Surveillance (Jon, Lisa, Annmarie)
  - Bloomberg This Weekend
  - Stock Market News and Analysis
"""
import os, re, json, datetime, logging
from show_fetcher import (run_fetch, fetch_rss, scrape_video_page,
                          extract_stocks)
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

CHANNEL_ID = "UCIALMKvObZNtJ6AmdCLP7Lg"   # Bloomberg Television
DATA_FILE  = os.path.join(os.path.dirname(__file__), "static", "bloomberg.json")
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

# Common words to skip for US ticker detection
_NOT_TICKERS = {
    "THE","AND","FOR","NOT","WITH","ARE","HAS","WAS","ITS","ALL","CAN","MAY",
    "INC","LLC","LTD","CEO","CFO","IPO","GDP","CPI","FED","USA","ETF","AIR",
    "FULL","LIVE","SHOW","NEWS","THIS","THAT","WILL","FROM","BEEN","THEY",
    "WHEN","WHAT","THAN","THEN","INTO","OVER","AFTER","BULL","BEAR","JUST",
    "HIGH","HITS","NEXT","LAST","NEAR","RATE","DEAL","BOOM","GAIN","LOSS",
    "RISE","FALL","SELL","HOLD","PLUS","STAY","MOVE","TAKE","KEEP","MAKE",
    "REAL","BEST","GOOD","LONG","EVEN","ONLY","MUCH","MANY","MOST","SOME",
    "BOTH","SUCH","SAME","MORE","LESS","VERY","WELL","HARD","FAST","SLOW",
    "EARN","BEAT","MISS","PLAN","TECH","FLAG","BASE","BIG","NEW","OLD","HOW",
    "WHO","HAVE","SAID","AMID","AMID","ALSO","BACK","YEAR","WEEK","INTO",
    "PLUS","AMID","AMID","AMID","AMID","AMID","AMID","AMID","AMID","AMID",
    # Bloomberg-specific non-tickers
    "IRAN","IRAQ","ASIA","EURO","GOLD","BUND","BOND","REPO","SWAP","SPOT",
    "RISK","DEBT","BANK","FUND","FIRM","DEAL","CUTS","HIKE","JOBS","DATA",
    "CORE","SOFT","HARD","FLAT","LATE","WIDE","OPEN","HALF","FULL","PEAK",
    "DIVE","JUMP","SLIP","DROP","PUSH","PULL","DRAG","LIFT","EASE","HOLD",
    "NEAR","PAST","JUST","ONLY","EVEN","ALSO","WELL","VERY","MUCH","MOST",
    "AMID","AMID","AMID","AMID","AMID","US","UK","EU","UN","AI",
}

# Company name → ticker for Bloomberg frequent names
_COMPANY_MAP = {
    "Apple":"AAPL", "Microsoft":"MSFT", "Nvidia":"NVDA", "Amazon":"AMZN",
    "Google":"GOOGL", "Alphabet":"GOOGL", "Meta":"META", "Tesla":"TSLA",
    "Berkshire":"BRK-B", "JPMorgan":"JPM", "Goldman":"GS", "Morgan Stanley":"MS",
    "Cisco":"CSCO", "Intel":"INTC", "AMD":"AMD", "Broadcom":"AVGO",
    "Qualcomm":"QCOM", "Salesforce":"CRM", "Oracle":"ORCL", "IBM":"IBM",
    "Boeing":"BA", "Nike":"NKE", "Disney":"DIS", "Netflix":"NFLX",
    "Palantir":"PLTR", "Snowflake":"SNOW", "CrowdStrike":"CRWD",
    "Coinbase":"COIN", "Block":"SQ", "Robinhood":"HOOD",
    "ExxonMobil":"XOM", "Chevron":"CVX", "ConocoPhillips":"COP",
}

def _extract_bloomberg_tickers(title, desc=""):
    """Extract US stock tickers from Bloomberg-style titles/descriptions."""
    us = set()
    text = title + " " + desc

    # Pattern 1: Explicit $ tickers
    for tok in re.findall(r'\$([A-Z]{2,5})\b', text):
        if tok not in _NOT_TICKERS:
            us.add(tok)

    # Pattern 2: Uppercase 2-5 letter words that look like tickers
    # Bloomberg often writes "Cisco", "Nvidia" etc. (capitalized, not all-caps)
    # so extract from known company map first
    for company, ticker in _COMPANY_MAP.items():
        if company.lower() in text.lower():
            us.add(ticker)

    # Pattern 3: ALL-CAPS tickers in title (less common in Bloomberg)
    for tok in re.findall(r'\b([A-Z]{2,5})\b', title):
        if tok not in _NOT_TICKERS and len(tok) >= 2:
            us.add(tok)

    # Pattern 4: "Stock Surges/Falls" context — try to get ticker before it
    for m in re.finditer(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+(?:Stock|Shares?|Surge|Soar|Fall|Drop|Climb|Rally)', text):
        company_name = m.group(1)
        for c, t in _COMPANY_MAP.items():
            if c.lower() == company_name.lower():
                us.add(t)

    return sorted(us)


def run_daily_fetch():
    """Fetch Bloomberg videos with US ticker extraction."""
    log.info("Bloomberg: fetching channel %s", CHANNEL_ID)

    existing = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                old = json.load(f)
            existing = {v["id"]: v for v in old.get("videos", [])}
        except Exception:
            pass

    rss_videos = fetch_rss(CHANNEL_ID, 15)
    cached_vids = [v for v in rss_videos if existing.get(v["id"])]
    new_vids    = [v for v in rss_videos if not existing.get(v["id"])]
    log.info("Bloomberg: %d cached, %d new", len(cached_vids), len(new_vids))

    def _scrape(v):
        details  = scrape_video_page(v["id"])
        all_text = v["title"] + " " + " ".join(details.get("hashtags", []))
        desc     = " ".join(details.get("desc_lines", []))
        us = _extract_bloomberg_tickers(all_text, desc)
        return {**v, **details, "tw_stocks": [], "us_stocks": us, "gemini": None}

    scraped = {}
    if new_vids:
        with ThreadPoolExecutor(max_workers=min(8, len(new_vids))) as ex:
            futs = {ex.submit(_scrape, v): v["id"] for v in new_vids}
            for fut in as_completed(futs):
                vid_id = futs[fut]
                try:
                    scraped[vid_id] = fut.result()
                    existing[vid_id] = scraped[vid_id]
                except Exception as e:
                    log.warning("Bloomberg scrape failed %s: %s", vid_id, e)

    results = [scraped.get(v["id"]) or existing.get(v["id"]) or v for v in rss_videos]

    all_videos = list({v["id"]: v for v in existing.values()}.values())
    all_videos.sort(key=lambda x: x.get("pub_ts", 0) or x.get("date", ""), reverse=True)
    all_videos = all_videos[:30]

    payload = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "videos": all_videos}
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)
    log.info("Bloomberg: saved %d videos", len(all_videos))
    return payload


def summarize_bloomberg(video_url: str):
    """Bloomberg-specific Gemini summary using English prompt."""
    if not GEMINI_KEY:
        return None
    # Check duration — Bloomberg full shows (Surveillance etc.) are 2-3hrs,
    # which exceed Gemini's 1M token limit. Skip if > 60 minutes.
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                data = __import__('json').load(f)
            vid_id = video_url.split("v=")[-1]
            for v in data.get("videos", []):
                if v["id"] == vid_id:
                    dur = v.get("duration", "")
                    if dur and dur.count(":") >= 2:   # H:MM:SS format = over 1 hour
                        log.warning("Bloomberg video %s too long (%s), skipping Gemini", vid_id, dur)
                        return {"error": "too_long",
                                "message": f"影片過長（{dur}），超過 Gemini 分析上限（1小時）。請選擇較短的片段影片。",
                                "stocks": [], "market": [], "key_points": [], "sentiment": "N/A"}
                    break
        except Exception:
            pass

    try:
        from google import genai
        from google.genai import types as gtypes
        client = genai.Client(api_key=GEMINI_KEY)
        prompt = """Analyze this Bloomberg Television video and return a JSON summary:

{
  "stocks": [
    {"code": "TICKER (2-5 uppercase letters, US stock symbol)", "name": "Company Name",
     "view": "bullish/bearish/neutral", "target": "price target if mentioned or null",
     "note": "brief note"}
  ],
  "market": ["key market insight 1", "key insight 2", "key insight 3"],
  "key_points": ["most important investment point 1", "point 2", "point 3", "point 4", "point 5"],
  "sentiment": "optimistic/cautious/neutral"
}

Only include stocks explicitly mentioned. Use 2-5 letter uppercase US ticker symbols only.
Respond with valid JSON only, no markdown formatting."""

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=gtypes.Content(parts=[
                gtypes.Part(file_data=gtypes.FileData(
                    file_uri=video_url, mime_type="video/*"
                )),
                gtypes.Part(text=prompt)
            ])
        )
        raw = response.text.strip()
        raw = __import__('re').sub(r'^```(?:json)?\s*', '', raw, flags=8)
        raw = __import__('re').sub(r'\s*```$', '', raw, flags=8)
        result = __import__('json').loads(raw)
        result["raw"] = response.text
        log.info("Bloomberg Gemini OK for %s", video_url)
        return result
    except Exception as e:
        err_str = str(e)
        if "token" in err_str.lower() and "exceed" in err_str.lower():
            log.warning("Bloomberg Gemini token limit: %s", err_str[:100])
            return {"error": "too_long",
                    "message": "影片過長，超過 Gemini 分析上限（1小時）。請選擇較短的片段影片。",
                    "stocks": [], "market": [], "key_points": [], "sentiment": "N/A"}
        log.warning("Bloomberg Gemini failed: %s", err_str[:200])
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_daily_fetch()
    print(f"\n✓ Fetched {len(result['videos'])} videos")
    for v in result["videos"][:5]:
        print(f"\n[{v['date']}] {v['title'][:65]}")
        print(f"  US: {v.get('us_stocks')}")
        if v.get("chapters"):
            print(f"  Chapters: {[c['title'][:30] for c in v['chapters'][:2]]}")
