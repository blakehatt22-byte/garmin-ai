#!/usr/bin/env python3
"""
Turn garmin/data.json into a static dashboard in docs/.

    python build_site.py                 # local preview: writes docs/data.json (plaintext)
    SITE_PASSPHRASE=... python build_site.py   # published: writes docs/data.enc (AES-256-GCM)

The dashboard (docs/index.html) is fully self-contained: Chart.js is vendored at
docs/vendor/chart.umd.min.js, so the page has no external dependencies. When an
encrypted payload is present it asks for the passphrase and decrypts in the
browser; the passphrase never leaves your machine.
"""

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "garmin" / "data.json"
DOCS = ROOT / "docs"

PBKDF2_ITERATIONS = 250_000


# --------------------------------------------------------------------------- #
# Shaping the raw Garmin dump into a compact summary the browser can chart.
# --------------------------------------------------------------------------- #

def _get(d, *path, default=None):
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
    return d if d is not None else default


def _hours(seconds):
    if not seconds:
        return None
    return round(seconds / 3600, 2)


def _clock(timestamp_local):
    """Garmin '*Local' fields: epoch-ms already shifted to local wall time,
    or occasionally an ISO string. Return 'HH:MM'."""
    if not timestamp_local:
        return None
    if isinstance(timestamp_local, (int, float)) or str(timestamp_local).isdigit():
        secs = int(timestamp_local) / 1000
        return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%H:%M")
    text = str(timestamp_local).replace("T", " ")
    try:
        return text.split(" ")[1][:5]
    except IndexError:
        return None


def shape_day(day, wellness):
    sleep_dto = _get(wellness, "sleep", "dailySleepDTO", default={})
    hrv = _get(wellness, "hrv", "hrvSummary", default={})
    stats = wellness.get("stats") or {}

    body_battery = wellness.get("body_battery") or []
    bb_values = []
    if body_battery:
        for reading in (body_battery[0].get("bodyBatteryValuesArray") or []):
            if isinstance(reading, list) and len(reading) > 1 and reading[1] is not None:
                bb_values.append(reading[1])

    readiness_list = wellness.get("training_readiness") or []
    readiness = readiness_list[0] if isinstance(readiness_list, list) and readiness_list else {}

    return {
        "date": day,
        "sleepH": _hours(sleep_dto.get("sleepTimeSeconds")),
        "deepH": _hours(sleep_dto.get("deepSleepSeconds")),
        "lightH": _hours(sleep_dto.get("lightSleepSeconds")),
        "remH": _hours(sleep_dto.get("remSleepSeconds")),
        "awakeH": _hours(sleep_dto.get("awakeSleepSeconds")),
        "sleepScore": _get(sleep_dto, "sleepScores", "overall", "value"),
        "bedtime": _clock(sleep_dto.get("sleepStartTimestampLocal")),
        "waketime": _clock(sleep_dto.get("sleepEndTimestampLocal")),
        "sleepHr": sleep_dto.get("avgHeartRate"),
        "respiration": sleep_dto.get("averageRespirationValue"),

        "hrvLast": hrv.get("lastNightAvg"),
        "hrvWeekly": hrv.get("weeklyAvg"),
        "hrvStatus": hrv.get("status"),
        "hrvBandLow": _get(hrv, "baseline", "balancedLow"),
        "hrvBandHigh": _get(hrv, "baseline", "balancedUpper"),

        "rhr": stats.get("restingHeartRate"),
        "rhr7": stats.get("lastSevenDaysAvgRestingHeartRate"),
        "steps": stats.get("totalSteps"),
        "stepGoal": stats.get("dailyStepGoal"),

        "bbHigh": max(bb_values) if bb_values else stats.get("bodyBatteryHighestValue"),
        "bbLow": min(bb_values) if bb_values else stats.get("bodyBatteryLowestValue"),
        "bbEnd": bb_values[-1] if bb_values else stats.get("bodyBatteryMostRecentValue"),

        "stressAvg": stats.get("averageStressLevel"),
        "stressMax": stats.get("maxStressLevel"),

        "readiness": readiness.get("score"),
        "readinessLevel": readiness.get("level"),
        "readinessNote": readiness.get("feedbackShort"),
    }


def _miles(meters):
    if meters is None:
        return None
    return round(meters / 1609.34, 2)


def _round(value, ndigits=1):
    return round(value, ndigits) if isinstance(value, (int, float)) else None


def shape_activity(a):
    zones = [round((a.get(f"hrTimeInZone_{i}") or 0) / 60, 1) for i in range(1, 6)]
    start = a.get("startTimeLocal") or ""
    dist_mi = _miles(a.get("distance"))
    return {
        "date": start.split(" ")[0] if start else None,
        "start": start,
        "name": a.get("activityName") or "Untitled",
        "type": _get(a, "activityType", "typeKey", default="unknown"),
        "durMin": round((a.get("duration") or 0) / 60, 1),
        "distMi": dist_mi if dist_mi else None,
        "avgHr": a.get("averageHR"),
        "maxHr": a.get("maxHR"),
        "calories": a.get("calories"),
        "load": _round(a.get("activityTrainingLoad"), 1),
        "aeTE": _round(a.get("aerobicTrainingEffect"), 1),
        "anTE": _round(a.get("anaerobicTrainingEffect"), 1),
        "teLabel": a.get("trainingEffectLabel"),
        "elevGainM": round(a["elevationGain"]) if a.get("elevationGain") else None,
        "zones": zones,
    }


def build_summary(data):
    days = [shape_day(day, w) for day, w in sorted(data.get("wellness", {}).items())]

    seen = set()
    activities = []
    for a in data.get("activities", []):
        aid = a.get("activityId")
        if aid in seen:
            continue
        seen.add(aid)
        activities.append(shape_activity(a))
    activities.sort(key=lambda x: x["start"] or "")

    return {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days,
        "activities": activities,
    }


# --------------------------------------------------------------------------- #
# Encryption (browser decrypts with WebCrypto: PBKDF2-SHA256 + AES-GCM).
# --------------------------------------------------------------------------- #

def encrypt_payload(plaintext: str, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = os.urandom(16)
    iv = os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, PBKDF2_ITERATIONS, dklen=32)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    b64 = lambda raw: base64.b64encode(raw).decode()
    return {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iter": PBKDF2_ITERATIONS,
        "salt": b64(salt),
        "iv": b64(iv),
        "ct": b64(ciphertext),
    }


# --------------------------------------------------------------------------- #

def main():
    if not DATA_FILE.exists():
        sys.exit(f"No data yet at {DATA_FILE} - run sync_garmin.py first.")

    data = json.loads(DATA_FILE.read_text())
    summary = build_summary(data)
    payload_json = json.dumps(summary, separators=(",", ":"), default=str)

    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(INDEX_HTML)
    (DOCS / ".nojekyll").write_text("")
    if not (DOCS / "vendor" / "chart.umd.min.js").exists():
        print("WARNING: docs/vendor/chart.umd.min.js is missing - charts will not render.")

    passphrase = os.environ.get("SITE_PASSPHRASE", "").strip()
    if not passphrase and os.environ.get("CI"):
        sys.exit(
            "Refusing to build an unencrypted dashboard in CI: the SITE_PASSPHRASE "
            "secret is not set. Add it in the repo's Settings > Secrets and variables > Actions."
        )
    enc_file = DOCS / "data.enc"
    plain_file = DOCS / "data.json"

    if passphrase:
        (enc_file).write_text(json.dumps(encrypt_payload(payload_json, passphrase)))
        if plain_file.exists():
            plain_file.unlink()
        mode = f"encrypted -> {enc_file.relative_to(ROOT)}"
    else:
        plain_file.write_text(payload_json)
        if enc_file.exists():
            enc_file.unlink()
        mode = (
            f"PLAINTEXT -> {plain_file.relative_to(ROOT)} "
            "(set SITE_PASSPHRASE to encrypt for publishing)"
        )

    print(f"Built dashboard in {DOCS.relative_to(ROOT)}/  ({mode})")
    print(f"  {len(summary['days'])} wellness days, {len(summary['activities'])} activities")
    if not passphrase:
        print("  Preview locally:  python -m http.server -d docs 8000  ->  http://localhost:8000")


# --------------------------------------------------------------------------- #
# The dashboard. One file, no build step. Chart.js from cdnjs.
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Garmin Dashboard</title>
<script src="vendor/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d21; --muted:#5b6470; --line:#e6e8ec;
    --accent:#3b6ef5; --good:#1f9d55; --warn:#d97706; --bad:#dc2626;
    --sleep-deep:#2b4a8b; --sleep-light:#6c8ed6; --sleep-rem:#8b5cf6; --sleep-awake:#d1d5db;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1216; --card:#171b21; --ink:#e8eaed; --muted:#9aa4b2; --line:#262b33;
      --accent:#6a93ff; --sleep-awake:#3a3f47;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:22px 20px 8px;max-width:1180px;margin:0 auto}
  h1{margin:0;font-size:20px;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px;margin-top:3px}
  main{max-width:1180px;margin:0 auto;padding:12px 20px 60px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0 6px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .kpi .label{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.6px}
  .kpi .value{font-size:22px;font-weight:650;margin-top:4px}
  .kpi .foot{color:var(--muted);font-size:11.5px;margin-top:2px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin-top:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px 8px}
  .card h2{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--muted);
    text-transform:uppercase;letter-spacing:.6px}
  .card .cv{position:relative;height:240px}
  .wide{grid-column:1/-1}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:right;padding:7px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  thead th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
  tbody tr:hover{background:rgba(127,127,127,.06)}
  .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600}
  .tableWrap{overflow-x:auto}
  #gate{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;padding:20px;z-index:10}
  #gate .box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;max-width:360px;width:100%}
  #gate h1{font-size:17px;margin-bottom:4px}
  #gate p{color:var(--muted);font-size:12.5px;margin:6px 0 14px}
  #gate input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;
    background:var(--bg);color:var(--ink);font-size:14px}
  #gate button{margin-top:10px;width:100%;padding:10px;border:0;border-radius:9px;
    background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  #gate .err{color:var(--bad);font-size:12.5px;margin-top:8px;min-height:16px}
  .hidden{display:none!important}
</style>
</head>
<body>
<div id="gate" class="hidden">
  <form class="box" id="gateForm">
    <h1>Garmin Dashboard</h1>
    <p>This dashboard is encrypted. Enter your passphrase to view it.</p>
    <input type="password" id="pass" autocomplete="current-password" autofocus placeholder="Passphrase">
    <button type="submit">Unlock</button>
    <div class="err" id="gateErr"></div>
  </form>
</div>

<header>
  <h1>Garmin Dashboard</h1>
  <div class="sub" id="updated"></div>
</header>
<main id="app" class="hidden">
  <div class="kpis" id="kpis"></div>
  <div class="grid">
    <div class="card"><h2>Sleep (hours &amp; stages)</h2><div class="cv"><canvas id="cSleep"></canvas></div></div>
    <div class="card"><h2>HRV &mdash; overnight avg vs balanced range</h2><div class="cv"><canvas id="cHrv"></canvas></div></div>
    <div class="card"><h2>Resting heart rate</h2><div class="cv"><canvas id="cRhr"></canvas></div></div>
    <div class="card"><h2>Body Battery (low &rarr; high, dot = end of day)</h2><div class="cv"><canvas id="cBb"></canvas></div></div>
    <div class="card"><h2>Steps vs goal</h2><div class="cv"><canvas id="cSteps"></canvas></div></div>
    <div class="card"><h2>Stress (avg &amp; max)</h2><div class="cv"><canvas id="cStress"></canvas></div></div>
    <div class="card wide"><h2>Training load per activity &amp; 7-day rolling total</h2><div class="cv"><canvas id="cLoad"></canvas></div></div>
  </div>
  <div class="card wide" style="margin-top:14px">
    <h2>Activities</h2>
    <div class="tableWrap"><table id="acts"><thead><tr>
      <th>Date</th><th>Activity</th><th>Type</th><th>Duration (min)</th><th>Distance (mi)</th>
      <th>Avg HR</th><th>Max HR</th><th>Cal</th><th>Load</th><th>Aerobic TE</th><th>Anaerobic TE</th>
    </tr></thead><tbody></tbody></table></div>
  </div>
</main>

<script>
const $ = s => document.querySelector(s);
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));

async function decryptPayload(enc, passphrase){
  const km = await crypto.subtle.importKey("raw", new TextEncoder().encode(passphrase),
    {name:"PBKDF2"}, false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    {name:"PBKDF2", salt:b64(enc.salt), iterations:enc.iter, hash:"SHA-256"},
    km, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({name:"AES-GCM", iv:b64(enc.iv)}, key, b64(enc.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}

async function boot(){
  // Plaintext local-preview mode.
  const plain = await fetch("data.json").then(r => r.ok ? r.json() : null).catch(() => null);
  if (plain){ render(plain); return; }

  const enc = await fetch("data.enc").then(r => r.json());
  const gate = $("#gate"), err = $("#gateErr");
  gate.classList.remove("hidden");

  const tryPass = async (pass) => {
    try{
      const data = await decryptPayload(enc, pass);
      try{ sessionStorage.setItem("gp", pass); }catch(e){}
      gate.classList.add("hidden");
      render(data);
    }catch(e){ err.textContent = "Wrong passphrase."; try{ sessionStorage.removeItem("gp"); }catch(_){}}
  };

  let saved = null;
  try{ saved = sessionStorage.getItem("gp"); }catch(e){}
  if (saved) tryPass(saved);

  $("#gateForm").addEventListener("submit", e => { e.preventDefault(); err.textContent=""; tryPass($("#pass").value); });
}

// ------------------------------------------------------------------ rendering
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const num = v => (v === null || v === undefined || v === "" || Number.isNaN(+v)) ? null : +v;
const last = arr => arr.length ? arr[arr.length - 1] : null;
Chart.defaults.color = css("--muted");
Chart.defaults.borderColor = css("--line");
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.maintainAspectRatio = false;
Chart.defaults.animation = false;

const HRV_COLORS = {BALANCED:"--good", UNBALANCED:"--warn", LOW:"--bad", POOR:"--bad"};

function kpi(label, value, foot, color){
  const c = color ? `style="color:${css(color)}"` : "";
  return `<div class="kpi"><div class="label">${label}</div>
    <div class="value" ${c}>${value ?? "&mdash;"}</div>
    <div class="foot">${foot ?? ""}</div></div>`;
}

function render(data){
  $("#app").classList.remove("hidden");
  const gen = new Date(data.generatedAt);
  $("#updated").textContent =
    `Last synced ${gen.toLocaleString()} · ${data.days.length} days · ${data.activities.length} activities`;

  const D = data.days, labels = D.map(d => d.date.slice(5));
  const L = last(D) || {};

  // --- KPIs
  const fmtH = h => h == null ? null : `${Math.floor(h)}h ${Math.round((h % 1) * 60)}m`;
  const load7 = sumLoad(data.activities, D.length ? last(D).date : null);
  $("#kpis").innerHTML = [
    kpi("Last night sleep", fmtH(num(L.sleepH)), L.sleepScore ? `score ${L.sleepScore}` : (L.bedtime ? `${L.bedtime}–${L.waketime}` : "")),
    kpi("HRV (overnight)", num(L.hrvLast) != null ? `${L.hrvLast} ms` : null,
        L.hrvStatus ? L.hrvStatus.toLowerCase() : "", HRV_COLORS[L.hrvStatus] || null),
    kpi("Resting HR", num(L.rhr) != null ? `${L.rhr} bpm` : null, num(L.rhr7) != null ? `7-day avg ${L.rhr7}` : ""),
    kpi("Training readiness", L.readiness ?? null, L.readinessLevel ? L.readinessLevel.toLowerCase() : ""),
    kpi("Body Battery now", L.bbEnd ?? null, (L.bbLow != null ? `range ${L.bbLow}–${L.bbHigh}` : "")),
    kpi("7-day training load", load7 || null, "sum of activity load"),
  ].join("");

  // --- Sleep
  new Chart($("#cSleep"), {
    data: {
      labels,
      datasets: [
        bar("Deep", D.map(d => num(d.deepH)), css("--sleep-deep"), "y"),
        bar("Light", D.map(d => num(d.lightH)), css("--sleep-light"), "y"),
        bar("REM", D.map(d => num(d.remH)), css("--sleep-rem"), "y"),
        bar("Awake", D.map(d => num(d.awakeH)), css("--sleep-awake"), "y"),
        {...lineDS("Sleep score", D.map(d => num(d.sleepScore)), css("--accent"), "y1"), type:"line"},
      ],
    },
    options: stacked({ y:{title:t("hours"), stacked:true, beginAtZero:true},
      y1:{position:"right", title:t("score"), grid:{drawOnChartArea:false}, suggestedMin:0, suggestedMax:100} }),
  });

  // --- HRV with balanced band
  new Chart($("#cHrv"), {
    data: { labels, datasets: [
      floorDS(D.map(d => num(d.hrvBandLow))),
      ceilDS(D.map(d => num(d.hrvBandHigh))),
      lineDS("Overnight avg", D.map(d => num(d.hrvLast)), css("--accent")),
      lineDS("7-day avg", D.map(d => num(d.hrvWeekly)), css("--muted"), null, true),
    ]},
    options: base({ y:{title:t("ms")} }),
  });

  // --- Resting HR
  new Chart($("#cRhr"), {
    data: { labels, datasets: [
      lineDS("Resting HR", D.map(d => num(d.rhr)), css("--bad")),
      lineDS("7-day avg", D.map(d => num(d.rhr7)), css("--muted"), null, true),
    ]},
    options: base({ y:{title:t("bpm")} }),
  });

  // --- Body Battery floating bars + end dot
  new Chart($("#cBb"), {
    data: { labels, datasets: [
      { label:"Low→High", type:"bar", data: D.map(d => [num(d.bbLow), num(d.bbHigh)]),
        backgroundColor: css("--accent")+"55", borderColor: css("--accent"), borderWidth:1,
        borderSkipped:false, borderRadius:4, barPercentage:.5 },
      { label:"End of day", type:"line", showLine:false, data: D.map(d => num(d.bbEnd)),
        pointRadius:4, pointBackgroundColor: css("--ink") },
    ]},
    options: base({ y:{min:0, max:100, title:t("battery")} }),
  });

  // --- Steps
  new Chart($("#cSteps"), {
    data: { labels, datasets: [
      bar("Steps", D.map(d => num(d.steps)), css("--good")),
      {...lineDS("Goal", D.map(d => num(d.stepGoal)), css("--muted"), null, true), pointRadius:0},
    ]},
    options: base({ y:{beginAtZero:true, title:t("steps")} }),
  });

  // --- Stress
  new Chart($("#cStress"), {
    data: { labels, datasets: [
      bar("Avg stress", D.map(d => num(d.stressAvg)), css("--warn")),
      lineDS("Max stress", D.map(d => num(d.stressMax)), css("--bad"), null, true),
    ]},
    options: base({ y:{beginAtZero:true, max:100, title:t("stress")} }),
  });

  // --- Training load per activity + rolling 7-day
  const A = data.activities;
  const aLabels = A.map(a => `${a.date.slice(5)} ${a.type.replace(/_training$/, "")}`);
  const rolling = A.map((_, i) => {
    const end = new Date(A[i].date), start = new Date(end); start.setDate(end.getDate() - 6);
    return A.reduce((s, x) => {
      const d = new Date(x.date);
      return s + (d >= start && d <= end ? (num(x.load) || 0) : 0);
    }, 0);
  });
  new Chart($("#cLoad"), {
    data: { labels: aLabels, datasets: [
      bar("Activity load", A.map(a => num(a.load)), css("--accent")),
      {...lineDS("7-day rolling load", rolling, css("--bad"), "y1"), type:"line"},
    ]},
    options: stacked({ y:{beginAtZero:true, title:t("load")},
      y1:{position:"right", beginAtZero:true, grid:{drawOnChartArea:false}, title:t("7-day load")} }, false),
  });

  // --- Activities table
  const tb = $("#acts tbody");
  const cell = v => `<td>${v ?? "&mdash;"}</td>`;
  tb.innerHTML = [...A].reverse().map(a => `<tr>
    <td>${a.date}</td><td>${a.name}</td><td>${a.type}</td>
    ${cell(a.durMin)}${cell(a.distMi)}${cell(a.avgHr)}${cell(a.maxHr)}${cell(a.calories)}
    ${cell(a.load != null ? Math.round(a.load) : null)}${cell(a.aeTE)}${cell(a.anTE)}
  </tr>`).join("");
}

// chart helpers
const t = txt => ({ display:true, text:txt });
const bar = (label, data, color, yAxisID) => {
  const d = { type:"bar", label, data, backgroundColor:color, borderColor:color, borderRadius:3, borderWidth:0 };
  if (yAxisID) d.yAxisID = yAxisID;
  return d;
};
const lineDS = (label, data, color, yAxisID, dashed) => {
  const d = { type:"line", label, data, borderColor:color, backgroundColor:color,
    tension:.3, pointRadius:2, borderWidth:2, borderDash: dashed ? [5,4] : [], spanGaps:true, fill:false };
  if (yAxisID) d.yAxisID = yAxisID;
  return d;
};
// balanced-range shading: an invisible floor line, then a ceiling line that fills down to it
const floorDS = data => ({ type:"line", label:"__floor", data, borderColor:"transparent", pointRadius:0, fill:false });
const ceilDS = data => ({ type:"line", label:"Balanced range", data, borderColor:"transparent",
  pointRadius:0, backgroundColor: css("--good")+"22", fill:"-1" });
function base(scales){
  return { interaction:{mode:"index", intersect:false},
    plugins:{ legend:{ labels:{ boxWidth:10, filter: i => i.text !== "__floor" } } },
    scales: Object.assign({ x:{ grid:{display:false} } }, scales) };
}
function stacked(scales, stackX = true){
  const o = base(scales);
  o.scales.x.stacked = stackX;
  return o;
}
function sumLoad(acts, endDate){
  if (!endDate) return 0;
  const end = new Date(endDate), start = new Date(end); start.setDate(end.getDate() - 6);
  return Math.round(acts.reduce((s, a) => {
    const d = new Date(a.date);
    return s + (d >= start && d <= end ? (+a.load || 0) : 0);
  }, 0));
}

boot();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
