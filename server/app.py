"""
BirdNET server — FastAPI wrapper around birdnetlib for the PWA.

Endpoints:
  GET  /healthz          health probe
  POST /analyze          multipart audio + lat/lon/week → top-k species hits
  POST /enrich           {species, lat, lon} → Claude narrative + eBird regional check
  GET  /history          recent detections from sqlite
  GET  /detection/{id}   single detection record + audio file path

Audio is persisted to audio_log/ and metadata to db/birdnet.sqlite.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import httpx
import soundfile as sf
from anthropic import Anthropic
from birdnetlib import Recording
from birdnetlib.analyzer import Analyzer
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
AUDIO_DIR = ROOT / "audio_log"
DB_PATH = ROOT / "db" / "birdnet.sqlite"

# Load Sean's clawd .env first (has ANTHROPIC_API_KEY), then local .env override.
load_dotenv(Path.home() / "clawd" / ".env")
load_dotenv(ROOT / ".env", override=True)

EBIRD_API_KEY = os.getenv("EBIRD_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
DEFAULT_MIN_CONF = float(os.getenv("BIRDNET_MIN_CONFIDENCE", "0.15"))

AUDIO_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Single Analyzer instance — model load is slow, ~5s cold.
analyzer = Analyzer()

anthropic_client: Optional[Anthropic] = None
if ANTHROPIC_API_KEY:
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS detections (
              id            TEXT PRIMARY KEY,
              ts            TEXT NOT NULL,
              lat           REAL,
              lon           REAL,
              week          INTEGER,
              min_conf      REAL,
              audio_path    TEXT,
              top_label     TEXT,
              top_sci       TEXT,
              top_conf      REAL,
              hits_json     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS detections_ts ON detections(ts DESC);
            """
        )


init_db()

app = FastAPI(title="BirdNET PWA Server", version="0.1.0")

# PWA on github pages + local + tailscale funnel can all hit us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "anthropic": bool(anthropic_client),
        "ebird": bool(EBIRD_API_KEY),
        "model_loaded": analyzer is not None,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
    }


@app.post("/analyze")
async def analyze(
    audio: UploadFile = File(...),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
    week: Optional[int] = Form(None),
    min_confidence: float = Form(DEFAULT_MIN_CONF),
) -> JSONResponse:
    if audio.content_type and not audio.content_type.startswith(("audio/", "video/", "application/octet-stream")):
        # browser MediaRecorder sometimes labels webm as video/webm with opus audio — still fine
        pass

    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "empty audio payload")

    det_id = uuid.uuid4().hex[:12]
    now = dt.datetime.utcnow()
    week_val = week or int(now.strftime("%V"))

    # Persist raw upload, then convert to wav for BirdNET (it wants PCM).
    src_ext = (audio.filename or "clip").rsplit(".", 1)[-1].lower()
    if src_ext not in {"wav", "webm", "ogg", "m4a", "mp3", "flac"}:
        src_ext = "webm"
    raw_path = AUDIO_DIR / f"{det_id}.{src_ext}"
    raw_path.write_bytes(raw)

    wav_path = AUDIO_DIR / f"{det_id}.wav"
    try:
        await _to_wav(raw_path, wav_path)
    except Exception as exc:
        raise HTTPException(400, f"audio decode failed: {exc}") from exc

    recording = Recording(
        analyzer,
        str(wav_path),
        lat=lat if lat is not None else None,
        lon=lon if lon is not None else None,
        week_48=week_val,
        min_conf=float(min_confidence),
    )
    recording.analyze()

    raw_hits = recording.detections or []
    # Collapse per-segment hits into per-species best hits.
    by_species: dict[str, dict] = {}
    for h in raw_hits:
        sci = h.get("scientific_name") or h.get("scientific") or ""
        label = h.get("common_name") or h.get("common") or sci
        conf = float(h.get("confidence", 0.0))
        key = sci or label
        if key not in by_species or conf > by_species[key]["confidence"]:
            by_species[key] = {
                "common_name": label,
                "scientific_name": sci,
                "confidence": conf,
                "start_s": float(h.get("start_time", 0.0)),
                "end_s": float(h.get("end_time", 0.0)),
            }
    hits = sorted(by_species.values(), key=lambda x: x["confidence"], reverse=True)[:5]

    top = hits[0] if hits else None
    with db() as conn:
        conn.execute(
            "INSERT INTO detections "
            "(id, ts, lat, lon, week, min_conf, audio_path, top_label, top_sci, top_conf, hits_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                det_id,
                now.isoformat() + "Z",
                lat,
                lon,
                week_val,
                float(min_confidence),
                str(raw_path.relative_to(ROOT)),
                top["common_name"] if top else None,
                top["scientific_name"] if top else None,
                top["confidence"] if top else None,
                json.dumps(hits),
            ),
        )

    return JSONResponse(
        {
            "id": det_id,
            "ts": now.isoformat() + "Z",
            "lat": lat,
            "lon": lon,
            "week": week_val,
            "min_confidence": min_confidence,
            "hits": hits,
            "audio_url": f"/audio/{raw_path.name}",
        }
    )


@app.get("/audio/{name}")
def get_audio(name: str) -> FileResponse:
    p = AUDIO_DIR / name
    if not p.exists() or ".." in name:
        raise HTTPException(404, "audio not found")
    return FileResponse(p)


class EnrichBody(BaseModel):
    species: str
    scientific_name: Optional[str] = None
    confidence: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


@app.post("/enrich")
async def enrich(body: EnrichBody) -> dict:
    out: dict = {"species": body.species, "scientific_name": body.scientific_name}

    out["ebird"] = await _ebird_regional_check(body.species, body.scientific_name, body.lat, body.lon)
    out["narrative"] = await _claude_narrative(body, out["ebird"])
    out["wikipedia"] = await _wikipedia_summary(body.species, body.scientific_name)

    return out


@app.get("/history")
def history(limit: int = 50) -> dict:
    limit = max(1, min(limit, 500))
    with db() as conn:
        rows = conn.execute(
            "SELECT id, ts, lat, lon, top_label, top_sci, top_conf "
            "FROM detections ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@app.get("/detection/{det_id}")
def detection(det_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM detections WHERE id = ?", (det_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such detection")
    d = dict(row)
    d["hits"] = json.loads(d.pop("hits_json", "[]"))
    return d


# Serve the PWA from /web as the site root (Mac-side access).
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _to_wav(src: Path, dst: Path) -> None:
    """Convert any browser-recorded audio to 48k mono WAV via ffmpeg.

    BirdNET resamples to 48k internally, but soundfile can't always read webm/opus.
    Shell out to ffmpeg — fast and bulletproof.
    """
    import asyncio

    if src.suffix.lower() == ".wav":
        # Re-encode anyway to normalize sample rate / channels.
        pass

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        "48000",
        "-f",
        "wav",
        str(dst),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "ignore")[:500])


async def _ebird_regional_check(common: str, sci: Optional[str], lat: Optional[float], lon: Optional[float]) -> dict:
    """Cross-check whether the detected species has been seen in the area recently."""
    if not (EBIRD_API_KEY and lat is not None and lon is not None):
        return {"available": False, "reason": "no api key or no geo"}

    headers = {"X-eBirdApiToken": EBIRD_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Recent obs of this species within 25km.
            params = {"lat": lat, "lng": lon, "dist": 25, "back": 30}
            sci_q = sci or common
            r = await client.get(
                f"https://api.ebird.org/v2/data/obs/geo/recent/{sci_q}",
                params=params,
                headers=headers,
            )
            r.raise_for_status()
            obs = r.json() or []
            # Hotspots near here too.
            r2 = await client.get(
                "https://api.ebird.org/v2/ref/hotspot/geo",
                params={"lat": lat, "lng": lon, "dist": 25, "fmt": "json"},
                headers=headers,
            )
            r2.raise_for_status()
            hotspots = r2.json() or []
    except Exception as exc:
        return {"available": False, "reason": f"ebird error: {exc}"}

    return {
        "available": True,
        "recent_obs_count": len(obs),
        "nearest_obs": obs[:3],
        "hotspots_nearby": [h.get("locName") for h in hotspots[:5]],
    }


async def _claude_narrative(body: EnrichBody, ebird: dict) -> Optional[str]:
    if not anthropic_client:
        return None

    plausibility = ""
    if ebird.get("available"):
        n = ebird.get("recent_obs_count", 0)
        plausibility = (
            f"eBird shows {n} recent observations of this species within 25km in the last 30 days."
            if n
            else "eBird shows no recent observations of this species within 25km in the last 30 days — uncommon here right now."
        )

    prompt = (
        f"You are a sharp birding guide. The user just identified a bird from audio: "
        f"**{body.species}** ({body.scientific_name or 'unknown'}) with confidence "
        f"{(body.confidence or 0)*100:.0f}%. Location: lat={body.lat}, lon={body.lon}. {plausibility}\n\n"
        "Give a tight 4-6 sentence brief covering: what it sounds like, what to look for visually, "
        "habitat, and any notable behavior. Plain prose, no markdown headings, no bullets. "
        "If the confidence is low or eBird says it's uncommon, note that and suggest one likely-mistaken-for species."
    )

    try:
        msg = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if hasattr(b, "text"))
    except Exception as exc:
        return f"(claude error: {exc})"


async def _wikipedia_summary(common: str, sci: Optional[str]) -> Optional[dict]:
    title = (sci or common).replace(" ", "_")
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
            if r.status_code != 200 and sci:
                # fall back to common name
                r = await client.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{common.replace(' ', '_')}"
                )
            if r.status_code != 200:
                return None
            data = r.json()
        return {
            "title": data.get("title"),
            "extract": data.get("extract"),
            "url": (data.get("content_urls", {}).get("desktop") or {}).get("page"),
            "thumbnail": (data.get("thumbnail") or {}).get("source"),
        }
    except Exception:
        return None
