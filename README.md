# Scout Index — local app

A dark-themed, searchable player database with real market value history charts
and transfer records — built on top of the open **transfermarkt-datasets** project
(https://github.com/dcaribou/transfermarkt-datasets), which extracts and republishes
Transfermarkt data weekly.

This covers the **entire dataset** (tens of thousands of players across major
leagues), not just a top-N snapshot — so "top 500", "top 1000", or "every player
who's ever had a market value" are all just a filter/sort away.

## Why this exists instead of a static site

Transfermarkt itself blocks automated scraping, and I (Claude) can't reach it or
the dataset's file host from my sandboxed environment. Your machine has no such
restriction, so this app downloads the data once, locally, when you run it.

## Setup

```bash
cd scout-index-app
pip install -r requirements.txt
python app.py
```

First run downloads four files (players, market value history, transfers, clubs —
roughly 150–300MB combined, exact size depends on the dataset's current state) into
`./data/`. This can take a few minutes depending on your connection. Every run after
that is instant, since the files are cached locally.

Then open **http://localhost:5000** in your browser.

## What you get

- **Search** any player by name, club, or nationality across the full dataset
- **Filter** by position and nationality, **sort** by value, name, or age
- Click any player card for:
  - Full profile (age, height, foot, contract expiry, agent)
  - **Market value history chart** — the actual value-over-time graph
  - **Transfer history** — every recorded move with fee and value-at-time
- Everything is real, sourced data — nothing here is estimated or invented

## Updating the data later

The dataset refreshes weekly upstream. To pull a fresh copy, just delete the
`data/` folder and re-run `python app.py` — it'll re-download automatically.

```bash
rm -rf data/
python app.py
```

## AI Q&A ("Ask the desk")

The AI panel calls a server-side route (`/api/ask`) backed by **Groq's free tier**
(no credit card, 14,400 requests/day) so no cost lands on you and no API key is
ever exposed to the browser.

1. Get a free key at https://console.groq.com/keys
2. Locally: copy `.env.example` to `.env`, paste your key in, and either
   `export $(cat .env | xargs)` before running, or use `python-dotenv` / your
   shell's usual method to load it as an environment variable named `GROQ_API_KEY`.
3. On a host (Vercel, Render, etc.): add `GROQ_API_KEY` in the project's
   environment variable settings and redeploy.

If the key isn't set, the panel will show a clear "not configured yet" message
instead of failing silently.

## Building the small, deployable dataset (`trimmer.py`)

This app runs in two modes, auto-detected — you don't configure anything by hand:

1. **Full mode** (what you've been running): no `data_trimmed/` folder exists yet,
   so `app.py` downloads the complete dataset into `data/` and loads all of it.
2. **Trimmed mode**: once `data_trimmed/*.parquet` exists, `app.py` loads those
   instead — no download, no network call, starts instantly. This is what you deploy.

To generate the trimmed dataset:

```bash
# 1. Make sure the full dataset has been downloaded at least once
python app.py     # let it finish starting, then Ctrl+C

# 2. Build the trimmed version (top 10,000 players by market value, by default)
python trimmer.py

# 3. Confirm it worked
python app.py     # should now say "Loading trimmed dataset... (no download needed)"
```

`trimmer.py` keeps the top N players (`--top 10000` by default), their **complete**
market value history (the graph isn't trimmed — every kept player keeps their full
timeline), their complete transfer history, and every club referenced by either.
Output lands in `data_trimmed/` as Parquet files, typically a few MB to a few tens
of MB combined depending on `--top`. Commit that folder to your repo — that's the
only "data download" your deployment will ever need.

Want a different cutoff later? `python trimmer.py --top 5000` (or any number),
then re-commit.

## Deploying to Vercel

As of mid-2026, Vercel auto-detects Flask apps with **zero config** — it just looks
for an `app` variable in `app.py`. With `data_trimmed/` committed to the repo:

1. Push this folder to a GitHub repo (make sure `data_trimmed/` is **not** in
   `.gitignore` — it needs to ship with the deployment; `data/` with the full
   CSVs should stay ignored, it's only for local trimming).
2. Import the repo in the Vercel dashboard, or run `vercel` from this folder.
3. In the project's Settings → Environment Variables, add `GROQ_API_KEY`.
4. Deploy. `vercel.json` here just bumps the function's max duration to 30s so the
   AI route has room to breathe — Vercel handles everything else automatically.

Your bundle (app code + `data_trimmed/`) needs to stay under Vercel's 500MB function
bundle limit — a 10,000-player trimmed dataset is nowhere close to that.

## Notes

- This runs entirely on your machine. No data about what you search is sent anywhere.
- Market values are Transfermarkt's own community-estimated valuations, not sale prices.
- Some lower-profile players may have sparse value history or no recorded transfers —
  that's a gap in the underlying data, not a bug.
- If the download URL ever goes stale, check
  https://github.com/dcaribou/transfermarkt-datasets for the current link and update
  `BASE_URL` in `app.py`.
