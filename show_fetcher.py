"""
Generic show fetcher — shared logic for EBC and USTV video pages.
Pass a ShowConfig to fetch RSS, scrape pages, extract stocks.
"""
import os, re, json, datetime, logging, urllib.request, html as _html
import calendar as _calendar
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# ── Company name → stock code map ────────────────────────────────────────────
CN_TO_CODE = {
    "台積電":"2330","TSMC":"2330","鴻海":"2317","聯發科":"2454",
    "聯電":"2303","台達電":"2308","廣達":"2382","緯創":"3231",
    "瑞昱":"2379","聯詠":"3034","世界先進":"5347","矽力":"6415",
    "日月光":"3711","大立光":"3008","台灣大":"3045","中華電":"2412",
    "富邦金":"2881","國泰金":"2882","中信金":"2891","玉山金":"2884",
    "兆豐金":"2886","第一金":"2892","合庫金":"5880","元大金":"2885",
    "台塑":"1301","南亞":"1303","台化":"1326","台塑化":"6505",
    "中鋼":"2002","陽明":"2609","長榮":"2603","萬海":"2615",
    "創意":"3443","力積電":"6770","南亞科":"2408",
    "邁威爾":"MRVL","Marvell":"MRVL","輝達":"NVDA","NVIDIA":"NVDA",
    "蘋果":"AAPL","Apple":"AAPL","微軟":"MSFT","亞馬遜":"AMZN",
    "超微":"AMD","博通":"AVGO","高通":"QCOM","特斯拉":"TSLA",
    "谷歌":"GOOGL","Meta":"META",
}


def _fetch_url(url, timeout=8):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


def extract_stocks(text):
    """Extract TW (4-digit) and US stock codes from text."""
    if not text:
        return [], []
    tw = []
    for m in re.finditer(r'(?<![\d:(])\b([12345689]\d{3})\b(?!\d|:)', text):
        code = m.group(1)
        if 1101 <= int(code) <= 9999:
            tw.append(code)
    us = list(set(re.findall(r'\$([A-Z]{2,5})\b', text)))
    for cn, code in CN_TO_CODE.items():
        if cn in text and code and code != "None":
            if code.isupper() and len(code) <= 5 and code not in us:
                us.append(code)
            elif code.isdigit() and code not in tw:
                tw.append(code)
    return list(set(tw)), list(set(us))


def fetch_rss(channel_or_playlist_id, max_videos=15):
    """Fetch latest videos from YouTube RSS feed (channel or playlist)."""
    try:
        import feedparser
        # Detect playlist (PL prefix) vs channel (UC prefix)
        if channel_or_playlist_id.startswith("PL"):
            url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={channel_or_playlist_id}"
        else:
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_or_playlist_id}"
        channel_id = channel_or_playlist_id
        raw = _fetch_url(url)
        feed = feedparser.parse(raw)
        videos = []
        for entry in feed.entries[:max_videos]:
            vid_id = getattr(entry, "yt_videoid", None) or entry.id.split(":")[-1]
            pub = getattr(entry, "published", "") or ""
            date, pub_tw, pub_ts = pub[:10] if pub else "", "", 0
            try:
                pt = entry.published_parsed
                pub_ts = _calendar.timegm(pt)
                tw_dt  = datetime.datetime.utcfromtimestamp(pub_ts + 8 * 3600)
                date   = tw_dt.strftime("%Y-%m-%d")
                pub_tw = tw_dt.strftime("%m/%d %H:%M")
            except Exception:
                pass
            videos.append({
                "id": vid_id, "title": entry.title,
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "date": date, "pub_tw": pub_tw, "pub_ts": pub_ts,
                "thumbnail": f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg",
            })
        return videos
    except Exception as e:
        log.error("RSS fetch error: %s", e)
        return []


def scrape_video_page(vid_id):
    """Scrape YouTube video page for chapters, description, hashtags, duration."""
    try:
        page = _fetch_url(f"https://www.youtube.com/watch?v={vid_id}")
        # Duration
        duration = ""
        dur_m = re.search(r'"lengthSeconds":"(\d+)"', page)
        if dur_m:
            secs = int(dur_m.group(1))
            if secs >= 3600:
                duration = f"{secs//3600}:{(secs%3600)//60:02d}:{secs%60:02d}"  # H:MM:SS
            else:
                duration = f"{secs//60}:{secs%60:02d}"  # M:SS
        # Description
        desc = ""
        for pat in [r'"description":{"simpleText":"((?:[^"\\]|\\.)*)"',
                    r'"attributedDescription":{"content":"((?:[^"\\]|\\.)*)"']:
            m = re.search(pat, page)
            if m:
                desc = m.group(1).replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
                break
        hashtags = [h for h in re.findall(r'#(\S+)', desc) if len(h) < 15][:8]
        # Chapters
        chapters = []
        for m in re.finditer(r'"chapterRenderer".*?"title":\{"simpleText":"([^"]+)"\}.*?"timeRangeStartMillis":(\d+)', page, re.DOTALL):
            chapters.append({"time": int(m.group(2)) // 1000, "title": m.group(1)})
        if not chapters:
            for m in re.finditer(r'"startMillis":"(\d+)"[^}]*"title":\{"simpleText":"([^"]+)"\}', page):
                chapters.append({"time": int(m.group(1)) // 1000, "title": m.group(2)})
        if not chapters:
            for m in re.finditer(r'\((\d{1,2}:\d{2}(?::\d{2})?)\)\s+(.+?)(?:\n|$)', desc):
                t = m.group(1).split(":")
                secs = int(t[-1]) + int(t[-2])*60 + (int(t[-3])*3600 if len(t)==3 else 0)
                chapters.append({"time": secs, "title": m.group(2).strip()})
        # Clean description lines
        desc_lines = [
            l.strip() for l in desc.split("\n")
            if l.strip() and not l.strip().startswith("http")
            and "◆" not in l and "#" not in l and len(l.strip()) > 4
        ][:3]
        return {"duration": duration, "chapters": chapters[:6],
                "hashtags": hashtags, "desc_lines": desc_lines}
    except Exception as e:
        log.warning("scrape_video_page(%s) error: %s", vid_id, e)
        return {"duration": "", "chapters": [], "hashtags": [], "desc_lines": []}


def run_fetch(channel_id, data_file, gemini_key="", gemini_model="gemini-2.5-flash", max_videos=15):
    """
    Main fetch function: RSS → scrape → Gemini (if key set) → save JSON.
    Returns the payload dict.
    """
    log.info("ShowFetcher: fetching %s", channel_id)

    # Load existing cache
    existing = {}
    if os.path.exists(data_file):
        try:
            with open(data_file, encoding="utf-8") as f:
                old = json.load(f)
            existing = {v["id"]: v for v in old.get("videos", [])}
        except Exception:
            pass

    rss_videos = fetch_rss(channel_id, max_videos)

    # Split into cached (instant) and new (need scraping)
    cached_videos = []
    new_videos    = []
    for v in rss_videos:
        if existing.get(v["id"]):
            cached_videos.append(v)
        else:
            new_videos.append(v)

    log.info("ShowFetcher: %d cached, %d new to scrape", len(cached_videos), len(new_videos))

    # ── Scrape new videos in PARALLEL ──────────────────────────────────────
    def _process_new(v):
        vid_id  = v["id"]
        details = scrape_video_page(vid_id)
        all_text = (v["title"] + " " + " ".join(details.get("hashtags", []))
                    + " " + " ".join(details.get("desc_lines", [])))
        tw, us = extract_stocks(all_text)
        return {**v, **details,
                "tw_stocks": sorted(set(tw)),
                "us_stocks": sorted(set(us)),
                "gemini":    None}   # Gemini runs separately on demand

    scraped = {}
    if new_videos:
        workers = min(8, len(new_videos))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_process_new, v): v["id"] for v in new_videos}
            for fut in as_completed(futs):
                vid_id = futs[fut]
                try:
                    scraped[vid_id] = fut.result()
                except Exception as e:
                    log.warning("Scrape failed %s: %s", vid_id, e)

    # ── Merge results ───────────────────────────────────────────────────────
    results = []
    for v in rss_videos:
        vid_id = v["id"]
        if vid_id in scraped:
            full = scraped[vid_id]
            existing[vid_id] = full
            results.append(full)
        else:
            cached = existing.get(vid_id, v)
            results.append(cached)

    all_videos = list({v["id"]: v for v in existing.values()}.values())
    all_videos.sort(key=lambda x: x.get("pub_ts", 0) or x.get("date",""), reverse=True)
    all_videos = all_videos[:30]

    payload = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "videos": all_videos}
    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    tmp = data_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, data_file)

    log.info("ShowFetcher: saved %d videos to %s", len(all_videos), data_file)
    return payload
