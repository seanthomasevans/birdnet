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
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.indigenous import dataset_coverage, lookup_names, lookup_territory
from server.listener import Broadcaster, Listener

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

LISTENER_RTSP_URL = os.getenv("LISTENER_RTSP_URL", "").strip()
LISTENER_LAT = float(os.getenv("LISTENER_LAT", "44.92"))
LISTENER_LON = float(os.getenv("LISTENER_LON", "-79.37"))
LISTENER_CHUNK_SECS = int(os.getenv("LISTENER_CHUNK_SECS", "10"))
LISTENER_MIN_CONF = float(os.getenv("LISTENER_MIN_CONF", "0.4"))

AUDIO_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Single Analyzer instance — model load is slow, ~5s cold.
analyzer = Analyzer()

# birdnetlib's bundled extract_embeddings() fails on the stock interpreter
# because intermediate tensors aren't preserved. Rebuild the interpreter
# with experimental_preserve_all_tensors=True so we can pull the 1024-dim
# embedding tensor (the GLOBAL_AVG_POOL layer immediately before the 6522-
# class softmax). This is what we cluster on for individual-bird ID.
try:
    import tensorflow as _tf
    analyzer.interpreter = _tf.lite.Interpreter(
        model_path=analyzer.model_path,
        experimental_preserve_all_tensors=True,
    )
    analyzer.interpreter.allocate_tensors()
except Exception as _e:
    import logging as _logging
    _logging.getLogger("birdnet").warning(
        "embeddings disabled — could not rebuild interpreter: %s", _e
    )

INDIVIDUAL_THRESHOLD = float(os.getenv("INDIVIDUAL_COSINE_THRESHOLD", "0.12"))

anthropic_client: Optional[Anthropic] = None
if ANTHROPIC_API_KEY:
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# In-process pubsub for the continuous-listener wall projection.
broadcaster = Broadcaster()
listener: Optional[Listener] = None


async def _enrich_for_listener(scientific_name: str, lat: float, lon: float) -> dict:
    """Lighter enrich for the listener loop — no Claude call (too slow,
    too costly to run on every detection). Just territory + Indigenous names."""
    territory = await lookup_territory(lat, lon)
    languages = (territory or {}).get("languages_at_location") or None
    indigenous = lookup_names(scientific_name, languages)
    return {"territory": territory, "indigenous": indigenous}


def _assign_individual(scientific_name: str, embedding: "np.ndarray") -> str:
    """Online cosine-distance clustering for individual-bird ID.

    For each new detection of a species we already have embeddings for,
    find the nearest existing centroid. If cosine distance < threshold, the
    detection joins that cluster (same bird). Otherwise mint a new cluster.

    Centroid here is the mean of all embeddings stored for a given
    individual_id — recomputed on the fly from sqlite. This is fine for
    hundreds to a few thousand detections per species; if it ever gets
    expensive we can cache centroids in a separate table.

    The threshold is an empirical knob — INDIVIDUAL_COSINE_THRESHOLD env
    var, default 0.12. Tighter = more clusters (over-splits); looser = more
    lumping (under-splits). 0.10–0.15 is reasonable for BirdNET embeddings;
    needs to be tuned against actual cottage data over a few days."""
    import numpy as _np
    species_slug = "".join(c if c.isalnum() else "-" for c in scientific_name.lower()).strip("-")
    vec = _np.asarray(embedding, dtype="float32")
    vec_norm = vec / (_np.linalg.norm(vec) + 1e-9)

    with db() as conn:
        rows = conn.execute(
            "SELECT individual_id, embedding FROM detections "
            "WHERE top_sci = ? AND embedding IS NOT NULL AND individual_id IS NOT NULL",
            (scientific_name,),
        ).fetchall()

    if not rows:
        return f"{species_slug}-001"

    # Bucket existing embeddings by individual_id, compute per-cluster centroid.
    buckets: dict[str, list] = {}
    for r in rows:
        try:
            v = _np.frombuffer(r["embedding"], dtype="float32")
            if v.shape[0] != vec.shape[0]:
                continue
        except Exception:
            continue
        buckets.setdefault(r["individual_id"], []).append(v)

    best_id, best_dist = None, 1.0
    for ind_id, vecs in buckets.items():
        centroid = _np.mean(_np.stack(vecs), axis=0)
        centroid /= (_np.linalg.norm(centroid) + 1e-9)
        dist = 1.0 - float(_np.dot(vec_norm, centroid))
        if dist < best_dist:
            best_dist = dist
            best_id = ind_id

    if best_id is not None and best_dist < INDIVIDUAL_THRESHOLD:
        return best_id

    # Mint a new cluster numbered after the existing count.
    existing_nums = []
    prefix = f"{species_slug}-"
    for ind_id in buckets.keys():
        if ind_id.startswith(prefix):
            try:
                existing_nums.append(int(ind_id[len(prefix):]))
            except ValueError:
                pass
    next_num = (max(existing_nums) + 1) if existing_nums else 1
    return f"{prefix}{next_num:03d}"


async def _persist_listener_detection(event: dict, chunk_path: Path) -> Optional[str]:
    """Copy the listener's chunk audio into audio_log/ and write a sqlite row.
    Returns the relative audio path for inclusion in the broadcast event.

    Catalog history depends on this: every detection that lights up the wall
    also lands a row in detections{source='listener'} so /catalog/today can
    reconstruct the day. The raw audio is kept so a later pass can pull
    BirdNET embeddings for individual identification."""
    import shutil as _shutil
    det_id = uuid.uuid4().hex[:12]
    dest = AUDIO_DIR / f"listener_{det_id}.wav"
    try:
        _shutil.copyfile(chunk_path, dest)
        audio_rel = str(dest.relative_to(ROOT))
    except Exception:
        audio_rel = None

    sci = event.get("scientific_name") or ""
    common = event.get("common_name") or ""
    conf = float(event.get("confidence") or 0.0)
    week_val = int(dt.datetime.utcnow().isocalendar().week)
    hits = [{
        "common_name": common,
        "scientific_name": sci,
        "confidence": conf,
    }]

    # Individual ID — only when we got an embedding from the chunk processor.
    embedding_blob = None
    individual_id = None
    emb = event.get("_embedding")
    if emb is not None:
        try:
            import numpy as _np
            arr = _np.asarray(emb, dtype="float32")
            embedding_blob = arr.tobytes()
            individual_id = _assign_individual(sci, arr)
        except Exception as exc:
            import logging as _logging
            _logging.getLogger("birdnet").warning(
                "individual id failed for %s: %s", sci, exc
            )

    with db() as conn:
        conn.execute(
            "INSERT INTO detections "
            "(id, ts, lat, lon, week, min_conf, audio_path, top_label, top_sci, "
            " top_conf, hits_json, source, territory_json, indigenous_json, "
            " embedding, individual_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                det_id,
                event.get("ts") or (dt.datetime.utcnow().isoformat() + "Z"),
                event.get("lat"),
                event.get("lon"),
                week_val,
                LISTENER_MIN_CONF,
                audio_rel,
                common,
                sci,
                conf,
                json.dumps(hits),
                "listener",
                json.dumps(event.get("territory")) if event.get("territory") else None,
                json.dumps(event.get("indigenous")) if event.get("indigenous") else None,
                embedding_blob,
                individual_id,
            ),
        )
    event["id"] = det_id
    if individual_id:
        event["individual_id"] = individual_id
    # Strip the embedding before broadcasting — clients don't need the
    # 1024-float blob, and it bloats every WebSocket frame.
    event.pop("_embedding", None)
    return f"/audio/{dest.name}" if audio_rel else None


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
        # Additive migrations: new columns get appended in place so an existing
        # db file from v0.1 keeps working without a rebuild. Each ADD is wrapped
        # because SQLite raises if the column already exists.
        for col, decl in [
            ("source", "TEXT DEFAULT 'tap'"),
            ("territory_json", "TEXT"),
            ("indigenous_json", "TEXT"),
            ("embedding", "BLOB"),
            ("individual_id", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE detections ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS detections_source_ts "
            "ON detections(source, ts DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS detections_sci_ind "
            "ON detections(top_sci, individual_id)"
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


@app.on_event("startup")
async def _start_listener() -> None:
    global listener
    if not LISTENER_RTSP_URL:
        return
    listener = Listener(
        rtsp_url=LISTENER_RTSP_URL,
        lat=LISTENER_LAT,
        lon=LISTENER_LON,
        chunk_secs=LISTENER_CHUNK_SECS,
        min_conf=LISTENER_MIN_CONF,
        analyzer=analyzer,
        broadcaster=broadcaster,
        enrich_cb=_enrich_for_listener,
        persist_cb=_persist_listener_detection,
    )
    await listener.start()


@app.on_event("shutdown")
async def _stop_listener() -> None:
    if listener:
        await listener.stop()


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "anthropic": bool(anthropic_client),
        "ebird": bool(EBIRD_API_KEY),
        "model_loaded": analyzer is not None,
        "indigenous_dataset": dataset_coverage(),
        "listener": {
            "configured": bool(LISTENER_RTSP_URL),
            "running": bool(listener and listener.task and not listener.task.done()),
            "subscribers": broadcaster.subscriber_count(),
            "stats": listener.stats if listener else None,
        },
        "ts": dt.datetime.utcnow().isoformat() + "Z",
    }


@app.post("/stream/test")
async def stream_test(scientific_name: str = "Cyanocitta cristata", persist: bool = False) -> dict:
    """Fire a synthetic detection through the broadcaster — visually validate
    the /wall viewer without waiting on a real bird at the cottage.
    Pass persist=true to also write a row to the catalog so the today-strip
    picks it up; otherwise the test event is broadcast-only and the diary
    stays honest."""
    territory = await lookup_territory(LISTENER_LAT, LISTENER_LON)
    languages = (territory or {}).get("languages_at_location") or None
    indigenous = lookup_names(scientific_name, languages)
    event = {
        "type": "detection",
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "common_name": (indigenous.get("common_name") or scientific_name),
        "scientific_name": scientific_name,
        "confidence": 0.95,
        "lat": LISTENER_LAT,
        "lon": LISTENER_LON,
        "territory": territory,
        "indigenous": indigenous,
        "source": "listener" if persist else "synthetic",
        "synthetic": True,
    }
    if persist:
        det_id = uuid.uuid4().hex[:12]
        week_val = int(dt.datetime.utcnow().isocalendar().week)
        with db() as conn:
            conn.execute(
                "INSERT INTO detections "
                "(id, ts, lat, lon, week, min_conf, audio_path, top_label, top_sci, "
                " top_conf, hits_json, source, territory_json, indigenous_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    det_id, event["ts"], LISTENER_LAT, LISTENER_LON, week_val,
                    LISTENER_MIN_CONF, None,
                    event["common_name"], scientific_name, 0.95,
                    json.dumps([{"common_name": event["common_name"],
                                 "scientific_name": scientific_name,
                                 "confidence": 0.95}]),
                    "listener",
                    json.dumps(territory) if territory else None,
                    json.dumps(indigenous) if indigenous else None,
                ),
            )
        event["id"] = det_id
    await broadcaster.publish(event)
    return {"ok": True, "subscribers": broadcaster.subscriber_count(), "persisted": persist}


@app.websocket("/stream/events")
async def stream_events(ws: WebSocket) -> None:
    """Live detection feed for the /wall projection viewer."""
    await ws.accept()
    q = broadcaster.subscribe()
    try:
        # Send the most recent detection on connect so a freshly-opened
        # wall isn't blank for the first few minutes.
        if listener and listener.stats.get("last_detection"):
            await ws.send_json(listener.stats["last_detection"])
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unsubscribe(q)


@app.get("/territory")
async def territory(lat: float, lon: float) -> dict:
    """Whose traditional territory is this lat/lon on? Native Land Digital lookup."""
    return await lookup_territory(lat, lon)


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

    # Persist raw upload as `<id>_raw.<ext>`, then convert to normalized `<id>.wav`.
    # Separate names so ffmpeg never collides when the source is already wav.
    src_ext = (audio.filename or "clip").rsplit(".", 1)[-1].lower()
    if src_ext not in {"wav", "webm", "ogg", "m4a", "mp3", "flac", "mp4"}:
        src_ext = "webm"
    raw_path = AUDIO_DIR / f"{det_id}_raw.{src_ext}"
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

    # Indigenous names + territory acknowledgment. Territory lookup only when
    # we have GPS; name lookup runs either way (so a chickadee detected
    # without geolocation still surfaces gijigijigaaneshiinh).
    territory_info: Optional[dict] = None
    if body.lat is not None and body.lon is not None:
        territory_info = await lookup_territory(body.lat, body.lon)
    languages = (territory_info or {}).get("languages_at_location") or None
    out["territory"] = territory_info
    out["indigenous"] = lookup_names(body.scientific_name, languages)

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


@app.get("/catalog")
def catalog(date: Optional[str] = None, source: Optional[str] = None) -> dict:
    """The day's bird catalog. ?date=YYYY-MM-DD (UTC) — defaults to today.
    ?source=listener|tap to filter. One row per species: first/last heard,
    count of detections, max confidence, plus the first cited Indigenous
    name so the wall's today-strip can render the endonym directly."""
    day = date or dt.datetime.utcnow().date().isoformat()
    # ts is stored as ISO8601 with trailing Z, so a prefix LIKE matches the day.
    params: list = [f"{day}%"]
    sql = (
        "SELECT id, ts, top_label, top_sci, top_conf, source, indigenous_json, "
        "individual_id FROM detections WHERE ts LIKE ?"
    )
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY ts ASC"
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    by_sci: dict[str, dict] = {}
    for r in rows:
        sci = r["top_sci"] or ""
        if not sci:
            continue
        entry = by_sci.get(sci)
        if entry is None:
            ind = json.loads(r["indigenous_json"]) if r.get("indigenous_json") else None
            first_name = (ind or {}).get("names", [None])[0] if ind and ind.get("available") else None
            languages = (ind or {}).get("languages") or {}
            lang_key = (first_name or {}).get("language") if first_name else None
            lang_meta = languages.get(lang_key) if lang_key else None
            entry = {
                "scientific_name": sci,
                "common_name": r["top_label"],
                "first_heard": r["ts"],
                "last_heard": r["ts"],
                "count": 0,
                "max_conf": 0.0,
                "indigenous_name": (first_name or {}).get("word") if first_name else None,
                "indigenous_phonetic": (first_name or {}).get("phonetic") if first_name else None,
                "indigenous_language": (lang_meta or {}).get("english_name") if lang_meta else None,
                "indigenous_endonym": (lang_meta or {}).get("endonym") if lang_meta else None,
                "individuals": set(),
            }
            by_sci[sci] = entry
        entry["last_heard"] = r["ts"]
        entry["count"] += 1
        if (r["top_conf"] or 0) > entry["max_conf"]:
            entry["max_conf"] = float(r["top_conf"] or 0)
        if r.get("individual_id"):
            entry["individuals"].add(r["individual_id"])

    for s in by_sci.values():
        ids = sorted(s.pop("individuals"))
        s["individual_ids"] = ids
        s["individual_count"] = len(ids)
    species = sorted(by_sci.values(), key=lambda x: x["last_heard"], reverse=True)
    total_individuals = sum(s["individual_count"] for s in species)
    return {
        "date": day,
        "species_count": len(species),
        "detection_count": len(rows),
        "individual_count": total_individuals,
        "species": species,
    }


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
    # Wikipedia REST API blocks default httpx UA with 403. Identify properly.
    headers = {"User-Agent": "BirdNET-PWA/0.1 (https://github.com/seanthomasevans/birdnet)"}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
            r = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
            if r.status_code != 200 and sci:
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
