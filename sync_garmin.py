#!/usr/bin/env python3
"""
Pull your own data from Garmin Connect (read-only) and save it locally as
plain-English markdown notes plus a data.json file.

Usage:
    python sync_garmin.py login       # one-time interactive login
    python sync_garmin.py sync 3      # pull the last 3 days (default: 7)

This script never writes anything back to your Garmin account - it only
reads activities and daily wellness data.
"""

import getpass
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
)

ROOT = Path(__file__).resolve().parent
TOKENSTORE = ROOT / ".garmin_tokens"
OUTPUT_DIR = ROOT / "garmin"
WELLNESS_DIR = OUTPUT_DIR / "wellness"
ACTIVITIES_DIR = OUTPUT_DIR / "activities"
DATA_FILE = OUTPUT_DIR / "data.json"


def ask_mfa_code() -> str:
    return input(
        "Garmin sent you a one-time verification code (check your email/phone/app) - enter it here: "
    ).strip()


def login() -> Garmin:
    """Log in to Garmin Connect, reusing a saved token when possible."""
    # Headless / CI runs supply the saved token through an env var instead of
    # an interactive login. Write it to the tokenstore so refreshes land there.
    tokens_json = os.environ.get("GARMIN_TOKENS_JSON")
    token_file = TOKENSTORE / "garmin_tokens.json"
    if tokens_json and not token_file.exists():
        TOKENSTORE.mkdir(parents=True, exist_ok=True)
        token_file.write_text(tokens_json)
        try:
            token_file.chmod(0o600)
        except OSError:
            pass

    try:
        garmin = Garmin()
        garmin.login(str(TOKENSTORE))
        return garmin
    except (FileNotFoundError, GarminConnectAuthenticationError):
        pass

    if not sys.stdin.isatty():
        raise SystemExit(
            "No valid Garmin login token, and not running interactively.\n"
            "Run `python sync_garmin.py login` on your own machine, then copy the new\n"
            ".garmin_tokens/garmin_tokens.json into the GARMIN_TOKENS_JSON repo secret."
        )

    print("No saved Garmin login found (or it expired) - let's log in.")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden while typing): ")

    garmin = Garmin(email=email, password=password, prompt_mfa=ask_mfa_code)
    garmin.login(str(TOKENSTORE))
    print(f"Login successful. Saved a login token to {TOKENSTORE} for next time.")
    print("Your email and password were not saved anywhere.")
    return garmin


def safe_call(fn, *args):
    """Call a Garmin API method and return None instead of crashing if it fails."""
    try:
        return fn(*args)
    except Exception as e:
        print(f"  (skipped {fn.__name__}: {e})")
        return None


def m(value, unit=""):
    if value is None:
        return "unknown"
    return f"{value}{unit}"


def meters_to_miles(m_val):
    if m_val is None:
        return None
    return round(m_val / 1609.34, 2)


def seconds_to_hms(s):
    if not s:
        return "0:00:00"
    s = int(s)
    h, rem = divmod(s, 3600)
    mm, ss = divmod(rem, 60)
    return f"{h}:{mm:02d}:{ss:02d}"


def write_wellness_note(day: str, wellness: dict):
    lines = [f"# Wellness - {day}", ""]

    sleep = wellness.get("sleep") or {}
    daily_sleep = sleep.get("dailySleepDTO") or {}
    if daily_sleep:
        total_sec = daily_sleep.get("sleepTimeSeconds")
        lines.append("## Sleep")
        lines.append(f"- Total sleep: {seconds_to_hms(total_sec)}")
        lines.append(f"- Deep sleep: {seconds_to_hms(daily_sleep.get('deepSleepSeconds'))}")
        lines.append(f"- Light sleep: {seconds_to_hms(daily_sleep.get('lightSleepSeconds'))}")
        lines.append(f"- REM sleep: {seconds_to_hms(daily_sleep.get('remSleepSeconds'))}")
        lines.append(f"- Awake: {seconds_to_hms(daily_sleep.get('awakeSleepSeconds'))}")
        score = (sleep.get("sleepScores") or {}).get("overall", {}).get("value")
        lines.append(f"- Sleep score: {m(score)}")
        lines.append("")

    hrv = wellness.get("hrv") or {}
    hrv_summary = hrv.get("hrvSummary") or {}
    if hrv_summary:
        lines.append("## HRV (heart rate variability)")
        lines.append(f"- Last night average: {m(hrv_summary.get('lastNightAvg'), ' ms')}")
        lines.append(f"- 7-day average: {m(hrv_summary.get('weeklyAvg'), ' ms')}")
        lines.append(f"- Status: {m(hrv_summary.get('status'))}")
        lines.append("")

    stats = wellness.get("stats") or {}
    if stats:
        lines.append("## Resting heart rate & activity")
        lines.append(f"- Resting heart rate: {m(stats.get('restingHeartRate'), ' bpm')}")
        lines.append(f"- Steps: {m(stats.get('totalSteps'))}")
        lines.append(f"- Total calories: {m(stats.get('totalKilocalories'))}")
        lines.append("")

    body_battery = wellness.get("body_battery") or []
    if body_battery:
        readings = body_battery[0].get("bodyBatteryValuesArray") or []
        values = [v[1] for v in readings if isinstance(v, list) and len(v) > 1 and v[1] is not None]
        lines.append("## Body Battery")
        if values:
            lines.append(f"- Range today: {min(values)} to {max(values)}")
            lines.append(f"- End of day: {values[-1]}")
        else:
            lines.append("- No data recorded")
        lines.append("")

    stress = wellness.get("stress") or {}
    if stress:
        lines.append("## Stress")
        lines.append(f"- Average stress level: {m(stress.get('avgStressLevel'))}")
        lines.append(f"- Max stress level: {m(stress.get('maxStressLevel'))}")
        lines.append("")

    readiness = wellness.get("training_readiness") or []
    if readiness:
        r = readiness[0] if isinstance(readiness, list) else readiness
        lines.append("## Training Readiness")
        lines.append(f"- Score: {m(r.get('score'))} ({m(r.get('level'))})")
        feedback = r.get("feedbackLong") or r.get("feedbackShort")
        if feedback:
            lines.append(f"- Garmin's note: {feedback}")
        lines.append("")

    WELLNESS_DIR.mkdir(parents=True, exist_ok=True)
    (WELLNESS_DIR / f"{day}.md").write_text("\n".join(lines))


def write_activity_note(activity: dict):
    activity_id = activity.get("activityId")
    name = activity.get("activityName") or "Untitled activity"
    activity_type = (activity.get("activityType") or {}).get("typeKey", "unknown")
    start_time = (activity.get("startTimeLocal") or "unknown-date")
    day = start_time.split(" ")[0] if start_time != "unknown-date" else "unknown-date"

    distance_mi = meters_to_miles(activity.get("distance"))
    duration = seconds_to_hms(activity.get("duration"))
    avg_hr = activity.get("averageHR")
    max_hr = activity.get("maxHR")
    calories = activity.get("calories")
    avg_pace = activity.get("averageSpeed")
    elevation_gain = activity.get("elevationGain")
    training_effect_aerobic = activity.get("aerobicTrainingEffect")
    training_effect_anaerobic = activity.get("anaerobicTrainingEffect")

    lines = [
        f"# {name}",
        "",
        f"- Date: {start_time}",
        f"- Type: {activity_type}",
        f"- Duration: {duration}",
    ]
    if distance_mi is not None:
        lines.append(f"- Distance: {distance_mi} miles")
    if avg_hr:
        lines.append(f"- Average heart rate: {avg_hr} bpm")
    if max_hr:
        lines.append(f"- Max heart rate: {max_hr} bpm")
    if calories:
        lines.append(f"- Calories: {calories}")
    if elevation_gain:
        lines.append(f"- Elevation gain: {round(elevation_gain)} m")
    if training_effect_aerobic:
        lines.append(f"- Aerobic training effect: {training_effect_aerobic}")
    if training_effect_anaerobic:
        lines.append(f"- Anaerobic training effect: {training_effect_anaerobic}")
    lines.append("")

    ACTIVITIES_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "-")
    filename = f"{day}-{activity_id}-{safe_name or 'activity'}.md"
    (ACTIVITIES_DIR / filename).write_text("\n".join(lines))
    return filename


def sync(garmin: Garmin, num_days: int):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_data = {"wellness": {}, "activities": []}
    if DATA_FILE.exists():
        try:
            all_data = json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    all_data.setdefault("wellness", {})
    all_data.setdefault("activities", [])

    today = date.today()
    print(f"\nPulling wellness data for the last {num_days} day(s)...")
    for i in range(num_days):
        day = (today - timedelta(days=i)).isoformat()
        print(f"  {day}")
        wellness = {
            "sleep": safe_call(garmin.get_sleep_data, day),
            "hrv": safe_call(garmin.get_hrv_data, day),
            "stats": safe_call(garmin.get_stats, day),
            "body_battery": safe_call(garmin.get_body_battery, day, day),
            "stress": safe_call(garmin.get_stress_data, day),
            "training_readiness": safe_call(garmin.get_training_readiness, day),
        }
        all_data["wellness"][day] = wellness
        write_wellness_note(day, wellness)

    print(f"\nPulling recent activities...")
    start_day = (today - timedelta(days=num_days - 1)).isoformat()
    end_day = today.isoformat()
    activities = safe_call(garmin.get_activities_by_date, start_day, end_day) or []
    print(f"  found {len(activities)} activit{'y' if len(activities) == 1 else 'ies'}")

    existing_ids = {a.get("activityId") for a in all_data["activities"]}
    for activity in activities:
        if activity.get("activityId") not in existing_ids:
            all_data["activities"].append(activity)
        filename = write_activity_note(activity)
        print(f"    wrote {filename}")

    DATA_FILE.write_text(json.dumps(all_data, indent=2, default=str))
    print(f"\nDone. Notes saved in {OUTPUT_DIR}")
    return all_data


def main():
    args = sys.argv[1:]
    command = args[0] if args else "sync"

    if command == "login":
        login()
        return

    num_days = 7
    if command == "sync" and len(args) > 1:
        num_days = int(args[1])
    elif command.isdigit():
        num_days = int(command)

    garmin = login()
    sync(garmin, num_days)


if __name__ == "__main__":
    main()
