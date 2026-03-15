"""
FVE AI Agent - proteus.deltagreen.cz
=====================================
Třívrstvá logika:
  - Každých 5 minut: rychlá reaktivní kontrola (bez AI)
  - Každou hodinu:   plné přehodnocení s Claude AI
  - Každý den 14:30: denní plán po zveřejnění cen OTE
"""

import os
import json
import subprocess
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Prague")

# ============================================================
# KONFIGURACE
# ============================================================
PORTAL_URL   = "https://proteus.deltagreen.cz"
API_URL      = "https://proteus.deltagreen.cz/api/trpc/inverters.controls.updateManualControl?batch=1"
INVERTER_ID  = "tgqgq7sjswuw1renbowwddlf"

PORTAL_USERNAME  = os.environ.get("PORTAL_USERNAME", "")
PORTAL_PASSWORD  = os.environ.get("PORTAL_PASSWORD", "")
PORTAL_SESSION   = os.environ["PORTAL_SESSION"]
PORTAL_CSRF      = os.environ["PORTAL_CSRF"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LATITUDE  = float(os.environ.get("FVE_LAT", "50.0755"))
LONGITUDE = float(os.environ.get("FVE_LON", "14.4378"))

VYKUP_PRAH_CZK   = 1.5    # Kč/kWh — pod touto cenou se prodej do sítě nevyplatí (opotřebení bat. ~0.6 + marže)
BATERIE_MIN_PCT  = 20     # % — pod touto hodnotou zastav prodej z baterie
CENA_DRAHA_CZK   = 3.5    # Kč/kWh — nad touto cenou považujeme elektřinu za drahou (špička)
CENA_LEVNA_CZK   = 0.5    # Kč/kWh — pod touto cenou považujeme elektřinu za levnou (přebytek FVE)
BATERIE_OPOTREBENI_CZK = 1.67  # Kč/kWh — skutečné opotřebení článků (90000 Kč / 54000 kWh)
BATERIE_UCINNOST      = 0.85   # round-trip účinnost střídač+baterie
BATERIE_DISTRIBUCE_BONUS = 0.40  # Kč/kWh — rozdíl VT/NT distribuce ve prospěch nabíjení
BATERIE_KAPACITA_KWH  = 10.0   # kWh — využitelná kapacita baterie
NOCNI_NABIJENI_MIN_SPREAD = 1.8  # Kč/kWh — minimální spread spot cen aby se nabíjení vyplatilo
BATERIE_MAX_NOCNI_PCT = 80     # % — maximální nabití přes noc (nechej rezervu pro FVE)
HISTORIE_SOUBOR = "history.json"
MOD_SOUBOR      = "current_mode.json"
HISTORIE_MAX_ZAZNAMU = 30 * 24  # 30 dní po hodinách = max 720 záznamů

MODY = {
    "SELLING_INSTEAD_OF_BATTERY_CHARGE": "🟡 Prodej do sítě místo nabíjení",
    "USING_FROM_GRID_INSTEAD_OF_BATTERY":"🩵 Šetření energie v baterii",
    "SAVING_TO_BATTERY":                 "🩵 Nabíjení baterie ze sítě",
    "BLOCKING_GRID_OVERFLOW":            "🔴 Zákaz přetoků",
    "DEFAULT":                           "⚪ Výchozí mód",
}

# ============================================================
# TELEGRAM
# ============================================================

def telegram(zprava: str, tichy: bool = False):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": zprava,
                "parse_mode": "HTML",
                "disable_notification": tichy,
            },
            timeout=10,
        )
        print("📱 Telegram: odesláno")
    except Exception as e:
        print(f"📱 Telegram chyba: {e}")

# ============================================================
# PŘIHLÁŠENÍ
# ============================================================

def prihlasit_se() -> requests.Session | None:
    print("🔐 Přihlašuji se (cookie)...")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Origin": PORTAL_URL,
        "Referer": f"{PORTAL_URL}/cs/household/hzvoejscpwua00vkzlw28vm0/overview",
        "x-proteus-csrf": PORTAL_CSRF,
        "trpc-accept": "application/jsonl",
    })
    session.cookies.set("proteus_session", PORTAL_SESSION, domain="proteus.deltagreen.cz")
    session.cookies.set("proteus_csrf", PORTAL_CSRF, domain="proteus.deltagreen.cz")
    # Ověření — zkusíme jednoduchý request
    try:
        resp = session.get(f"{PORTAL_URL}/api/trpc/households.getAll?batch=1",
                           params={"input": "[{}]"}, timeout=10)
        if resp.status_code == 200:
            print("   ✅ Cookie session OK")
            return session
        print(f"   ⚠️ Status {resp.status_code} — zkouším pokračovat")
        return session  # Vrátíme session i tak, může fungovat pro jiné endpointy
    except Exception as e:
        print(f"   ⚠️ Ověření selhalo: {e} — zkouším pokračovat")
        return session

# ============================================================
# STAV FVE
# ============================================================

def ziskat_stav_fve(session: requests.Session) -> dict | None:
    print("📊 Načítám stav FVE...")
    try:
        resp = session.get(
            f"{PORTAL_URL}/api/trpc/inverters.lastState?batch=1",
            params={"input": json.dumps({"0": {"json": {"inverterId": INVERTER_ID}}})},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"   ⚠️ Status: {resp.status_code} | Raw: {resp.text[:300]}")
            return None

        # Odpověď je superjson stream — hledáme objekt s daty (index 2)
        # Formát: více JSON objektů za sebou oddělených newline
        raw = resp.text
        data = None
        for chunk in raw.strip().split("\n"):
            try:
                obj = json.loads(chunk)
                # Hledáme chunk s "json": [2, 0, [...]] — to jsou reálná data
                j = obj.get("json", [])
                if isinstance(j, list) and len(j) >= 3 and j[0] == 2:
                    data = j[2][0][0]
                    break
            except:
                continue

        if not data:
            print(f"   ⚠️ Nepodařilo se naparsovat data")
            print(f"   RAW: {repr(raw[:500])}")
            return None

        # Skutečné názvy polí z API (ověřeno z Network Response):
        # batteryStateOfCharge, photovoltaicPower, consumptionPower, gridPower
        # gridPower > 0 = odběr ze sítě, < 0 = přetok do sítě
        stav = {
            "baterie_procent": data.get("batteryStateOfCharge", 0),
            "vyroba_w":        data.get("photovoltaicPower", 0),
            "spotreba_w":      data.get("consumptionPower", 0),
            "odber_site_w":    data.get("gridPower", 0),       # + = odběr, - = přetok
            "baterie_w":       data.get("batteryPower", 0),    # výkon baterie (+ nabíjení)
            "aktivni_mod":     data.get("statusActiveControl") or "DEFAULT",
        }
        pretok = max(0, -stav["odber_site_w"])
        odber  = max(0,  stav["odber_site_w"])
        bat_w  = stav["baterie_w"]
        if bat_w > 100:   bat_info = f"+{bat_w}W ↑nabíjí"
        elif bat_w < -100: bat_info = f"{bat_w}W ↓vybíjí"
        else:              bat_info = f"{bat_w}W ~klid"
        print(f"   🔋 {stav['baterie_procent']}% [{bat_info}] | ☀️ {stav['vyroba_w']}W | 🏠 {stav['spotreba_w']}W | 🔌 odběr {odber}W | ↑ přetok {pretok}W")
        return stav
    except Exception as e:
        print(f"   ⚠️ Chyba: {e}")
        return None

# ============================================================
# POČASÍ (Open-Meteo)
# ============================================================

def ziskat_pocasi() -> dict | None:
    print("🌤️ Načítám počasí...")
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":     LATITUDE,
                "longitude":    LONGITUDE,
                "hourly":       "cloud_cover,direct_radiation",
                "daily":        "cloud_cover_mean,sunshine_duration,precipitation_sum",
                "timezone":     "Europe/Prague",
                "forecast_days": 2,
            },
            timeout=10,
        )
        data = resp.json()
        hodina = datetime.now(TZ).hour
        dnes = {
            "oblacnost":    data["daily"]["cloud_cover_mean"][0],
            "slunce_h":     round(data["daily"]["sunshine_duration"][0] / 3600, 1),
            "srazky_mm":    data["daily"]["precipitation_sum"][0],
        }
        zitrek = {
            "oblacnost":    data["daily"]["cloud_cover_mean"][1],
            "slunce_h":     round(data["daily"]["sunshine_duration"][1] / 3600, 1),
            "srazky_mm":    data["daily"]["precipitation_sum"][1],
        }
        print(f"   Dnes: {dnes['oblacnost']}% oblak, {dnes['slunce_h']}h | Zítra: {zitrek['oblacnost']}% oblak, {zitrek['slunce_h']}h")
        return {
            "dnes":   dnes,
            "zitrek": zitrek,
            "aktualni_radiace_wm2": data["hourly"]["direct_radiation"][hodina] if hodina < 48 else 0,
            "radiace_zbytek":       data["hourly"]["direct_radiation"][hodina:hodina + 12],
        }
    except Exception as e:
        print(f"   ⚠️ Chyba: {e}")
        return None

# ============================================================
# CENY ELEKTŘINY (spotovaelektrina.cz — 15min intervaly)
# ============================================================

def ziskat_ceny() -> dict | None:
    print("💰 Načítám ceny spotovaelektrina.cz...")
    try:
        resp = requests.get(
            "https://spotovaelektrina.cz/api/v1/price/get-prices-json-qh",
            timeout=10,
        )
        data = resp.json()
        now = datetime.now(TZ)
        h = now.hour
        m = now.minute

        # Ceny jsou v Kč/MWh (priceCZK: 2596 = 2.596 Kč/kWh) — dělíme 1000
        dnes   = [round(p["priceCZK"] / 1000, 3) for p in data["hoursToday"]]
        zitrek = [round(p["priceCZK"] / 1000, 3) for p in data["hoursTomorrow"]]

        # Aktuální čtvrthodinový index
        idx = h * 4 + m // 15
        aktualni = dnes[idx] if idx < len(dnes) else dnes[-1]

        # Průměrné hodinové ceny (pro přehled a denní plán)
        czk_hod = [round(sum(dnes[i*4:(i+1)*4]) / 4, 2) for i in range(24)]

        # Zbytek dne od aktuálního indexu (15min granularita)
        zbytek_15min = dnes[idx:]

        # Hodiny s drahými/levnými/zápornými cenami
        hodiny_drahe   = [i for i, c in enumerate(czk_hod) if c >= CENA_DRAHA_CZK]
        hodiny_levne   = [i for i, c in enumerate(czk_hod) if c <= CENA_LEVNA_CZK]
        hodiny_zaporne = [i for i, c in enumerate(czk_hod) if c < 0]

        # Aktuální level přímo z API
        aktualni_level = data["hoursToday"][idx]["level"] if idx < len(data["hoursToday"]) else "unknown"

        print(f"   Aktuální: {aktualni} Kč/kWh [{aktualni_level}] | Min: {min(dnes):.2f} | Max: {max(dnes):.2f}")
        return {
            "aktualni":       aktualni,
            "aktualni_level": aktualni_level,
            "prumer":         round(sum(czk_hod) / len(czk_hod), 2),
            "max":            max(dnes),
            "min":            min(dnes),
            "vsechny_15min":  dnes,
            "zitrek_15min":   zitrek,
            "prumer_zitrek":  round(sum(zitrek) / len(zitrek), 2) if zitrek else None,
            "zbytek_15min":   zbytek_15min,
            "hodiny_drahe":   hodiny_drahe,
            "hodiny_levne":   hodiny_levne,
            "hodiny_zaporne": hodiny_zaporne,
        }
    except Exception as e:
        print(f"   ⚠️ Chyba: {e}")
        return None
# ============================================================

# ============================================================
# REAKTIVNÍ KONTROLA — rychlá pravidla bez AI
# ============================================================

def reaktivni_kontrola(stav: dict | None, ceny: dict | None) -> tuple[str, str] | None:
    """Vrátí (mod, duvod) pokud je třeba okamžitě zasáhnout, jinak None."""
    if not stav:
        return None

    bat      = stav["baterie_procent"]
    aktivni  = stav.get("aktivni_mod", "DEFAULT")
    cena     = ceny["aktualni"] if ceny and ceny.get("aktualni") is not None else None

    # Záporná cena → zákaz přetoků
    if cena is not None and cena < 0 and aktivni != "BLOCKING_GRID_OVERFLOW":
        return "BLOCKING_GRID_OVERFLOW", f"Záporná cena {cena} Kč/kWh — blokuji přetoky"

    # Cena pod prahem výkupu a prodáváme → zastav
    if cena is not None and cena < VYKUP_PRAH_CZK and aktivni == "SELLING_INSTEAD_OF_BATTERY_CHARGE":
        return "DEFAULT", f"Cena {cena} Kč/kWh pod prahem výkupu {VYKUP_PRAH_CZK} — zastavuji prodej"

    return None

# ============================================================
# CLAUDE AI ROZHODOVÁNÍ
# ============================================================

def claude_rozhodne(stav: dict | None, pocasi: dict | None, ceny: dict | None, historie: list = [], nocni_analyza: dict | None = None, denni_analyza: dict | None = None) -> tuple[str, str]:
    print("🧠 Ptám se Claude AI...")
    cas = datetime.now(TZ).strftime("%H:%M")
    hodina = datetime.now(TZ).hour

    historie_text = formatovat_historii_pro_claude(historie)
    prompt = f"""Jsi AI agent řídící fotovoltaickou elektrárnu (FVE) s baterií v České republice.
Instalace: FVE 10 kWp, baterie 10 kWh využitelných, roční spotřeba domácnosti ~11 MWh.

## Aktuální čas: {cas} (hodina {hodina})

## Stav FVE a baterie
{formovat_stav_pro_prompt(stav) if stav else "Nedostupné"}

## Počasí
{json.dumps(pocasi, ensure_ascii=False, indent=2) if pocasi else "Nedostupné"}

## Spotové ceny elektřiny (Kč/kWh) — 15min intervaly
Aktuální cenová hladina: {ceny.get("aktualni_level", "?").upper() if ceny else "?"}
DŮLEŽITÉ: Hodiny označené "✓ proběhlo" jsou MINULOST — nelze je využít!
{formovat_ceny_pro_prompt(ceny, hodina) if ceny else "Nedostupné"}

## Historické profily spotřeby a výroby FVE
{vypocitat_profily_z_historie(historie)}

## Historie posledních rozhodnutí (učení z minulosti)
Formát: čas | baterie% | tok baterie | FVE výroba | cena[level] | zítra oblačnost/slunce → mód (důvod)
{historie_text}

## Analýza denního nabíjení (optimální čas pro nabití ze sítě)
{formovat_denni_analyzu(denni_analyza) if denni_analyza else "Mimo denní okno (6:00-21:00) — analýza není relevantní"}

## Analýza nočního nabíjení
{formovat_nocni_analyzu(nocni_analyza) if nocni_analyza else "Mimo noční okno (00:00-05:59) — analýza není relevantní"}

## Pevná pravidla
1. NIKDY nepoužívej mód SELLING_FROM_BATTERY — prodej z baterie je zakázán
2. Při záporné ceně VŽDY nastav BLOCKING_GRID_OVERFLOW
3. Opotřebení baterie ~0,6 Kč/kWh — zohledni při rozhodování
4. Vždy aktivní jen JEDEN mód (nebo DEFAULT)
5. Mysli dopředu — zvaž zbytek dne i zítřek
6. SAVING_TO_BATTERY (nabíjení ze sítě): Používej ve dvou situacích:

   A) DENNÍ nabíjení (6:00-21:00): Použij "Analýzu denního nabíjení" výše:
      - Pokud "Doporučená akce" = "NABÍJEJ TEĎ" → aktivuj SAVING_TO_BATTERY
      - Pokud "Doporučená akce" = "POČKEJ do HH:MM" → nastav DEFAULT, ještě není optimální čas
      - Pokud baterie > 90% → DEFAULT (není třeba nabíjet)
      - Pokud přebytek FVE (výroba - spotřeba) > 1500W → DEFAULT (baterie se nabíjí sama)
      PŘÍKLAD: baterie 21%, konec levného 14:00, potřeba 1.4h → zahaj v 12:36 → NABÍJEJ TEĎ pokud je po 12:36
      PŘÍKLAD: baterie 21%, konec levného 14:00, potřeba 1.4h → POČKEJ pokud je teprve 10:00

   B) NOČNÍ nabíjení (00:00-05:59): Použij analýzu nočního nabíjení výše:
      - Pokud "Doporučená akce" = "NABIJ TEĎ" → aktivuj SAVING_TO_BATTERY
      - Pokud "Doporučená akce" = "POČKEJ" → nastav DEFAULT, ještě není nejlevnější hodina
      - Pokud "Doporučená akce" = "NENABÍJEJ" → DEFAULT
      - Nabíjej dokud baterie nedosáhne: aktuální_SOC + doporučené_nabití_%
      - Jakmile baterie dosáhne cílového SOC → přepni na DEFAULT

## Dostupné módy (POUZE tyto 4 + DEFAULT)
- SELLING_INSTEAD_OF_BATTERY_CHARGE → výroba FVE do sítě místo nabíjení (dopoledne, vysoké ceny > {VYKUP_PRAH_CZK} Kč)
- USING_FROM_GRID_INSTEAD_OF_BATTERY → šetři baterii, ber ze sítě (cena sítě < opotřebení baterie)
- SAVING_TO_BATTERY                 → nabij baterii z levné sítě (nízké ceny + zítra zataženo/málo slunce)
- BLOCKING_GRID_OVERFLOW            → zákaz přetoků (záporné nebo velmi nízké ceny)
- DEFAULT                           → výchozí chování FVE (standardní situace)

Odpověz POUZE tímto JSON, bez dalšího textu:
{{"mod": "NÁZEV_MÓDU", "duvod": "Stručně česky max 120 znaků"}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        text = resp.json()["content"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        mod = parsed.get("mod", "DEFAULT")
        duvod = parsed.get("duvod", "—")
        if mod not in MODY:
            mod = "DEFAULT"
        print(f"   ✅ {mod} — {duvod}")
        return mod, duvod
    except Exception as e:
        print(f"   ❌ Chyba Claude: {e}")
        return "DEFAULT", "Chyba AI — výchozí mód"

# ============================================================
# DENNÍ PLÁN (14:30)
# ============================================================

def denni_plan(pocasi: dict | None, ceny: dict | None):
    print("📅 Sestavuji denní plán...")

    def fmt(hodiny):
        return ", ".join(f"{h}:00" for h in hodiny) if hodiny else "žádné"

    if not ceny:
        telegram("⚠️ <b>FVE Denní plán</b>\n\nNepodařilo se načíst ceny OTE.")
        return

    z = pocasi.get("zitrek", {}) if pocasi else {}
    zprava = (
        f"📅 <b>FVE Denní plán — {datetime.now(TZ).strftime('%d.%m.%Y')}</b>\n\n"
        f"💰 <b>Ceny elektřiny dnes:</b>\n"
        f"   Průměr: <b>{ceny['prumer']} Kč/kWh</b>\n"
        f"   Max: <b>{ceny['max']} Kč/kWh</b>  |  Min: <b>{ceny['min']} Kč/kWh</b>\n\n"
        f"🔴 Drahé hodiny (>{CENA_DRAHA_CZK} Kč): {fmt(ceny['hodiny_drahe'])}\n"
        f"🟢 Levné hodiny (<{CENA_LEVNA_CZK} Kč): {fmt(ceny['hodiny_levne'])}\n"
        f"⛔ Záporné ceny: {fmt(ceny['hodiny_zaporne'])}\n\n"
        f"🌤️ <b>Zítra:</b> oblačnost {z.get('oblacnost','?')}%, slunce {z.get('slunce_h','?')}h\n\n"
        f"🤖 Agent bude optimalizovat automaticky každou hodinu."
    )
    telegram(zprava)

# ============================================================
# NASTAVENÍ MÓDU NA PORTÁLU
# ============================================================

def nastavit_mod(session: requests.Session, mod: str) -> bool:
    print(f"🖱️ Nastavuji: {MODY.get(mod, mod)}")

    # Vypni všechny ostatní módy
    for typ in [m for m in MODY if m not in ("DEFAULT", mod)]:
        try:
            session.post(API_URL, json={"0": {"json": {"type": typ, "inverterId": INVERTER_ID, "state": "DISABLED"}}}, timeout=10)
        except:
            pass

    if mod == "DEFAULT":
        print("   ✅ Všechny módy vypnuty (DEFAULT)")
        return True

    try:
        resp = session.post(
            API_URL,
            json={"0": {"json": {"type": mod, "inverterId": INVERTER_ID, "state": "ENABLED"}}},
            timeout=15,
        )
        ok = resp.status_code == 200
        print(f"   {'✅ Nastaveno' if ok else f'❌ Chyba {resp.status_code}'}")
        return ok
    except Exception as e:
        print(f"   ❌ Výjimka: {e}")
        return False


# ============================================================
# FORMÁTOVÁNÍ CEN PRO CLAUDE PROMPT
# ============================================================

# ============================================================
# ANALÝZA DENNÍHO NABÍJENÍ
# ============================================================

def analyzovat_denni_nabijeni(ceny: dict, stav: dict | None, hodina: int) -> dict | None:
    """
    Analyzuje optimální čas pro denní nabíjení baterie ze sítě.
    Najde konec levného období dynamicky z průběhu cen.
    Vypočítá potřebný čas nabíjení podle aktuálního SOC.
    """
    if not stav or not ceny:
        return None
    if not (6 <= hodina <= 21):
        return None  # Pouze denní okno

    dnes = ceny.get("vsechny_15min", [])
    if not dnes:
        return None

    def hod_prumer(h):
        blok = dnes[h*4:(h+1)*4]
        return round(sum(blok)/len(blok), 3) if len(blok) == 4 else None

    # Budoucí hodinové ceny od aktuální hodiny
    budouci = {}
    for h in range(hodina, 24):
        c = hod_prumer(h)
        if c is not None:
            budouci[h] = c

    if len(budouci) < 3:
        return None

    # Najdi levné období — hodiny pod mediánem budoucích cen
    hodnoty = sorted(budouci.values())
    median = hodnoty[len(hodnoty) // 2]
    levne_hodiny = {h: c for h, c in budouci.items() if c <= median}

    if not levne_hodiny:
        return None

    # Průměr levného období
    prumer_levne = round(sum(levne_hodiny.values()) / len(levne_hodiny), 3)

    # Zlom = první hodina kde cena > průměr_levného × 2.0
    konec_levneho = max(levne_hodiny.keys())  # výchozí = poslední levná hodina
    for h in sorted(budouci.keys()):
        if budouci[h] > prumer_levne * 2.0 and h > hodina:
            # Najdi poslední levnou hodinu před tímto zlomem
            levne_pred_zlomem = [lh for lh in levne_hodiny if lh < h]
            if levne_pred_zlomem:
                konec_levneho = max(levne_pred_zlomem)
            break

    # Ověř ekonomickou výhodnost — spread mezi špičkou a levným obdobím
    spicka_cena = max(budouci.values()) if budouci else 0
    spread = round(spicka_cena - prumer_levne, 3)
    naklady_na_kwh = round(prumer_levne / BATERIE_UCINNOST + BATERIE_OPOTREBENI_CZK - BATERIE_DISTRIBUCE_BONUS, 3)
    ekonomicky_vyhodni = spread >= NOCNI_NABIJENI_MIN_SPREAD

    # Výpočet potřebného času nabíjení
    soc = stav.get("baterie_procent", 50)
    zbyvajici_kwh = round((100 - soc) / 100 * BATERIE_KAPACITA_KWH, 2)
    cas_nabijeni_h = round(zbyvajici_kwh / 5.5, 2)  # 5.5 kW výkon nabíjení

    # Doporučený čas zahájení
    zahajeni_h = konec_levneho - cas_nabijeni_h
    zahajeni_celych = int(zahajeni_h)
    zahajeni_min = int((zahajeni_h - zahajeni_celych) * 60)

    # Je TEĎ správný čas začít?
    # Porovnáváme v minutách pro přesnost (agent běží každých 10 min)
    now = datetime.now(TZ)
    aktualni_min = hodina * 60 + now.minute
    zahajeni_min_celkem = zahajeni_celych * 60 + zahajeni_min
    konec_min_celkem = konec_levneho * 60 + 59
    nabij_ted = aktualni_min >= zahajeni_min_celkem and aktualni_min <= konec_min_celkem

    # Cena v doporučenou hodinu
    cena_pri_zahajeni = budouci.get(zahajeni_celych, budouci.get(hodina))

    return {
        "prumer_levne_czk":     prumer_levne,
        "konec_levneho_h":      konec_levneho,
        "zlom_cena":            budouci.get(konec_levneho + 1),
        "spicka_cena":          spicka_cena,
        "spread_czk":           spread,
        "naklady_na_kwh":       naklady_na_kwh,
        "ekonomicky_vyhodni":   ekonomicky_vyhodni,
        "soc_pct":              soc,
        "zbyvajici_kwh":        zbyvajici_kwh,
        "cas_nabijeni_h":       cas_nabijeni_h,
        "zahajeni_h":           zahajeni_celych,
        "zahajeni_min":         zahajeni_min,
        "cena_pri_zahajeni":    cena_pri_zahajeni,
        "nabij_ted":            nabij_ted,
        "budouci_ceny":         {f"{h:02d}:00": v for h, v in sorted(budouci.items())},
    }

def formovat_denni_analyzu(a: dict) -> str:
    """Formátuje analýzu denního nabíjení pro Claude prompt."""
    if not a:
        return "Analýza nedostupná."

    if not a.get('ekonomicky_vyhodni'):
        akce = f"NENABÍJEJ — spread {a.get('spread_czk')} Kč je pod minimem {NOCNI_NABIJENI_MIN_SPREAD} Kč"
    elif a["nabij_ted"]:
        akce = "NABÍJEJ TEĎ"
    else:
        akce = f"POČKEJ do {a['zahajeni_h']:02d}:{a['zahajeni_min']:02d}"

    ekonomika = "VYPLATÍ SE" if a.get("ekonomicky_vyhodni") else f"NEVYPLATÍ SE (spread {a.get('spread_czk')} Kč < min {NOCNI_NABIJENI_MIN_SPREAD} Kč)"
    radky = [
        f"Průměr levného období: {a['prumer_levne_czk']} Kč/kWh",
        f"Večerní špička: {a.get('spicka_cena')} Kč/kWh | Spread: {a.get('spread_czk')} Kč/kWh",
        f"Skutečné náklady na 1 kWh z baterie: {a.get('naklady_na_kwh')} Kč (opotřebení+ztráty-distribuce)",
        f"Ekonomické hodnocení: {ekonomika}",
        f"Konec levného období: {a['konec_levneho_h']:02d}:00 (pak ceny rostou na ~{a['zlom_cena']} Kč)",
        f"",
        f"Baterie: {a['soc_pct']}% → zbývá nabít: {a['zbyvajici_kwh']} kWh",
        f"Čas nabíjení při 5.5kW: {a['cas_nabijeni_h']}h",
        f"Doporučené zahájení: {a['zahajeni_h']:02d}:{a['zahajeni_min']:02d} (cena ~{a['cena_pri_zahajeni']} Kč/kWh)",
        f"Doporučená akce: {akce}",
        f"",
        f"Budoucí ceny dnes:",
    ]
    for h, c in a["budouci_ceny"].items():
        marker = " ◀ NYNÍ" if h == f"{a.get('soc_pct', 0):02d}:00" else ""
        radky.append(f"  {h}: {c} Kč/kWh")

    return "\n".join(radky)


# ============================================================
# ANALÝZA NOČNÍHO NABÍJENÍ
# ============================================================

def analyzovat_nocni_nabijeni(ceny: dict, historie: list, hodina: int) -> dict | None:
    """
    Analyzuje zda se vyplatí nabíjet baterii z levné noční elektřiny.
    Volá se v nočních hodinách 00:00-05:59.
    Vrátí dict s analýzou nebo None pokud není relevantní.
    """
    if not (0 <= hodina <= 5):
        return None  # Mimo noční okno (00:00-05:59)

    dnes   = ceny.get("vsechny_15min", [])
    zitrek = ceny.get("zitrek_15min", [])

    def hod_prumer(data, h):
        blok = data[h*4:(h+1)*4]
        return round(sum(blok)/len(blok), 3) if len(blok) == 4 else None

    # --- Ranní špička zítra (6:00-9:00) ---
    ranni_ceny = [hod_prumer(zitrek, h) for h in range(6, 10) if hod_prumer(zitrek, h) is not None]
    if not ranni_ceny:
        return None
    ranni_spicka = round(sum(ranni_ceny) / len(ranni_ceny), 3)

    # --- Nejlevnější noční hodiny (00:00-05:59) ze zítřejších cen ---
    nocni_ceny = {}
    for h in range(0, 6):
        c = hod_prumer(zitrek, h)
        if c is not None:
            nocni_ceny[h] = c

    if not nocni_ceny:
        return None

    nejlevnejsi_hodina = min(nocni_ceny, key=nocni_ceny.get)
    nejlevnejsi_cena   = nocni_ceny[nejlevnejsi_hodina]

    # --- Skutečné náklady na 1 kWh z baterie (správný výpočet) ---
    # Nabití 1 kWh do baterie stojí: cena_nakupu / účinnost (ztráty konverze)
    # + opotřebení článků - bonus distribuce VT/NT
    naklady_na_kwh = round(nejlevnejsi_cena / BATERIE_UCINNOST + BATERIE_OPOTREBENI_CZK - BATERIE_DISTRIBUCE_BONUS, 3)
    uspora = round(ranni_spicka - naklady_na_kwh, 3)
    vyhodni = uspora >= 0 and (ranni_spicka - nejlevnejsi_cena) >= NOCNI_NABIJENI_MIN_SPREAD

    # --- Odhad potřebné energie z historie ---
    ranni_zaznamy = [z for z in historie
                     if z.get("vyroba_w") is not None
                     and z.get("spotreba_w") is not None
                     and 6 <= int(z.get("cas", "00:00").split(" ")[-1].split(":")[0]) <= 9]

    if ranni_zaznamy:
        avg_spotreba = round(sum(z["spotreba_w"] for z in ranni_zaznamy) / len(ranni_zaznamy))
        avg_vyroba   = round(sum(z["vyroba_w"]   for z in ranni_zaznamy) / len(ranni_zaznamy))
        deficit_w    = max(0, avg_spotreba - avg_vyroba)
        deficit_kwh  = round(deficit_w * 3 / 1000, 2)  # 3 hodiny ranní špičky
        cilovy_soc_prirustek = round(min(deficit_kwh / BATERIE_KAPACITA_KWH * 100, BATERIE_MAX_NOCNI_PCT), 1)
    else:
        avg_spotreba = None
        avg_vyroba   = None
        deficit_kwh  = None
        cilovy_soc_prirustek = 30  # výchozí odhad pokud není historie

    # Je TEĎ vhodný čas začít nabíjet?
    # Nabíjej pouze pokud aktuální cena je do 0.3 Kč/kWh od nejlevnější noční ceny
    aktualni_cena_nocni = nocni_ceny.get(hodina)
    if aktualni_cena_nocni is not None:
        rozdil_od_minima = round(aktualni_cena_nocni - nejlevnejsi_cena, 3)
        nabij_ted = vyhodni and rozdil_od_minima <= 0.3
    else:
        rozdil_od_minima = None
        nabij_ted = False

    return {
        "ranni_spicka_czk":      ranni_spicka,
        "nejlevnejsi_cena_czk":  nejlevnejsi_cena,
        "nejlevnejsi_hodina":    nejlevnejsi_hodina,
        "aktualni_nocni_cena":   aktualni_cena_nocni,
        "rozdil_od_minima_czk":  rozdil_od_minima,
        "nabij_ted":             nabij_ted,
        "uspora_czk":            uspora,
        "vyhodni":               vyhodni,
        "avg_ranni_spotreba_w":  avg_spotreba,
        "avg_ranni_vyroba_w":    avg_vyroba,
        "deficit_kwh":           deficit_kwh,
        "cilovy_soc_prirustek":  cilovy_soc_prirustek,
        "nocni_ceny_prehled":    {f"{h:02d}:00": v for h, v in sorted(nocni_ceny.items())},
    }


def formovat_nocni_analyzu(a: dict) -> str:
    """Formátuje analýzu nočního nabíjení pro Claude prompt."""
    if not a:
        return "Analýza nedostupná."
    vyhodni = "ANO — nabíjení se vyplatí" if a["vyhodni"] else "NE — nabíjení se nevyplatí"
    if a.get("nabij_ted"):
        cas_akce = f"NABIJ TEĎ (aktuální cena {a['aktualni_nocni_cena']} Kč, jen {a['rozdil_od_minima_czk']} Kč nad minimem)"
    elif a["vyhodni"]:
        cas_akce = f"POČKEJ na {a['nejlevnejsi_hodina']:02d}:00 (aktuální cena {a.get('aktualni_nocni_cena','?')} Kč, minimum až za {a['rozdil_od_minima_czk']} Kč)"
    else:
        cas_akce = "NENABÍJEJ — nevyplatí se"
    radky = [
        f"Ranní špička zítra (6-9h): {a['ranni_spicka_czk']} Kč/kWh",
        f"Nejlevnější noční hodina: {a['nejlevnejsi_hodina']:02d}:00 = {a['nejlevnejsi_cena_czk']} Kč/kWh",
        f"Aktuální noční cena: {a.get('aktualni_nocni_cena','?')} Kč/kWh",
        f"Skutečné náklady na 1 kWh z baterie: {round(a['nejlevnejsi_cena_czk']/BATERIE_UCINNOST + BATERIE_OPOTREBENI_CZK - BATERIE_DISTRIBUCE_BONUS, 2)} Kč",
        f"Spread (špička - levná): {round(a['ranni_spicka_czk'] - a['nejlevnejsi_cena_czk'], 2)} Kč/kWh (min. {NOCNI_NABIJENI_MIN_SPREAD} Kč)",
        f"Úspora: {a['uspora_czk']} Kč/kWh",
        f"Vyhodnocení: {vyhodni}",
        f"Doporučená akce: {cas_akce}",
        "",
        f"Průměrná ranní spotřeba (z historie): {a['avg_ranni_spotreba_w']} W",
        f"Průměrná ranní výroba FVE (z historie): {a['avg_ranni_vyroba_w']} W",
        f"Odhadovaný deficit energie: {a['deficit_kwh']} kWh",
        f"Doporučené nabití baterie: +{a['cilovy_soc_prirustek']}% oproti aktuálnímu stavu",
        "",
        "Noční ceny:",
    ]
    for h, c in a["nocni_ceny_prehled"].items():
        radky.append(f"  {h}: {c} Kč/kWh")
    return "\n".join(radky)


def formovat_stav_pro_prompt(stav: dict) -> str:
    """Přehledný souhrn stavu FVE včetně toku baterie."""
    bat_pct  = stav.get("baterie_procent", 0)
    bat_w    = stav.get("baterie_w", 0)
    vyroba   = stav.get("vyroba_w", 0)
    spotreba = stav.get("spotreba_w", 0)
    odber    = stav.get("odber_site_w", 0)
    mod      = stav.get("aktivni_mod", "DEFAULT")

    # Tok baterie
    if bat_w > 100:
        bat_tok = f"+{bat_w}W (nabíjí se)"
    elif bat_w < -100:
        bat_tok = f"{bat_w}W (vybíjí se)"
    else:
        bat_tok = f"{bat_w}W (v klidu)"

    # Tok sítě
    if odber > 50:
        sit_tok = f"+{odber}W (odběr ze sítě)"
    elif odber < -50:
        sit_tok = f"{odber}W (přetok do sítě)"
    else:
        sit_tok = f"{odber}W (vyrovnáno)"

    return (
        f"Baterie:      {bat_pct}% | tok: {bat_tok}\n"
        f"FVE výroba:   {vyroba}W\n"
        f"Spotřeba:     {spotreba}W\n"
        f"Síť:          {sit_tok}\n"
        f"Aktivní mód:  {mod}"
    )


def formovat_ceny_pro_prompt(ceny: dict, hodina: int) -> str:
    """Vytvoří přehledný souhrn cen pro Claude — dnes po hodinách + zítřek."""
    dnes   = ceny.get("vsechny_15min", [])
    zitrek = ceny.get("zitrek_15min", [])

    def hod_prumer(data, h):
        blok = data[h*4:(h+1)*4]
        return round(sum(blok)/len(blok), 2) if blok else None

    def uroven(c):
        if c is None: return "?"
        if c < 0:    return "⛔záporná"
        if c < 0.5:  return "🟢velmi levná"
        if c < 1.5:  return "🟢levná"
        if c < 2.5:  return "🟡střední"
        if c < 3.5:  return "🟠drahá"
        return "🔴velmi drahá"

    from datetime import timedelta
    now_tz = datetime.now(TZ)
    dnes_datum   = now_tz.strftime("%d.%m.%Y")
    zitrek_datum = (now_tz + timedelta(days=1)).strftime("%d.%m.%Y")

    radky = [f"Aktuální: {ceny.get('aktualni')} Kč/kWh | Min dnes: {ceny.get('min')} | Max dnes: {ceny.get('max')}",
             f"Průměr zítřka: {ceny.get('prumer_zitrek')} Kč/kWh",
             "",
             f"DNES {dnes_datum} (hodinové průměry):"]

    for h in range(24):
        c = hod_prumer(dnes, h)
        if h < hodina:
            marker = "✓ proběhlo"
        elif h == hodina:
            marker = "◀ NYNÍ"
        else:
            marker = ""
        radky.append(f"  {h:02d}:00  {f"{c:5.3f}" if c is not None else "  ???"} Kč  {uroven(c)} {marker}")

    radky += ["", f"ZÍTŘEK {zitrek_datum} (hodinové průměry):"]
    for h in range(24):
        c = hod_prumer(zitrek, h)
        radky.append(f"  {h:02d}:00  {f"{c:5.3f}" if c is not None else "  ???"} Kč  {uroven(c)}")

    return "\n".join(radky)


# ============================================================
# PAMĚŤ — HISTORIE ROZHODNUTÍ
# ============================================================

def nacist_aktualni_mod() -> str:
    """Načte naposledy nastavený mód z lokálního souboru."""
    try:
        if os.path.exists(MOD_SOUBOR):
            with open(MOD_SOUBOR, "r") as f:
                data = json.load(f)
            return data.get("mod", "DEFAULT")
    except:
        pass
    return "DEFAULT"

def ulozit_aktualni_mod(mod: str):
    """Uloží aktuálně nastavený mód do souboru."""
    try:
        with open(MOD_SOUBOR, "w") as f:
            json.dump({"mod": mod, "cas": datetime.now(TZ).strftime("%d.%m.%Y %H:%M")}, f)
        # Commitnout spolu s historií
        subprocess.run(["git", "add", MOD_SOUBOR], capture_output=True)
    except Exception as e:
        print(f"   ⚠️ Chyba uložení módu: {e}")


def nacist_historii() -> list:
    """Načte historii rozhodnutí z history.json."""
    try:
        if os.path.exists(HISTORIE_SOUBOR):
            with open(HISTORIE_SOUBOR, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"📚 Historie: načteno {len(data)} záznamů")
            return data
    except Exception as e:
        print(f"📚 Historie: chyba načítání — {e}")
    return []

def ulozit_zaznam(historie: list, zaznam: dict) -> list:
    """Přidá nový záznam do historie a ořízne na max počet."""
    historie.append(zaznam)
    # Ořízni na posledních 720 záznamů (30 dní × 24 hodin)
    if len(historie) > HISTORIE_MAX_ZAZNAMU:
        historie = historie[-HISTORIE_MAX_ZAZNAMU:]
    try:
        with open(HISTORIE_SOUBOR, "w", encoding="utf-8") as f:
            json.dump(historie, f, ensure_ascii=False, indent=2)
        print(f"📚 Historie: uloženo ({len(historie)} záznamů)")
    except Exception as e:
        print(f"📚 Historie: chyba uložení — {e}")
    return historie

def commitnout_historii():
    """Commitne history.json zpět do GitHub repozitáře."""
    try:
        subprocess.run(["git", "config", "user.email", "fve-agent@github-actions"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "FVE Agent"], check=True, capture_output=True)
        subprocess.run(["git", "add", HISTORIE_SOUBOR], check=True, capture_output=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        if result.returncode != 0:  # Jsou změny
            subprocess.run(["git", "commit", "-m", f"history: update {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"], check=True, capture_output=True)
            subprocess.run(["git", "push"], check=True, capture_output=True)
            print("📚 Historie: commitnuto do repozitáře")
        else:
            print("📚 Historie: žádné změny k commitnutí")
    except Exception as e:
        print(f"📚 Historie: chyba commitu — {e}")

def vypocitat_profily_z_historie(historie: list) -> str:
    """Vypočítá průměrné hodinové profily spotřeby a výroby FVE z historie."""
    if len(historie) < 24:
        return "Nedostatek dat pro profily (min. 24 záznamů)."

    from collections import defaultdict
    spotreba_hod  = defaultdict(list)
    vyroba_hod    = defaultdict(list)
    baterie_hod   = defaultdict(list)

    for z in historie:
        cas_str = z.get("cas", "")
        try:
            h = int(cas_str.split(" ")[-1].split(":")[0])
        except:
            continue
        if z.get("spotreba_w") is not None:
            spotreba_hod[h].append(z["spotreba_w"])
        if z.get("vyroba_w") is not None:
            vyroba_hod[h].append(z["vyroba_w"])
        if z.get("baterie_w") is not None:
            baterie_hod[h].append(z["baterie_w"])

    radky = ["Průměrný hodinový profil (z historie):"]
    radky.append(f"{'Hod':>4} | {'Spotřeba':>8} | {'FVE':>7} | {'Baterie':>8} | Bilance")
    radky.append("-" * 55)

    for h in range(24):
        avg_s = round(sum(spotreba_hod[h]) / len(spotreba_hod[h])) if spotreba_hod[h] else None
        avg_v = round(sum(vyroba_hod[h])   / len(vyroba_hod[h]))   if vyroba_hod[h]   else None
        avg_b = round(sum(baterie_hod[h])  / len(baterie_hod[h]))  if baterie_hod[h]  else None
        bilance = round(avg_v - avg_s) if avg_v is not None and avg_s is not None else None
        bil_str = f"{bilance:+d}W" if bilance is not None else "?"
        radky.append(
            f"  {h:02d}:00 | {str(avg_s)+'W':>8} | {str(avg_v)+'W':>7} | {str(avg_b)+'W':>8} | {bil_str}"
        )

    # Přidej denní průměry
    vse_spotreba = [v for vals in spotreba_hod.values() for v in vals]
    vse_vyroba   = [v for vals in vyroba_hod.values()   for v in vals]
    if vse_spotreba:
        radky.append(f"\nPrůměrná spotřeba: {round(sum(vse_spotreba)/len(vse_spotreba))}W")
    if vse_vyroba:
        radky.append(f"Průměrná výroba FVE: {round(sum(vse_vyroba)/len(vse_vyroba))}W")

    return "\n".join(radky)


def formatovat_historii_pro_claude(historie: list) -> str:
    """Formátuje posledních 48 hodinových záznamů pro Claude prompt."""
    if not historie:
        return "Žádná historie zatím není k dispozici."

    # Vezmi posledních 48 záznamů (2 dny)
    posledni = historie[-48:]
    radky = []
    for z in posledni:
        cas = z.get("cas", "?")
        mod = z.get("mod", "?")
        duvod = z.get("duvod", "?")
        bat = z.get("baterie_pct", "?")
        bat_w = z.get("baterie_w", 0) or 0
        cena = z.get("cena_czk", "?")
        level = z.get("cena_level", "?")
        vyroba = z.get("vyroba_w", "?")
        oblak_zitrek = z.get("oblacnost_zitrek_pct", "?")
        slunce_zitrek = z.get("slunce_zitrek_h", "?")
        if bat_w > 100:    bat_tok = f"+{bat_w}↑"
        elif bat_w < -100: bat_tok = f"{bat_w}↓"
        else:              bat_tok = "~0"
        radky.append(f"{cas} | bat:{bat}%[{bat_tok}W] | FVE:{vyroba}W | cena:{cena}Kč[{level}] | zítra:{oblak_zitrek}%ob/{slunce_zitrek}h☀ → {mod} ({duvod[:60]})")

    return "\n".join(radky)

# ============================================================
# MAIN
# ============================================================

def main():
    now    = datetime.now(TZ)
    cas    = now.strftime("%d.%m.%Y %H:%M")
    hodina = now.hour
    minuta = now.minute

    print("=" * 55)
    print(f"🌞 FVE Agent — {cas}")
    print("=" * 55)

    force_run    = os.environ.get("FORCE_RUN", "").lower() == "true"
    je_hodinovy  = (minuta < 5) or (30 <= minuta < 35) or force_run
    je_denni     = (hodina == 14 and minuta < 20)

    # Načíst historii
    historie = nacist_historii()

    # Sbírání dat
    pocasi = ziskat_pocasi()
    ceny   = ziskat_ceny()
    session = prihlasit_se()
    stav   = ziskat_stav_fve(session) if session else None

    # Noční analýza (relevantní jen 00:00-05:59)
    nocni_analyza = analyzovat_nocni_nabijeni(ceny, historie, hodina) if ceny else None
    if nocni_analyza:
        if nocni_analyza["vyhodni"]:
            print(f"   🌙 Noční nabíjení: VYHODNÉ | úspora {nocni_analyza['uspora_czk']} Kč/kWh | cíl +{nocni_analyza['cilovy_soc_prirustek']}%")
        else:
            print(f"   🌙 Noční nabíjení: NEVYHODNÉ | úspora {nocni_analyza['uspora_czk']} Kč/kWh")

    # Denní analýza (relevantní jen 6:00-21:00)
    denni_analyza = analyzovat_denni_nabijeni(ceny, stav, hodina) if ceny and stav else None
    if denni_analyza:
        akce = "NABÍJEJ TEĎ" if denni_analyza["nabij_ted"] else f"čekej do {denni_analyza['zahajeni_h']:02d}:{denni_analyza['zahajeni_min']:02d}"
        print(f"   ☀️ Denní nabíjení: konec levného {denni_analyza['konec_levneho_h']:02d}:00 | potřeba {denni_analyza['cas_nabijeni_h']}h | {akce}")

    # Denní plán — ochrana proti dvojitému odeslání
    if je_denni:
        dnes_str = datetime.now(TZ).strftime("%Y-%m-%d")
        posledni_plan_soubor = "last_daily_plan.json"
        posledni_plan = ""
        try:
            if os.path.exists(posledni_plan_soubor):
                posledni_plan = json.load(open(posledni_plan_soubor)).get("datum", "")
        except:
            pass
        if posledni_plan != dnes_str:
            denni_plan(pocasi, ceny)
            json.dump({"datum": dnes_str}, open(posledni_plan_soubor, "w"))
            subprocess.run(["git", "add", posledni_plan_soubor], capture_output=True)
        else:
            print("📅 Denní plán dnes již odeslán — přeskakuji")

    # Reaktivní kontrola — každých 5 minut
    reaktivni = reaktivni_kontrola(stav, ceny)

    if reaktivni:
        novy_mod, duvod = reaktivni
        print(f"\n⚡ REAKTIVNÍ ZÁSAH: {novy_mod} — {duvod}")
    elif je_hodinovy:
        novy_mod, duvod = claude_rozhodne(stav, pocasi, ceny, historie, nocni_analyza, denni_analyza)
    else:
        print("\nℹ️ Vše OK — žádná reaktivní změna, hodinový cyklus ještě nenastal")
        return

    if not session:
        telegram(f"⚠️ <b>FVE Agent — {cas}</b>\n\n❌ Nelze se přihlásit na portál!")
        return

    predchozi = nacist_aktualni_mod()
    uspech    = nastavit_mod(session, novy_mod)
    if uspech:
        ulozit_aktualni_mod(novy_mod)

    # Telegram — jen při změně
    if uspech and novy_mod != predchozi:
        bat     = f"{stav['baterie_procent']}%" if stav else "?"
        vyroba  = f"{stav['vyroba_w']} W"       if stav else "?"
        spotreba= f"{stav['spotreba_w']} W"      if stav else "?"
        odber   = f"{stav['odber_site_w']} W"    if stav else "?"
        cena_t  = f"{ceny['aktualni']} Kč/kWh"  if ceny and ceny.get("aktualni") else "?"

        zprava = (
            f"⚡ <b>FVE Agent — {cas}</b>\n\n"
            f"🔄 <b>Změna módu:</b>\n"
            f"   {MODY.get(predchozi, predchozi)}\n"
            f"   ↓\n"
            f"   <b>{MODY.get(novy_mod, novy_mod)}</b>\n\n"
            f"📊 <b>Aktuální stav:</b>\n"
            f"   🔋 Baterie: <b>{bat}</b>\n"
            f"   ☀️ Výroba FVE: <b>{vyroba}</b>\n"
            f"   🏠 Spotřeba: <b>{spotreba}</b>\n"
            f"   🔌 Odběr sítě: <b>{odber}</b>\n"
            f"   💰 Cena spot: <b>{cena_t}</b>\n\n"
            f"📋 <i>{duvod}</i>"
        )
        telegram(zprava)
    elif not uspech:
        telegram(
            f"❌ <b>FVE Agent — {cas}</b>\n\n"
            f"Nepodařilo se nastavit <b>{MODY.get(novy_mod, novy_mod)}</b>\n"
            f"Zkontrolujte portál nebo přihlašovací údaje."
        )
    else:
        print(f"ℹ️ Mód beze změny ({MODY.get(novy_mod, novy_mod)}) — bez notifikace")

    # Uložit záznam do historie (jen při hodinovém cyklu nebo reaktivním zásahu)
    if je_hodinovy or reaktivni:
        zaznam = {
            "cas":                  cas,
            "mod":                  novy_mod,
            "duvod":                duvod,
            "baterie_pct":          stav.get("baterie_procent") if stav else None,
            "vyroba_w":             stav.get("vyroba_w") if stav else None,
            "spotreba_w":           stav.get("spotreba_w") if stav else None,
            "odber_site_w":         stav.get("odber_site_w") if stav else None,
            "baterie_w":            stav.get("baterie_w") if stav else None,
            "cena_level":           ceny.get("aktualni_level") if ceny else None,
            "cena_czk":             ceny.get("aktualni") if ceny else None,
            "cena_min":             ceny.get("min") if ceny else None,
            "cena_max":             ceny.get("max") if ceny else None,
            "oblacnost_dnes_pct":   pocasi["dnes"]["oblacnost"] if pocasi else None,
            "slunce_dnes_h":        pocasi["dnes"]["slunce_h"] if pocasi else None,
            "oblacnost_zitrek_pct": pocasi["zitrek"]["oblacnost"] if pocasi else None,
            "slunce_zitrek_h":      pocasi["zitrek"]["slunce_h"] if pocasi else None,
            "uspech":               uspech,
        }
        historie = ulozit_zaznam(historie, zaznam)
        commitnout_historii()

    print("\n✅ Hotovo")


if __name__ == "__main__":
    main()
