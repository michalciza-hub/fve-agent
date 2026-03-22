"""
FVE Agent - proteus.deltagreen.cz
===================================
Jedna vrstva, 4 pravidla, bez AI, spousti se kazdych 10 minut.
Nocni report s Claude AI se posila jednou denne o pulnoci.

PRAVIDLA (priorita):
  1. BLOCKING_GRID_OVERFLOW  - cena < 0.45 Kc
  2. SAVING_TO_BATTERY nocni - 00:00-05:59, spread >= 1.8 Kc
  3. SAVING_TO_BATTERY denni - 06:00-21:00, spread >= 1.8 Kc
  4. USING_FROM_GRID         - cena < 1.67 Kc
  5. DEFAULT
"""

import os
import json
import subprocess
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Prague")

# ============================================================
# KONFIGURACE
# ============================================================
PORTAL_URL  = "https://proteus.deltagreen.cz"
API_URL     = "https://proteus.deltagreen.cz/api/trpc/inverters.controls.updateManualControl?batch=1"
INVERTER_ID = "tgqgq7sjswuw1renbowwddlf"

PORTAL_SESSION    = os.environ["PORTAL_SESSION"]
PORTAL_CSRF       = os.environ["PORTAL_CSRF"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

LATITUDE  = float(os.environ.get("FVE_LAT", "50.6270"))
LONGITUDE = float(os.environ.get("FVE_LON", "14.0754"))

PRETOK_PRAH_CZK        = 0.45   # Kc/kWh - pod timto zapni blocking
PRETOK_HYSTEREZE_CZK   = 0.65   # Kc/kWh - nad timto vypni blocking (hystereze proti kmitani)
SETRENI_PRAH_CZK       = 1.67
NABIJENI_MIN_SPREAD    = 1.8
BATERIE_KAPACITA_KWH   = 10.0
BATERIE_VYKON_NABIJENI = 5.5
BATERIE_MAX_SOC        = 90
NOCNI_MAX_SOC          = 80
NOCNI_TOLERANCE_CZK    = 0.3

HISTORIE_SOUBOR      = "history.json"
REPORT_SOUBOR        = "last_report.json"
MOD_SOUBOR           = "current_mode.json"
HISTORIE_MAX_ZAZNAMU = 30 * 24

MODY_LABEL = {
    "SAVING_TO_BATTERY":                  "🩵 Nabíjení baterie ze sítě",
    "USING_FROM_GRID_INSTEAD_OF_BATTERY": "🩵 Šetření energie v baterii",
    "BLOCKING_GRID_OVERFLOW":             "🔴 Zákaz přetoků",
    "DEFAULT":                            "⚪ Výchozí mód",
}


# ============================================================
# TELEGRAM
# ============================================================

def telegram(zprava: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": zprava,
                  "parse_mode": "HTML", "disable_notification": False},
            timeout=10,
        )
        print("Telegram: odeslano")
    except Exception as e:
        print(f"Telegram chyba: {e}")

# ============================================================
# PRIHLASENI
# ============================================================

def prihlasit_se():
    print("Prihlaseni (cookie)...")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Origin": PORTAL_URL,
        "Referer": f"{PORTAL_URL}/cs/household/hzvoejscpwua00vkzlw28vm0/overview",
        "x-proteus-csrf": PORTAL_CSRF,
        "trpc-accept": "application/jsonl",
    })
    session.cookies.set("proteus_session", PORTAL_SESSION, domain="proteus.deltagreen.cz")
    session.cookies.set("proteus_csrf", PORTAL_CSRF, domain="proteus.deltagreen.cz")
    try:
        resp = session.get(f"{PORTAL_URL}/api/trpc/households.getAll?batch=1",
                           params={"input": "[{}]"}, timeout=10)
        if resp.status_code == 200:
            print("   Cookie session OK")
        else:
            print(f"   Status {resp.status_code} - pokracuji")
    except Exception as e:
        print(f"   Overeni selhalo: {e}")
    return session

# ============================================================
# STAV FVE
# ============================================================

def ziskat_stav_fve(session):
    print("Nacteni stavu FVE...")
    try:
        resp = session.get(
            f"{PORTAL_URL}/api/trpc/inverters.lastState?batch=1",
            params={"input": json.dumps({"0": {"json": {"inverterId": INVERTER_ID}}})},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"   Status: {resp.status_code}")
            return None

        raw = resp.text
        data = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) > 0:
                data = parsed[0].get("result", {}).get("data", {}).get("json")
        except:
            pass
        if not data:
            for chunk in raw.strip().split("\n"):
                try:
                    obj = json.loads(chunk)
                    j = obj.get("json", [])
                    if isinstance(j, list) and len(j) >= 3 and j[0] == 2:
                        data = j[2][0][0]
                        break
                    if isinstance(j, dict) and "batteryStateOfCharge" in j:
                        data = j
                        break
                except:
                    continue
        if not data:
            print(f"   Nepodarilo se naparsovat. Raw: {raw[:200]}")
            return None

        def sf(val, default=0):
            try:
                return float(val) if val is not None else default
            except:
                return default

        stav = {
            "baterie_procent": sf(data.get("batteryStateOfCharge")),
            "vyroba_w":        sf(data.get("photovoltaicPower")),
            "spotreba_w":      sf(data.get("consumptionPower")),
            "odber_site_w":    sf(data.get("gridPower")),
            "baterie_w":       sf(data.get("batteryPower")),
        }
        pretok = max(0, -stav["odber_site_w"])
        odber  = max(0,  stav["odber_site_w"])
        bw = stav["baterie_w"]
        bi = f"+{bw:.0f}W nabiji" if bw > 100 else (f"{bw:.0f}W vybiji" if bw < -100 else "klid")
        print(f"   Baterie {stav['baterie_procent']:.0f}% [{bi}] | FVE {stav['vyroba_w']:.0f}W | Spotreba {stav['spotreba_w']:.0f}W | Odber {odber:.0f}W | Pretok {pretok:.0f}W")
        return stav
    except Exception as e:
        print(f"   Chyba: {e}")
        return None


# ============================================================
# CENY
# ============================================================

def ziskat_ceny():
    print("Nacteni cen spotovaelektrina.cz...")
    try:
        resp = requests.get("https://spotovaelektrina.cz/api/v1/price/get-prices-json-qh", timeout=10)
        data = resp.json()
        now  = datetime.now(TZ)
        h, m = now.hour, now.minute
        dnes   = [round(p["priceCZK"] / 1000, 3) for p in data["hoursToday"]]
        zitrek = [round(p["priceCZK"] / 1000, 3) for p in data["hoursTomorrow"]]
        idx    = h * 4 + m // 15
        aktualni       = dnes[idx] if idx < len(dnes) else dnes[-1]
        aktualni_level = data["hoursToday"][idx]["level"] if idx < len(data["hoursToday"]) else "unknown"
        print(f"   Aktualni: {aktualni} Kc/kWh [{aktualni_level}] | Min: {min(dnes):.3f} | Max: {max(dnes):.3f}")
        return {
            "aktualni":       aktualni,
            "aktualni_level": aktualni_level,
            "vsechny_15min":  dnes,
            "zitrek_15min":   zitrek,
        }
    except Exception as e:
        print(f"   Chyba: {e}")
        return None

# ============================================================
# POCASI
# ============================================================

def ziskat_pocasi():
    print("Nacteni pocasi...")
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": LATITUDE, "longitude": LONGITUDE,
                    "daily": "cloud_cover_mean,sunshine_duration,precipitation_sum",
                    "timezone": "Europe/Prague", "forecast_days": 2},
            timeout=10,
        )
        d = resp.json()["daily"]
        dnes   = {"oblacnost": d["cloud_cover_mean"][0], "slunce_h": round(d["sunshine_duration"][0] / 3600, 1)}
        zitrek = {"oblacnost": d["cloud_cover_mean"][1], "slunce_h": round(d["sunshine_duration"][1] / 3600, 1)}
        print(f"   Dnes: {dnes['oblacnost']}% oblak {dnes['slunce_h']}h | Zitra: {zitrek['oblacnost']}% oblak {zitrek['slunce_h']}h")
        return {"dnes": dnes, "zitrek": zitrek}
    except Exception as e:
        print(f"   Chyba: {e}")
        return None

# ============================================================
# PAMET
# ============================================================

def nacist_aktualni_mod_z_portalu(session) -> str:
    """Precte aktualni aktivni mod primo z portalu - zdroj pravdy."""
    try:
        input_data = json.dumps({
            "0": {"json": {"inverterId": INVERTER_ID}},
            "1": {"json": {"householdId": "hzvoejscpwua00vkzlw28vm0", "documentType": "FLEXIBILITY_CONTRACT"}}
        })
        resp = session.get(
            f"{PORTAL_URL}/api/trpc/inverters.controls.state,households.activeSignatureRequests?batch=1",
            params={"input": input_data},
            timeout=10,
        )
        if resp.status_code != 200:
            return nacist_aktualni_mod_ze_souboru()

        # Parsuj superjson stream - hledame chunk index 5 s manualControls
        raw = resp.text
        for chunk in raw.strip().split("\n"):
            try:
                obj = json.loads(chunk)
                j = obj.get("json", [])
                if isinstance(j, list) and len(j) >= 3 and j[0] == 5:
                    data = j[2][0][0]
                    manual_controls = data.get("manualControls", [])
                    for ctrl in manual_controls:
                        if ctrl.get("state") == "ENABLED":
                            mod = ctrl.get("type", "DEFAULT")
                            print(f"   Aktivni mod z portalu: {mod}")
                            return mod
                    print("   Aktivni mod z portalu: DEFAULT (vse DISABLED)")
                    return "DEFAULT"
            except:
                continue
        return nacist_aktualni_mod_ze_souboru()
    except Exception as e:
        print(f"   Chyba cteni modu z portalu: {e}")
        return nacist_aktualni_mod_ze_souboru()


def nacist_aktualni_mod_ze_souboru() -> str:
    """Fallback - nacte mod z lokalniho souboru."""
    try:
        if os.path.exists(MOD_SOUBOR):
            return json.load(open(MOD_SOUBOR)).get("mod", "DEFAULT")
    except:
        pass
    return "DEFAULT"


def nacist_aktualni_mod(session=None) -> str:
    """Nacte aktualni mod - preferuje portal, fallback na soubor."""
    if session:
        return nacist_aktualni_mod_z_portalu(session)
    return nacist_aktualni_mod_ze_souboru()

def ulozit_aktualni_mod(mod):
    try:
        json.dump({"mod": mod, "cas": datetime.now(TZ).strftime("%d.%m.%Y %H:%M")}, open(MOD_SOUBOR, "w"))
        subprocess.run(["git", "add", MOD_SOUBOR], capture_output=True)
    except Exception as e:
        print(f"   Chyba ulozeni modu: {e}")

def nacist_historii():
    try:
        if os.path.exists(HISTORIE_SOUBOR):
            data = json.load(open(HISTORIE_SOUBOR, encoding="utf-8"))
            print(f"Historie: nacteno {len(data)} zaznamu")
            return data
    except:
        pass
    return []

def ulozit_zaznam(historie, zaznam):
    historie.append(zaznam)
    if len(historie) > HISTORIE_MAX_ZAZNAMU:
        historie = historie[-HISTORIE_MAX_ZAZNAMU:]
    try:
        json.dump(historie, open(HISTORIE_SOUBOR, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"Historie: ulozeno ({len(historie)} zaznamu)")
    except Exception as e:
        print(f"Historie chyba: {e}")
    return historie

def commitnout_historii():
    try:
        subprocess.run(["git", "config", "user.email", "fve-agent@github-actions"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "FVE Agent"], check=True, capture_output=True)
        subprocess.run(["git", "add", HISTORIE_SOUBOR], check=True, capture_output=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"history: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"], check=True, capture_output=True)
            subprocess.run(["git", "push"], check=True, capture_output=True)
            print("Historie: commitnuto")
        else:
            print("Historie: bez zmen")
    except Exception as e:
        print(f"Historie chyba commitu: {e}")


# ============================================================
# ANALYZA NOCNIHO NABIJENI
# ============================================================

def analyzovat_nocni(ceny, stav, hodina, minuta):
    if not (0 <= hodina <= 5):
        return None
    dnes   = ceny.get("vsechny_15min", [])
    zitrek = ceny.get("zitrek_15min", [])

    def hp(data, h):
        blok = data[h*4:(h+1)*4]
        return round(sum(blok)/len(blok), 3) if len(blok) == 4 else None

    ranni = [hp(zitrek, h) for h in range(6, 10) if hp(zitrek, h) is not None]
    if not ranni:
        return None
    ranni_spicka = round(sum(ranni) / len(ranni), 3)

    nocni_ceny = {h: hp(dnes, h) for h in range(0, 6) if hp(dnes, h) is not None}
    if not nocni_ceny:
        return None

    nejlevnejsi_h    = min(nocni_ceny, key=nocni_ceny.get)
    nejlevnejsi_cena = nocni_ceny[nejlevnejsi_h]
    spread           = round(ranni_spicka - nejlevnejsi_cena, 3)
    vyhodni          = spread >= NABIJENI_MIN_SPREAD

    aktualni_cena = nocni_ceny.get(hodina)
    nabij_ted = vyhodni and aktualni_cena is not None and (aktualni_cena - nejlevnejsi_cena) <= NOCNI_TOLERANCE_CZK

    print(f"   Nocni: spicka {ranni_spicka} Kc, nejlevnejsi {nejlevnejsi_h:02d}:00={nejlevnejsi_cena} Kc, spread {spread} Kc, {'VYHODNI' if vyhodni else 'NEVYHODNI'}, {'NABIJEJ' if nabij_ted else 'cekej'}")

    return {
        "vyhodni":          vyhodni,
        "nabij_ted":        nabij_ted,
        "nejlevnejsi_h":    nejlevnejsi_h,
        "nejlevnejsi_cena": nejlevnejsi_cena,
        "ranni_spicka":     ranni_spicka,
        "spread":           spread,
        "cil_soc":          NOCNI_MAX_SOC,
    }

# ============================================================
# ANALYZA DENNÍHO NABIJENI
# ============================================================

def analyzovat_denni(ceny, stav, hodina, minuta):
    if not (6 <= hodina <= 21):
        return None
    dnes = ceny.get("vsechny_15min", [])

    def hp(h):
        blok = dnes[h*4:(h+1)*4]
        return round(sum(blok)/len(blok), 3) if len(blok) == 4 else None

    budouci = {h: hp(h) for h in range(hodina, 24) if hp(h) is not None}
    if len(budouci) < 3:
        return None

    levne = {h: c for h, c in budouci.items() if c < 1.5}
    if not levne:
        return None

    prumer_levne = round(sum(levne.values()) / len(levne), 3)
    spicka       = max(budouci.values())
    spread       = round(spicka - prumer_levne, 3)
    vyhodni      = spread >= NABIJENI_MIN_SPREAD
    if not vyhodni:
        return None

    konec_levneho = max(levne.keys())
    for h in sorted(budouci.keys()):
        if budouci[h] > prumer_levne * 2.0 and h > hodina:
            pred = [lh for lh in levne if lh < h]
            if pred:
                konec_levneho = max(pred)
            break

    soc            = stav.get("baterie_procent", 50) if stav else 50
    zbyvajici_kwh  = round(max(BATERIE_MAX_SOC - soc, 0) / 100 * BATERIE_KAPACITA_KWH, 2)
    cas_nabijeni_h = round(zbyvajici_kwh / BATERIE_VYKON_NABIJENI, 2)
    zahajeni_float = konec_levneho - cas_nabijeni_h
    zahajeni_hod   = int(zahajeni_float)
    zahajeni_min_v = int((zahajeni_float - zahajeni_hod) * 60)

    akt_min    = hodina * 60 + minuta
    zah_min    = zahajeni_hod * 60 + zahajeni_min_v
    konec_min  = konec_levneho * 60 + 59
    nabij_ted  = akt_min >= zah_min and akt_min <= konec_min

    print(f"   Denni: prumer levneho {prumer_levne} Kc, spicka {spicka} Kc, spread {spread} Kc, konec {konec_levneho:02d}:00, zahajeni {zahajeni_hod:02d}:{zahajeni_min_v:02d}, {'NABIJEJ' if nabij_ted else 'cekej'}")

    return {
        "vyhodni":       vyhodni,
        "nabij_ted":     nabij_ted,
        "prumer_levne":  prumer_levne,
        "spicka":        spicka,
        "spread":        spread,
        "konec_levneho": konec_levneho,
        "zahajeni_hod":  zahajeni_hod,
        "zahajeni_min":  zahajeni_min_v,
    }


# ============================================================
# ROZHODOVANI - 4 PRAVIDLA
# ============================================================

def rozhodnout(stav, ceny, pocasi, nocni, denni, predchozi, hodina):
    if not ceny:
        return "DEFAULT", "Ceny nedostupne"

    cena      = ceny.get("aktualni", 0)
    soc       = stav.get("baterie_procent", 50) if stav else 50
    oblacnost = pocasi["dnes"]["oblacnost"] if pocasi else 50
    prebytek  = (stav.get("vyroba_w", 0) - stav.get("spotreba_w", 0)) if stav else 0

    # PRAVIDLO 1: BLOCKING_GRID_OVERFLOW
    if cena < PRETOK_PRAH_CZK:
        return "BLOCKING_GRID_OVERFLOW", f"Cena {cena} Kc < prah {PRETOK_PRAH_CZK} Kc - blokuji pretoky"
    if predchozi == "BLOCKING_GRID_OVERFLOW" and cena >= PRETOK_HYSTEREZE_CZK:
        return "DEFAULT", f"Cena {cena} Kc nad prahem {PRETOK_HYSTEREZE_CZK} Kc - pretoky povoleny"

    # PRAVIDLO 2: NOCNI NABIJENI
    if nocni:
        cil = nocni["cil_soc"]
        if predchozi == "SAVING_TO_BATTERY":
            if soc >= cil:
                return "DEFAULT", f"Nocni nabijeni dokonceno - baterie {soc:.0f}% dosahla cile {cil}%"
            return "SAVING_TO_BATTERY", f"Nocni nabijeni pokracuje - baterie {soc:.0f}% -> cil {cil}%"
        if nocni["nabij_ted"]:
            return "SAVING_TO_BATTERY", f"Nocni nabijeni: {nocni['nejlevnejsi_cena']} Kc/kWh, spread {nocni['spread']} Kc, cil {cil}%"
        if nocni["vyhodni"]:
            return "DEFAULT", f"Cekam na {nocni['nejlevnejsi_h']:02d}:00 ({nocni['nejlevnejsi_cena']} Kc) - ted jeste neni nejlevneji"
        return "DEFAULT", f"Nocni nabijeni nevyhodne - spread {nocni['spread']} Kc < {NABIJENI_MIN_SPREAD} Kc"

    # PRAVIDLO 3: DENNI NABIJENI
    if denni:
        if predchozi == "SAVING_TO_BATTERY":
            if cena > denni["prumer_levne"] * 2.0:
                return "DEFAULT", f"Levne obdobi skoncilo - cena {cena} Kc, ukoncuji nabijeni"
            if hodina > denni["konec_levneho"]:
                return "DEFAULT", f"Cas levneho obdobi vyprsel ({denni['konec_levneho']:02d}:00) - ukoncuji nabijeni"
            if soc >= BATERIE_MAX_SOC:
                return "DEFAULT", f"Baterie {soc:.0f}% - nabijeni dokonceno"
            return "SAVING_TO_BATTERY", f"Denni nabijeni pokracuje - baterie {soc:.0f}% -> cil {BATERIE_MAX_SOC}%"
        if denni["nabij_ted"]:
            if soc >= BATERIE_MAX_SOC:
                return "DEFAULT", f"Baterie {soc:.0f}% - dostatecne nabita"
            if oblacnost >= 70 or prebytek < 500:
                return "SAVING_TO_BATTERY", f"Denni nabijeni: {denni['prumer_levne']} Kc/kWh, spread {denni['spread']} Kc, oblacnost {oblacnost}%, konec {denni['konec_levneho']:02d}:00"
        else:
            return "DEFAULT", f"Cekam na optimalni cas nabijeni {denni['zahajeni_hod']:02d}:{denni['zahajeni_min']:02d}"

    # PRAVIDLO 4: SETRENI BATERIE
    if cena < SETRENI_PRAH_CZK:
        return "USING_FROM_GRID_INSTEAD_OF_BATTERY", f"Cena {cena} Kc < opotrebeni baterie {SETRENI_PRAH_CZK} Kc - setrim baterii"
    if predchozi == "USING_FROM_GRID_INSTEAD_OF_BATTERY" and cena >= SETRENI_PRAH_CZK:
        return "DEFAULT", f"Cena {cena} Kc nad prahem {SETRENI_PRAH_CZK} Kc - ukoncuji setreni baterie"

    return "DEFAULT", "Standardni provoz"

# ============================================================
# NASTAVENI MODU
# ============================================================

def nastavit_mod(session, mod):
    print(f"Nastavuji: {MODY_LABEL.get(mod, mod)}")
    vsechny_mody = ["SAVING_TO_BATTERY", "USING_FROM_GRID_INSTEAD_OF_BATTERY",
                    "BLOCKING_GRID_OVERFLOW", "SELLING_INSTEAD_OF_BATTERY_CHARGE"]
    for typ in [m for m in vsechny_mody if m != mod]:
        try:
            session.post(API_URL, json={"0": {"json": {"type": typ, "inverterId": INVERTER_ID, "state": "DISABLED"}}}, timeout=10)
        except:
            pass
    if mod == "DEFAULT":
        print("   Vsechny mody vypnuty (DEFAULT)")
        return True
    try:
        resp = session.post(API_URL, json={"0": {"json": {"type": mod, "inverterId": INVERTER_ID, "state": "ENABLED"}}}, timeout=15)
        ok = resp.status_code == 200
        print(f"   {'Nastaveno OK' if ok else f'Chyba {resp.status_code}'}")
        return ok
    except Exception as e:
        print(f"   Vyjimka: {e}")
        return False

# ============================================================
# DENNI PLAN
# ============================================================

def denni_plan(ceny, pocasi):
    print("Denni plan...")
    if not ceny:
        telegram("FVE Denni plan: nepodařilo se načíst ceny.")
        return
    dnes = ceny.get("vsechny_15min", [])
    def hp(h):
        blok = dnes[h*4:(h+1)*4]
        return round(sum(blok)/len(blok), 3) if len(blok) == 4 else None
    levne  = [h for h in range(24) if (hp(h) or 999) < 1.5]
    drahe  = [h for h in range(24) if (hp(h) or 0) > 3.5]
    zaporne = [h for h in range(24) if (hp(h) or 0) < 0]
    def fmt(hh): return ", ".join(f"{h}:00" for h in hh) if hh else "zadne"
    z = pocasi.get("zitrek", {}) if pocasi else {}
    zprava = (
        f"FVE Denni plan {datetime.now(TZ).strftime('%d.%m.%Y')}\n\n"
        f"Ceny dnes: min {min(dnes):.3f} Kc | max {max(dnes):.3f} Kc | prumer {round(sum(dnes)/len(dnes),3)} Kc\n\n"
        f"Levne hodiny (<1.5 Kc): {fmt(levne)}\n"
        f"Drahe hodiny (>3.5 Kc): {fmt(drahe)}\n"
        f"Zaporne ceny: {fmt(zaporne)}\n\n"
        f"Zitra: {z.get('oblacnost','?')}% oblacnost, {z.get('slunce_h','?')}h slunce"
    )
    telegram(zprava)



# ============================================================
# NOCNI REPORT (00:00) - souhrn vcerejska + plan na dnes
# ============================================================

def claude_dotaz(prompt):
    if not ANTHROPIC_API_KEY:
        return None
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
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"   Claude chyba: {e}")
        return None


def nocni_report(historie, ceny, pocasi):
    print("Sestavuji nocni report...")
    now       = datetime.now(TZ)
    vcera     = (now - timedelta(days=1)).strftime("%d.%m.%Y")
    dnes      = now.strftime("%d.%m.%Y")
    vcera_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # --- ZPRAVA 1: Souhrn vcerejska ---
    zaznamy = [z for z in historie if z.get("cas", "").startswith(vcera_str)]
    if zaznamy:
        radky = []
        for z in zaznamy:
            cas_z    = z.get("cas", "?").split(" ")[-1]
            mod_z    = MODY_LABEL.get(z.get("mod", "DEFAULT"), z.get("mod", "DEFAULT"))
            cena_z   = z.get("cena_czk", "?")
            bat_z    = z.get("baterie_pct", "?")
            vyroba_z = z.get("vyroba_w", "?")
            duvod_z  = z.get("duvod", "")
            radky.append(f"{cas_z} | {mod_z} | bat:{bat_z}% | FVE:{vyroba_z}W | {cena_z} Kc | {duvod_z}")
        oblacnost = zaznamy[0].get("oblacnost_dnes_pct", "?")
        slunce    = zaznamy[0].get("slunce_dnes_h", "?")
        log_text  = "\n".join(radky)

        prompt1 = (
            "Jsi asistent ktery sleduje fotovoltaickou elektrarnu s baterii.\n"
            "Napiste kratke lidske shrnuti 3-5 vet cesky co agent FVE delal dne " + vcera + ".\n"
            "Vysvetli proc se rozhodoval tak jak se rozhodoval ve vztahu k cenam a pocasi.\n"
            "But konkretni - zmín kdy nabil, kdy setril baterii, kdy blokoval pretoky a proc.\n\n"
            "Pocasi: oblacnost " + str(oblacnost) + "%, slunce " + str(slunce) + "h\n\n"
            "Zaznamy:\n" + log_text + "\n\n"
            "Detailni logika agenta:\n"
            "\n"
            "DETAILNI LOGIKA AGENTA (4 pravidla, vyhodnocuji se v tomto poradi):\n"
            "\n"
            "PRAVIDLO 1 - BLOCKING_GRID_OVERFLOW (zakaz pretoku do site):\n"
            "  ZAPNUTI: cena < 0.45 Kc/kWh (pod vykupnim prahem dodavatele - pretok prodelava)\n"
            "  VYPNUTI: cena >= 0.45 Kc -> DEFAULT\n"
            "  Poznamka: FVE si sama poresi co dela s prebytkem (omezi vyroba nebo nabiji baterii)\n"
            "\n"
            "PRAVIDLO 2 - SAVING_TO_BATTERY nocni (00:00-05:59):\n"
            "  ZAPNUTI: spread = (prumer ranni spicky 06:00-09:00) - (nejlevnejsi nocni hodina) >= 1.8 Kc\n"
            "           AND aktualni cena je nejlevnejsi nebo max 0.3 Kc nad minimem noci\n"
            "           AND baterie < 80%\n"
            "  POKRACOVANI: baterie < 80% -> pokracuj v nabijeni az do 80%\n"
            "  VYPNUTI: baterie >= 80% -> DEFAULT\n"
            "  Poznamka: ceka na nejlevnejsi hodinu, nenabiji driv\n"
            "\n"
            "PRAVIDLO 3 - SAVING_TO_BATTERY denni (06:00-21:00):\n"
            "  ZAPNUTI: spread = (vecerni spicka) - (prumer levnych hodin pod 1.5 Kc) >= 1.8 Kc\n"
            "           AND aktualni cas >= (konec levneho obdobi - potrebny cas nabijeni)\n"
            "           AND baterie < 90%\n"
            "           AND (oblacnost >= 70% NEBO prebytek FVE < 500W)\n"
            "           Dulezite: pokud sviti slunce (oblacnost < 70%) a FVE vyrabi prebytek > 500W,\n"
            "           baterie se nabiji z FVE zadarmo - sitove nabijeni se NEAKTIVUJE\n"
            "  POKRACOVANI: cena < prumer_levneho * 2.0 AND baterie < 90% -> pokracuj\n"
            "  VYPNUTI: cena prekrocila levne obdobi NEBO baterie >= 90% -> DEFAULT\n"
            "  Cas zahajeni: konec_levneho - (zbyvajici_kWh / 5.5kW vykon nabijeni)\n"
            "\n"
            "PRAVIDLO 4 - USING_FROM_GRID_INSTEAD_OF_BATTERY (setreni baterie):\n"
            "  ZAPNUTI: cena < 1.67 Kc/kWh (levnejsi nez opotrebeni baterie)\n"
            "  VYPNUTI: cena >= 1.67 Kc -> DEFAULT\n"
            "  Dulezite: mód drzi dokud cena nevystoupi, nezavisi na stavu baterie ani vyrobe FVE\n"
            "\n"
            "DEFAULT - vychozi rezim:\n"
            "  FVE nabiji baterii z vlastni vyroba, baterie pokryva spotrebu,\n"
            "  prebytky jdou do site za aktualni vykupni cenu\n"
            "\n\n"
            "Napiste pouze text shrnuti bez nadpisu."
        )
        shrnuti = claude_dotaz(prompt1)
        zprava1 = ("FVE Report " + vcera + "\n\n" + shrnuti) if shrnuti else ("FVE Report " + vcera + " - nelze sestavit shrnuti.")
    else:
        zprava1 = "FVE Report " + vcera + " - zadne zaznamy."
    telegram(zprava1)

    # --- ZPRAVA 2: Plan na dnes ---
    if not ceny or not pocasi:
        telegram("FVE Plan " + dnes + " - chybi data.")
        return

    dnes_data = ceny.get("vsechny_15min", [])

    def hp(h):
        blok = dnes_data[h*4:(h+1)*4]
        return round(sum(blok)/len(blok), 3) if len(blok) == 4 else None

    radky_cen = []
    for h in range(24):
        c = hp(h)
        if c is not None:
            if c < 0:       u = "zaporna"
            elif c < 0.45:  u = "pod vykupem"
            elif c < 1.5:   u = "levna"
            elif c < 1.67:  u = "pod opotrebenim"
            elif c < 3.5:   u = "stredni"
            else:            u = "draha spicka"
            radky_cen.append(f"  {h:02d}:00 = {c} Kc ({u})")

    stav_dummy = {"baterie_procent": 50}
    nocni_a = analyzovat_nocni(ceny, stav_dummy, 1, 0)
    denni_a = analyzovat_denni(ceny, stav_dummy, 10, 0)

    nocni_info = ""
    if nocni_a:
        nocni_info = (
            "Nocni nabijeni: nejlevnejsi " + str(nocni_a["nejlevnejsi_h"]) + ":00"
            " (" + str(nocni_a["nejlevnejsi_cena"]) + " Kc),"
            " ranni spicka " + str(nocni_a["ranni_spicka"]) + " Kc,"
            " spread " + str(nocni_a["spread"]) + " Kc"
            " - " + ("vyhodne" if nocni_a["vyhodni"] else "nevyhodne")
        )
    denni_info = ""
    if denni_a:
        denni_info = (
            "Denni nabijeni: prumer levneho " + str(denni_a["prumer_levne"]) + " Kc,"
            " vecerni spicka " + str(denni_a["spicka"]) + " Kc,"
            " spread " + str(denni_a["spread"]) + " Kc,"
            " konec levneho " + str(denni_a["konec_levneho"]) + ":00"
        )

    prompt2 = (
        "Jsi asistent ktery planuje provoz fotovoltaicke elektrarny s baterii.\n"
        "Napiste kratky lidsky plan 4-6 vet cesky co agent FVE bude delat dne " + dnes + ".\n"
        "Vysvetli kdy se ocekava nabijeni ze site, setreni baterie, blokovani pretoku.\n"
        "Zmín konkretni hodiny a ceny. But prakticky.\n\n"
        "Pocasi " + dnes + ": oblacnost " + str(pocasi["dnes"]["oblacnost"]) + "%, slunce " + str(pocasi["dnes"]["slunce_h"]) + "h\n"
        "Pocasi zitra: oblacnost " + str(pocasi["zitrek"]["oblacnost"]) + "%, slunce " + str(pocasi["zitrek"]["slunce_h"]) + "h\n\n"
        + nocni_info + "\n" + denni_info + "\n\n"
        "Hodinove ceny:\n" + "\n".join(radky_cen) + "\n\n"
        "Detailni logika agenta:\n"
        "\n"
        "DETAILNI LOGIKA AGENTA (4 pravidla, vyhodnocuji se v tomto poradi):\n"
        "\n"
        "PRAVIDLO 1 - BLOCKING_GRID_OVERFLOW (zakaz pretoku do site):\n"
        "  ZAPNUTI: cena < 0.45 Kc/kWh (pod vykupnim prahem dodavatele - pretok prodelava)\n"
        "  VYPNUTI: cena >= 0.45 Kc -> DEFAULT\n"
        "  Poznamka: FVE si sama poresi co dela s prebytkem (omezi vyroba nebo nabiji baterii)\n"
        "\n"
        "PRAVIDLO 2 - SAVING_TO_BATTERY nocni (00:00-05:59):\n"
        "  ZAPNUTI: spread = (prumer ranni spicky 06:00-09:00) - (nejlevnejsi nocni hodina) >= 1.8 Kc\n"
        "           AND aktualni cena je nejlevnejsi nebo max 0.3 Kc nad minimem noci\n"
        "           AND baterie < 80%\n"
        "  POKRACOVANI: baterie < 80% -> pokracuj v nabijeni az do 80%\n"
        "  VYPNUTI: baterie >= 80% -> DEFAULT\n"
        "  Poznamka: ceka na nejlevnejsi hodinu, nenabiji driv\n"
        "\n"
        "PRAVIDLO 3 - SAVING_TO_BATTERY denni (06:00-21:00):\n"
        "  ZAPNUTI: spread = (vecerni spicka) - (prumer levnych hodin pod 1.5 Kc) >= 1.8 Kc\n"
        "           AND aktualni cas >= (konec levneho obdobi - potrebny cas nabijeni)\n"
        "           AND baterie < 90%\n"
        "           AND (oblacnost >= 70% NEBO prebytek FVE < 500W)\n"
        "           Dulezite: pokud sviti slunce (oblacnost < 70%) a FVE vyrabi prebytek > 500W,\n"
        "           baterie se nabiji z FVE zadarmo - sitove nabijeni se NEAKTIVUJE\n"
        "  POKRACOVANI: cena < prumer_levneho * 2.0 AND baterie < 90% -> pokracuj\n"
        "  VYPNUTI: cena prekrocila levne obdobi NEBO baterie >= 90% -> DEFAULT\n"
        "  Cas zahajeni: konec_levneho - (zbyvajici_kWh / 5.5kW vykon nabijeni)\n"
        "\n"
        "PRAVIDLO 4 - USING_FROM_GRID_INSTEAD_OF_BATTERY (setreni baterie):\n"
        "  ZAPNUTI: cena < 1.67 Kc/kWh (levnejsi nez opotrebeni baterie)\n"
        "  VYPNUTI: cena >= 1.67 Kc -> DEFAULT\n"
        "  Dulezite: mód drzi dokud cena nevystoupi, nezavisi na stavu baterie ani vyrobe FVE\n"
        "\n"
        "DEFAULT - vychozi rezim:\n"
        "  FVE nabiji baterii z vlastni vyroba, baterie pokryva spotrebu,\n"
        "  prebytky jdou do site za aktualni vykupni cenu\n"
        "\n\n"
        "Napiste pouze text planu bez nadpisu."
    )
    plan = claude_dotaz(prompt2)
    zprava2 = ("FVE Plan " + dnes + "\n\n" + plan) if plan else ("FVE Plan " + dnes + " - nelze sestavit plan.")
    telegram(zprava2)


# ============================================================
# MAIN
# ============================================================

def main():
    now    = datetime.now(TZ)
    cas    = now.strftime("%d.%m.%Y %H:%M")
    hodina = now.hour
    minuta = now.minute

    print("=" * 55)
    print(f"FVE Agent {cas}")
    print("=" * 55)

    historie = nacist_historii()
    pocasi   = ziskat_pocasi()
    ceny     = ziskat_ceny()
    session  = prihlasit_se()
    stav     = ziskat_stav_fve(session) if session else None

    nocni = analyzovat_nocni(ceny, stav or {}, hodina, minuta) if ceny else None
    denni = analyzovat_denni(ceny, stav or {}, hodina, minuta) if ceny else None

    # Nocni report jednou denne o 00:00-00:09
    if hodina == 0 and minuta < 10:
        dnes_str = now.strftime("%Y-%m-%d")
        posledni_report = ""
        try:
            if os.path.exists(REPORT_SOUBOR):
                posledni_report = json.load(open(REPORT_SOUBOR)).get("datum", "")
        except:
            pass
        if posledni_report != dnes_str:
            nocni_report(historie, ceny, pocasi)
            json.dump({"datum": dnes_str}, open(REPORT_SOUBOR, "w"))
            subprocess.run(["git", "add", REPORT_SOUBOR], capture_output=True)
        else:
            print("Nocni report dnes jiz odeslan")

    # Denni plan jednou denne ve 14:00-14:20
    if hodina == 14 and minuta < 20:
        dnes_str = now.strftime("%Y-%m-%d")
        plan_soubor = "last_daily_plan.json"
        posledni = ""
        try:
            if os.path.exists(plan_soubor):
                posledni = json.load(open(plan_soubor)).get("datum", "")
        except:
            pass
        if posledni != dnes_str:
            denni_plan(ceny, pocasi)
            json.dump({"datum": dnes_str}, open(plan_soubor, "w"))
            subprocess.run(["git", "add", plan_soubor], capture_output=True)

    predchozi = nacist_aktualni_mod(session)
    novy_mod, duvod = rozhodnout(stav, ceny, pocasi, nocni, denni, predchozi, hodina)
    print(f"\nRozhodnuti: {MODY_LABEL.get(novy_mod, novy_mod)} - {duvod}")

    if not session:
        telegram(f"FVE Agent {cas}: nelze se prihlasit na portal!")
        return

    uspech = nastavit_mod(session, novy_mod)
    if uspech:
        ulozit_aktualni_mod(novy_mod)

    if uspech and novy_mod != predchozi:
        bat      = f"{stav['baterie_procent']:.0f}%" if stav else "?"
        vyroba   = f"{stav['vyroba_w']:.0f} W"       if stav else "?"
        spotreba = f"{stav['spotreba_w']:.0f} W"      if stav else "?"
        odber    = f"{stav['odber_site_w']:.0f} W"    if stav else "?"
        cena_t   = f"{ceny['aktualni']} Kc/kWh"       if ceny else "?"
        zprava = (
            f"FVE Agent {cas}\n\n"
            f"Zmena modu:\n"
            f"  {MODY_LABEL.get(predchozi, predchozi)}\n"
            f"  ->\n"
            f"  {MODY_LABEL.get(novy_mod, novy_mod)}\n\n"
            f"Stav:\n"
            f"  Baterie: {bat}\n"
            f"  Vyroba FVE: {vyroba}\n"
            f"  Spotreba: {spotreba}\n"
            f"  Odber site: {odber}\n"
            f"  Cena spot: {cena_t}\n\n"
            f"{duvod}"
        )
        telegram(zprava)
    elif not uspech:
        telegram(f"FVE Agent {cas}: nepodarilo se nastavit {MODY_LABEL.get(novy_mod, novy_mod)}")
    else:
        print(f"Mod beze zmeny ({MODY_LABEL.get(novy_mod, novy_mod)}) - bez notifikace")

    zaznam = {
        "cas":                  cas,
        "mod":                  novy_mod,
        "duvod":                duvod,
        "baterie_pct":          stav.get("baterie_procent") if stav else None,
        "baterie_w":            stav.get("baterie_w") if stav else None,
        "vyroba_w":             stav.get("vyroba_w") if stav else None,
        "spotreba_w":           stav.get("spotreba_w") if stav else None,
        "odber_site_w":         stav.get("odber_site_w") if stav else None,
        "cena_czk":             ceny.get("aktualni") if ceny else None,
        "cena_level":           ceny.get("aktualni_level") if ceny else None,
        "oblacnost_dnes_pct":   pocasi["dnes"]["oblacnost"] if pocasi else None,
        "slunce_dnes_h":        pocasi["dnes"]["slunce_h"] if pocasi else None,
        "oblacnost_zitrek_pct": pocasi["zitrek"]["oblacnost"] if pocasi else None,
        "slunce_zitrek_h":      pocasi["zitrek"]["slunce_h"] if pocasi else None,
    }
    historie = ulozit_zaznam(historie, zaznam)
    commitnout_historii()
    print("\nHotovo")


if __name__ == "__main__":
    main()
