#!/usr/bin/env python3
"""Padel-monitor voor Tennis en Padel Meerzicht (KNLTB ClubApp-gegevens).

Haalt het baanoverzicht op zoals de ClubApp dat zelf doet en meldt via
Telegram twee soorten banen die sinds de vorige controle NIEUW vrij zijn:

  - groen: online te boeken in de ClubApp
  - grijs: vrij, maar te kort om online te boeken (bel de club)

Alleen standaardbibliotheek, geen extra installatie nodig.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")  # de GitHub-server draait op UTC, wij rekenen in NL-tijd

API = "https://api.knltb.club/v1/pub/tennis/clubs/{club_id}/availability_timeline"
UA = "padel-monitor/2.0 (persoonlijk gebruik)"
STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_day(club_id, day):
    """Geeft het JSON-overzicht voor één dag. Met KNLTB_SAMPLE kun je testen met een bestand."""
    sample = os.environ.get("KNLTB_SAMPLE")
    if sample:
        with open(sample, encoding="utf-8") as f:
            return json.load(f)
    key = os.environ.get("KNLTB_API_KEY", "").strip()
    if key.lower().startswith("basic "):  # per ongeluk mee gekopieerd: weghalen
        key = key[6:].strip()
    if not getattr(fetch_day, "_gemeld", False):
        fetch_day._gemeld = True
        print(f"API-sleutel gevonden ({len(key)} tekens).")
    if not key:
        print("KNLTB_API_KEY ontbreekt (GitHub-geheim niet ingesteld).", file=sys.stderr)
        sys.exit(1)
    qs = urllib.parse.urlencode({"time_from": day.isoformat()})
    url = f"{API.format(club_id=club_id)}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {key}",
            "Accept": "application/json",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def to_local(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def parse_day(data):
    """Geeft lijst met dicts: datum, start, einde, duur (minuten), baan, soort ('groen'/'grijs')."""
    slots = []
    for court in data.get("timeline_court_availability", []):
        cd = court.get("court_details", {})
        if cd.get("sport") != "Padel" or cd.get("disabled"):
            continue
        baan = cd.get("name", "?")
        for blk in court.get("timeline", {}).get("blocks", []):
            bt = blk.get("block_type")
            if bt == "available":
                # 2players/3players/4players bevatten dezelfde tijden; één lijst is genoeg.
                for s in (blk.get("slots") or {}).get("2players", []):
                    if not s.get("available"):
                        continue
                    a, b = to_local(s["start_time"]), to_local(s["end_time"])
                    slots.append(_slot(a, b, baan, "groen", s.get("timeblock")))
            elif bt == "tooShort":
                a, b = to_local(blk["start"]), to_local(blk["end"])
                slots.append(_slot(a, b, baan, "grijs", None))
    return slots


def _slot(a, b, baan, soort, duur):
    return {
        "datum": a.date().isoformat(),
        "start": a.strftime("%H:%M"),
        "einde": b.strftime("%H:%M"),
        "duur": duur or int((b - a).total_seconds() // 60),
        "baan": baan,
        "soort": soort,
    }


def to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def bookable_now(slot, cfg, now):
    """Valt de start binnen het rollende boekvenster (standaard 72 uur)?"""
    start = datetime.fromisoformat(f'{slot["datum"]}T{slot["start"]}:00').replace(tzinfo=TZ)
    uren = cfg.get("boekvenster_uren", 72)
    return now < start <= now + timedelta(hours=uren)


def wanted(slot, cfg):
    """Past de baan bij de gewenste dagen, tijden en duur?"""
    d = date.fromisoformat(slot["datum"])
    dag = DAGEN[d.weekday()]
    windows = cfg.get("tijdvensters", {}).get(dag)
    if not windows:
        return False
    if slot["soort"] == "groen":
        if cfg.get("duur_minuten") and slot["duur"] not in cfg["duur_minuten"]:
            return False
    else:  # grijs: moet lang genoeg zijn om te spelen
        if slot["duur"] < cfg.get("grijs_min_minuten", 60):
            return False
    start = to_minutes(slot["start"])
    for w in windows:
        van, tot = w.split("-")
        if to_minutes(van) <= start <= to_minutes(tot):
            return True
    return False


def key(slot):
    return f'{slot["datum"]}|{slot["start"]}|{slot["duur"]}|{slot["baan"]}|{slot["soort"]}'


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram niet ingesteld; bericht wordt alleen getoond:\n" + text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        print(
            f"Telegram weigerde het bericht (HTTP {e.code}): "
            f"{e.read().decode('utf-8', errors='replace')}",
            file=sys.stderr,
        )
        sys.exit(1)


def fmt(slot):
    d = date.fromisoformat(slot["datum"])
    return (
        f'{DAGEN[d.weekday()]} {d.strftime("%d-%m")} '
        f'{slot["start"]}-{slot["einde"]} ({slot["duur"]} min) · {slot["baan"]}'
    )


def bouw_bericht(nieuw, cfg):
    groen = sorted((s for s in nieuw if s["soort"] == "groen"), key=_sorteer)
    grijs = sorted((s for s in nieuw if s["soort"] == "grijs"), key=_sorteer)
    delen = ["🎾 Padelbaan vrij bij Meerzicht!"]
    if groen:
        delen.append("\n✅ Online te boeken in de ClubApp:")
        delen += ["• " + fmt(s) for s in groen[:15]]
        if len(groen) > 15:
            delen.append(f"…en {len(groen) - 15} meer")
    if grijs:
        tel = cfg.get("telefoon", "")
        delen.append(f"\n📞 Vrij, maar niet online te boeken. Bel de club {tel}:".rstrip())
        delen += ["• " + fmt(s) for s in grijs[:15]]
        if len(grijs) > 15:
            delen.append(f"…en {len(grijs) - 15} meer")
    return "\n".join(delen)


def _sorteer(s):
    return (s["datum"], s["start"], s["baan"])


def main():
    cfg = load_config()
    club_id = cfg["club_id"]
    now = datetime.now(TZ)
    if os.environ.get("NOW_OVERRIDE"):  # alleen voor testen
        now = datetime.fromisoformat(os.environ["NOW_OVERRIDE"]).replace(tzinfo=TZ)
    vandaag = now.date()
    state = load_state()
    new_state = dict(state)
    nieuw = []
    fouten = 0
    gezien = {"groen": 0, "grijs": 0}
    passend = {"groen": 0, "grijs": 0}
    dagen = cfg.get("dagen_vooruit", 3) + 1

    for offset in range(dagen):
        day = vandaag + timedelta(days=offset)
        ds = day.isoformat()
        try:
            data = fetch_day(club_id, day)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            # Bij een fout houden we de vorige stand voor die dag vast,
            # zodat we geen valse 'nieuw vrij'-meldingen krijgen.
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                detail = f" (HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')})"
            print(f"Fout bij {ds}: {e}{detail}", file=sys.stderr)
            fouten += 1
            time.sleep(1)
            continue
        alle = {}
        for s in parse_day(data):
            if s["datum"] == ds:  # alleen de gevraagde dag
                alle[key(s)] = s
        for s in alle.values():
            gezien[s["soort"]] += 1
        slots = [s for s in alle.values() if wanted(s, cfg) and bookable_now(s, cfg, now)]
        for s in slots:
            passend[s["soort"]] += 1
        vorige = set(state.get(ds, []))
        for s in slots:
            if key(s) not in vorige:
                nieuw.append(s)
        new_state[ds] = sorted(key(s) for s in slots)
        time.sleep(1)  # rustig aan: 1 seconde tussen verzoeken

    # Oude dagen opruimen
    for ds in list(new_state):
        if ds < vandaag.isoformat():
            del new_state[ds]

    # Alleen als ALLES mislukte stoppen met foutcode (zichtbaar in GitHub).
    if fouten == dagen:
        print("Alle verzoeken mislukt; stand niet bijgewerkt.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Gezien: {gezien['groen']} groene en {gezien['grijs']} grijze banen. "
        f"Binnen je wensen: {passend['groen']} groen, {passend['grijs']} grijs."
    )

    if nieuw:
        send_telegram(bouw_bericht(nieuw, cfg))
        print(f"{len(nieuw)} nieuwe baan/banen gemeld.")
    else:
        print(f"{now:%H:%M} Geen nieuwe banen binnen je wensen.")

    # Pas opslaan NA het versturen: mislukt Telegram, dan proberen we het de
    # volgende keer opnieuw en gaat de melding niet verloren.
    save_state(new_state)


if __name__ == "__main__":
    main()
