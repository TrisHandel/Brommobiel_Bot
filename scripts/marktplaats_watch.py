#!/usr/bin/env python3
"""
Marktplaats watcher -> Telegram

Zoekt op Marktplaats.nl naar de opgegeven zoektermen, filtert advertenties
met "scootmobiel" in de titel eruit, en stuurt nieuwe advertenties (die nog
niet eerder gezien zijn) als Telegram-bericht.

LET OP: Marktplaats heeft geen officiele publieke zoek-API voor dit doel.
Dit script leest de gewone zoekresultaten-pagina uit (net als een browser
zou doen) en probeert daar de advertentiedata uit te halen. Marktplaats kan
de site-structuur op elk moment wijzigen, waardoor dit script kan stoppen
met werken. Als dat gebeurt: kijk in de GitHub Actions-log naar de foutmelding
en geef die door, dan kan de parsing-logica aangepast worden.

Gebruik alleen voor persoonlijk gebruik, met een redelijke interval (niet
vaker dan elke paar minuten), uit respect voor Marktplaats' servers.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# Tussen deze uren (Nederlandse tijd) worden berichten stil verstuurd:
# ze komen wel in Telegram binnen, maar zonder pop-up/geluid op je telefoon.
QUIET_HOURS_START = 0   # 00:00
QUIET_HOURS_END = 5     # 05:00 (dus stil van 00:00 t/m 04:59)
TIMEZONE = ZoneInfo("Europe/Amsterdam")


def in_quiet_hours() -> bool:
    now = datetime.now(TIMEZONE)
    return QUIET_HOURS_START <= now.hour < QUIET_HOURS_END

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

# Zoektermen die worden gecontroleerd (elke term = een aparte zoekopdracht
# over heel Marktplaats, alle categorieen)
SEARCH_TERMS = [
    "aixam",
    "brommobiel",
    "45km auto",
]

# Advertenties waarvan de titel een van deze woorden bevat, worden genegeerd
EXCLUDE_TITLE_KEYWORDS = [
    "scootmobiel",
]

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "seen_ids.json"
MAX_SEEN_IDS = 5000  # voorkomt dat het state-bestand oneindig groeit

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# Apart, gemute kanaal voor "geen nieuwe advertenties"-heartbeats. Valt terug
# op de hoofdchat als deze niet is ingesteld.
TELEGRAM_HEARTBEAT_CHAT_ID = os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID") or TELEGRAM_CHAT_ID

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


# ---------------------------------------------------------------------------
# Marktplaats ophalen en parsen
# ---------------------------------------------------------------------------

def fetch_search_html(query: str) -> str:
    url = f"https://www.marktplaats.nl/q/{quote(query)}/"
    params = {"sortBy": "SORT_INDEX", "sortOrder": "DECREASING"}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=25)
    resp.raise_for_status()
    return resp.text


def _walk_for_listings(node, results, seen_obj_ids):
    """Loopt recursief door de __NEXT_DATA__ JSON-boom en verzamelt dicts
    die eruitzien als een advertentie."""
    if isinstance(node, dict):
        obj_id = id(node)
        if obj_id in seen_obj_ids:
            return
        seen_obj_ids.add(obj_id)

        title = node.get("title")
        item_id = node.get("itemId") or node.get("id") or node.get("listingId")
        has_price_field = any(k in node for k in ("priceInfo", "price", "priceCents"))
        vip_url = node.get("vipUrl") or node.get("url") or node.get("relativeUrl")

        if title and item_id and (has_price_field or vip_url):
            results.append(node)

        for v in node.values():
            _walk_for_listings(v, results, seen_obj_ids)
    elif isinstance(node, list):
        for v in node:
            _walk_for_listings(v, results, seen_obj_ids)


def parse_listings_from_next_data(html: str):
    match = NEXT_DATA_RE.search(html)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"  waarschuwing: __NEXT_DATA__ kon niet als JSON gelezen worden: {e}", file=sys.stderr)
        return []

    raw_results = []
    _walk_for_listings(data, raw_results, set())

    listings = []
    for item in raw_results:
        listings.append(normalize_listing(item))
    return [l for l in listings if l is not None]


def normalize_listing(item: dict):
    item_id = str(item.get("itemId") or item.get("id") or item.get("listingId") or "").strip()
    title = item.get("title")
    if not item_id or not title:
        return None

    # Prijs kan op meerdere manieren aangeleverd worden
    price_text = None
    price_info = item.get("priceInfo")
    if isinstance(price_info, dict):
        cents = price_info.get("priceCents")
        if cents is not None:
            price_text = f"€ {cents / 100:,.0f}".replace(",", ".")
        elif price_info.get("priceType"):
            price_text = price_info.get("priceType")
    if price_text is None and item.get("price") is not None:
        price_text = str(item.get("price"))

    # URL kan relatief of absoluut zijn
    url = item.get("vipUrl") or item.get("url") or item.get("relativeUrl") or ""
    if url and url.startswith("/"):
        url = "https://www.marktplaats.nl" + url
    if not url and item_id:
        url = f"https://www.marktplaats.nl/v/{item_id}"

    location = ""
    loc = item.get("location") or item.get("sellerInformation")
    if isinstance(loc, dict):
        location = loc.get("cityName") or loc.get("city") or ""

    return {
        "id": item_id,
        "title": unescape(str(title)),
        "price": price_text or "onbekend",
        "url": url,
        "location": location,
    }


def parse_listings_fallback(html: str):
    """Ruwe fallback op basis van regex, voor het geval __NEXT_DATA__ ontbreekt
    of van structuur is veranderd. Minder betrouwbaar, maar beter dan niets."""
    listings = []
    # Zoekt naar advertentielinks zoals /v/categorie/subcategorie/mXXXXXXX-titel-slug
    for m in re.finditer(r'href="(/v/[^"]+?/(m\d+)-[^"?]*)"[^>]*>.*?</a>', html):
        pass  # HTML-structuur is te wisselvallig om hier betrouwbaar op te bouwen
    return listings


def fetch_listings(query: str):
    html = fetch_search_html(query)
    listings = parse_listings_from_next_data(html)
    if not listings:
        listings = parse_listings_fallback(html)
    return listings


# ---------------------------------------------------------------------------
# State (welke advertenties al gezien zijn)
# ---------------------------------------------------------------------------

def load_seen_ids() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen_ids(seen_ids: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = list(seen_ids)[-MAX_SEEN_IDS:]
    STATE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text: str, silent: bool = False, chat_id: str = None):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat:
        print("TELEGRAM_BOT_TOKEN of chat-ID ontbreekt, kan geen bericht sturen.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
            "disable_notification": "true" if silent else "false",
        },
        timeout=20,
    )
    if not resp.ok:
        print(f"Telegram-fout ({resp.status_code}): {resp.text}", file=sys.stderr)


def format_message(listing: dict) -> str:
    title = listing["title"]
    price = listing["price"]
    location = listing["location"]
    url = listing["url"]
    location_line = f"\n📍 {location}" if location else ""
    return f"🆕 <b>{title}</b>\n💶 {price}{location_line}\n{url}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen_ids = load_seen_ids()
    first_run = len(seen_ids) == 0
    new_listings = {}
    search_errors = []

    for term in SEARCH_TERMS:
        print(f"Zoeken naar: {term}")
        try:
            listings = fetch_listings(term)
        except requests.RequestException as e:
            print(f"  fout bij ophalen van '{term}': {e}", file=sys.stderr)
            search_errors.append(f"{term}: {e}")
            continue

        print(f"  {len(listings)} advertenties gevonden")
        for listing in listings:
            title_lower = listing["title"].lower()
            if any(bad.lower() in title_lower for bad in EXCLUDE_TITLE_KEYWORDS):
                continue
            if listing["id"] in seen_ids or listing["id"] in new_listings:
                continue
            new_listings[listing["id"]] = listing

        time.sleep(1)  # even pauzeren tussen zoekopdrachten, netjes voor de server

    seen_ids.update(new_listings.keys())
    save_seen_ids(seen_ids)

    timestamp = datetime.now(TIMEZONE).strftime("%d-%m-%Y %H:%M")
    quiet = in_quiet_hours()  # 00:00-05:00: berichten wel versturen, maar zonder pop-up

    if first_run:
        # Bij de allereerste run alleen de state vullen, niet alles spammen
        print(f"Eerste run: {len(new_listings)} advertenties opgeslagen als 'al gezien', geen meldingen verstuurd.")
        send_telegram_message(
            f"👋 Marktplaats-watcher is gestart ({timestamp}). "
            f"{len(new_listings)} bestaande advertenties opgeslagen als basis. "
            f"Vanaf nu krijg je een melding bij elke check.",
            silent=quiet,
        )
        return

    if not new_listings:
        print("Geen nieuwe advertenties.")
        if search_errors:
            # Een echte fout is belangrijk genoeg om op je hoofdchat te melden,
            # met normale pop-up, zodat je 'm niet mist.
            send_telegram_message(
                f"⚠️ Check uitgevoerd ({timestamp}), geen nieuwe advertenties, "
                f"maar er ging iets mis bij: {'; '.join(search_errors)}"
            )
        else:
            # Rustige heartbeat: naar het apart gemute kanaal, stil.
            send_telegram_message(
                f"✅ Check uitgevoerd ({timestamp}) — geen nieuwe advertenties.",
                silent=True,
                chat_id=TELEGRAM_HEARTBEAT_CHAT_ID,
            )
        return

    # Echte nieuwe advertentie(s): normale pop-up, behalve tijdens de nachtelijke
    # stille uren (00:00-05:00) — dan komt het bericht wel binnen, maar stil.
    print(f"{len(new_listings)} nieuwe advertentie(s), Telegram-berichten versturen...")
    for listing in new_listings.values():
        send_telegram_message(format_message(listing), silent=quiet)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
