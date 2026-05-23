# BirdNET — phone-to-mac bird ID

Tap a button on your phone. The Mac listens with Cornell's BirdNET model. Top hits come back with a narrative (Claude) and a regional plausibility check (eBird).

**Live PWA:** https://seanthomasevans.github.io/birdnet/
**API endpoint (Mac via Tailscale Funnel):** https://seans-macbook-pro-m3.tail65b106.ts.net:8443

```
┌─────────────┐     HTTPS (Tailscale Funnel)     ┌─────────────────┐
│  phone PWA  │ ───────────────────────────────▶ │   FastAPI       │
│ (GH Pages)  │ ◀─────────────────────────────── │   BirdNET       │
└─────────────┘       JSON: hits + audio_url     │   Mac M3 36GB   │
                                                 └─────────────────┘
```

## Stack

- **Model:** [BirdNET-Analyzer](https://github.com/kahst/BirdNET-Analyzer) via [`birdnetlib`](https://pypi.org/project/birdnetlib/). Cornell Lab of Ornithology + TU Chemnitz. ~6000 species. MIT-licensed model weights.
- **Server:** FastAPI + `birdnetlib` + sqlite, runs on the Mac.
- **Frontend:** Vanilla JS PWA, deployed to GitHub Pages. Records audio via `MediaRecorder`, sends to server with browser geolocation.
- **Reach:** Tailscale Funnel gives the Mac a public HTTPS URL at `*.ts.net`, so the PWA on GitHub Pages can talk to it from anywhere.
- **Enrichment:** Claude Opus 4.7 writes the species brief. eBird API checks "common here right now?" Wikipedia provides the thumbnail.

## Quick start

```bash
# 1. install deps
cd ~/workspace/birdnet
python3 -m venv .venv
source .venv/bin/activate
pip install birdnetlib fastapi 'uvicorn[standard]' python-multipart \
            soundfile httpx python-dotenv anthropic librosa resampy tensorflow

# 2. config
cp .env.example .env       # add ANTHROPIC_API_KEY (optional, for narratives)
                           # add EBIRD_API_KEY    (optional, for regional check)

# 3. run
./server/run.sh            # listens on 0.0.0.0:8000

# 4. expose via Tailscale Funnel (one time)
tailscale funnel 8000      # gives you https://<host>.<tailnet>.ts.net
```

Open the PWA on your phone (either the GH Pages URL or the served `/` from the Mac) → tap the button.

## Endpoints

| Method | Path                | Body / params                                       | Returns                          |
|--------|---------------------|-----------------------------------------------------|----------------------------------|
| GET    | `/healthz`          | —                                                   | `{ok, anthropic, ebird, ts}`     |
| POST   | `/analyze`          | multipart `audio`, `lat`, `lon`, `week`, `min_confidence` | `{id, hits[], audio_url}` |
| POST   | `/enrich`           | `{species, scientific_name, confidence, lat, lon}`  | `{narrative, ebird, wikipedia}`  |
| GET    | `/history`          | `?limit=50`                                         | `{items[], count}`               |
| GET    | `/detection/{id}`   | —                                                   | full record                      |
| GET    | `/audio/{name}`     | —                                                   | raw audio file                   |

## Geolocation

BirdNET takes `lat`, `lon`, `week_48` as model inputs — it filters its 6000-species softmax to species plausible in that region/season. The PWA grabs the browser's geolocation and passes it on every `/analyze` call.

## Indigenous bird names + territory acknowledgment

On detection, the PWA also surfaces:

- **Whose traditional territory** the listener is standing on (via the Native Land Digital API when a key is configured, with a coarse regional fallback for Ontario otherwise).
- **The bird's name** in the Indigenous language(s) traditionally spoken at that location — written in the language's orthography, with a phonetic guide, an audio pronunciation when sourced, and a citation back to the community-led dictionary that holds the word.

The v0.1 dataset (`server/data/indigenous_names.json`) covers 15 common Ontario birds in **Anishinaabemowin**, every entry cited to the Ojibwe People's Dictionary. **Kanien'kéha (Mohawk)** and **Cree (nēhiyawēwin / ililīmowin)** language scaffolding is in place; names will be added as community-led sources are integrated.

Principles:

- **Cited or not present.** Every name links to its source dictionary. We never fabricate names or generate phonetics speculatively.
- **Community labour stays visible.** Audio playback links back to the source so the speaker's contribution is acknowledged.
- **The dataset is small on purpose.** Quality, attribution, and community-led sourcing beat coverage.

To upgrade territory lookup from the regional fallback to per-point boundaries, get a free Native Land Digital API key at <https://api-docs.native-land.ca> and set `NATIVELAND_API_KEY` in `.env`.

## Privacy

Audio + metadata live on your Mac in `audio_log/` and `db/birdnet.sqlite`. Nothing is sent to a third party unless you tap **tell me about this bird** (which then calls Claude + eBird + Wikipedia + Native Land Digital for that one species).

## Tradeoffs

- **In-browser TF.js**: rejected. The model is ~50MB to download, mobile inference is slower, and you lose the ability to do server-side enrichment cleanly.
- **Continuous listening (BirdNET-Pi style)**: not the goal here. Tap-to-listen is intentional for spot ID.
- **Mac sleep**: when the Mac sleeps, the Funnel goes down. Either `caffeinate -d` while birding or move the server to a Pi.
