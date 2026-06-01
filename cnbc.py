"""CNBC Television (@CNBCtelevision) — Mad Money, Fast Money, Halftime Report."""
import os, re, json, datetime, logging
from show_fetcher import (run_fetch, fetch_rss, scrape_video_page, extract_stocks)
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

CHANNEL_ID   = "UCrp_UI8XtuYfpiqluWLD7Lw"   # CNBC Television
DATA_FILE    = os.path.join(os.path.dirname(__file__), "static", "cnbc.json")
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

PLAYLISTS = [
    {"id": "PLVbP054jv0KpV2leJ9HHIMqZEkCPX-iPV",
     "category": "Mad Money",
     "icon": "🔥"},
    {"id": "PLVbP054jv0KprCik69kaho7DjutkRPqzd",
     "category": "Fast Money",
     "icon": "⚡"},
    {"id": "PLVbP054jv0KrF5ekWCH7TCBtsr62jGSLV",
     "category": "Halftime Report",
     "icon": "📊"},
]

_NOT_TICKERS = {
    "THE","AND","FOR","NOT","WITH","ARE","HAS","WAS","ITS","ALL","CAN","MAY",
    "INC","LLC","LTD","CEO","CFO","IPO","GDP","CPI","FED","USA","ETF","AIR",
    "CNBC","THIS","THAT","WILL","FROM","BEEN","THEY","WHEN","WHAT","THAN",
    "THEN","INTO","OVER","AFTER","BULL","BEAR","JUST","ALSO","BACK","YEAR",
    "HIGH","HITS","NEWS","LIVE","NEXT","LAST","NEAR","RATE","DEAL","BOOM",
    "EARN","BEAT","MISS","PLAN","TECH","FLAG","BASE","BIG","NEW","OLD","HOW",
    "WHO","HAVE","SAID","AMID","GOLD","BOND","BANK","FUND","RISK","DATA",
    "CORE","SOFT","HARD","FLAT","LATE","WIDE","OPEN","HALF","FULL","PEAK",
    "MAD","FAST","HALF","CRAMER","MONEY","REPORT","SHOW","AUDIO","ONLY",
}

_COMPANY_MAP = {
    "Apple":"AAPL","Microsoft":"MSFT","Nvidia":"NVDA","Amazon":"AMZN",
    "Google":"GOOGL","Alphabet":"GOOGL","Meta":"META","Tesla":"TSLA",
    "Berkshire":"BRK-B","JPMorgan":"JPM","Goldman":"GS","Morgan Stanley":"MS",
    "Cisco":"CSCO","Intel":"INTC","AMD":"AMD","Broadcom":"AVGO",
    "Qualcomm":"QCOM","Salesforce":"CRM","Oracle":"ORCL","IBM":"IBM",
    "Boeing":"BA","Nike":"NKE","Disney":"DIS","Netflix":"NFLX",
    "Palantir":"PLTR","Snowflake":"SNOW","CrowdStrike":"CRWD",
    "Coinbase":"COIN","Robinhood":"HOOD","Dell":"DELL","Gap":"GPS",
    "Applied Materials":"AMAT","Lam Research":"LRCX","KLA":"KLAC",
    "ServiceNow":"NOW","Workday":"WDAY","Palo Alto":"PANW",
    "Exxon":"XOM","Chevron":"CVX","JPMorgan":"JPM","Bank of America":"BAC",
    "Wells Fargo":"WFC","Citigroup":"C","Goldman Sachs":"GS",
}


def _extract_cnbc_tickers(title, desc=""):
    """Extract US stock tickers from CNBC-style titles."""
    us = set()
    text = title + " " + desc
    # $TICKER pattern
    for t in re.findall(r'\$([A-Z]{2,5})\b', text):
        if t not in _NOT_TICKERS:
            us.add(t)
    # Company name mapping
    for company, ticker in _COMPANY_MAP.items():
        if company.lower() in text.lower():
            us.add(ticker)
    # ALL-CAPS 2-5 letter words (CNBC uses these often in titles)
    for t in re.findall(r'\b([A-Z]{2,5})\b', title):
        if t not in _NOT_TICKERS and len(t) >= 2:
            us.add(t)
    return sorted(us)


def summarize_cnbc(video_url: str):
    """CNBC-specific Gemini summary using English prompt."""
    if not GEMINI_KEY:
        return None
    # Duration check — skip audio-only / long shows
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
            vid_id = video_url.split("v=")[-1]
            for v in data.get("videos", []):
                if v["id"] == vid_id:
                    dur = v.get("duration", "")
                    if dur and dur.count(":") >= 2:
                        log.warning("CNBC video %s too long (%s), skipping", vid_id, dur)
                        return {"error": "too_long",
                                "message": f"影片過長（{dur}），超過 Gemini 分析上限（1小時）",
                                "stocks": [], "market": [], "key_points": [], "sentiment": "N/A"}
                    break
        except Exception:
            pass
    try:
        from google import genai
        from google.genai import types as gtypes
        client = genai.Client(api_key=GEMINI_KEY)
        prompt = """請用繁體中文分析這個 CNBC（Mad Money/Fast Money/Halftime Report）節目，以 JSON 格式回覆：

{
  "stocks": [
    {"code": "美股代碼（2-5個大寫字母）", "name": "公司名稱",
     "view": "看多/看空/中性", "target": "目標價（如有提及，否則 null）",
     "note": "Cramer或分析師的簡短評論"}
  ],
  "market": ["整體市場觀點1", "觀點2", "觀點3"],
  "key_points": ["最重要投資建議1", "建議2", "建議3", "建議4"],
  "sentiment": "樂觀/謹慎/中性"
}

只包含節目中有明確觀點的股票，使用真實美股代碼。
只回覆 JSON，不要 markdown 格式。"""

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=gtypes.Content(parts=[
                gtypes.Part(file_data=gtypes.FileData(file_uri=video_url, mime_type="video/*")),
                gtypes.Part(text=prompt)
            ])
        )
        raw = response.text.strip()
        raw = __import__('re').sub(r'^```(?:json)?\s*', '', raw, flags=8)
        raw = __import__('re').sub(r'\s*```$', '', raw, flags=8)
        result = __import__('json').loads(raw)
        result["raw"] = response.text
        log.info("CNBC Gemini OK for %s", video_url)
        return result
    except Exception as e:
        err = str(e)
        if "token" in err.lower() and "exceed" in err.lower():
            return {"error": "too_long",
                    "message": "影片過長，超過 Gemini 分析上限（1小時）",
                    "stocks": [], "market": [], "key_points": [], "sentiment": "N/A"}
        log.warning("CNBC Gemini failed: %s", err[:200])
        return None


def run_daily_fetch():
    """Fetch CNBC videos from Mad Money, Fast Money, Halftime Report playlists."""
    log.info("CNBC: fetching %d playlists", len(PLAYLISTS))

    existing = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                old = json.load(f)
            existing = {v["id"]: v for v in old.get("videos", [])}
        except Exception:
            pass

    all_rss = []
    for pl in PLAYLISTS:
        pl_videos = fetch_rss(pl["id"], 8)
        for v in pl_videos:
            v["category"]      = pl["category"]
            v["category_icon"] = pl["icon"]
        all_rss.extend(pl_videos)
        log.info("CNBC: %s → %d videos", pl["category"], len(pl_videos))

    seen, rss_videos = set(), []
    for v in all_rss:
        if v["id"] not in seen:
            seen.add(v["id"])
            rss_videos.append(v)

    new_vids = [v for v in rss_videos if not existing.get(v["id"])]
    log.info("CNBC: %d total, %d new", len(rss_videos), len(new_vids))

    def _scrape(v):
        details  = scrape_video_page(v["id"])
        all_text = v["title"] + " " + " ".join(details.get("hashtags", []))
        desc     = " ".join(details.get("desc_lines", []))
        us = _extract_cnbc_tickers(all_text, desc)
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
                    log.warning("CNBC scrape failed %s: %s", vid_id, e)

    results = []
    for v in rss_videos:
        vid_id = v["id"]
        entry = scraped.get(vid_id) or existing.get(vid_id) or v
        entry["category"]      = v.get("category", "")
        entry["category_icon"] = v.get("category_icon", "")
        results.append(entry)

    all_videos = list({v["id"]: v for v in results}.values())
    all_videos.sort(key=lambda x: x.get("pub_ts", 0) or x.get("date", ""), reverse=True)
    all_videos = all_videos[:30]

    payload = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "videos": all_videos}
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)
    log.info("CNBC: saved %d videos", len(all_videos))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_daily_fetch()
    print(f"\n✓ Fetched {len(result['videos'])} videos")
    for v in result["videos"][:5]:
        print(f"\n[{v['date']}] [{v.get('category','')}] {v['title'][:60]}")
        print(f"  US: {v.get('us_stocks',[])} dur={v.get('duration','?')}")
