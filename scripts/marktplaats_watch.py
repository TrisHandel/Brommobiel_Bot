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
import random
import re
import statistics
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

# Als deze env-variabele op "true" staat: alle huidige matches worden stil
# aan seen_ids toegevoegd (net als bij de allereerste run), zonder dat er
# ook maar 1 Telegram-melding wordt verstuurd. Handig na het toevoegen van
# nieuwe zoektermen, om de inhaalvloed van bestaande advertenties te vermijden.
FORCE_SEED_ONLY = os.environ.get("SEED_ONLY", "").strip().lower() == "true"

# Run-nummer van GitHub Actions (bv. "42"), voor herkenbaarheid in Telegram-
# berichten. Leeg als het script buiten Actions wordt gedraaid.
RUN_NUMBER = os.environ.get("RUN_NUMBER", "").strip()
RUN_LABEL = f" [run #{RUN_NUMBER}]" if RUN_NUMBER else ""


def in_quiet_hours() -> bool:
    now = datetime.now(TIMEZONE)
    return QUIET_HOURS_START <= now.hour < QUIET_HOURS_END

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

# Zoektermen die worden gecontroleerd (elke term = een aparte zoekopdracht
# over heel Marktplaats, alle categorieen)
#
# Bewust teruggebracht van 91 naar ~25 kernwoorden: Marktplaats' zoekmachine
# doorzoekt titel EN omschrijving op tekst, dus een zoekopdracht op een
# hoofdmodel (bv. "Aixam Coupé") vangt de meeste specifieke uitvoeringen
# (GTi, Vision, Sensation, Emotion, enz.) toch al automatisch mee. Minder
# termen = minder verzoeken per run = kleinere kans op een 403-blokkade.
SEARCH_TERMS = [
    "Aixam",
    "Axiam",
    "Aixan",
    "Aixem",
    "Aixam City",
    "Aixam Coupé",
    "Aixam Coupe",
    "Aixam Crossline",
    "Aixam Crossover",
    "Aixam Cross",
    "Aixam GTO",
    "Aixam Scouty",
    "Aixam Sport",
    "Aixam Minauto",
    "Minauto City",
    "Aixam D-Truck",
    "Aixam Pro",
    "Aixam Kubota",
    "Aixam diesel",
    "Aixam defect",
    "Aixam schade",
    "Aixam project",
    "Aixam opknapper",
    "Aixam onderdelen",
    "45 km auto defect",
]

# Veiligheidsnet: Marktplaats matcht bij meerdere zoekwoorden (bv. "Aixam
# defect") niet altijd op ALLE woorden samen — soms komen er ook advertenties
# terug die maar 1 van de woorden bevatten (bv. een Audi met "defect" in de
# titel). Daarom vereisen we hier hard dat 1 van deze merknamen letterlijk in
# de titel staat, ongeacht wat Marktplaats zelf teruggeeft.
REQUIRED_BRAND_KEYWORDS = ["aixam", "axiam", "aixan", "aixem", "minauto"]

# Advertenties waarvan de titel een van deze woorden bevat, worden genegeerd
EXCLUDE_TEXT_KEYWORDS = [
    "scootmobiel",
    # Opkoop-/inkoopbedrijven en reclame ("Wij kopen uw Aixam!", etc.)
    "wij kopen",
    "we kopen",
    "ik koop",
    "aankoop",
    "inkoop",
    "wij betalen",
    "we betalen",
    "contant geld",
    "direct geld",
    "gratis ophalen",
    "gratis inleveren",
    "spoedaankoop",
    "verkoop uw",
    "verkoop jouw",
    "verkoopt u uw",
    "wilt u uw",
    "auto's opkopen",
    "auto opkopen",
    "voertuigen opkopen",
    "taxatie",
    "inruilen",
    "handelaar",
    "dealer",
    # Showroom-/dealeradvertenties (bedrijven die zelf voorraad verkopen)
    "showroom",
    "ons aanbod",
    "onze occasions",
    "ons magazijn",
    "configurator",
    "bekijk ons volledige aanbod",
    "bekijk onze showroom",
    # Taal die wijst op handelsvoorraad (meerdere exemplaren), niet op één
    # eenmalige particuliere verkoop
    "meer op voorraad",
    "meerdere op voorraad",
    "diverse op voorraad",
    "ruime keuze",
    "ruim aanbod",
    "meer exemplaren beschikbaar",
    "meer soortgelijke",
    "meer van dit merk",
    "altijd meerdere",
    "wisselende voorraad",
    "dagelijks nieuwe aanbiedingen",
    # Extra opkoop-/inkooptaal, herkend in daadwerkelijke Marktplaats-teksten
    "inkoop specialist",
    "wij kopen iedere",
    "we kopen iedere",
    "ongeacht de staat",
    "hoogste prijs",
    "gezocht!",
    "*gezocht*",
    "€€gezocht€€",
    "zoal dagelijks ophalen",
    "zoal dagelijks inkopen",
    "auto inkoop service",
    "verkoop snel en moeiteloos",
    "beste autoverkoop",
    "snelle en transparante afhandeling",
    "geld op uw rekening",
    "canta en brommobiel inkoop",
    "canta & brommobiel inkoop",
    "brommobiel of canta",
    "10 jaar gespecialiseerd",
    "grootste brommobiel center",
]

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "seen_ids.json"
MAX_SEEN_IDS = 5000  # voorkomt dat het state-bestand oneindig groeit

# Prijsgeschiedenis per model, voor de "goede deal?"-vergelijking. Ruw en
# zonder correctie voor bouwjaar/km — puur een signaal, geen harde waarheid.
PRICE_HISTORY_FILE = Path(__file__).resolve().parent.parent / "state" / "price_history.json"
MAX_PRICES_PER_MODEL = 100  # hoeveel recente prijzen we per model bewaren
MIN_SAMPLES_FOR_COMPARISON = 4  # onder dit aantal is een gemiddelde nog te onbetrouwbaar
DEAL_DISCOUNT_THRESHOLD = 0.40  # 40%+ onder de mediaan = "mogelijke topdeal"

# Los van seen_ids: onthoudt welke advertentie-ID's al hebben meegeteld in de
# prijsgeschiedenis. Dit MOET los staan van "is dit nieuw voor de gebruiker",
# anders telt een advertentie die al lang bekend is (dus geen melding meer
# waard) nooit mee voor de prijsvergelijking — met als gevolg dat de
# prijsgeschiedenis nooit gevuld raakt voor advertenties die al bestonden
# vóór deze functie werd toegevoegd.
PRICED_IDS_FILE = Path(__file__).resolve().parent.parent / "state" / "priced_ids.json"
MAX_PRICED_IDS = 5000

# Modelnamen waarop we de titel matchen, van specifiek naar algemeen (zodat
# "Aixam Crossline Sport" bijv. als "Crossline" geteld wordt, niet als iets
# generieks). De eerste match in de titel wint.
MODEL_KEYWORDS = [
    "Crossline", "Crossover", "Cross", "Coupé", "Coupe", "Scouty",
    "City", "GTO", "Minauto", "D-Truck", "Kubota", "Pro", "Sport",
]

# Staat/conditie-tiers, van slechtst naar best. We checken titel+omschrijving
# in deze volgorde en gebruiken de EERSTE match — zo voorkomen we dat een
# sloper wordt vergeleken met de prijs van een showroomexemplaar (en dus ten
# onrechte als "topdeal" wordt gezien). Zonder match: gewone "gebruikt"-tier.
CONDITION_TIERS = [
    ("sloop/onderdelen", [
        "sloop", "sloper", "sloopauto", "voor onderdelen", "demontage", "export",
    ]),
    ("schade/defect", [
        "schade", "defect", "kapot", "loopt niet", "start niet", "total loss",
        "motor kapot", "storing",
    ]),
    ("opknapper/project", [
        "opknapper", "project", "sleutelklaar", "voor de klusser",
    ]),
    ("nieuwstaat", [
        "nieuwstaat", "als nieuw", "showroomstaat", "showroom staat",
        "fabrieksnieuw", "0 km", "zo goed als nieuw",
    ]),
]
DEFAULT_CONDITION_TIER = "gebruikt"

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.marktplaats.nl/",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
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

    # We filteren NIET meer op account-type (isDealer/sellerType): een dealer
    # kan ook een losse, goedkope inruiler verkopen die je wél wilt zien. Het
    # enige harde filter hier is een gesponsorde/advertentie-plaatsing. Het
    # onderscheid handelaar-met-veel-voorraad vs eenmalige koop maken we op
    # tekst, zie EXCLUDE_TEXT_KEYWORDS ("meer op voorraad" e.d.).
    is_business = False
    if item.get("sponsored") is True or item.get("isSponsored") is True or item.get("isAdvertisement") is True:
        is_business = True

    # Best-effort: Marktplaats kan de (korte) omschrijving onder verschillende
    # veldnamen aanleveren. We proberen de bekendste varianten; als geen
    # ervan aanwezig is, blijft description gewoon leeg (geen harde fout).
    description = (
        item.get("description")
        or item.get("shortDescription")
        or item.get("descriptionHtml")
        or item.get("plainTextDescription")
        or ""
    )

    return {
        "id": item_id,
        "title": unescape(str(title)),
        "description": unescape(str(description)),
        "price": price_text or "onbekend",
        "url": url,
        "location": location,
        "is_business": is_business,
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


def load_priced_ids() -> set:
    if PRICED_IDS_FILE.exists():
        try:
            return set(json.loads(PRICED_IDS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_priced_ids(priced_ids: set):
    PRICED_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = list(priced_ids)[-MAX_PRICED_IDS:]
    PRICED_IDS_FILE.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Prijsvergelijking per model ("is dit een goede deal?")
# ---------------------------------------------------------------------------

def load_price_history() -> dict:
    if PRICE_HISTORY_FILE.exists():
        try:
            return json.loads(PRICE_HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_price_history(history: dict):
    PRICE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = {
        model: {cond: prices[-MAX_PRICES_PER_MODEL:] for cond, prices in conditions.items()}
        for model, conditions in history.items()
    }
    PRICE_HISTORY_FILE.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


def guess_model(title: str) -> str:
    title_lower = title.lower()
    for model in MODEL_KEYWORDS:
        if model.lower() in title_lower:
            return model
    return "Aixam (overig)"


CURRENT_YEAR = datetime.now(ZoneInfo("Europe/Amsterdam")).year


def extract_year(title: str):
    """Haalt een bouwjaar uit de titel, bv. '2015', 'bj. 2015' of 'bj \\'18'.
    Geeft None terug als er niks betrouwbaars te vinden is."""
    # Volledig jaartal, bv. "2015"
    match = re.search(r"\b(19[89]\d|20[0-3]\d)\b", title)
    if match:
        year = int(match.group(1))
        if 1980 <= year <= CURRENT_YEAR + 1:
            return year
    # Verkort jaartal met "bj"/"bouwjaar"/'-teken, bv. "bj. '18" of "bj 08"
    match = re.search(r"(?:bj\.?|bouwjaar)\s*'?(\d{2})\b", title, re.IGNORECASE)
    if match:
        two_digit = int(match.group(1))
        year = 2000 + two_digit if two_digit <= (CURRENT_YEAR - 2000) else 1900 + two_digit
        return year
    return None


def guess_condition(text: str) -> str:
    """Bepaalt een ruwe staat-tier op basis van titel+omschrijving, zodat we
    prijzen alleen vergelijken binnen dezelfde conditie (een sloper mag niet
    met een showroomexemplaar vergeleken worden)."""
    text_lower = text.lower()
    # Bugfix: "GEEN schade" / "zonder defect" mag niet als schade/defect
    # tellen — we verwijderen ontkenningen vóór een negatief woord eerst.
    text_lower = re.sub(
        r"\b(geen|zonder|niet)\s+(schade|defect|storing|kapot|sloop)\b", "", text_lower
    )
    for tier_name, keywords in CONDITION_TIERS:
        if any(kw in text_lower for kw in keywords):
            return tier_name
    return DEFAULT_CONDITION_TIER


def parse_price_to_number(price_text: str):
    """Zet '€ 1.234' om naar 1234.0. Geeft None terug bij 'onbekend',
    'Bieden', 'Gratis' e.d. — die kunnen we niet numeriek vergelijken."""
    if not price_text:
        return None
    digits = re.sub(r"[^\d]", "", price_text)
    if not digits:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def classify_price(model: str, condition: str, price: float, history: dict):
    """Vergelijkt price met de mediaan van eerder gezien prijzen voor dit
    model EN dezelfde conditie-tier (dus geen sloper vs. showroomexemplaar).
    Geeft (tier_label, emoji, mediaan, verschil_percentage) terug, of
    (None, None, None, None) als er nog te weinig data is om te vergelijken."""
    samples = history.get(model, {}).get(condition, [])
    if len(samples) < MIN_SAMPLES_FOR_COMPARISON:
        return None, None, None, None
    median_price = statistics.median(samples)
    if median_price <= 0:
        return None, None, None, None
    # Positief % = goedkoper dan gemiddeld, negatief % = duurder dan gemiddeld
    diff_pct = (median_price - price) / median_price

    if diff_pct >= DEAL_DISCOUNT_THRESHOLD:
        return "topdeal", "🔥", median_price, diff_pct
    if diff_pct >= 0.15:
        return "goede prijs", "🟢", median_price, diff_pct
    if diff_pct >= -0.15:
        return "gemiddelde prijs", "🟡", median_price, diff_pct
    return "aan de prijzige kant", "🔴", median_price, diff_pct


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text: str, silent: bool = False, chat_id: str = None):
    """Verstuurt een bericht. Geeft het message_id terug bij succes (nodig om
    het bericht evt. te kunnen pinnen), of None bij een fout."""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat:
        print("TELEGRAM_BOT_TOKEN of chat-ID ontbreekt, kan geen bericht sturen.", file=sys.stderr)
        return None
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
        return None
    try:
        return resp.json().get("result", {}).get("message_id")
    except (ValueError, AttributeError):
        return None


def pin_telegram_message(message_id: int, chat_id: str = None):
    """Pint een bericht vast in de chat, zodat topdeals bovenaan blijven
    staan. Best-effort: als pinnen niet mag/lukt, laten we de rest van het
    script gewoon doorlopen."""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage"
    resp = requests.post(
        url,
        data={"chat_id": target_chat, "message_id": message_id, "disable_notification": "false"},
        timeout=20,
    )
    if not resp.ok:
        print(f"Telegram-pin-fout ({resp.status_code}): {resp.text}", file=sys.stderr)


def format_message(listing: dict) -> str:
    title = listing["title"]
    price = listing["price"]
    location = listing["location"]
    url = listing["url"]
    location_line = f"\n📍 {location}" if location else ""
    condition = listing.get("condition", "onbekend")

    price_tier = listing.get("price_tier")
    if price_tier:
        tier_label, emoji, median_price, diff_pct = price_tier
        pct = round(abs(diff_pct) * 100)
        richting = "goedkoper" if diff_pct >= 0 else "duurder"
        header = "🔥 <b>MOGELIJKE TOPDEAL</b>\n" if tier_label == "topdeal" else ""
        downgrade_note = ""
        if listing.get("topdeal_downgraded"):
            missing = []
            if listing.get("model") == "Aixam (overig)":
                missing.append("model")
            if listing.get("year") is None:
                missing.append("bouwjaar")
            downgrade_note = (
                f"\nℹ️ Korting zou een topdeal-niveau halen, maar {' en '.join(missing)} "
                f"niet duidelijk genoeg uit de titel — daarom geen topdeal-prioriteit."
            )
        return (
            f"{header}"
            f"<b>{title}</b>\n"
            f"💶 {price} — {emoji} {tier_label} "
            f"(gemiddeld voor '{condition}'-advertenties van dit model: ~€ {median_price:,.0f}, "
            f"dus ~{pct}% {richting})\n"
            f"⚠️ Ruwe schatting op basis van model + conditie + prijs alleen — "
            f"check zelf bouwjaar, km-stand en staat!"
            f"{downgrade_note}"
            f"{location_line}\n{url}"
        )

    return (
        f"🆕 <b>{title}</b>\n💶 {price} "
        f"<i>(nog te weinig data voor dit model+conditie om te vergelijken)</i>"
        f"{location_line}\n{url}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen_ids = load_seen_ids()
    price_history = load_price_history()
    priced_ids = load_priced_ids()
    first_run = len(seen_ids) == 0
    new_listings = {}
    newly_priced_count = 0
    search_errors = []

    consecutive_forbidden = 0
    aborted_early = False

    for term in SEARCH_TERMS:
        print(f"Zoeken naar: {term}")
        try:
            listings = fetch_listings(term)
        except requests.RequestException as e:
            print(f"  fout bij ophalen van '{term}': {e}", file=sys.stderr)
            search_errors.append(f"{term}: {e}")
            if "403" in str(e) or "Forbidden" in str(e):
                consecutive_forbidden += 1
            else:
                consecutive_forbidden = 0
            if consecutive_forbidden >= 5:
                # Marktplaats blokkeert kennelijk actief binnen deze run.
                # Doorgaan maakt het waarschijnlijk alleen erger en kost
                # alleen maar tijd — stop deze run vroegtijdig, de rest van
                # de termen proberen we vanzelf weer bij de volgende run.
                remaining = SEARCH_TERMS[SEARCH_TERMS.index(term) + 1:]
                search_errors.append(
                    f"Run vroegtijdig gestopt na {consecutive_forbidden}x 403 op rij "
                    f"({len(remaining)} termen niet geprobeerd)."
                )
                aborted_early = True
                break
            time.sleep(4)  # bij een fout iets langer wachten voordat we verder gaan
            continue
        else:
            consecutive_forbidden = 0

        print(f"  {len(listings)} advertenties gevonden")
        for listing in listings:
            if listing.get("is_business"):
                continue
            # Titel + omschrijving samen checken: sommige advertenties noemen
            # "Aixam" pas in de omschrijving, en opkoopbedrijven verstoppen
            # hun taal ("wij kopen iedere...") ook vaak in de omschrijving.
            combined_text = f"{listing['title']} {listing.get('description', '')}".lower()
            if not any(brand in combined_text for brand in REQUIRED_BRAND_KEYWORDS):
                continue
            if any(bad.lower() in combined_text for bad in EXCLUDE_TEXT_KEYWORDS):
                continue

            # Model + conditie + numerieke prijs bepalen. Dit doen we voor
            # ELKE gevonden advertentie die door de filters komt, OOK als
            # die al eerder gezien is — anders vult de prijsgeschiedenis
            # zich nooit voor advertenties die al bestonden vóór deze
            # functie werd toegevoegd.
            model = guess_model(listing["title"])
            condition = guess_condition(combined_text)
            numeric_price = parse_price_to_number(listing["price"])
            year = extract_year(listing["title"])
            listing["model"] = model
            listing["condition"] = condition
            listing["numeric_price"] = numeric_price
            listing["year"] = year

            if numeric_price is not None:
                tier_label, emoji, median_price, diff_pct = classify_price(
                    model, condition, numeric_price, price_history
                )
                if tier_label:
                    # Topdeal-status ("prioriteit": pin + altijd pop-up) mag
                    # alleen als merk (al gegarandeerd), model, bouwjaar EN
                    # vraagprijs allemaal betrouwbaar bekend zijn. Ontbreekt
                    # bouwjaar of is het model niet herkend (valt in "Aixam
                    # (overig)")? Dan downgraden we naar een gewone melding
                    # zonder prioriteit, met een duidelijke toelichting.
                    model_known = model != "Aixam (overig)"
                    if tier_label == "topdeal" and not (model_known and year is not None):
                        listing["topdeal_downgraded"] = True
                        tier_label, emoji = "goede prijs", "🟢"
                    listing["price_tier"] = (tier_label, emoji, median_price, diff_pct)

                # Elke unieke advertentie draagt precies 1x bij aan de
                # prijsgeschiedenis, ongeacht of hij al "gezien" was.
                if listing["id"] not in priced_ids:
                    price_history.setdefault(model, {}).setdefault(condition, []).append(numeric_price)
                    priced_ids.add(listing["id"])
                    newly_priced_count += 1

            if listing["id"] in seen_ids or listing["id"] in new_listings:
                continue

            new_listings[listing["id"]] = listing

        # Iets langere, licht willekeurige pauze tussen zoekopdrachten: minder
        # herkenbaar als robotverkeer dan een vast interval van 1 seconde.
        time.sleep(random.uniform(2.5, 4.5))

    seen_ids.update(new_listings.keys())
    save_seen_ids(seen_ids)

    # Prijshistorie + priced_ids zijn al bijgewerkt in de loop hierboven
    # (voor elke unieke, kwalificerende advertentie, gezien of niet).
    save_price_history(price_history)
    save_priced_ids(priced_ids)

    timestamp = datetime.now(TIMEZONE).strftime("%d-%m-%Y %H:%M")
    quiet = in_quiet_hours()  # 00:00-05:00: berichten wel versturen, maar zonder pop-up

    if first_run or FORCE_SEED_ONLY:
        # Bij de allereerste run, of bij een handmatige seed-run na het
        # toevoegen van nieuwe zoektermen: alleen de state vullen, niet spammen.
        print(f"Seed-run: {len(new_listings)} advertenties opgeslagen als 'al gezien', geen meldingen verstuurd.")
        if first_run:
            send_telegram_message(
                f"👋 Marktplaats-watcher is gestart ({timestamp}){RUN_LABEL}. "
                f"{len(new_listings)} bestaande advertenties opgeslagen als basis "
                f"({newly_priced_count} met bruikbare prijs voor de prijsvergelijking). "
                f"Vanaf nu krijg je een melding bij elke check.",
                silent=quiet,
            )
        else:
            send_telegram_message(
                f"🔄 Seed-run uitgevoerd ({timestamp}){RUN_LABEL}: {len(new_listings)} advertenties "
                f"toegevoegd aan de al-gezien-lijst, en {newly_priced_count} advertenties "
                f"(nieuw of al bekend) met prijs meegenomen in de prijsvergelijking. "
                f"Geen meldingen verstuurd.",
                silent=True,
                chat_id=TELEGRAM_HEARTBEAT_CHAT_ID,
            )
        return

    if not new_listings:
        print("Geen nieuwe advertenties.")
        if search_errors:
            # Bij véél gelijktijdige fouten (bv. Marktplaats blokkeert de bot
            # tijdelijk) een korte samenvatting sturen i.p.v. elke losse fout
            # te noemen — anders wordt het bericht te lang voor Telegram
            # (limiet ~4096 tekens) en komt het helemaal niet aan.
            forbidden_count = sum(1 for e in search_errors if "403" in e or "Forbidden" in e)
            if len(search_errors) >= 10 and forbidden_count >= len(search_errors) - 2:
                text = (
                    f"🚫 Check uitgevoerd ({timestamp}){RUN_LABEL}: {len(search_errors)} van de "
                    f"{len(SEARCH_TERMS)} zoekopdrachten kregen een 403 Forbidden. "
                    f"Marktplaats blokkeert de bot mogelijk (tijdelijk of structureel) — "
                    f"check de Actions-log als dit blijft aanhouden."
                )
            else:
                summary = "; ".join(search_errors[:5])
                extra = f" (+{len(search_errors) - 5} meer)" if len(search_errors) > 5 else ""
                text = (
                    f"⚠️ Check uitgevoerd ({timestamp}){RUN_LABEL}, geen nieuwe advertenties, "
                    f"maar er ging iets mis bij: {summary}{extra}"
                )
            # Een echte fout is belangrijk genoeg om op je hoofdchat te melden,
            # met normale pop-up, zodat je 'm niet mist.
            send_telegram_message(text[:4000])
        else:
            # Rustige heartbeat: naar het apart gemute kanaal, stil.
            send_telegram_message(
                f"✅ Check uitgevoerd ({timestamp}){RUN_LABEL} — geen nieuwe advertenties.",
                silent=True,
                chat_id=TELEGRAM_HEARTBEAT_CHAT_ID,
            )
        return

    # Echte nieuwe advertentie(s): normale pop-up, behalve tijdens de nachtelijke
    # stille uren (00:00-05:00) — dan komt het bericht wel binnen, maar stil.
    print(f"{len(new_listings)} nieuwe advertentie(s), Telegram-berichten versturen...")
    for listing in new_listings.values():
        price_tier = listing.get("price_tier")
        is_topdeal = bool(price_tier) and price_tier[0] == "topdeal"
        # Een mogelijke topdeal altijd met pop-up versturen, ook 's nachts —
        # dit is precies het soort melding waarvoor je wél gewekt wilt worden.
        message_id = send_telegram_message(format_message(listing), silent=(quiet and not is_topdeal))
        if is_topdeal and message_id:
            pin_telegram_message(message_id)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
