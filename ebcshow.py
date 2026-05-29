"""
EBC Money Show (理財達人秀) daily fetcher.
Uses yt-dlp to get video list + metadata (title, chapters, hashtags, description).
No transcript needed — chapters + title contain the key discussion topics.
"""
import os, re, json, datetime, logging

log = logging.getLogger(__name__)

CHANNEL_URL = "https://www.youtube.com/@EBCmoneyshow/videos"
DATA_FILE   = os.path.join(os.path.dirname(__file__), "static", "ebcshow.json")
MAX_VIDEOS  = 5   # fetch latest N videos

# ── Stock code extraction ─────────────────────────────────────────────────────
# Chinese company name → stock code mapping
CN_TO_CODE = {
    "台積電":"2330","TSMC":"2330","鴻海":"2317","聯發科":"2454",
    "聯電":"2303","台達電":"2308","廣達":"2382","緯創":"3231",
    "瑞昱":"2379","聯詠":"3034","世界先進":"5347","矽力":"6415",
    "日月光":"3711","大立光":"3008","台灣大":"3045","中華電":"2412",
    "富邦金":"2881","國泰金":"2882","中信金":"2891","玉山金":"2884",
    "兆豐金":"2886","第一金":"2892","合庫金":"5880","元大金":"2885",
    "台塑":"1301","南亞":"1303","台化":"1326","台塑化":"6505",
    "中鋼":"2002","中鴻":"2014","東鋼":"2006",
    "陽明":"2609","長榮":"2603","萬海":"2615",
    "創意":"3443","閎康":"3587","力積電":"6770","南亞科":"2408",
    "邁威爾":"MRVL","Marvell":"MRVL","輝達":"NVDA","NVIDIA":"NVDA",
    "蘋果":"AAPL","Apple":"AAPL","微軟":"MSFT","亞馬遜":"AMZN",
    "超微":"AMD","博通":"AVGO","高通":"QCOM","特斯拉":"TSLA",
    "谷歌":"GOOGL","Meta":"META","記憶體": None,"CPO": None,
}

def _extract_stocks(text: str):
    """Extract TW (4-digit) and US ($TICKER) stock codes from text."""
    if not text:
        return [], []
    # TW: 4-digit numbers 1000-9999, not preceded by : or ( (avoid timestamps)
    tw = []
    for m in re.finditer(r'(?<![\d:(])\b([12345689]\d{3})\b(?![\d:])', text):
        code = m.group(1)
        if 1000 <= int(code) <= 9999:
            tw.append(code)
    # US: $TICKER or known company names
    us = list(set(re.findall(r'\$([A-Z]{2,5})\b', text)))
    # From company names
    for cn, code in CN_TO_CODE.items():
        if cn in text and code:
            if len(code) <= 5 and code.isupper():
                if code not in us:
                    us.append(code)
            elif code.isdigit() and code not in tw:
                tw.append(code)
    return list(set(tw)), list(set(us))


def _parse_date(date_str: str) -> str:
    """Parse yt-dlp upload_date (YYYYMMDD) to readable format."""
    try:
        d = datetime.datetime.strptime(date_str, "%Y%m%d")
        return d.strftime("%Y-%m-%d")
    except Exception:
        return date_str or ""


def fetch_latest_videos():
    """Use yt-dlp to get latest videos with metadata."""
    try:
        import yt_dlp
    except ImportError:
        log.error("yt-dlp not installed")
        return []

    ydl_opts = {
        "quiet":         True,
        "no_warnings":   True,
        "extract_flat":  False,   # need full info for chapters/description
        "playlistend":   MAX_VIDEOS,
        "skip_download": True,
        "ignoreerrors":  True,
    }
    videos = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(CHANNEL_URL, download=False)
            for entry in (info.get("entries") or [])[:MAX_VIDEOS]:
                if not entry:
                    continue
                vid_id  = entry.get("id","")
                title   = entry.get("title","") or ""
                desc    = entry.get("description","") or ""
                date    = _parse_date(entry.get("upload_date",""))
                thumb   = (entry.get("thumbnail") or
                           f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg")
                # Chapters = structured discussion topics
                chapters = [
                    {"time": int(c.get("start_time",0)),
                     "title": c.get("title","").strip()}
                    for c in (entry.get("chapters") or [])
                    if c.get("title","").strip()
                ]
                # Hashtags from description
                hashtags = re.findall(r'#(\S+)', desc)
                hashtags = [h for h in hashtags if h != "理財達人秀"][:8]
                # Extract description first few useful lines (before URL lines)
                desc_lines = [
                    l.strip() for l in desc.split("\n")
                    if l.strip() and not l.strip().startswith("http")
                    and not l.strip().startswith("◆") and "#" not in l
                ][:3]
                # Extract stock codes from all text
                all_text = title + " " + desc + " " + " ".join(hashtags)
                tw_stocks, us_stocks = _extract_stocks(all_text)
                # Duration
                dur = entry.get("duration") or 0
                dur_fmt = f"{dur//3600}:{(dur%3600)//60:02d}" if dur >= 3600 else f"{dur//60}:{dur%60:02d}"

                videos.append({
                    "id":         vid_id,
                    "title":      title,
                    "url":        f"https://www.youtube.com/watch?v={vid_id}",
                    "date":       date,
                    "thumbnail":  thumb,
                    "duration":   dur_fmt if dur else "",
                    "chapters":   chapters,
                    "hashtags":   hashtags,
                    "desc_lines": desc_lines,
                    "tw_stocks":  sorted(set(tw_stocks)),
                    "us_stocks":  sorted(set(us_stocks)),
                })
    except Exception as e:
        log.error("fetch_latest_videos error: %s", e)
    return videos


def run_daily_fetch():
    """Main function: fetch latest videos and save to JSON."""
    log.info("EBC Show: starting daily fetch")
    videos = fetch_latest_videos()

    # Load existing to preserve previously fetched data
    existing = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                old = json.load(f)
            existing = {v["id"]: v for v in old.get("videos", [])}
        except Exception:
            pass

    # Merge: new data overrides old for same ID
    for v in videos:
        existing[v["id"]] = v

    # Keep only latest MAX_VIDEOS * 3 (preserve some history)
    all_videos = list(existing.values())
    all_videos.sort(key=lambda x: x.get("date",""), reverse=True)
    all_videos = all_videos[:MAX_VIDEOS * 3]

    payload = {
        "updated":   datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "videos":    all_videos,
    }
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    log.info("EBC Show: saved %d videos", len(all_videos))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_daily_fetch()
    print(f"Fetched {len(result['videos'])} videos")
    for v in result["videos"]:
        print(f"\n[{v['date']}] {v['title'][:70]}")
        print(f"  TW stocks: {v['tw_stocks']} | US: {v['us_stocks']}")
        if v['chapters']:
            print(f"  Chapters: {[c['title'][:30] for c in v['chapters']]}")
