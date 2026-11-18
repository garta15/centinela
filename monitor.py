#!/usr/bin/env python3
"""
CENTINELA FAMILIAR
Monitor de riesgos de gran escala que pueden cortar las comunicaciones.

Lee fuentes abiertas -> calcula nivel de riesgo -> publica:
  docs/pulso.json    (nivel + hora; lo lee el Atajo y los motores de respaldo)
  docs/estado.enc    (todo el detalle, CIFRADO si existe FAMILY_KEY)
  docs/status.json   (solo si NO hay FAMILY_KEY)
  docs/feed.xml      (RSS)
  docs/alerta.txt    (texto plano, respaldo mínimo)
  docs/index.html    (panel)
y envía notificación push (ntfy) y Telegram (opcional). Todo gratis.
cuando el nivel alcanza el umbral configurado.

Uso:  python monitor.py            (ejecución normal)
      python monitor.py --dry-run  (no envía notificaciones)
      python monitor.py --test-alert (envía una alerta de prueba)
"""
import os, sys, json, math, time, hashlib, html, re, csv, io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import requests, yaml

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")
STATE_FILE = os.path.join(BASE, "state.json")
UA = {"User-Agent": "CentinelaFamiliar/1.0 (uso privado no comercial)"}
TIMEOUT = 20

NIVELES = ["NORMAL", "VIGILANCIA", "ELEVADO", "ALTO", "CRÍTICO"]
EMOJI = ["🟢", "🔵", "🟡", "🟠", "🔴"]
CFG = yaml.safe_load(open(os.path.join(BASE, "config.yaml"), encoding="utf-8"))
# Datos sensibles: vienen de Secrets/.env, nunca del repositorio público.
#   ZONAS_FAMILIA = [{"nombre":"Base","lat":18.2,"lon":-66.5,"radio_km":600}, ...]
#   PAISES_FAMILIA = PR,US
if os.getenv("ZONAS_FAMILIA"):
    CFG["zonas_familia"] = json.loads(os.getenv("ZONAS_FAMILIA"))
if os.getenv("PAISES_FAMILIA"):
    CFG["paises_familia"] = [c.strip().upper() for c in os.getenv("PAISES_FAMILIA").split(",") if c.strip()]
CLAVE = os.getenv("FAMILY_KEY", "")   # si existe, todo lo público se publica CIFRADO
NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------ util
def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r

def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))

def zona_cercana(lat, lon):
    for z in CFG.get("zonas_familia", []):
        d = km(lat, lon, z["lat"], z["lon"])
        if d <= z["radio_km"]:
            return z["nombre"], round(d)
    return None, None

def senal(fuente, categoria, nivel, titulo, detalle="", lat=None, lon=None,
          url="", clave=None, alcance="global", seccion="infra"):
    nivel = max(0, min(4, int(nivel)))
    return {
        "id": hashlib.sha1((clave or f"{fuente}|{titulo}").encode()).hexdigest()[:12],
        "fuente": fuente, "categoria": categoria, "nivel": nivel,
        "nivel_txt": NIVELES[nivel], "titulo": titulo, "detalle": detalle,
        "lat": lat, "lon": lon, "url": url, "alcance": alcance, "seccion": seccion,
        "hora_utc": NOW.isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ fuentes
# Enfoque: PUERTO RICO + ESTADOS UNIDOS + amenazas a NORTEAMÉRICA +
# actividad MILITAR en el CARIBE. Lo global solo entra si afecta aquí.
TITULARES = []        # titulares en vivo para el panel (tipo RSS)
AVIONES = []          # aeronaves militares con transpondedor sobre el Caribe
ESTADO_REF = {}       # estado persistente (lo llena main) para líneas base

CARIBE = (8.0, 28.0, -90.0, -58.0)          # lat_min, lat_max, lon_min, lon_max
PR_CAJA = (16.5, 20.5, -68.5, -64.0)        # PR + Islas Vírgenes + Pasaje de la Mona

def en_caja(lat, lon, c):
    return lat is not None and lon is not None and c[0] <= lat <= c[1] and c[2] <= lon <= c[3]

def nws(params):
    h = dict(UA); h["Accept"] = "application/geo+json"
    r = requests.get("https://api.weather.gov/alerts/active", params=params, headers=h, timeout=TIMEOUT)
    r.raise_for_status(); return r.json().get("features", [])

def f_nws_pr():
    """Alertas OFICIALES del Servicio Nacional de Meteorología para PR e Islas Vírgenes."""
    out = []
    mapa = {"Extreme": 3, "Severe": 2, "Moderate": 1}
    criticos = ("Hurricane Warning", "Tsunami Warning", "Civil Danger", "Civil Emergency",
                "Nuclear", "Radiological", "Shelter In Place", "Evacuation", "Extreme Wind")
    vistos = set()
    for area in ("PR", "VI"):
        for f in nws({"area": area}):
            p = f["properties"]
            if p["event"] in vistos: continue
            vistos.add(p["event"])
            n = mapa.get(p.get("severity"), 0)
            if any(c in p["event"] for c in criticos): n = max(n, 3)
            if "Tsunami Warning" in p["event"] or "Hurricane Warning" in p["event"]: n = 4
            if n == 0: continue
            out.append(senal("NWS (oficial)", "Alerta oficial", n, f"{p['event']} — {area}",
                             (p.get("headline") or "")[:300], url="https://www.weather.gov/sju/",
                             clave=f"nws-{p['id']}", alcance="familia", seccion="pr"))
    return out

def f_nws_nacional():
    """Alertas EXTREMAS en todo EE. UU. y alertas civiles/nucleares (cualquier severidad)."""
    out = []
    civiles = ("Civil Danger Warning", "Civil Emergency Message", "Nuclear Power Plant Warning",
               "Radiological Hazard Warning", "Shelter In Place Warning", "Evacuation Immediate",
               "Law Enforcement Warning", "Hazardous Materials Warning")
    extremas = nws({"severity": "Extreme"})
    civ = [f for f in nws({"event": ",".join(civiles)})] if True else []
    for f in civ:
        p = f["properties"]
        out.append(senal("NWS (oficial)", "Alerta civil EE. UU.", 3, f"{p['event']}: {p.get('areaDesc','')[:80]}",
                         (p.get("headline") or "")[:300], url="https://alerts.weather.gov/",
                         clave=f"nwsciv-{p['id']}", seccion="eeuu"))
    eventos = {}
    for f in extremas:
        p = f["properties"]; eventos.setdefault(p["event"], []).append(p.get("areaDesc", ""))
    graves = ("Hurricane", "Tsunami", "Extreme Wind", "Storm Surge", "Tornado Emergency")
    for ev, areas in eventos.items():
        n = 1 if len(areas) < 5 else 2
        if any(g in ev for g in graves): n = 3 if len(areas) >= 3 else 2
        out.append(senal("NWS (oficial)", "Alerta extrema EE. UU.", n, f"{ev} — {len(areas)} zona(s) en EE. UU.",
                         "; ".join(areas)[:300], url="https://alerts.weather.gov/",
                         clave=f"nwsext-{ev}-{NOW:%Y%m%d}", seccion="eeuu"))
    return out

def f_huracanes():
    """Centro Nacional de Huracanes (Atlántico): sistemas activos y menciones de PR."""
    out = []
    x = ET.fromstring(get("https://www.nhc.noaa.gov/index-at.xml").content)
    for it in x.iter("item"):
        t = it.findtext("title", ""); d = it.findtext("description", "") or ""
        if not any(k in t for k in ("Advisory", "Summary for", "Watch", "Warning")): continue
        pr = "Puerto Rico" in d or "Puerto Rico" in t or "Virgin Islands" in d
        n = 3 if pr else 1
        if pr and ("Hurricane Warning" in d or "Hurricane Warning" in t): n = 4
        out.append(senal("NHC", "Huracán", n, t[:140], ("Menciona a Puerto Rico. " if pr else "") +
                         re.sub("<[^>]+>", " ", d)[:250], url=it.findtext("link", ""),
                         clave=f"nhc-{t}", alcance="familia" if pr else "global", seccion="pr" if pr else "infra"))
    return out

def f_tsunami():
    """Centro Nacional de Alerta de Tsunamis (cubre PR e Islas Vírgenes)."""
    out = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    x = ET.fromstring(get("https://www.tsunami.gov/events/xml/PAAQAtom.xml").content)
    for e in x.findall("a:entry", ns):
        t = e.findtext("a:title", "", ns); s = (e.findtext("a:summary", "", ns) or "")
        upd = e.findtext("a:updated", "", ns)
        try:
            if (NOW - datetime.fromisoformat(upd.replace("Z", "+00:00"))).total_seconds() > 86400: continue
        except ValueError:
            pass
        txt = (t + " " + s)
        n = 4 if "Warning" in txt else 3 if "Advisory" in txt else 2 if "Watch" in txt else 0
        if not n: continue
        cerca = any(k in txt for k in ("Puerto Rico", "Virgin Islands", "Caribbean"))
        out.append(senal("NTWC Tsunami", "Tsunami", n if cerca else max(1, n - 2), t[:140],
                         re.sub("<[^>]+>", " ", s)[:250], url="https://www.tsunami.gov/",
                         clave=f"tsu-{t}-{upd}", alcance="familia" if cerca else "global",
                         seccion="pr" if cerca else "eeuu"))
    return out

def f_sismos():
    """USGS: sismos M4+ en la zona de PR (24 h) y M6+ en EE. UU. continental."""
    out = []
    ini = (NOW - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    q = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    pr = get(q, params={"format": "geojson", "starttime": ini, "minmagnitude": 4,
                        "minlatitude": PR_CAJA[0], "maxlatitude": PR_CAJA[1],
                        "minlongitude": PR_CAJA[2], "maxlongitude": PR_CAJA[3]}).json()["features"]
    us = get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson").json()["features"]
    us = [f for f in us if (f["properties"].get("mag") or 0) >= 6 and
          en_caja(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0], (18, 72, -170, -60))]
    for f, sec in [(f, "pr") for f in pr] + [(f, "eeuu") for f in us]:
        p = f["properties"]; lon, lat = f["geometry"]["coordinates"][:2]; m = p.get("mag") or 0
        n = 1 if m < 5.5 else 2 if m < 6.5 else 3 if m < 7.3 else 4
        if p.get("tsunami"): n = min(4, n + 1)
        out.append(senal("USGS", "Sismo", n, f"Sismo M{m:.1f} — {p.get('place')}",
                         "Bandera de tsunami USGS." if p.get("tsunami") else f"Profundidad {f['geometry']['coordinates'][2]:.0f} km",
                         lat, lon, p.get("url", ""), clave=f["id"], alcance="familia", seccion=sec))
    return out

def f_clima_espacial():
    """NOAA SWPC: tormentas que degradan GPS, satélites, radio HF y red eléctrica en EE. UU."""
    out = []
    d = get("https://services.swpc.noaa.gov/products/noaa-scales.json").json()
    act = d["0"]
    g = int(act["G"]["Scale"] or 0); r = int(act["R"]["Scale"] or 0)
    m = {3: 2, 4: 3, 5: 4}
    if g >= 3:
        out.append(senal("NOAA SWPC", "Clima espacial", m[g], f"Tormenta geomagnética G{g} ({act['G']['Text']})",
                         "Puede degradar GPS, satélites, radio y red eléctrica.", url="https://www.swpc.noaa.gov/",
                         clave=f"swpc-G{g}-{act['DateStamp']}", seccion="infra"))
    if r >= 3:
        out.append(senal("NOAA SWPC", "Clima espacial", m[r], f"Apagón de radio R{r} ({act['R']['Text']})",
                         "Afecta radio HF y navegación en el lado diurno.", url="https://www.swpc.noaa.gov/",
                         clave=f"swpc-R{r}-{act['DateStamp']}", seccion="infra"))
    for k in ("1", "2", "3"):
        gf = d.get(k, {}).get("G", {}).get("Scale")
        if gf and int(gf) >= 4:
            out.append(senal("NOAA SWPC", "Clima espacial", 2, f"Pronóstico: tormenta G{gf} el {d[k]['DateStamp']}",
                             url="https://www.swpc.noaa.gov/", clave=f"swpc-f{gf}-{d[k]['DateStamp']}", seccion="infra"))
    return out

def f_ioda():
    """IODA: caídas de internet a nivel país SOLO para PR, EE. UU. e Islas Vírgenes."""
    out = []
    until = int(time.time()); frm = until - 3 * 3600
    fam = set(CFG.get("paises_familia") or ["PR", "US"]) | {"PR", "US", "VI"}
    d = get("https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts",
            params={"from": frm, "until": until, "entityType": "country", "limit": 500}).json()
    peor = {}
    for a in d.get("data") or []:
        e = a["entity"]
        if e["code"] not in fam or a.get("level") not in ("critical", "warning"): continue
        hv = a.get("historyValue") or 0; v = a.get("value") or 0
        caida = (1 - v / hv) * 100 if hv else 0
        if caida > peor.get(e["code"], (0,))[0]:
            peor[e["code"]] = (caida, e["name"], a.get("datasource"))
    for code, (caida, nombre, src) in peor.items():
        if caida < 3: continue
        n = 1 if caida < 8 else 2 if caida < 20 else 3 if caida < 50 else 4
        out.append(senal("IODA", "Internet", n, f"Caída de internet en {nombre}: -{caida:.0f}% ({src})",
                         "Medido por IODA (Georgia Tech) en las últimas 3 h.",
                         url=f"https://ioda.inetintel.cc.gatech.edu/country/{code}",
                         clave=f"ioda-{code}-{NOW:%Y%m%d%H}", alcance="familia",
                         seccion="pr" if code in ("PR", "VI") else "eeuu"))
    return out

def f_apple():
    """iMessage, FaceTime, Find My: los canales que usa la familia."""
    out = []
    t = get("https://www.apple.com/support/systemstatus/data/system_status_en_US.js").text
    d = json.loads(t[t.find("{"): t.rfind("}") + 1])
    crit = set(CFG.get("apple_criticos", ["iMessage", "FaceTime", "Find My"]))
    for s in d.get("services", []):
        if s["serviceName"] not in crit: continue
        for ev in s.get("events", []):
            if ev.get("eventStatus") != "ongoing": continue
            n = 3 if ev.get("statusType") == "Outage" else 2
            out.append(senal("Apple", "Canal familiar", n, f"{s['serviceName']}: {ev.get('statusType')}",
                             ev.get("message", "")[:300], url="https://www.apple.com/support/systemstatus/",
                             clave=f"apple-{ev.get('messageId')}", alcance="familia", seccion="eeuu"))
    return out

def f_pizzint():
    """PizzINT: índice de actividad nocturna cerca del Pentágono (señal blanda, no oficial)."""
    out = []
    d = get("https://www.pizzint.watch/api/dashboard-data").json()
    lvl = int(d.get("defcon_level") or 5)
    mapa = {5: 0, 4: 1, 3: 2, 2: 2, 1: 3}
    det = (d.get("defcon_details") or {}).get("reason", "")
    n = mapa.get(lvl, 0)
    if d.get("has_active_spikes"): n = max(n, 1)
    if n:
        out.append(senal("PizzINT", "Indicador Pentágono", n, f"PizzINT nivel {lvl} — {d.get('active_spikes', 0)} pico(s) activos",
                         f"Señal no oficial; solo cuenta junto a otras. {det}", url="https://www.pizzint.watch/",
                         clave=f"pizz-{lvl}-{NOW:%Y%m%d%H}", seccion="amenazas"))
    return out

# Aeronaves de interés (códigos ICAO de tipo)
TIPOS_CLAVE = {"E6": "E-6B Mercury (mando nuclear)", "E3TF": "E-3 AWACS", "E3CF": "E-3 AWACS",
               "R135": "RC-135 (reconocimiento)", "P8": "P-8 Poseidon (patrulla marítima)",
               "K35R": "KC-135 (reabastecimiento)", "KC46": "KC-46 (reabastecimiento)",
               "E2": "E-2 Hawkeye (portaaviones)", "C17": "C-17 (transporte)", "C5M": "C-5 (transporte)",
               "B52": "B-52", "B1": "B-1", "F35": "F-35", "FA18": "F/A-18 (portaaviones)",
               "MQ9": "MQ-9 Reaper", "Q4": "RQ-4 Global Hawk", "E8": "E-8 JSTARS", "V22": "V-22 Osprey"}

def f_militar_caribe():
    """ADS-B (adsb.lol): aeronaves militares con transpondedor sobre el Caribe vs. su promedio."""
    out = []
    d = get("https://api.adsb.lol/v2/mil").json()
    car = [a for a in d.get("ac", []) if en_caja(a.get("lat"), a.get("lon"), CARIBE)]
    for a in car:
        AVIONES.append({"callsign": (a.get("flight") or "").strip() or a.get("r") or a.get("hex"),
                        "tipo": a.get("t") or "?", "desc": TIPOS_CLAVE.get(a.get("t") or "", ""),
                        "lat": round(a["lat"], 2), "lon": round(a["lon"], 2),
                        "alt": a.get("alt_baro"), "cerca_pr": en_caja(a["lat"], a["lon"], PR_CAJA)})
    hist = ESTADO_REF.setdefault("mil_caribe", [])
    hist.append(len(car)); del hist[:-96]                 # ~24 h de corridas cada 15 min
    base = sorted(hist[:-1])[len(hist[:-1]) // 2] if len(hist) > 8 else None
    clave_t = [a for a in AVIONES if a["desc"]]
    det = ", ".join(sorted({f"{a['desc']}" for a in clave_t}))[:250]
    n = 0
    if base is not None and len(car) >= 6 and len(car) >= 2.5 * max(base, 1): n = 2
    elif len(car) >= 4: n = 1
    if any(a["tipo"] in ("E6", "E3TF", "E3CF", "R135", "B52", "B1") for a in AVIONES): n = max(n, 1)
    if n or car:
        out.append(senal("ADS-B (adsb.lol)", "Militar Caribe", n,
                         f"{len(car)} aeronave(s) militar(es) visibles sobre el Caribe" +
                         (f" (promedio {base})" if base is not None else ""),
                         det or "Solo aeronaves con transpondedor encendido; muchas vuelan sin él.",
                         url="https://adsb.lol/?mil", clave=f"mil-{NOW:%Y%m%d%H}", seccion="militar"))
    return out

def noticias(consultas, horas, seccion, categoria, peso_max=2, umbral=2):
    """Google News RSS. Solo cuenta artículos de las últimas `horas`. Guarda titulares."""
    out = []
    for c in consultas:
        q, idioma, etiqueta = c[:3]
        sec = c[3] if len(c) > 3 else seccion
        es = idioma == "es"
        try:
            x = get("https://news.google.com/rss/search",
                    params={"q": f"{q} when:1d", "hl": "es-419" if es else "en-US", "gl": "US",
                            "ceid": "US:es-419" if es else "US:en"}).content
        except Exception as e:
            print("  ! noticias", q, e); continue
        recientes = []
        for it in ET.fromstring(x).iter("item"):
            try:
                t = datetime.strptime(it.findtext("pubDate", ""), "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (NOW - t).total_seconds() <= horas * 3600:
                recientes.append((t, it.findtext("title", ""), it.findtext("link", "")))
        recientes.sort(reverse=True)
        for t, tit, link in recientes[:4]:
            TITULARES.append({"hora_utc": t.isoformat(timespec="minutes"), "titulo": tit[:200],
                              "url": link, "seccion": sec, "tema": etiqueta})
        if len(recientes) >= umbral:
            n = 1 if len(recientes) < umbral * 3 else min(peso_max, 2)
            out.append(senal("Prensa (sin verificar)", categoria, n,
                             f"{etiqueta}: {len(recientes)} noticia(s) en {horas} h",
                             recientes[0][1][:200], url=recientes[0][2],
                             clave=f"news-{q}-{NOW:%Y%m%d}", seccion=sec))
        time.sleep(0.7)
    return out

def f_noticias_comunicaciones():
    return noticias([
        ('Puerto Rico ("sin servicio" OR avería OR interrupción) (Claro OR Liberty OR "T-Mobile" OR AT&T OR internet OR celular)', "es", "Averías telecom PR"),
        ('Puerto Rico (apagón OR "relevo de carga") LUMA', "es", "Apagones PR (LUMA)"),
        ('("AT&T outage" OR "Verizon outage" OR "T-Mobile outage")', "en", "Caídas de celulares EE. UU.", "eeuu"),
        ('("911 outage" OR "nationwide cell outage" OR "internet outage" United States)', "en", "Caídas 911 / internet EE. UU.", "eeuu"),
        ('(cyberattack OR hack) (telecom OR "power grid") "United States"', "en", "Ciberataques a telecom/red eléctrica", "eeuu"),
        ('("undersea cable" OR "submarine cable") (Caribbean OR "Puerto Rico" OR "United States") damaged', "en", "Cables submarinos"),
    ], horas=6, seccion="pr", categoria="Comunicaciones (prensa)")

def f_noticias_amenazas():
    return noticias([
        ('(ICBM OR "missile launch" OR "missile test") ("United States" OR "North America" OR NORAD)', "en", "Misiles / ICBM"),
        ('NORAD (intercept OR alert OR scramble)', "en", "NORAD"),
        ('("homeland" OR "United States") ("nuclear threat" OR "imminent attack" OR "terror alert")', "en", "Amenazas a EE. UU."),
        ('("Emergency Alert System" OR "Presidential Alert" OR "national emergency") United States', "en", "Alertas nacionales"),
    ], horas=6, seccion="amenazas", categoria="Amenaza Norteamérica (prensa)")

def f_noticias_militar():
    return noticias([
        ('(Navy OR warship OR "aircraft carrier" OR destroyer OR "amphibious") (Caribbean OR "Puerto Rico")', "en", "Movimiento naval Caribe"),
        ('(Venezuela OR Cuba) (military OR warships OR navy OR strike) Caribbean', "en", "Venezuela / Cuba"),
        ('("Roosevelt Roads" OR "Fuerte Buchanan" OR "Guardia Nacional") Puerto Rico militares', "es", "Militares en PR"),
        ('(Russian OR Chinese) (warships OR submarine OR navy) (Cuba OR Caribbean OR Venezuela)', "en", "Rusia / China en el Caribe"),
    ], horas=12, seccion="militar", categoria="Militar Caribe (prensa)", umbral=3)

def f_gpsjam():
    """GPSJam: interferencia GPS solo en el Caribe / cerca de PR."""
    out = []
    import h3
    dia = get("https://gpsjam.org/data/manifest.csv").text.strip().splitlines()[-1].split(",")[0]
    malos = []
    for r in csv.DictReader(io.StringIO(get(f"https://gpsjam.org/data/{dia}-h3_4.csv").text)):
        g = int(r["count_good_aircraft"]); b = int(r["count_bad_aircraft"])
        if g + b >= 5 and b / (g + b) > 0.10:
            lat, lon = h3.cell_to_latlng(r["hex"])
            if en_caja(lat, lon, CARIBE): malos.append((lat, lon))
    if malos:
        cerca = [m for m in malos if en_caja(m[0], m[1], PR_CAJA)]
        out.append(senal("GPSJam", "GPS/GNSS", 2 if cerca else 1,
                         f"Interferencia GPS en {len(malos)} zona(s) del Caribe ({dia})" + (" — incluye PR" if cerca else ""),
                         "Puede indicar guerra electrónica o interferencia.", url=f"https://gpsjam.org/?date={dia}",
                         clave=f"gpsjam-car-{dia}", seccion="militar"))
    return out

FUENTES = [("Alertas NWS Puerto Rico", f_nws_pr), ("Alertas NWS EE. UU.", f_nws_nacional),
           ("Huracanes (NHC)", f_huracanes), ("Tsunami (NTWC)", f_tsunami), ("Sismos (USGS)", f_sismos),
           ("Internet PR/EE. UU. (IODA)", f_ioda), ("Apple iMessage/FaceTime", f_apple),
           ("Clima espacial (NOAA)", f_clima_espacial), ("PizzINT", f_pizzint),
           ("Militar Caribe (ADS-B)", f_militar_caribe), ("GPS Caribe (GPSJam)", f_gpsjam),
           ("Prensa: comunicaciones", f_noticias_comunicaciones), ("Prensa: amenazas", f_noticias_amenazas),
           ("Prensa: militar Caribe", f_noticias_militar)]


# ------------------------------------------------------------------ evaluación
def evaluar(senales):
    nivel = max([s["nivel"] for s in senales], default=0)
    # Convergencia: señales elevadas de CATEGORÍAS distintas (no 3 noticias iguales)
    # La prensa cuenta como UNA sola categoría y nunca basta por sí sola.
    ofic = {s["categoria"] for s in senales if s["nivel"] >= 2 and not s["fuente"].startswith("Prensa")}
    prensa = any(s["nivel"] >= 2 and s["fuente"].startswith("Prensa") for s in senales)
    cats = ofic | ({"Prensa"} if prensa else set())
    conv = CFG.get("convergencia_senales", 3)
    motivo = ""
    if len(cats) >= conv and len(ofic) >= 2 and nivel < 3:
        nivel = 3
        motivo = f"Convergencia: señales elevadas en {len(cats)} categorías ({', '.join(sorted(cats))})."
    top = sorted(senales, key=lambda s: (-s["nivel"], s["fuente"]))
    if nivel == 0:
        resumen = "Sin riesgos relevantes para Puerto Rico y EE. UU. en este momento."
    else:
        resumen = "; ".join(s["titulo"] for s in top if s["nivel"] >= max(1, nivel - 1))[:500]
        if motivo:
            resumen = motivo + " " + resumen
    return nivel, resumen, top


# ------------------------------------------------------------------ cifrado
def cifrar(obj, clave):
    """AES-256-GCM con clave derivada por PBKDF2-SHA256 (compatible con WebCrypto del navegador)."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    it = 250_000
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(hashes.SHA256(), 32, salt, it).derive(clave.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "alg": "PBKDF2-SHA256/AES-256-GCM", "iter": it,
            "salt": b(salt), "iv": b(iv), "ct": b(ct)}


# ------------------------------------------------------------------ salidas
def escribir_salidas(nivel, resumen, senales, estado_fuentes, historial):
    os.makedirs(DOCS, exist_ok=True)
    url = CFG.get("url_panel", "")
    status = {
        "sistema": CFG["nombre_sistema"], "actualizado_utc": NOW.isoformat(timespec="seconds"),
        "nivel": nivel, "nivel_txt": NIVELES[nivel], "emoji": EMOJI[nivel],
        "resumen": resumen, "mensaje_corto": f"{EMOJI[nivel]} {NIVELES[nivel]}: {resumen}"[:300],
        "senales": senales, "fuentes": estado_fuentes, "panel": url,
        "motor": os.getenv("MOTOR", "github"),
        "titulares": sorted(TITULARES, key=lambda t: t["hora_utc"], reverse=True)[:40],
        "aviones_militares": AVIONES[:60],
    }
    pulso = {"actualizado_utc": status["actualizado_utc"], "nivel": nivel, "motor": status["motor"]}
    json.dump(pulso, open(os.path.join(DOCS, "pulso.json"), "w"), indent=1)

    if CLAVE:
        # ---- MODO CIFRADO: lo público no revela nada personal ----
        status["historial"] = historial[:40]
        status["consulta_manual"] = CFG.get("consulta_manual", [])
        json.dump(cifrar(status, CLAVE), open(os.path.join(DOCS, "estado.enc"), "w"))
        for viejo in ("status.json", "alerta.txt"):
            if os.path.exists(os.path.join(DOCS, viejo)):
                os.remove(os.path.join(DOCS, viejo))
        items = "".join(
            f"""<item><title>🔒 {EMOJI[h['nivel']]} Nueva alerta (nivel {h['nivel']}) — abre el panel</title>
<link>{html.escape(url)}</link><guid isPermaLink="false">{h['id']}-{h['hora_utc']}</guid>
<pubDate>{format_datetime(datetime.fromisoformat(h['hora_utc']))}</pubDate>
<description>Contenido cifrado. Ábrelo en el panel familiar con la clave.</description></item>"""
            for h in historial[:40])
        rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>{CFG['nombre_sistema']} — {EMOJI[nivel]} nivel {nivel}</title>
<link>{url}</link><description>Contenido cifrado</description>
<lastBuildDate>{format_datetime(NOW)}</lastBuildDate><ttl>15</ttl>
{items}
</channel></rss>"""
        open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8").write(rss)
        tpl = open(os.path.join(BASE, "panel_cifrado.html"), encoding="utf-8").read()
        open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(
            tpl.replace("{{NOMBRE}}", html.escape(CFG["nombre_sistema"])))
        return

    json.dump(status, open(os.path.join(DOCS, "status.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    open(os.path.join(DOCS, "alerta.txt"), "w", encoding="utf-8").write(
        f"{EMOJI[nivel]} {NIVELES[nivel]} | {NOW:%Y-%m-%d %H:%M} UTC\n{resumen}\n")

    # RSS: un item por alerta de historial (últimas 40)
    items = []
    for h in historial[:40]:
        items.append(f"""<item><title>{html.escape(h['titulo'])}</title>
<link>{html.escape(h.get('url') or url)}</link><guid isPermaLink="false">{h['id']}-{h['hora_utc']}</guid>
<pubDate>{format_datetime(datetime.fromisoformat(h['hora_utc']))}</pubDate>
<category>{h['nivel_txt']}</category><description>{html.escape(h['nivel_txt'] + ' · ' + h['fuente'] + ' · ' + (h.get('detalle') or ''))}</description></item>""")
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>{CFG['nombre_sistema']} — {EMOJI[nivel]} {NIVELES[nivel]}</title>
<link>{url}</link><description>{html.escape(resumen)}</description>
<lastBuildDate>{format_datetime(NOW)}</lastBuildDate><ttl>15</ttl>
{''.join(items)}
</channel></rss>"""
    open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8").write(rss)
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(
        panel_html(nivel, resumen, senales, estado_fuentes, historial))


def panel_html(nivel, resumen, senales, fuentes, historial):
    colores = ["#1f9d55", "#2b6cb0", "#c99a06", "#dd6b20", "#c53030"]
    filas = "".join(
        f"<tr><td><span class='pill' style='background:{colores[s['nivel']]}'>{s['nivel_txt']}</span></td>"
        f"<td>{html.escape(s['categoria'])}</td><td><a href='{html.escape(s['url'] or '#')}' target='_blank'>"
        f"{html.escape(s['titulo'])}</a><div class='det'>{html.escape(s['detalle'] or '')}</div></td>"
        f"<td>{html.escape(s['fuente'])}</td></tr>" for s in senales) or \
        "<tr><td colspan=4 class='det'>Sin señales activas.</td></tr>"
    fu = "".join(f"<li>{'✅' if v['ok'] else '⚠️'} {html.escape(k)} <span class='det'>{html.escape(v.get('nota',''))}</span></li>"
                 for k, v in fuentes.items())
    man = "".join(f"<li><a href='{html.escape(m['url'])}' target='_blank' rel='noopener'>{html.escape(m['nombre'])}</a></li>"
                  for m in CFG.get("consulta_manual", []))
    hist = "".join(f"<li>{h['hora_utc'][:16].replace('T',' ')} · {EMOJI[h['nivel']]} {html.escape(h['titulo'])}</li>"
                   for h in historial[:25]) or "<li class='det'>Sin alertas registradas.</li>"
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">
<meta name="robots" content="noindex,nofollow"><title>{CFG['nombre_sistema']} · {NIVELES[nivel]}</title>
<style>
:root{{color-scheme:light dark;--bg:#0f1115;--fg:#e8e8ea;--mut:#9aa0a6;--card:#181b22;--line:#2a2f3a}}
@media (prefers-color-scheme:light){{:root{{--bg:#f6f7f9;--fg:#15171c;--mut:#5f6368;--card:#fff;--line:#e3e5e8}}}}
body{{margin:0;font:15px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:980px;margin:auto;padding:20px}} a{{color:inherit}}
.hero{{border-radius:14px;padding:20px 22px;color:#fff;background:{colores[nivel]}}}
.hero h1{{margin:0;font-size:28px}} .hero p{{margin:6px 0 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:16px}}
h2{{font-size:16px;margin:0 0 10px}} table{{width:100%;border-collapse:collapse}}
td{{border-top:1px solid var(--line);padding:8px 6px;vertical-align:top}}
.pill{{color:#fff;border-radius:99px;padding:2px 8px;font-size:12px;white-space:nowrap}}
.det{{color:var(--mut);font-size:13px}} ul{{margin:0;padding-left:18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}
</style></head><body><main>
<div class="hero"><h1>{EMOJI[nivel]} {NIVELES[nivel]}</h1><p>{html.escape(resumen)}</p>
<p style="opacity:.85;font-size:13px">Actualizado {NOW:%Y-%m-%d %H:%M} UTC · se refresca cada 5 min</p></div>
<div class="card"><h2>Señales activas</h2><table>{filas}</table></div>
<div class="grid">
<div class="card"><h2>Historial de alertas</h2><ul>{hist}</ul></div>
<div class="card"><h2>Estado de las fuentes</h2><ul>{fu}</ul></div>
<div class="card"><h2>Consulta manual (OSINT)</h2><ul>{man}</ul></div>
</div>
<p class="det">Datos abiertos: NOAA SWPC, USGS, GDACS, IODA, Apple System Status, Statuspage, Google News, GPSJam.
Fuentes: <a href="status.json">status.json</a> · <a href="feed.xml">RSS</a></p>
</main></body></html>"""


# ------------------------------------------------------------------ notificaciones
def notificar(titulo, cuerpo, nivel, dry=False):
    url = CFG.get("url_panel", "")
    if CLAVE:
        for z in CFG.get("zonas_familia", []):
            cuerpo = cuerpo.replace(z["nombre"], "zona familiar")
    if dry:
        print(f"[DRY] {titulo}\n{cuerpo}")
        return
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        atajo = os.getenv("SHORTCUT_NAME") or "Alerta Familiar"
        msg = {"topic": topic, "title": titulo, "message": cuerpo,
               "priority": 5 if nivel >= 4 else 4 if nivel == 3 else 3,
               "tags": ["rotating_light"] if nivel >= 3 else ["warning"], "click": url,
               "actions": [
                   {"action": "view", "label": "Enviar mi ubicación",
                    # el riesgo viaja dentro del enlace: el Atajo no necesita internet para leerlo
                    "url": f"shortcuts://run-shortcut?name={requests.utils.quote(atajo)}"
                           f"&input=text&text={requests.utils.quote((titulo + ' | ' + cuerpo)[:400])}"},
                   {"action": "view", "label": "Confirmo / estoy bien",
                    "url": "shortcuts://run-shortcut?name=" + requests.utils.quote(os.getenv("SHORTCUT_CONFIRMO") or "Confirmo")},
                   {"action": "view", "label": "Ver panel", "url": url}]}
        h = {"Authorization": f"Bearer {os.getenv('NTFY_TOKEN')}"} if os.getenv("NTFY_TOKEN") else {}
        try:
            requests.post(server, json=msg, headers=h, timeout=TIMEOUT).raise_for_status()
            print("  ✓ ntfy enviado")
        except Exception as e:
            print("  ! ntfy:", e)
    # Telegram (opcional)
    bt, chats = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_IDS")
    if bt and chats:
        for c in chats.split(","):
            try:
                requests.post(f"https://api.telegram.org/bot{bt}/sendMessage", timeout=TIMEOUT,
                              data={"chat_id": c.strip(), "text": f"{titulo}\n{cuerpo}\n{url}"}).raise_for_status()
                print("  ✓ Telegram enviado")
            except Exception as e:
                print("  ! Telegram:", e)


# ------------------------------------------------------------------ multi-motor
def primario_vivo():
    """En motores de RESPALDO: devuelve True si algún motor primario publicó
    status.json hace menos de MAX_SILENCIO_MIN minutos (entonces no notificamos,
    para no duplicar alertas). Si todos callan, este motor toma el control."""
    if os.getenv("ROL", "primario").lower() != "respaldo":
        return False
    limite = int(os.getenv("MAX_SILENCIO_MIN", "45"))
    for u in [x.strip() for x in os.getenv("PRIMARIOS", "").split(",") if x.strip()]:
        try:
            d = get(u).json()
            t = datetime.fromisoformat(d["actualizado_utc"])
            edad = (NOW - t).total_seconds() / 60
            print(f"  primario {u}: hace {edad:.0f} min")
            if edad <= limite:
                return True
        except Exception as e:
            print(f"  primario {u}: sin respuesta ({e})")
    print("  ⚠️ Ningún primario responde: este motor de respaldo enviará alertas.")
    return False


# ------------------------------------------------------------------ main
def main():
    dry = "--dry-run" in sys.argv
    if "--test-alert" in sys.argv:
        notificar("🧪 PRUEBA — Centinela Familiar",
                  "Esto es una prueba. Si la recibiste, el canal funciona. Toca 'Enviar mi ubicación' para probar el Atajo.",
                  3, dry)
        return
    estado = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"enviadas": {}, "historial": [], "ultimo_nivel": 0}
    ESTADO_REF.update(estado)
    senales, fuentes = [], {}
    for nombre, fn in FUENTES:
        try:
            r = fn(); senales += r
            fuentes[nombre] = {"ok": True, "nota": f"{len(r)} señal(es)"}
            print(f"✓ {nombre}: {len(r)}")
        except Exception as e:
            fuentes[nombre] = {"ok": False, "nota": str(e)[:120]}
            print(f"✗ {nombre}: {e}")

    nivel, resumen, senales = evaluar(senales)
    # Si muchas fuentes fallan a la vez, eso también es una señal
    caidas = sum(1 for v in fuentes.values() if not v["ok"])
    if caidas >= max(4, len(FUENTES) // 2):
        s = senal("Centinela", "Sistema", 2, f"{caidas} fuentes de datos sin respuesta",
                  "Puede ser un problema de red amplio o del propio servidor.", clave=f"self-{NOW:%Y%m%d%H}")
        senales.insert(0, s); nivel = max(nivel, 2)

    umbral = CFG.get("nivel_minimo_alerta", 3)
    nuevas = [s for s in senales if s["nivel"] >= 2 and s["id"] not in estado["enviadas"]]
    for s in nuevas:
        estado["enviadas"][s["id"]] = s["hora_utc"]
        estado["historial"].insert(0, s)
    estado["historial"] = estado["historial"][:200]
    # limpiar ids viejos (7 días)
    lim = (NOW - timedelta(days=7)).isoformat()
    estado["enviadas"] = {k: v for k, v in estado["enviadas"].items() if v >= lim}

    criticas = [s for s in nuevas if s["nivel"] >= umbral]
    subio = nivel >= umbral and nivel > estado.get("ultimo_nivel", 0)
    silenciar = primario_vivo()
    if silenciar and (criticas or subio):
        print("  (respaldo) primario activo: no se envían alertas desde este motor")
    if silenciar:
        pass
    elif criticas or subio:
        cuerpo = "\n".join(f"• {s['titulo']}" for s in (criticas or senales[:4]))
        notificar(f"{EMOJI[nivel]} RIESGO {NIVELES[nivel]} — comunicaciones", cuerpo +
                  "\n\nActiva el protocolo familiar.", nivel, dry)
    elif nivel < umbral <= estado.get("ultimo_nivel", 0):
        notificar("🟢 Riesgo bajó", f"Nivel actual: {NIVELES[nivel]}. {resumen}", 2, dry)
    estado["ultimo_nivel"] = nivel
    estado["mil_caribe"] = ESTADO_REF.get("mil_caribe", [])

    escribir_salidas(nivel, resumen, senales, fuentes, estado["historial"])
    json.dump(estado, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)
    print(f"\nNIVEL: {EMOJI[nivel]} {NIVELES[nivel]} — {resumen}")


if __name__ == "__main__":
    main()
