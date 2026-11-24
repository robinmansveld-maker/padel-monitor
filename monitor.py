#!/usr/bin/env python3
"""Padel-monitor voor Tennis en Padel Meerzicht (KNLTB Meet & Play).

Haalt de openbare baanoverzichten op (geen inlog nodig), bepaalt welke
vrije padelbanen binnen jouw wensen vallen en stuurt een Telegram-bericht
voor banen die sinds de vorige controle NIEUW vrij zijn gekomen.

Alleen standaardbibliotheek, geen extra installatie nodig.
"""
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")  # de GitHub-server draait op UTC, wij rekenen in NL-tijd

BASE = "https://meetandplay.nl/club/{club_id}"
LIVEWIRE_URL = "https://meetandplay.nl/livewire/update"
CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
SNAP_RE = re.compile(r'wire:snapshot="([^"]+)"')
TIME_CHIP_RE = re.compile(r'name="time"\s+value="(\d{1,2}:\d{2})"')
UA = "Mozilla/5.0 (compatible; padel-monitor/1.0; persoonlijk gebruik)"
STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]

DURATION_RE = re.compile(r"<h4>\s*(\d+)\s*minuten\s*</h4>", re.I)
SLOT_SPLIT = '<a wire:click="selectSlot('
TIME_RE = re.compile(
    r'class="timeslot-time">\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})', re.S
)
NAME_RE = re.compile(r'class="timeslot-name">\s*([^<]+?)\s*<', re.S)
PRICE_RE = re.compile(r"€\s*([\d.,]+)")


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_day(club_id, day):
    """Geeft een lijst met HTML-stukken terug: één per starttijd van die dag.

    De pagina toont per keer alleen de banen van één gekozen starttijd
    (de knoppen 09:00, 11:00, 12:30, ...). Daarom vragen we elke starttijd
    apart op, net zoals de website zelf doet als je op zo'n knop klikt.
    """
    qs = urllib.parse.urlencode({"date": day.isoformat(), "sport": "padel"})
    url = f"{BASE.format(club_id=club_id)}?{qs}"
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as r:
        page = r.read().decode("utf-8", errors="replace")

    csrf = CSRF_RE.search(page)
    snapshot = None
    for m in SNAP_RE.finditer(page):
        s = html.unescape(m.group(1))
        if "club.v2.court-show" in s:
            snapshot = s
            break
    tijden = list(dict.fromkeys(TIME_CHIP_RE.findall(page)))
    if not tijden:
        return [page]  # geen starttijden = geen vrije banen die dag
    if not (csrf and snapshot):
        raise ValueError("pagina heeft een andere opbouw dan verwacht")

    stukken = []
    for t in tijden:
        body = json.dumps(
            {
                "_token": csrf.group(1),
                "components": [
                    {"snapshot": snapshot, "updates": {"time": t}, "calls": []}
                ],
            }
        ).encode()
        rq = urllib.request.Request(
            LIVEWIRE_URL,
            data=body,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Livewire": "1",
                "X-CSRF-TOKEN": csrf.group(1),
                "Referer": url,
            },
        )
        with opener.open(rq, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        stukken.append(data["components"][0]["effects"]["html"])
        time.sleep(0.3)
    return stukken


def parse_slots(page, day):
    """Geeft lijst met dicts: datum, start, einde, duur, baan, prijs."""
    # Posities van duur-koppen ("60 minuten" / "90 minuten") in de pagina.
    headings = [(m.start(), int(m.group(1))) for m in DURATION_RE.finditer(page)]
    slots = []
    pos = 0
    while True:
        i = page.find(SLOT_SPLIT, pos)
        if i == -1:
            break
        j = page.find("</a>", i)
        j = len(page) if j == -1 else j
        chunk = page[i:j]
        pos = j
        t = TIME_RE.search(chunk)
        n = NAME_RE.search(chunk)
        if not (t and n):
            continue
        duur = None
        for hpos, hdur in headings:
            if hpos < i:
                duur = hdur
        p = PRICE_RE.search(page[i : j + 400])
        slots.append(
            {
                "datum": day.isoformat(),
                "start": t.group(1).zfill(5),
                "einde": t.group(2).zfill(5),
                "duur": duur,
                "baan": html.unescape(n.group(1)).strip(),
                "prijs": p.group(1) if p else "",
            }
        )
    return slots


def to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def bookable_now(slot, cfg, now):
    """Is de baan nu al te boeken? Je kunt tot X uur vooruit boeken (rollend venster)."""
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
    if cfg.get("duur_minuten") and slot["duur"] not in cfg["duur_minuten"]:
        return False
    start = to_minutes(slot["start"])
    for w in windows:
        van, tot = w.split("-")
        if to_minutes(van) <= start and start <= to_minutes(tot):
            return True
    return False


def key(slot):
    return f'{slot["datum"]}|{slot["start"]}|{slot["duur"]}|{slot["baan"]}'


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
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def fmt(slot):
    d = date.fromisoformat(slot["datum"])
    prijs = f' (€{slot["prijs"]})' if slot["prijs"] else ""
    duur = f'{slot["duur"]} min' if slot["duur"] else ""
    return (
        f'{DAGEN[d.weekday()]} {d.strftime("%d-%m")} '
        f'{slot["start"]}-{slot["einde"]} {duur} · {slot["baan"]}{prijs}'
    )


def main():
    cfg = load_config()
    club_id = cfg["club_id"]
    now = datetime.now(TZ)
    vandaag = now.date()
    state = load_state()
    new_state = dict(state)
    nieuw = []
    fouten = 0
    gezien = 0  # alle vrije banen die de site toont
    passend = 0  # daarvan binnen jouw tijden en boekvenster

    for offset in range(cfg.get("dagen_vooruit", 14) + 1):
        day = vandaag + timedelta(days=offset)
        ds = day.isoformat()
        try:
            paginas = fetch_day(club_id, day)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            # Bij een fout houden we de vorige stand voor die dag vast,
            # zodat we geen valse 'nieuw vrij'-meldingen krijgen.
            print(f"Fout bij {ds}: {e}", file=sys.stderr)
            fouten += 1
            time.sleep(1)
            continue
        # Alleen banen die nu al te boeken zijn (binnen het 72-uursvenster).
        # Zodra een vrije baan het venster binnenkomt, geldt hij als "nieuw" en volgt een melding.
        alle = {}
        for pagina in paginas:
            for s in parse_slots(pagina, day):
                alle[key(s)] = s
        slots = [
            s for s in alle.values() if wanted(s, cfg) and bookable_now(s, cfg, now)
        ]
        gezien += len(alle)
        passend += len(slots)
        keys = sorted(key(s) for s in slots)
        vorige = set(state.get(ds, []))
        if ds in state:  # eerste run van een dag: wel melden als er iets is
            pass
        for s in slots:
            if key(s) not in vorige:
                nieuw.append(s)
        new_state[ds] = keys
        time.sleep(1)  # rustig aan: 1 seconde tussen verzoeken

    # Oude dagen opruimen
    for ds in list(new_state):
        if ds < vandaag.isoformat():
            del new_state[ds]

    # Alleen als ALLES mislukte stoppen met foutcode (zichtbaar in GitHub).
    if fouten == cfg.get("dagen_vooruit", 14) + 1:
        print("Alle verzoeken mislukt; stand niet bijgewerkt.", file=sys.stderr)
        sys.exit(1)

    save_state(new_state)
    print(f"{gezien} vrije banen gezien, {passend} binnen je wensen.")

    if nieuw:
        nieuw.sort(key=lambda s: (s["datum"], s["start"], s["baan"]))
        regels = "\n".join("• " + fmt(s) for s in nieuw[:20])
        extra = f"\n…en {len(nieuw) - 20} meer" if len(nieuw) > 20 else ""
        link = f"https://meetandplay.nl/club/{club_id}?sport=padel"
        send_telegram(
            f"🎾 Padelbaan vrij bij Meerzicht!\n{regels}{extra}\n\nBoek snel: open je KNLTB ClubApp.\n{link}"
        )
        print(f"{len(nieuw)} nieuwe baan/banen gemeld.")
    else:
        print(f"{now:%H:%M} Geen nieuwe banen binnen je wensen.")


if __name__ == "__main__":
    main()
