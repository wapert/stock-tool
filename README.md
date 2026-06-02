# 股票分析工具 — Stock Analysis Tool

A full-stack web application for analysing US and Taiwan stocks, options, and financial news videos. Built with Flask + Gunicorn, deployed behind Nginx on Linux VMs.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Features](#features)
3. [Prerequisites](#prerequisites)
4. [DNS Setup](#dns-setup)
5. [External APIs & Keys](#external-apis--keys)
6. [Directory Structure](#directory-structure)
7. [Local Development Setup](#local-development-setup)
8. [Production Deployment (VM)](#production-deployment-vm)
   - [Oracle Cloud ARM64 (Primary)](#oracle-cloud-arm64-primary)
   - [GCP e2-micro (Mirror)](#gcp-e2-micro-mirror)
9. [Nginx Configuration](#nginx-configuration)
10. [Systemd Service](#systemd-service)
11. [Environment Variables (.env)](#environment-variables-env)
12. [gunicorn_config.py Reference](#gunicorn_configpy-reference)
13. [Caching Architecture](#caching-architecture)
14. [Video Pages & Gemini AI](#video-pages--gemini-ai)
15. [Pages & Routes Reference](#pages--routes-reference)
16. [Annual Maintenance — Calendar Data](#annual-maintenance--calendar-data)
17. [Updating / Re-deploying](#updating--re-deploying)
18. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
Browser (Desktop / Mobile PWA)
        │
        ▼
   Nginx (port 80/443, HTTPS via Certbot)
        │
        ▼
   Gunicorn  ──  4 workers (Oracle) / 1 worker (GCP)
        │
        ▼
   Flask app (app.py)
        ├── yfinance            ← US stock history / fundamentals
        ├── Alpaca IEX API      ← US real-time quotes (market hours)
        ├── Shioaji 永豐金 API  ← TW real-time quotes (Oracle only)
        ├── Gemini 2.5-flash    ← AI video analysis (all 4 video pages)
        └── YouTube RSS feeds   ← Video discovery (no yt-dlp needed)
```

Two live deployments:

| VM | Provider | Hostname | Workers | TW Real-time (Shioaji) |
|----|----------|----------|---------|------------------------|
| Oracle ARM64 A1 (6 GB RAM) | Oracle Cloud | your-oracle-domain.example.com | 4 | Yes |
| GCP e2-micro (1 GB RAM) | Google Cloud | your-gcp-domain.example.com | 1 | No (yfinance fallback) |

---

## Features

| Page | URL | Description |
|------|-----|-------------|
| Desktop home | `/` | Full-width stock analysis dashboard |
| Mobile home | `/m` | Responsive bottom-nav SPA |
| Market Overview | `/market` | US & TW index / sector data |
| Stock Scanner | via `/analyze` | Multi-symbol technical analysis with charts |
| Options | `/options` | Options chain + open interest |
| Serenity | `/serenity` | Portfolio watchlist |
| Calendar | `/calendar` | Earnings / economic event calendar |
| EBC 達人秀 | `/ebcshow` | EBC Money Show videos + Gemini AI analysis |
| USTV 錢線百分百 | `/ustv` | USTV financial show videos + Gemini AI analysis |
| IBD | `/ibd` | Investor's Business Daily videos + Gemini AI |
| CNBC | `/cnbc` | Mad Money / Fast Money / Halftime Report + Gemini AI |

All video pages: YouTube RSS feed → page scrape → Gemini 2.5-flash AI summary in 繁體中文.

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | Uses `zoneinfo` (stdlib), f-strings |
| pip | latest | For installing packages |
| Nginx | 1.18+ | Reverse proxy + SSL termination |
| Certbot | any | Let's Encrypt TLS certificates |
| Git | 2.x | Pulling updates from GitHub |
| A domain name | — | Required for HTTPS — see DNS Setup below |
| Miniforge (Oracle) | latest | ARM64 conda — some packages need ARM wheels |
| venv (GCP/local) | stdlib | Standard Python virtual environment |

---

## DNS Setup

You need a **domain name** that points to your VM's public IP before you can obtain an HTTPS certificate with Certbot. You can use a free dynamic DNS service or a paid registrar.

### Option A: Free DNS — mooo.com (freedns.afraid.org)

1. Go to https://freedns.afraid.org and create a free account
2. Click **Subdomains** → **Add a subdomain**
3. Choose a hostname (e.g. `mystocktool`) and a free parent domain (e.g. `mooo.com`)
4. Enter your VM's **public IP address** in the Destination field
5. Save — your domain is now `yourname.mooo.com`
6. Repeat for your second VM if you have one (e.g. `mystocktool2.mooo.com`)

### Option B: Free DNS — DuckDNS

1. Go to https://www.duckdns.org and sign in with GitHub/Google
2. Enter a subdomain name → click **add domain**
3. Your domain is `yourname.duckdns.org`
4. Update the IP to your VM's public IP on that page

### Option C: Paid domain (Namecheap, Google Domains, etc.)

1. Purchase a domain from any registrar
2. In the DNS settings, add an **A record** pointing to your VM's public IP:
   ```
   Type: A
   Name: @  (or a subdomain like "stock")
   Value: <your VM public IP>
   TTL: 300
   ```

### Finding your VM's public IP

- **Oracle Cloud:** Compute → Instances → your instance → Public IP address
- **GCP:** Compute Engine → VM instances → External IP column

### After DNS is set up

Verify the domain resolves before running Certbot:
```bash
ping your-domain.example.com
# Should resolve to your VM's IP
```

Then run Certbot to get a free HTTPS certificate:
```bash
sudo certbot --nginx -d your-domain.example.com
```

> **Note:** Free dynamic DNS subdomains (mooo.com, duckdns.org) are fully supported by Let's Encrypt / Certbot.

---

## External APIs & Keys

You need credentials from **three** services:

### 1. Google Gemini API — AI video analysis
- **Used by:** All 4 video pages (EBC, USTV, IBD, CNBC)
- **Model:** `gemini-2.5-flash`
- **Free tier:** Yes (limited RPM/day)
- **Get key:** https://aistudio.google.com/app/apikey
- **Env var:** `GEMINI_API_KEY`

### 2. Alpaca Markets API — US real-time quotes
- **Used by:** Portfolio sync, real-time US price display
- **Tier needed:** Free "IEX" data (delayed) is sufficient for price display
- **Get key:** https://app.alpaca.markets → Paper Trading → API Keys
- **Env vars:** `ALPACA_KEY`, `ALPACA_SECRET`, `ALPACA_BASE_URL`
  - Paper trading URL: `https://paper-api.alpaca.markets`
  - Live trading URL: `https://api.alpaca.markets`

### 3. Shioaji 永豐金證券 API — Taiwan real-time quotes
- **Used by:** Oracle VM only (TW stock real-time prices)
- **Required:** A 永豐金 (SinoPac) brokerage account with API access enabled
- **Get key:** https://sinotrade.github.io → apply for API access
- **Env vars:** `SHIOAJI_KEY`, `SHIOAJI_SECRET`
- **Optional:** If you don't have a TW broker account, leave these blank — the app falls back to yfinance (delayed data). Set `_simulation = True` in `shioaji_data.py` to use Shioaji's simulation mode.

---

## Directory Structure

```
stock_app/
├── app.py                  # Main Flask app — all routes
├── gunicorn_config.py      # Gunicorn workers, bind, timeout, post_fork hook
├── requirements.txt        # Python dependencies
├── Procfile                # Heroku-style launch reference
├── .env                    # Secret keys — NOT in git, create manually on each VM
│
├── market_data.py          # Market overview — indices, sectors
├── stock_data.py           # Per-stock analysis (yfinance + Alpaca)
├── alpaca_data.py          # Alpaca real-time price helper
├── shioaji_data.py         # Shioaji TW real-time price helper
├── options_data.py         # Options chain + open interest
├── show_fetcher.py         # Shared YouTube RSS scraper + stock extractor
├── ebcshow.py              # EBC 達人秀 fetcher + Gemini prompt
├── ustv.py                 # USTV 錢線百分百 fetcher + Gemini prompt
├── ibd.py                  # IBD fetcher + Gemini prompt
├── cnbc.py                 # CNBC fetcher + Gemini prompt
│
├── templates/              # Jinja2 HTML templates
│   ├── index.html          # Desktop main page
│   ├── mobile.html         # Mobile main page
│   ├── market.html / market_mobile.html
│   ├── scan.html
│   ├── options.html / options_mobile.html
│   ├── serenity.html
│   ├── calendar.html
│   ├── ebcshow.html
│   ├── ustv.html
│   ├── ibd.html
│   └── cnbc.html
│
└── static/
    ├── ebcshow.json        # Cached EBC video list (written at runtime)
    ├── ustv.json           # Cached USTV video list
    ├── ibd.json            # Cached IBD video list
    ├── cnbc.json           # Cached CNBC video list
    ├── tw_stocks.json      # TW stock name/code lookup table
    ├── us_stocks_top.json  # US stock autocomplete list
    ├── manifest.json       # PWA manifest
    ├── sw.js               # Service worker (PWA offline support)
    ├── icon-192.png
    └── icon-512.png
```

---

## Local Development Setup

```bash
# 1. Clone the repo
git clone https://github.com/wapert/stock-tool.git
cd stock-tool

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env with your API keys
nano .env
# Paste the contents from the "Environment Variables" section below

# 5. Run development server
python app.py
# Or with gunicorn (recommended — matches production):
gunicorn app:app --bind 127.0.0.1:5050 --workers 1 --timeout 120 --reload

# 6. Open browser
open http://localhost:5050
```

> **Note on Shioaji (macOS/Windows):** Shioaji is a TW-only broker library. It fails gracefully if credentials are missing — the app falls back to yfinance for TW data automatically.

---

## Production Deployment (VM)

### Oracle Cloud ARM64 (Primary)

**Specs used:** ARM64 A1 Flex, 4 OCPU, 6 GB RAM, Ubuntu 22.04 LTS

#### First-time setup

```bash
# 1. SSH into the VM
ssh -i your-ssh-key.key ubuntu@your-oracle-domain.example.com

# 2. Install Miniforge (ARM64 conda — required for shioaji ARM wheels)
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
bash Miniforge3-Linux-aarch64.sh -b -p ~/miniforge3
~/miniforge3/bin/conda init bash
source ~/.bashrc

# 3. Clone the repo
cd ~
git clone https://github.com/wapert/stock-tool.git
cd stock-tool

# 4. Install Python dependencies via conda's pip
~/miniforge3/bin/pip install -r requirements.txt

# 5. Create .env with API keys
nano .env

# 6. Install Nginx and Certbot
sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx

# 7. Configure Nginx (see Nginx section below)

# 8. Obtain SSL certificate
sudo certbot --nginx -d your-oracle-domain.example.com

# 9. Create systemd service (see Systemd section below)
sudo systemctl daemon-reload
sudo systemctl enable stocktool
sudo systemctl start stocktool
sudo systemctl status stocktool
```

#### gunicorn_config.py for Oracle

```python
# /home/ubuntu/stock-tool/gunicorn_config.py
import threading

workers = 4          # 4 CPU cores, 6 GB RAM
bind    = "127.0.0.1:5050"
timeout = 300        # Gemini video analysis can take up to 2 minutes

def post_fork(server, worker):
    """Pre-warm Shioaji connection in each worker right after fork."""
    def _prewarm():
        try:
            from shioaji_data import prewarm
            ok = prewarm()
            server.log.info(f"[worker {worker.pid}] Shioaji {'connected' if ok else 'skipped'}")
        except Exception as e:
            server.log.warning(f"[worker {worker.pid}] Shioaji prewarm: {e}")
    threading.Thread(target=_prewarm, daemon=True).start()
```

---

### GCP e2-micro (Mirror)

**Specs used:** e2-micro, 2 vCPU (shared), 1 GB RAM, Ubuntu 22.04 LTS

> **Important:** Shioaji is NOT used on GCP — the 1 GB RAM is insufficient. TW stocks use yfinance (delayed data) only.

#### First-time setup

```bash
# 1. SSH into the instance
ssh -i ~/.ssh/google_compute_engine YOUR_GCP_IP
# or: gcloud compute ssh --zone=us-central1-a INSTANCE_NAME

# 2. Install dependencies
sudo apt update && sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx git

# 3. Create service user and clone repo
sudo useradd -m youruser
sudo -u youruser bash -c "
  cd /home/youruser &&
  git clone https://github.com/wapert/stock-tool.git &&
  cd stock-tool &&
  python3 -m venv venv &&
  ./venv/bin/pip install -r requirements.txt
"

# 4. Create .env (ALPACA + GEMINI only; skip SHIOAJI on GCP)
sudo -u youruser nano /home/youruser/stock-tool/.env

# 5. Configure Nginx and get SSL cert
sudo certbot --nginx -d your-gcp-domain.example.com

# 6. Create systemd service and start
sudo systemctl daemon-reload
sudo systemctl enable stocktool
sudo systemctl start stocktool
```

---

## Nginx Configuration

Create `/etc/nginx/sites-available/stocktool`:

```nginx
server {
    listen 80;
    server_name your-domain.example.com;   # Replace with your own domain

    location / {
        proxy_pass         http://127.0.0.1:5050;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
    # Certbot will append the SSL block automatically
}
```

```bash
sudo ln -s /etc/nginx/sites-available/stocktool /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## Systemd Service

### Oracle (`/etc/systemd/system/stocktool.service`)

```ini
[Unit]
Description=Stock Analysis Tool
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/stock-tool
EnvironmentFile=/home/ubuntu/stock-tool/.env
ExecStart=/home/ubuntu/miniforge3/bin/gunicorn app:app \
    --config /home/ubuntu/stock-tool/gunicorn_config.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### GCP (`/etc/systemd/system/stocktool.service`)

```ini
[Unit]
Description=Stock Analysis Tool
After=network.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/stock-tool
Environment="PATH=/home/youruser/stock-tool/venv/bin"
ExecStart=/home/youruser/stock-tool/venv/bin/gunicorn app:app \
    --bind 127.0.0.1:5050 --workers 1 --timeout 120
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Environment Variables (.env)

Place this file in the repo root on **each VM**. It is in `.gitignore` — **never commit it to git**.

```ini
# ── Alpaca Markets (US real-time quotes + portfolio sync) ──────────────────
ALPACA_KEY=PKXXXXXXXXXXXXXXXXXXXXXXXX
ALPACA_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# ── Shioaji 永豐金 (TW real-time quotes) ────────────────────────────────────
# Required on Oracle VM. Leave blank on GCP — app falls back to yfinance.
SHIOAJI_KEY=your-sinopac-api-key
SHIOAJI_SECRET=your-sinopac-api-secret

# ── Google Gemini (AI video analysis) ──────────────────────────────────────
# Required on both VMs for video page AI features.
GEMINI_API_KEY=AIzaXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

The systemd `EnvironmentFile=` (Oracle) directive injects these into the gunicorn process at startup. `python-dotenv` also loads `.env` automatically for local development.

---

## gunicorn_config.py Reference

| Parameter | Oracle | GCP | Notes |
|-----------|--------|-----|-------|
| `workers` | `4` | `1` | GCP e2-micro has only 1 GB RAM |
| `bind` | `127.0.0.1:5050` | `127.0.0.1:5050` | Nginx proxies here |
| `timeout` | `300` s | `120` s | 300 s needed for Gemini video analysis |
| `post_fork` hook | Shioaji prewarm | Not used | Initialises TW broker connection per-worker |

---

## Caching Architecture

Because gunicorn spawns **multiple isolated worker processes**, an in-memory dict is not shared between workers. The app uses two cache strategies:

### Shared file cache (cross-worker)

Used for Scan and Market page results. Prevents every gunicorn worker re-fetching independently.

| Cache file | TTL | Rebuilt when |
|------------|-----|--------------|
| `/tmp/scan_cache_US.json` | 60 s | Next scan request after TTL |
| `/tmp/scan_cache_TW.json` | 60 s | Next scan request after TTL |
| `/tmp/market_cache_US.json` | 60 s | Next market request after TTL |
| `/tmp/market_cache_TW.json` | 120 s | Next market request after TTL |

**Write pattern:** write to `.tmp` then `os.replace()` (atomic rename — prevents partial reads).

### Gemini global lock

Only one AI video analysis runs at a time, across all workers and all video pages:

```
/tmp/gemini_global.lock
  Contents: {"vid_id": "abc123", "page": "cnbc", "started": 1748000000}
```

- Acquired with `O_CREAT | O_EXCL` (atomic — fails if file exists)
- Released in `finally:` block of each analysis thread
- Auto-expires after 5 minutes (stale guard)

### Video JSON cache

Each video page stores its video list in `static/{page}.json`. On Sync:
1. Existing `static/{page}.json` is loaded as `existing` dict (keyed by video ID)
2. Current RSS feed is fetched (latest ~8–15 videos)
3. New videos are scraped; existing cached data is preserved as-is
4. Final list = `existing.values()` (all cached) with new data overlaid
5. `filter_recent_videos(days=5)` removes anything older than 5 days
6. Written back atomically

This means old videos (up to 5 days) survive every Sync, and Gemini results already saved to a video entry are never lost.

---

## Video Pages & Gemini AI

### YouTube data source

No YouTube Data API key is needed. All pages use the public RSS feed:

```
Channel:  https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}
Playlist: https://www.youtube.com/feeds/videos.xml?playlist_id={PLAYLIST_ID}
```

RSS returns the latest ~15 items per channel, ~8 per playlist. Additional details (duration, chapters, description) are scraped from the public video page.

### Video page sources

| Page | Source type | ID |
|------|------------|-----|
| EBC 達人秀 | Channel | `UCQvsuaih5lE0n_Ne54nNezg` |
| USTV 錢線百分百 | Channel | See `ustv.py` |
| IBD | Channel | `UC5fZv7bPcF5j2RsfO-9OiLA` |
| CNBC Mad Money | Playlist | `PLVbP054jv0KpV2leJ9HHIMqZEkCPX-iPV` |
| CNBC Fast Money | Playlist | `PLVbP054jv0KprCik69kaho7DjutkRPqzd` |
| CNBC Halftime Report | Playlist | `PLVbP054jv0KrF5ekWCH7TCBtsr62jGSLV` |

### Gemini analysis flow

1. User clicks **▶ 立即分析** on a video card
2. Frontend POSTs `{id: vid_id}` to `/{page}/summarize`
3. Flask acquires `/tmp/gemini_global.lock`, spawns a `threading.Thread`
4. Thread calls `summarize_{page}(youtube_url)` — sends the URL directly to Gemini API
5. Gemini streams and analyses the video, returns structured JSON in 繁體中文
6. Result written to `static/{page}.json` (atomic replace); done-marker created at `/tmp/gemini_done_{page}_{vid_id}`
7. Frontend polls `/{page}/status?id={vid_id}` every 3 s
8. On `"done"`, page calls `loadData()` to re-render the card with AI results

**Token limit:** Videos > 1 hour are rejected (exceed Gemini's ~1M token limit). A Chinese error message is shown.

### Pending state persistence (localStorage)

Analysis state survives page navigation via page-specific `localStorage` keys:

| Page | Key |
|------|-----|
| CNBC | `gemini_pending_cnbc` |
| IBD | `gemini_pending_ibd` |
| EBC 達人秀 | `gemini_pending_ebc` |
| USTV 錢線百分百 | `gemini_pending_ustv` |

On page load, `_resumePending()` reads the key and resumes polling if an analysis was in-flight when the user navigated away.

---

## Pages & Routes Reference

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/` | Desktop home (auto-detects mobile) |
| GET | `/m` | Mobile home |
| GET | `/desktop` | Force desktop view |
| GET | `/market` | Market overview page |
| GET | `/market/data?market=US\|TW` | Market data JSON (shared file cache) |
| POST | `/analyze` | Analyse stock symbols, returns JSON |
| GET | `/search/stocks?q=` | US stock autocomplete |
| GET | `/search/tw_stocks?q=` | TW stock autocomplete |
| GET | `/news?symbol=` | Stock news feed |
| GET | `/chart/ma?symbol=&market=` | Moving average chart data |
| GET | `/options` | Options page |
| GET | `/options/data?symbol=` | Options chain JSON |
| GET | `/options/oi?symbol=` | Options open interest JSON |
| GET | `/profiles` | List portfolios |
| POST | `/profiles` | Create portfolio |
| GET/PUT/DELETE | `/profiles/<name>` | Get / update / delete portfolio |
| POST | `/profiles/<name>/sync` | Sync portfolio prices (Alpaca + Shioaji) |
| GET | `/calendar` | Calendar page |
| GET | `/calendar/events` | Calendar events JSON |
| GET | `/gemini/status` | Global Gemini lock status |
| GET | `/ebcshow` | EBC 達人秀 page |
| GET | `/ebcshow/data` | EBC video list JSON |
| POST | `/ebcshow/refresh` | Trigger RSS sync |
| POST | `/ebcshow/summarize` | Start Gemini analysis |
| GET | `/ebcshow/status?id=` | Poll analysis status |
| GET | `/ustv` | USTV 錢線百分百 page |
| GET/POST | `/ustv/data`, `/ustv/refresh`, `/ustv/summarize`, `/ustv/status` | Same pattern as EBC |
| GET | `/ibd` | IBD page |
| GET/POST | `/ibd/data`, `/ibd/refresh`, `/ibd/summarize`, `/ibd/status` | Same pattern |
| GET | `/cnbc` | CNBC page |
| GET/POST | `/cnbc/data`, `/cnbc/refresh`, `/cnbc/summarize`, `/cnbc/status` | Same pattern |

---

## Annual Maintenance — Calendar Data

The calendar page combines **auto-calculated** events (works forever) with **year-specific** data stored in `static/calendar_data.json`. The JSON file currently covers 2025–2027. Each January, add the new year's data so the calendar stays current.

### What is auto-calculated (no action needed)

| Data | Rule |
|------|------|
| US market holidays | Computed from fixed rules (MLK = 3rd Monday Jan, Easter formula, etc.) |
| TAIFEX settlement dates | 3rd Wednesday of each month, shifted back if it falls on a TW holiday |
| Taiwan earnings deadlines | Always Mar 31 / May 15 / Aug 14 / Nov 14 |

### What needs annual update (add to `calendar_data.json` each January)

| Data | Source | Frequency |
|------|--------|-----------|
| FOMC meeting dates | [federalreserve.gov](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) | Released ~12 months ahead |
| Taiwan market holidays | [TWSE holiday announcement](https://www.twse.com.tw/zh/products/holiday/holiday.html) | Published each October for next year |
| Taiwan CBC meeting dates | [cbc.gov.tw](https://www.cbc.gov.tw/tw/cp-1046-208610-A0053-1.html) | Published quarterly |

### Step-by-step update guide

#### 1. Open the calendar data file

```bash
# On your local machine (then push via git):
nano stock_app/static/calendar_data.json
```

#### 2. Add FOMC data for the new year

Go to https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm and find the schedule for the new year. Each meeting is a two-day event; the **second day** is the announcement day.

Meetings with a dot plot (★) occur in March, June, September, December.

Add a new key under `"fomc"`:

```json
"fomc": {
  "2027": [ ... existing ... ],
  "2028": [
    {"start": "2028-01-25", "end": "2028-01-26", "dot": false},
    {"start": "2028-03-19", "end": "2028-03-20", "dot": true},
    {"start": "2028-04-25", "end": "2028-04-26", "dot": false},
    {"start": "2028-06-13", "end": "2028-06-14", "dot": true},
    {"start": "2028-07-25", "end": "2028-07-26", "dot": false},
    {"start": "2028-09-19", "end": "2028-09-20", "dot": true},
    {"start": "2028-10-24", "end": "2028-10-25", "dot": false},
    {"start": "2028-12-12", "end": "2028-12-13", "dot": true}
  ]
}
```

> **`"dot": true`** marks meetings that include the Summary of Economic Projections (dot plot). These are always the March / June / September / December meetings.

#### 3. Add Taiwan market holidays for the new year

Go to the [TWSE holiday page](https://www.twse.com.tw/zh/products/holiday/holiday.html) — the announcement for the following year is published each October.

Add a new key under `"tw_holidays"`:

```json
"tw_holidays": {
  "2027": { ... existing ... },
  "2028": {
    "2028-01-01": "元旦",
    "2028-01-26": "農曆年封關",
    "2028-01-27": "春節",
    "2028-01-28": "春節",
    "2028-01-29": "春節",
    "2028-01-30": "春節",
    "2028-02-28": "和平紀念日",
    "2028-04-04": "兒童節/清明節",
    "2028-05-01": "勞動節",
    "2028-06-08": "端午節",
    "2028-09-15": "中秋節",
    "2028-10-10": "國慶日"
  }
}
```

> **Special dates to watch:**
> - 春節 (Lunar New Year) — date shifts each year; check TWSE announcement
> - 補假 (make-up holidays) — government announces these; check TWSE announcement
> - 農曆年封關 / 春節後開盤 — last trading day before and first trading day after CNY

#### 4. Add Taiwan CBC meeting dates for the new year

The CBC holds **four quarterly meetings** per year, typically the third Thursday of March, June, September, and December. Check [cbc.gov.tw](https://www.cbc.gov.tw) for the official schedule.

Add a new key under `"cbc_dates"`:

```json
"cbc_dates": {
  "2027": { ... existing ... },
  "2028": {
    "2028-03-16": "台灣央行利率決策 (Q1 2028)",
    "2028-06-15": "台灣央行利率決策 (Q2 2028)",
    "2028-09-21": "台灣央行利率決策 (Q3 2028)",
    "2028-12-21": "台灣央行利率決策 (Q4 2028)"
  }
}
```

#### 5. Commit, push, and deploy

```bash
cd stock_app
git add static/calendar_data.json
git commit -m "chore: add 2028 calendar data (FOMC, TW holidays, CBC)"
git push origin main
```

Then pull on both VMs (see [Updating / Re-deploying](#updating--re-deploying)).

### Missing year warning

If someone opens the calendar for a year not yet in `calendar_data.json`, the UI automatically shows a warning event:

> ⚠️ 2028 FOMC 資料未更新 — 請至 static/calendar_data.json 新增 2028 年 FOMC 資料

This is a reminder to add data; it does not break other calendar events.

### JSON file structure reference

```
static/calendar_data.json
├── fomc
│   └── "YYYY": [ {start, end, dot}, ... ]   ← 8 meetings per year
├── tw_holidays
│   └── "YYYY": { "YYYY-MM-DD": "名稱", ... }
└── cbc_dates
    └── "YYYY": { "YYYY-MM-DD": "名稱", ... }   ← 4 entries per year
```

---

## Updating / Re-deploying

Push your changes to GitHub, then on each VM:

**Oracle:**
```bash
ssh -i your-key.key ubuntu@your-oracle-domain.example.com \
  "cd ~/stock-tool && git pull origin main && \
   kill -9 \$(pgrep -f gunicorn) 2>/dev/null; sleep 1 && \
   nohup ~/miniforge3/bin/gunicorn app:app \
     --config gunicorn_config.py > /tmp/stock_gunicorn.log 2>&1 &"
```

**GCP:**
```bash
ssh YOUR_GCP_IP \
  "sudo -u youruser bash -c 'cd /home/youruser/stock-tool && git pull origin main' && \
   sudo systemctl restart stocktool.service"
```

Or restart via systemd (if service is configured on Oracle too):
```bash
sudo systemctl restart stocktool
```

---

## Troubleshooting

### App won't start
```bash
sudo journalctl -u stocktool -n 50 --no-pager
# Or if started manually:
cat /tmp/stock_gunicorn.log
```

### Gunicorn timeout / workers crash
- Increase `timeout` in `gunicorn_config.py` — Gemini analysis can take 2+ minutes
- Check free RAM: `free -m` — if < 200 MB, reduce worker count

### Shioaji connection fails (Oracle)
- Verify `SHIOAJI_KEY` and `SHIOAJI_SECRET` are in `.env`
- Confirm API access is enabled on your 永豐金 account (production key required for live data)
- To use simulation mode: set `_simulation = True` in `shioaji_data.py`

### Gemini analysis stuck
```bash
# Check if the global lock is stale (auto-expires after 5 min)
cat /tmp/gemini_global.lock
# Remove stale lock manually:
rm -f /tmp/gemini_global.lock
```

### Video sync drops old videos
- Each video fetcher must use `existing.values()` (not `results`) when building `all_videos`
- The 5-day retention filter runs after merging, so within-5-day videos are always kept
- Gemini results saved to a video entry survive syncs (they are in `existing`)

### Nginx 502 Bad Gateway
```bash
# Verify gunicorn is running on port 5050
ss -tlnp | grep 5050
# Validate nginx config
sudo nginx -t
# Check nginx error log
sudo tail -20 /var/log/nginx/error.log
```

### HTTPS certificate expired
```bash
sudo certbot renew
sudo systemctl reload nginx
```

### Stale shared file cache
```bash
# Safe to delete — caches rebuild automatically on next request
rm -f /tmp/scan_cache_*.json /tmp/market_cache_*.json
```

---

## Security Notes

- `.env` is **never committed** to git — create it manually on each VM after cloning
- All API keys are injected via environment variables only (never hardcoded)
- The two VMs share the same git repo but have **independent** `.env` files and Gemini API quotas
- The Gemini lock file is local to each VM — both VMs can run analyses simultaneously
- No user authentication is implemented — the app is intended for personal use on a private domain
- Nginx `proxy_set_header X-Real-IP` forwards the real client IP for access logs
