// BirdNET PWA — record audio on phone, send to Mac server, render results.
//
// Server URL is configurable in settings (defaults to Tailscale Funnel URL of Sean's Mac).
// Geolocation is requested up front so BirdNET can filter by region + week.

const DEFAULT_SERVER = "https://seans-macbook-pro-m3.tail65b106.ts.net";

const settings = {
  serverUrl: localStorage.getItem("birdnet.server") || DEFAULT_SERVER,
  captureSecs: Number(localStorage.getItem("birdnet.captureSecs") || 10),
  minConf: Number(localStorage.getItem("birdnet.minConf") || 0.15),
  autoEnrich: localStorage.getItem("birdnet.autoEnrich") !== "false",
};

let geo = null;          // { lat, lon, week }
let mediaStream = null;
let recorder = null;
let recordingChunks = [];
let recordingStart = 0;
let analyser = null;
let meterRaf = null;

// ── tab nav ───────────────────────────────────────────────────────
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.pane;
    document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === target));
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    if (target === "history-pane") refreshHistory();
    if (target === "settings-pane") refreshHealth();
  });
});

document.getElementById("settings-btn").addEventListener("click", () => {
  document.querySelector('.tabs button[data-pane="settings-pane"]').click();
});

// ── settings ──────────────────────────────────────────────────────
const $server = document.getElementById("server-url");
const $secs = document.getElementById("capture-secs");
const $conf = document.getElementById("min-conf");
const $enrich = document.getElementById("auto-enrich");

$server.value = settings.serverUrl;
$secs.value = settings.captureSecs;
$conf.value = settings.minConf;
$enrich.checked = settings.autoEnrich;

document.getElementById("save-settings").addEventListener("click", () => {
  settings.serverUrl = $server.value.replace(/\/$/, "");
  settings.captureSecs = Math.max(3, Math.min(30, Number($secs.value) || 10));
  settings.minConf = Math.max(0, Math.min(1, Number($conf.value) || 0.15));
  settings.autoEnrich = $enrich.checked;
  localStorage.setItem("birdnet.server", settings.serverUrl);
  localStorage.setItem("birdnet.captureSecs", String(settings.captureSecs));
  localStorage.setItem("birdnet.minConf", String(settings.minConf));
  localStorage.setItem("birdnet.autoEnrich", String(settings.autoEnrich));
  document.getElementById("record-sub").textContent = `${settings.captureSecs}s capture`;
  setStatus("settings saved", "success");
  refreshHealth();
});

document.getElementById("record-sub").textContent = `${settings.captureSecs}s capture`;

// ── geolocation ───────────────────────────────────────────────────
function isoWeek(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
}

function requestGeo() {
  const $geo = document.getElementById("geo-status");
  if (!navigator.geolocation) {
    $geo.textContent = "no geolocation — region filter off";
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      geo = {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        week: isoWeek(new Date()),
        accuracy: pos.coords.accuracy,
      };
      $geo.textContent = `${geo.lat.toFixed(4)}, ${geo.lon.toFixed(4)} · week ${geo.week} · ±${Math.round(geo.accuracy)}m`;
    },
    (err) => {
      $geo.textContent = `geo denied — region filter off (${err.message})`;
    },
    { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
  );
}
requestGeo();

// ── recording ─────────────────────────────────────────────────────
const $btn = document.getElementById("record-btn");
const $meter = document.getElementById("meter");
const $bar = document.getElementById("meter-bar");
const $time = document.getElementById("meter-time");

$btn.addEventListener("click", async () => {
  if (recorder && recorder.state === "recording") {
    stopRecording();
    return;
  }
  await startRecording();
});

async function startRecording() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: 48000, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
  } catch (err) {
    setStatus(`mic denied: ${err.message}`, "error");
    return;
  }

  recordingChunks = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : MediaRecorder.isTypeSupported("audio/mp4")
    ? "audio/mp4"
    : "";
  recorder = mime ? new MediaRecorder(mediaStream, { mimeType: mime }) : new MediaRecorder(mediaStream);
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) recordingChunks.push(e.data); };
  recorder.onstop = onRecorderStop;

  // Visual level meter via WebAudio.
  const ac = new (window.AudioContext || window.webkitAudioContext)();
  const src = ac.createMediaStreamSource(mediaStream);
  analyser = ac.createAnalyser();
  analyser.fftSize = 1024;
  src.connect(analyser);
  $meter.hidden = false;

  recordingStart = performance.now();
  recorder.start();
  $btn.classList.add("recording");
  $btn.setAttribute("aria-pressed", "true");
  $btn.querySelector(".record-label").textContent = "tap to stop";
  $btn.querySelector(".record-sub").textContent = "listening…";
  setStatus("recording…");

  drawMeter();
  // Auto-stop.
  setTimeout(() => {
    if (recorder && recorder.state === "recording") stopRecording();
  }, settings.captureSecs * 1000);
}

function drawMeter() {
  if (!analyser) return;
  const buf = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteTimeDomainData(buf);
  let peak = 0;
  for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
  const pct = Math.min(1, peak / 64);
  $bar.style.right = `${(1 - pct) * 100}%`;
  const elapsed = (performance.now() - recordingStart) / 1000;
  $time.textContent = `${elapsed.toFixed(1)}s / ${settings.captureSecs}s`;
  meterRaf = requestAnimationFrame(drawMeter);
}

function stopRecording() {
  if (recorder && recorder.state !== "inactive") recorder.stop();
  $btn.classList.remove("recording");
  $btn.setAttribute("aria-pressed", "false");
  $btn.querySelector(".record-label").textContent = "tap to listen";
  $btn.querySelector(".record-sub").textContent = `${settings.captureSecs}s capture`;
  if (meterRaf) cancelAnimationFrame(meterRaf);
  meterRaf = null;
}

async function onRecorderStop() {
  $meter.hidden = true;
  $bar.style.right = "100%";
  if (mediaStream) mediaStream.getTracks().forEach((t) => t.stop());

  if (!recordingChunks.length) {
    setStatus("no audio captured", "error");
    return;
  }
  const blob = new Blob(recordingChunks, { type: recorder.mimeType || "audio/webm" });
  setStatus("uploading + analyzing…");
  await sendForAnalysis(blob);
}

// ── server interaction ────────────────────────────────────────────
async function sendForAnalysis(blob) {
  const fd = new FormData();
  const ext = (blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm");
  fd.append("audio", blob, `clip.${ext}`);
  if (geo) {
    fd.append("lat", String(geo.lat));
    fd.append("lon", String(geo.lon));
    fd.append("week", String(geo.week));
  }
  fd.append("min_confidence", String(settings.minConf));

  try {
    const t0 = performance.now();
    const r = await fetch(`${settings.serverUrl}/analyze`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(`server ${r.status}: ${await r.text()}`);
    const data = await r.json();
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    renderResults(data, elapsed);
    setStatus(`${data.hits.length} hit${data.hits.length === 1 ? "" : "s"} · ${elapsed}s`, "success");
  } catch (err) {
    setStatus(`error: ${err.message}`, "error");
  }
}

function renderResults(data, elapsed) {
  const $r = document.getElementById("results");
  $r.hidden = false;
  $r.innerHTML = "";

  if (!data.hits.length) {
    $r.innerHTML = '<div class="hit"><div class="hit-name">no bird detected</div><div class="hit-sci">try a longer clip or lower the confidence threshold in settings</div></div>';
    return;
  }

  data.hits.forEach((hit, i) => {
    const card = document.createElement("div");
    card.className = "hit" + (i === 0 ? " top" : "");
    card.innerHTML = `
      <div class="hit-row">
        <div>
          <div class="hit-name">${escape(hit.common_name)}</div>
          <div class="hit-sci">${escape(hit.scientific_name || "")}</div>
        </div>
        <div class="hit-conf">${(hit.confidence * 100).toFixed(0)}%</div>
      </div>
      <div class="hit-actions">
        <button data-act="enrich">tell me about this bird</button>
        <button data-act="play">play clip</button>
      </div>
      <div class="enrich-out" hidden></div>
    `;
    const out = card.querySelector(".enrich-out");
    card.querySelector('[data-act="enrich"]').addEventListener("click", () => doEnrich(hit, out));
    card.querySelector('[data-act="play"]').addEventListener("click", () => playClip(data.audio_url));
    $r.appendChild(card);
  });

  if (settings.autoEnrich && data.hits[0]) {
    const out = $r.querySelector(".hit.top .enrich-out");
    doEnrich(data.hits[0], out);
  }
}

async function doEnrich(hit, $out) {
  $out.hidden = false;
  $out.innerHTML = "<em>looking up…</em>";
  try {
    const r = await fetch(`${settings.serverUrl}/enrich`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        species: hit.common_name,
        scientific_name: hit.scientific_name,
        confidence: hit.confidence,
        lat: geo?.lat,
        lon: geo?.lon,
      }),
    });
    if (!r.ok) throw new Error(`enrich ${r.status}`);
    const data = await r.json();

    let html = "";
    if (data.wikipedia?.thumbnail) {
      html += `<img class="thumb" src="${escape(data.wikipedia.thumbnail)}" alt="" />`;
    }
    if (data.narrative) {
      html += `<h4>brief</h4><p>${escape(data.narrative)}</p>`;
    } else if (data.wikipedia?.extract) {
      html += `<h4>wikipedia</h4><p>${escape(data.wikipedia.extract)}</p>`;
    }
    if (data.ebird?.available) {
      const n = data.ebird.recent_obs_count;
      const cls = n > 0 ? "good" : "";
      html += `<span class="ebird-line ${cls}">eBird: ${n} recent sightings within 25km in last 30 days</span>`;
      if (data.ebird.hotspots_nearby?.length) {
        html += `<br><span class="ebird-line">nearby hotspots: ${data.ebird.hotspots_nearby.slice(0, 3).map(escape).join(" · ")}</span>`;
      }
    }
    if (data.wikipedia?.url) {
      html += `<br><a href="${escape(data.wikipedia.url)}" target="_blank" rel="noopener" style="color:var(--accent);font-size:0.8rem">wikipedia →</a>`;
    }
    $out.innerHTML = html || "<em>no extra info</em>";
  } catch (err) {
    $out.innerHTML = `<em>lookup failed: ${escape(err.message)}</em>`;
  }
}

let audio;
function playClip(url) {
  if (!url) return;
  if (audio) { audio.pause(); audio = null; }
  audio = new Audio(`${settings.serverUrl}${url}`);
  audio.play().catch((e) => setStatus(`playback failed: ${e.message}`, "error"));
}

// ── history ───────────────────────────────────────────────────────
async function refreshHistory() {
  const $list = document.getElementById("history-list");
  $list.innerHTML = "<div class='small'>loading…</div>";
  try {
    const r = await fetch(`${settings.serverUrl}/history?limit=50`);
    if (!r.ok) throw new Error(`history ${r.status}`);
    const data = await r.json();
    if (!data.items.length) {
      $list.innerHTML = "<div class='small'>no detections yet</div>";
      return;
    }
    $list.innerHTML = "";
    for (const item of data.items) {
      const ts = new Date(item.ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      const conf = item.top_conf ? `${(item.top_conf * 100).toFixed(0)}%` : "—";
      const el = document.createElement("div");
      el.className = "history-item";
      el.innerHTML = `
        <div>
          <div>${escape(item.top_label || "(no detection)")}</div>
          <div class="ts">${ts}</div>
        </div>
        <div class="conf">${conf}</div>
      `;
      $list.appendChild(el);
    }
  } catch (err) {
    $list.innerHTML = `<div class='small'>error: ${escape(err.message)}</div>`;
  }
}

// ── health probe ──────────────────────────────────────────────────
async function refreshHealth() {
  const $h = document.getElementById("health");
  $h.textContent = "checking…";
  try {
    const r = await fetch(`${settings.serverUrl}/healthz`, { cache: "no-store" });
    const j = await r.json();
    $h.textContent = JSON.stringify(j, null, 2);
  } catch (err) {
    $h.textContent = `unreachable: ${err.message}`;
  }
}

// ── utils ─────────────────────────────────────────────────────────
function setStatus(msg, kind = "") {
  const $s = document.getElementById("status-line");
  $s.textContent = msg;
  $s.className = "status-line" + (kind ? " " + kind : "");
}

function escape(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ── service worker ────────────────────────────────────────────────
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js").catch(() => {});
}
