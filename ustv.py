"""錢線百分百 (非凡財經) — video show fetcher."""
import os, logging
from show_fetcher import run_fetch

log = logging.getLogger(__name__)

CHANNEL_ID = "UC_ObC9O0ZQ2FhW6u9_iFlZA"   # 錢線百分百
DATA_FILE  = os.path.join(os.path.dirname(__file__), "static", "ustv.json")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")


def run_daily_fetch():
    return run_fetch(CHANNEL_ID, DATA_FILE, GEMINI_KEY)


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    result = run_daily_fetch()
    print(f"\n✓ Fetched {len(result['videos'])} videos")
    for v in result["videos"][:5]:
        print(f"\n[{v['date']}] {v['title'][:65]}")
        print(f"  TW: {v.get('tw_stocks')} | US: {v.get('us_stocks')}")
        if v.get("chapters"):
            print(f"  Chapters: {[c['title'][:30] for c in v['chapters'][:3]]}")
