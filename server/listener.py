"""
Continuous BirdNET listener — pulls audio from an RTSP/HLS stream, chunks it
through BirdNET, and broadcasts detection events to subscribers.

Designed for a Nest cam (or any RTSP source) at a fixed location. The output
is consumed by /wall — a projection-friendly viewer that puts the bird's name
in the language of the people whose land the listener stands on front and
centre, with the Linnaean binomial as ancillary.

Architecture:
  ffmpeg ── segments RTSP audio into rotating WAV files in a tmpdir
            (-f segment -segment_time CHUNK_SECS)
       │
       ▼
  watcher loop ── notices each new closed segment, runs BirdNET on it in
                  a thread pool, enriches with territory + Indigenous names,
                  publishes an event to the Broadcaster.

Environment:
  LISTENER_RTSP_URL      e.g. rtsp://localhost:42469/<token>
  LISTENER_LAT           default 44.92  (Gravenhurst-area / Evanswood)
  LISTENER_LON           default -79.37
  LISTENER_CHUNK_SECS    default 10
  LISTENER_MIN_CONF      default 0.4   (higher than tap-to-record — fewer
                                        false positives on a wall projection)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("birdnet.listener")


class Broadcaster:
    """Trivial in-process pubsub. Each subscriber gets its own asyncio.Queue;
    slow subscribers get their oldest events dropped rather than backpressuring
    the listener."""

    def __init__(self) -> None:
        self.queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self.queues.remove(q)
        except ValueError:
            pass

    def subscriber_count(self) -> int:
        return len(self.queues)

    async def publish(self, event: dict) -> None:
        dead: list[asyncio.Queue] = []
        for q in list(self.queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest, keep the freshest — the wall always wants
                # the most recent name, never a backlog.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
        for q in dead:
            self.unsubscribe(q)


class Listener:
    """Continuous BirdNET listener. One per process; safe to start_once()."""

    def __init__(
        self,
        rtsp_url: str,
        lat: float,
        lon: float,
        chunk_secs: int,
        min_conf: float,
        analyzer: Any,
        broadcaster: Broadcaster,
        enrich_cb: Callable[[str, float, float], Awaitable[dict]],
        persist_cb: Optional[Callable[[dict, Path], Awaitable[Optional[str]]]] = None,
    ) -> None:
        self.rtsp_url = rtsp_url
        self.lat = lat
        self.lon = lon
        self.chunk_secs = max(3, int(chunk_secs))
        self.min_conf = float(min_conf)
        self.analyzer = analyzer
        self.broadcaster = broadcaster
        self.enrich_cb = enrich_cb
        # Called after a detection passes the cooldown gate. Should copy the
        # chunk audio to durable storage and write a sqlite row. Returns the
        # stored audio path (relative to the project root) for inclusion in
        # the broadcast event, or None if persist failed.
        self.persist_cb = persist_cb
        self.tmpdir: Optional[Path] = None
        self.ffmpeg: Optional[asyncio.subprocess.Process] = None
        self.task: Optional[asyncio.Task] = None
        self.stats = {
            "started_at": None,
            "chunks_processed": 0,
            "detections": 0,
            "last_detection": None,
            "last_chunk_at": None,
        }
        # Per-species cooldown so the wall doesn't strobe the same name back-
        # to-back when a robin won't shut up. 60s feels right.
        self._last_announced: dict[str, float] = {}
        self.cooldown_secs = 60.0

    async def start(self) -> None:
        if self.task and not self.task.done():
            log.info("listener already running")
            return
        self.tmpdir = Path(tempfile.mkdtemp(prefix="birdnet-listener-"))
        self.stats["started_at"] = dt.datetime.utcnow().isoformat() + "Z"
        await self._spawn_ffmpeg()
        self.task = asyncio.create_task(self._watcher_loop())
        log.info("listener started (rtsp=%s lat=%.4f lon=%.4f chunk=%ss min_conf=%.2f)",
                 self.rtsp_url, self.lat, self.lon, self.chunk_secs, self.min_conf)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.ffmpeg and self.ffmpeg.returncode is None:
            self.ffmpeg.terminate()
            try:
                await asyncio.wait_for(self.ffmpeg.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self.ffmpeg.kill()
        self.ffmpeg = None
        if self.tmpdir and self.tmpdir.exists():
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.tmpdir = None

    async def _spawn_ffmpeg(self) -> None:
        """Pull the RTSP audio track and rotate it into chunk_secs WAV files."""
        assert self.tmpdir is not None
        out_pattern = str(self.tmpdir / "chunk_%06d.wav")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vn",
            "-ac", "1",
            "-ar", "48000",
            "-c:a", "pcm_s16le",
            "-f", "segment",
            "-segment_time", str(self.chunk_secs),
            "-reset_timestamps", "1",
            out_pattern,
        ]
        self.ffmpeg = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _watcher_loop(self) -> None:
        """Poll the tmpdir for newly-closed chunks. ffmpeg writes chunk N+1
        starting at the moment chunk N closes, so a chunk is "done" when a
        newer-numbered one appears."""
        assert self.tmpdir is not None
        seen: set[str] = set()
        try:
            while True:
                # If ffmpeg died, respawn (network blip, stream reset, etc).
                if self.ffmpeg is None or self.ffmpeg.returncode is not None:
                    log.warning("ffmpeg exited (rc=%s); respawning in 3s",
                                self.ffmpeg.returncode if self.ffmpeg else "?")
                    await asyncio.sleep(3)
                    await self._spawn_ffmpeg()

                files = sorted(p for p in self.tmpdir.glob("chunk_*.wav"))
                # The newest file is still being written. Process the rest.
                ready = files[:-1] if len(files) >= 2 else []
                for path in ready:
                    if path.name in seen:
                        continue
                    seen.add(path.name)
                    try:
                        await self._process_chunk(path)
                    except Exception as exc:
                        log.warning("chunk %s failed: %s", path.name, exc)
                    finally:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass

                # Keep `seen` from growing unbounded.
                if len(seen) > 1024:
                    seen = set(list(seen)[-512:])

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watcher loop crashed")
            raise

    async def _process_chunk(self, path: Path) -> None:
        if path.stat().st_size < 10_000:
            return
        self.stats["chunks_processed"] += 1
        self.stats["last_chunk_at"] = dt.datetime.utcnow().isoformat() + "Z"

        analysis = await asyncio.get_running_loop().run_in_executor(
            None, self._analyze_sync, path
        )
        detections = analysis.get("detections") or []
        embeddings = analysis.get("embeddings") or []
        if not detections:
            return

        # Best per species — keep the detection itself so we can pick the
        # matching embedding window for that highest-confidence vocalization.
        best: dict[str, dict] = {}
        for d in detections:
            sci = d.get("scientific_name") or ""
            if sci not in best or d["confidence"] > best[sci]["confidence"]:
                best[sci] = d

        now = time.time()
        for sci, d in best.items():
            if d["confidence"] < self.min_conf:
                continue
            last = self._last_announced.get(sci, 0)
            if now - last < self.cooldown_secs:
                continue
            self._last_announced[sci] = now

            enrich = {}
            try:
                enrich = await self.enrich_cb(sci, self.lat, self.lon)
            except Exception as exc:
                log.warning("enrich failed for %s: %s", sci, exc)

            embedding = _pick_embedding(embeddings, d.get("start_s", 0.0))
            event = {
                "type": "detection",
                "ts": dt.datetime.utcnow().isoformat() + "Z",
                "common_name": d.get("common_name"),
                "scientific_name": sci,
                "confidence": d["confidence"],
                "lat": self.lat,
                "lon": self.lon,
                "territory": enrich.get("territory"),
                "indigenous": enrich.get("indigenous"),
                "source": "listener",
                # internal — stripped before broadcast in persist_cb
                "_embedding": embedding,
            }
            if self.persist_cb is not None:
                try:
                    audio_url = await self.persist_cb(event, path)
                    if audio_url:
                        event["audio_url"] = audio_url
                except Exception as exc:
                    log.warning("persist failed for %s: %s", sci, exc)
            self.stats["detections"] += 1
            self.stats["last_detection"] = event
            await self.broadcaster.publish(event)
            log.info("detected %s (%.0f%%) → %d subscribers",
                     d.get("common_name"), d["confidence"] * 100,
                     self.broadcaster.subscriber_count())

    def _analyze_sync(self, path: Path) -> dict:
        """Run BirdNET on a single chunk plus extract per-window embeddings.
        Sync — called via run_in_executor. Embeddings are the 1024-dim
        GLOBAL_AVG_POOL layer activations (the layer immediately before the
        classifier head); they're what we cluster on for individual ID."""
        from birdnetlib import Recording
        rec = Recording(
            self.analyzer,
            str(path),
            lat=self.lat,
            lon=self.lon,
            date=dt.datetime.now(),
            min_conf=self.min_conf,
        )
        rec.analyze()
        embeddings: list[dict] = []
        try:
            rec.extract_embeddings()
            embeddings = rec.embeddings_list or []
        except Exception as exc:
            log.warning("embedding extraction failed on %s: %s", path.name, exc)
        detections = [
            {
                "common_name": h.get("common_name") or h.get("common") or "",
                "scientific_name": h.get("scientific_name") or h.get("scientific") or "",
                "confidence": float(h.get("confidence", 0.0)),
                "start_s": float(h.get("start_time", 0.0)),
                "end_s": float(h.get("end_time", 0.0)),
            }
            for h in (rec.detections or [])
        ]
        return {"detections": detections, "embeddings": embeddings}


def _pick_embedding(embeddings: list[dict], detection_start_s: float) -> Optional[list]:
    """Match a detection's start_time to the embedding window that contains
    it. BirdNET emits 3-second windows that align with the detection times,
    so this is usually an exact match. Falls back to the closest window
    when the timing doesn't line up (rare — happens when the model uses a
    different stride than the embedding extractor)."""
    if not embeddings:
        return None
    for w in embeddings:
        if abs(w["start_time"] - detection_start_s) < 0.5:
            return w["embeddings"]
    closest = min(embeddings, key=lambda w: abs(w["start_time"] - detection_start_s))
    return closest["embeddings"]
