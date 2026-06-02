"""Investor's Business Daily (IBD) — video show fetcher."""
import os, re, logging
from show_fetcher import filter_recent_videos, run_fetch, extract_stocks, fetch_rss, scrape_video_page
from concurrent.futures import ThreadPoolExecutor, as_completed
import json, datetime

# Common English words to exclude from ticker detection
_NOT_TICKERS = {
    "THE","AND","FOR","NOT","WITH","ARE","HAS","WAS","ITS","ALL","CAN","MAY",
    "IBD","CEO","CFO","IPO","GDP","CPI","FED","USA","ETF","ETFs","ATH","ATL",
    "NYSE","NASDAQ","DOW","SPX","VIX","GDP","YOY","QOQ","AUM","ROI","EPS",
    "THIS","THAT","WILL","FROM","BEEN","THEY","WHEN","WHAT","THAN","THEN",
    "INTO","OVER","AFTER","BULL","BEAR","JUST","ALSO","BACK","YEAR","WEEK",
    "HIGH","HITS","NEWS","LIVE","SHOW","NEXT","LAST","NEAR","RATE","DEAL",
    "BOOM","GAIN","LOSS","RISE","FALL","SELL","HOLD","PLUS","STAY","MOVE",
    "TAKE","KEEP","MAKE","PLAY","LOOK","LEAD","LIKE","WANT","HELP","KNOW",
    "REAL","FULL","BEST","GOOD","LONG","STAY","EVEN","ONLY","MUCH","MANY",
    "MOST","SOME","BOTH","SUCH","SAME","MORE","LESS","VERY","WELL","HARD",
    "FAST","SLOW","LATE","EARN","BEAT","MISS","PLAN","TECH","FLAG","BASE",
    "AIR","OIL","GAS","LAW","ACT","BIG","NEW","OLD","HOW","WHY","WHO",
    "WHAT","WHEN","WHERE","WHICH","WHILE","YOUR","HAVE","BEEN","SAID",
}

def _extract_ibd_tickers(text):
    """Extract US tickers from IBD-style titles (no $ prefix needed)."""
    us = set()
    # Pattern 1: tickers after colon or semicolon, comma-separated
    # e.g. "FSLR, LLY, AAON In Focus" or "FLEX, FTNT, SNOW"
    for section in re.findall(r'[:;]\s*([A-Z, ]+?)(?:\s+(?:In|On|At|Are|Is|To|The|Watch|And)\b|$)', text):
        for tok in re.split(r'[,\s]+', section):
            tok = tok.strip()
            if 2 <= len(tok) <= 5 and tok.isalpha() and tok.upper() not in _NOT_TICKERS:
                us.add(tok.upper())
    # Pattern 2: any CAPS words 2-5 letters not in common words list
    for tok in re.findall(r'\b([A-Z]{2,5})\b', text):
        if tok not in _NOT_TICKERS and len(tok) >= 2:
            us.add(tok)
    # Also run standard extraction for $TICKER and company names
    _, standard_us = extract_stocks(text)
    us.update(standard_us)
    return sorted(us)

log = logging.getLogger(__name__)

CHANNEL_ID = "UC5fZv7bPcF5j2RsfO-9OiLA"   # Investor's Business Daily
DATA_FILE  = os.path.join(os.path.dirname(__file__), "static", "ibd.json")
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"


def run_daily_fetch():
    """Fetch IBD videos with enhanced US ticker extraction."""
    log.info("IBD: fetching channel %s", CHANNEL_ID)

    # Load existing cache
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
    log.info("IBD: %d cached, %d new", len(cached_vids), len(new_vids))

    def _scrape_ibd(v):
        details  = scrape_video_page(v["id"])
        all_text = v["title"] + " " + " ".join(details.get("hashtags", []))
        us_tickers = _extract_ibd_tickers(all_text)
        return {**v, **details,
                "tw_stocks": [],
                "us_stocks": us_tickers,
                "gemini":    None}

    scraped = {}
    if new_vids:
        with ThreadPoolExecutor(max_workers=min(8, len(new_vids))) as ex:
            futs = {ex.submit(_scrape_ibd, v): v["id"] for v in new_vids}
            for fut in as_completed(futs):
                vid_id = futs[fut]
                try:
                    scraped[vid_id] = fut.result()
                    existing[vid_id] = scraped[vid_id]
                except Exception as e:
                    log.warning("IBD scrape failed %s: %s", vid_id, e)

    results = []
    for v in rss_videos:
        vid_id = v["id"]
        results.append(scraped.get(vid_id) or existing.get(vid_id) or v)

    all_videos = list({v["id"]: v for v in existing.values()}.values())
    all_videos = filter_recent_videos(all_videos, days=5)
    all_videos.sort(key=lambda x: x.get("pub_ts", 0) or x.get("date", ""), reverse=True)
    all_videos = all_videos[:30]

    payload = {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "videos":  all_videos,
    }
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)
    log.info("IBD: saved %d videos", len(all_videos))
    return payload


def summarize_ibd(video_url: str):
    """IBD-specific Gemini summary using English prompt."""
    if not GEMINI_KEY:
        return None
    try:
        from google import genai
        from google.genai import types as gtypes
        client = genai.Client(api_key=GEMINI_KEY)
        prompt = """請用繁體中文分析這個 Investor's Business Daily (IBD) 節目，以 JSON 格式回覆：

{
  "stocks": [
    {"code": "美股代碼（2-5個大寫字母）", "name": "公司名稱",
     "view": "看多/看空/中性", "target": "目標價（如有提及，否則 null）",
     "note": "IBD分析師的簡短觀點"}
  ],
  "market": ["整體市場觀點1", "觀點2", "觀點3"],
  "key_points": ["最重要投資建議1", "建議2", "建議3", "建議4", "建議5"],
  "sentiment": "樂觀/謹慎/中性"
}

只包含節目中實際討論的股票，使用真實美股代碼。
只回覆 JSON，不要 markdown 格式。"""

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
        import re as _re2, json as _json2
        raw = _re2.sub(r'^```(?:json)?\s*', '', raw, flags=8)
        raw = _re2.sub(r'\s*```$', '', raw, flags=8)
        result = _json2.loads(raw)
        result["raw"] = response.text
        log.info("IBD Gemini OK for %s", video_url)
        return result
    except Exception as e:
        log.warning("IBD Gemini failed: %s", str(e)[:200])
        return None


if __name__ == "__main__":
    import json, logging
    logging.basicConfig(level=logging.INFO)
    result = run_daily_fetch()
    print(f"\n✓ Fetched {len(result['videos'])} videos")
    for v in result["videos"][:5]:
        print(f"\n[{v['date']}] {v['title'][:65]}")
        print(f"  TW: {v.get('tw_stocks')} | US: {v.get('us_stocks')}")
        if v.get("chapters"):
            print(f"  Chapters: {[c['title'][:30] for c in v['chapters'][:3]]}")
