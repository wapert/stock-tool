"""
EBC Money Show (理財達人秀) daily fetcher.
Strategy (bot-detection-free):
  1. YouTube RSS feed → latest video IDs + titles + dates
  2. YouTube page scrape → chapters + description (lightweight GET, no bot check)
  3. Extract TW/US stock codes from title + description + hashtags
"""
import os, re, json, datetime, logging, urllib.request, html

log = logging.getLogger(__name__)

CHANNEL_ID   = "UCQvsuaih5lE0n_Ne54nNezg"   # @EBCmoneyshow
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")   # set via env var on each VM
GEMINI_MODEL = "gemini-2.5-flash"
RSS_URL     = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
DATA_FILE   = os.path.join(os.path.dirname(__file__), "static", "ebcshow.json")
MAX_VIDEOS  = 6
HEADERS     = {"User-Agent":
               "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0.0.0 Safari/537.36",
               "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}

# ── Stock code extraction ─────────────────────────────────────────────────────
CN_TO_CODE = {
    "台積電":"2330","TSMC":"2330","鴻海":"2317","聯發科":"2454",
    "聯電":"2303","台達電":"2308","廣達":"2382","緯創":"3231",
    "瑞昱":"2379","聯詠":"3034","世界先進":"5347","矽力":"6415",
    "日月光":"3711","大立光":"3008","台灣大":"3045","中華電":"2412",
    "富邦金":"2881","國泰金":"2882","中信金":"2891","玉山金":"2884",
    "兆豐金":"2886","第一金":"2892","合庫金":"5880","元大金":"2885",
    "台塑":"1301","南亞":"1303","台化":"1326","台塑化":"6505",
    "中鋼":"2002","陽明":"2609","長榮":"2603","萬海":"2615",
    "創意":"3443","力積電":"6770","南亞科":"2408","功率元件":"None",
    "邁威爾":"MRVL","Marvell":"MRVL","輝達":"NVDA","NVIDIA":"NVDA",
    "蘋果":"AAPL","Apple":"AAPL","微軟":"MSFT","亞馬遜":"AMZN",
    "超微":"AMD","博通":"AVGO","高通":"QCOM","特斯拉":"TSLA",
    "谷歌":"GOOGL","Meta":"META","SOXL":"SOXL","台指":"None",
}

def _extract_stocks(text):
    if not text:
        return [], []
    # TW 4-digit codes (exclude timestamps like 04:50 → preceded by colon)
    tw = []
    for m in re.finditer(r'(?<![\d:(])\b([12345689]\d{3})\b(?!\d|:)', text):
        code = m.group(1)
        if 1101 <= int(code) <= 9999:
            tw.append(code)
    # US: $TICKER
    us = list(set(re.findall(r'\$([A-Z]{2,5})\b', text)))
    # Company names → codes
    for cn, code in CN_TO_CODE.items():
        if cn in text and code and code != "None":
            if code.isupper() and len(code) <= 5 and code not in us:
                us.append(code)
            elif code.isdigit() and code not in tw:
                tw.append(code)
    return list(set(tw)), list(set(us))


def _fetch_url(url, timeout=8):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


def summarize_with_gemini(video_url: str) -> dict | None:
    """
    Use Gemini to analyse the EBC Money Show video and return structured summary.
    Returns dict with keys: stocks, views, market, key_points, raw
    Returns None if API key missing / quota exceeded.
    """
    if not GEMINI_KEY:
        return None
    try:
        from google import genai
        from google.genai import types as gtypes

        client = genai.Client(api_key=GEMINI_KEY)
        prompt = """請用繁體中文分析這個理財達人秀（EBC財經台）節目，以JSON格式回覆：

{
  "stocks": [
    {"code": "股票代碼（台股4位數字或美股英文）", "name": "公司名稱", "view": "看多/看空/中性", "target": "目標價（如有提及，否則null）", "note": "簡短說明"}
  ],
  "market": ["整體市場觀點要點1", "要點2", "要點3"],
  "key_points": ["最重要投資建議1", "建議2", "建議3", "建議4", "建議5"],
  "sentiment": "樂觀/謹慎/中性"
}

只包含節目中實際提到的股票。如果不確定，寧可少寫。格式嚴格按照上面的JSON。"""

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
        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
        result = json.loads(raw)
        result["raw"] = response.text
        log.info("Gemini summary OK for %s", video_url)
        return result
    except Exception as e:
        log.warning("Gemini summary failed: %s", str(e)[:200])
        return None


def fetch_rss_videos():
    """Get latest videos from YouTube RSS feed (no bot detection)."""
    try:
        import feedparser
        raw = _fetch_url(RSS_URL)
        feed = feedparser.parse(raw)
        videos = []
        for entry in feed.entries[:MAX_VIDEOS]:
            vid_id = getattr(entry, "yt_videoid", None) or entry.id.split(":")[-1]
            pub    = getattr(entry, "published", "") or ""
            date   = pub[:10] if pub else ""
            videos.append({
                "id":      vid_id,
                "title":   entry.title,
                "url":     f"https://www.youtube.com/watch?v={vid_id}",
                "date":    date,
                "thumbnail": f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg",
            })
        return videos
    except Exception as e:
        log.error("RSS fetch error: %s", e)
        return []


def scrape_video_page(vid_id):
    """
    Scrape YouTube video page for chapters, description, hashtags.
    Returns dict with: chapters, hashtags, desc_lines, duration
    """
    try:
        page = _fetch_url(f"https://www.youtube.com/watch?v={vid_id}")

        # Duration from page
        dur_m = re.search(r'"lengthSeconds":"(\d+)"', page)
        duration = ""
        if dur_m:
            secs = int(dur_m.group(1))
            duration = f"{secs//3600}:{(secs%3600)//60:02d}" if secs >= 3600 else f"{secs//60}:{secs%60:02d}"

        # Description text (encoded in JSON in page source)
        desc = ""
        desc_m = re.search(r'"description":{"simpleText":"((?:[^"\\]|\\.)*)"}', page)
        if desc_m:
            raw = desc_m.group(1)
            # Simple JSON unescape without unicode_escape (preserves UTF-8)
            desc = raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        if not desc:
            desc_m2 = re.search(r'"attributedDescription":{"content":"((?:[^"\\]|\\.)*)"', page)
            if desc_m2:
                raw = desc_m2.group(1)
                desc = raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")

        # Hashtags
        hashtags = [h for h in re.findall(r'#(\S+)', desc) if h != "理財達人秀"][:8]

        # Chapters from engagementPanels or chapters key
        chapters = []
        # Method 1: chapterRenderer
        for m in re.finditer(r'"chapterRenderer".*?"title":\{"simpleText":"([^"]+)"\}.*?"timeRangeStartMillis":(\d+)', page, re.DOTALL):
            ms = int(m.group(2))
            chapters.append({"time": ms // 1000, "title": m.group(1)})
        # Method 2: macroMarkersListItemRenderer (newer)
        if not chapters:
            for m in re.finditer(r'"startMillis":"(\d+)"[^}]*"title":\{"simpleText":"([^"]+)"\}', page):
                chapters.append({"time": int(m.group(1)) // 1000, "title": m.group(2)})
        # Method 3: from description timestamps like (00:00) Title
        if not chapters:
            for m in re.finditer(r'\((\d{1,2}:\d{2}(?::\d{2})?)\)\s+(.+?)(?:\n|$)', desc):
                t_str = m.group(1)
                parts = t_str.split(":")
                secs  = int(parts[-1]) + int(parts[-2])*60 + (int(parts[-3])*3600 if len(parts)==3 else 0)
                chapters.append({"time": secs, "title": m.group(2).strip()})

        # Clean desc_lines (useful lines, not links/hashtags/boilerplate)
        desc_lines = []
        for line in desc.split("\n"):
            line = line.strip()
            if (line and not line.startswith("http") and "#" not in line
                    and "◆" not in line and len(line) > 4 and len(desc_lines) < 3):
                desc_lines.append(html.unescape(line))

        return {
            "duration":   duration,
            "chapters":   chapters[:6],
            "hashtags":   hashtags,
            "desc_lines": desc_lines,
        }
    except Exception as e:
        log.warning("scrape_video_page(%s) error: %s", vid_id, e)
        return {"duration":"","chapters":[],"hashtags":[],"desc_lines":[]}


def run_daily_fetch():
    """Main: fetch RSS → scrape pages → extract stocks → save JSON."""
    log.info("EBC Show: starting daily fetch")

    # Load existing data to preserve cached videos
    existing = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                old = json.load(f)
            existing = {v["id"]: v for v in old.get("videos", [])}
        except Exception:
            pass

    rss_videos = fetch_rss_videos()
    results = []
    for v in rss_videos:
        vid_id = v["id"]
        if vid_id in existing and existing[vid_id].get("chapters"):
            # Already have detailed data — reuse
            results.append(existing[vid_id])
            continue
        # Scrape page for details
        details = scrape_video_page(vid_id)
        # Extract stocks from all text
        all_text = (v["title"] + " " + " ".join(details.get("hashtags", []))
                    + " " + " ".join(details.get("desc_lines", [])))
        tw, us = _extract_stocks(all_text)
        # Gemini AI summary (run for all videos)
        gemini = None
        if GEMINI_KEY:
            gemini = summarize_with_gemini(v["url"])
            # Merge stocks from Gemini result if available
            if gemini and gemini.get("stocks"):
                for s in gemini["stocks"]:
                    code = s.get("code", "")
                    if code and re.match(r'^\d{4}$', code):
                        tw.append(code)
                    elif code and re.match(r'^[A-Z]{2,5}$', code):
                        us.append(code)
        full = {**v, **details,
                "tw_stocks": sorted(set(tw)),
                "us_stocks": sorted(set(us)),
                "gemini":    gemini}
        results.append(full)
        existing[vid_id] = full

    # Merge with previous and keep history
    all_videos = list({v["id"]: v for v in list(existing.values())}.values())
    all_videos.sort(key=lambda x: x.get("date",""), reverse=True)
    all_videos = all_videos[:MAX_VIDEOS * 3]

    payload = {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "videos":  all_videos,
    }
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    log.info("EBC Show: saved %d videos", len(all_videos))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_daily_fetch()
    print(f"\n✓ Fetched {len(result['videos'])} videos")
    for v in result["videos"][:5]:
        print(f"\n[{v['date']}] {v['title'][:65]}")
        print(f"  TW: {v.get('tw_stocks')} | US: {v.get('us_stocks')}")
        if v.get("chapters"):
            print(f"  Chapters: {[c['title'][:28] for c in v['chapters'][:3]]}")
        if v.get("hashtags"):
            print(f"  Tags: {v['hashtags'][:5]}")
