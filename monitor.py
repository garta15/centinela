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
          url="", clave=None, alcance="global"):
    nivel = max(0, min(4, int(nivel)))
    return {
        "id": hashlib.sha1((clave or f"{fuente}|{titulo}").encode()).hexdigest()[:12],
        "fuente": fuente, "categoria": categoria, "nivel": nivel,
        "nivel_txt": NIVELES[nivel], "titulo": titulo, "detalle": detalle,
        "lat": lat, "lon": lon, "url": url, "alcance": alcance,
        "hora_utc": NOW.isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ fuentes
def f_clima_espacial():
    """NOAA SWPC: tormentas geomagnéticas (G), apagones de radio (R), tormentas solares (S)."""
    out = []
    d = get("https://services.swpc.noaa.gov/products/noaa-scales.json").json()
    act = d["0"]
    mapa_g = {3: 2, 4: 3, 5: 4}
    mapa_r = {3: 2, 4: 3, 5: 4}
    g = int(act["G"]["Scale"] or 0); r = int(act["R"]["Scale"] or 0); s = int(act["S"]["Scale"] or 0)
    if g >= 1:
        out.append(senal("NOAA SWPC", "Clima espacial", mapa_g.get(g, 1),
                         f"Tormenta geomagnética G{g} ({act['G']['Text']})",
                         "G4-G5 puede degradar GPS, satélites, radio HF y red eléctrica.",
                         url="https://www.swpc.noaa.gov/", clave=f"swpc-G{g}-{act['DateStamp']}"))
    if r >= 1:
        out.append(senal("NOAA SWPC", "Clima espacial", mapa_r.get(r, 1),
                         f"Apagón de radio R{r} ({act['R']['Text']})",
                         "Afecta radio HF y navegación en el lado diurno de la Tierra.",
                         url="https://www.swpc.noaa.gov/", clave=f"swpc-R{r}-{act['DateStamp']}"))
    if s >= 3:
        out.append(senal("NOAA SWPC", "Clima espacial", 2, f"Tormenta de radiación solar S{s}",
                         "Puede afectar satélites y comunicaciones polares.",
                         url="https://www.swpc.noaa.gov/", clave=f"swpc-S{s}-{act['DateStamp']}"))
    # pronóstico (días 1-3): G4+ previsto
    for k in ("1", "2", "3"):
        g_f = d.get(k, {}).get("G", {}).get("Scale")
        if g_f and int(g_f) >= 4:
            out.append(senal("NOAA SWPC", "Clima espacial", 2,
                             f"Pronóstico: tormenta geomagnética G{g_f} el {d[k]['DateStamp']}",
                             url="https://www.swpc.noaa.gov/", clave=f"swpc-fG{g_f}-{d[k]['DateStamp']}"))
    return out


def f_sismos():
    """USGS: sismos M6+ de las últimas 24 h (y M5+ cerca de la familia)."""
    out = []
    d = get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson").json()
    for f in d["features"]:
        p = f["properties"]; lon, lat = f["geometry"]["coordinates"][:2]
        m = p.get("mag") or 0
        zona, dist = zona_cercana(lat, lon)
        if m < 6 and not (zona and m >= 5):
            continue
        n = 1
        if m >= 7: n = 2
        if m >= 7.8: n = 3
        if zona: n = max(n + 1, 2) if m >= 6 else 2
        if p.get("tsunami"): n += 1
        det = f"Profundidad {f['geometry']['coordinates'][2]} km."
        if p.get("tsunami"): det += " Posible tsunami (bandera USGS)."
        if zona: det += f" A {dist} km de {zona}."
        out.append(senal("USGS", "Sismo", n, f"Sismo M{m:.1f} — {p.get('place')}", det,
                         lat, lon, p.get("url", ""), clave=f["id"],
                         alcance="familia" if zona else "global"))
    return out


def f_gdacs():
    """GDACS (ONU/UE): alertas naranja y roja de desastres (ciclones, inundaciones, volcanes)."""
    out = []
    x = get("https://www.gdacs.org/xml/rss.xml").content
    ns = {"gdacs": "http://www.gdacs.org", "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
    for it in ET.fromstring(x).iter("item"):
        lvl = (it.findtext("gdacs:alertlevel", "", ns) or "").lower()
        if lvl not in ("orange", "red"):
            continue
        if (it.findtext("gdacs:iscurrent", "true", ns) or "true").lower() != "true":
            continue
        try:
            lat = float(it.findtext("geo:Point/geo:lat", None, ns))
            lon = float(it.findtext("geo:Point/geo:long", None, ns))
        except (TypeError, ValueError):
            lat = lon = None
        zona = dist = None
        if lat is not None:
            zona, dist = zona_cercana(lat, lon)
        n = 3 if lvl == "red" else 2
        if zona: n = min(4, n + 1)
        titulo = it.findtext("title", "")
        out.append(senal("GDACS", "Desastre", n, titulo,
                         (f"A {dist} km de {zona}. " if zona else "") + f"Alerta {lvl.upper()}.",
                         lat, lon, it.findtext("link", ""),
                         clave="gdacs-" + (it.findtext("guid", "") or titulo) + lvl,
                         alcance="familia" if zona else "global"))
    return out


def f_ioda():
    """IODA (Georgia Tech): caídas de internet a nivel país (BGP, sondeo activo, telescopio)."""
    out = []
    until = int(time.time()); frm = until - 3 * 3600
    d = get("https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts",
            params={"from": frm, "until": until, "entityType": "country", "limit": 500}).json()
    paises = {}
    for a in d.get("data") or []:
        if a.get("level") != "critical":
            continue
        e = a["entity"]; code = e["code"]
        hv = a.get("historyValue") or 0; v = a.get("value") or 0
        caida = (1 - v / hv) * 100 if hv else 0
        prev = paises.get(code)
        if not prev or caida > prev["caida"]:
            paises[code] = {"nombre": e["name"], "caida": caida, "fuente": a.get("datasource")}
    fam = set(CFG.get("paises_familia", []))
    for code, p in paises.items():
        if p["caida"] < 5 and code not in fam:
            continue  # ruido
        n = 1
        if p["caida"] >= 25: n = 2
        if code in fam: n = 3 if p["caida"] >= 10 else 2
        out.append(senal("IODA", "Internet", n,
                         f"Caída de internet en {p['nombre']} (-{p['caida']:.0f}% {p['fuente']})",
                         "Detectado por IODA en las últimas 3 h.",
                         url=f"https://ioda.inetintel.cc.gatech.edu/country/{code}",
                         clave=f"ioda-{code}-{NOW:%Y%m%d}", alcance="familia" if code in fam else "global"))
    umbral = CFG.get("ioda_paises_patron_global", 10)
    if len(paises) >= umbral:
        out.append(senal("IODA", "Internet", 3,
                         f"Patrón global: {len(paises)} países con caídas críticas simultáneas",
                         ", ".join(sorted(p["nombre"] for p in paises.values()))[:400],
                         url="https://ioda.inetintel.cc.gatech.edu/",
                         clave=f"ioda-global-{NOW:%Y%m%d%H}"))
    return out


def f_apple():
    """Estado de iMessage, FaceTime, iCloud, Find My (los canales de la familia)."""
    out = []
    t = get("https://www.apple.com/support/systemstatus/data/system_status_en_US.js").text
    t = t[t.find("{"): t.rfind("}") + 1]
    d = json.loads(t)
    vig = set(CFG.get("apple_servicios", []))
    for s in d.get("services", []):
        if s["serviceName"] not in vig:
            continue
        for ev in s.get("events", []):
            if ev.get("eventStatus") not in ("ongoing",):
                continue
            tipo = ev.get("statusType", "")
            critico = s["serviceName"] in CFG.get("apple_criticos", [])
            n = (3 if critico else 2) if tipo == "Outage" else (2 if critico else 1)
            out.append(senal("Apple", "Servicio familiar", n,
                             f"{s['serviceName']}: {tipo}", ev.get("message", "")[:300],
                             url="https://www.apple.com/support/systemstatus/",
                             clave=f"apple-{ev.get('messageId')}", alcance="familia"))
    return out


def f_statuspages():
    out = []
    mapa = {"minor": 1, "major": 2, "critical": 3}
    for sp in CFG.get("statuspages", []):
        try:
            st = get(sp["url"]).json()["status"]
        except Exception as e:
            print(f"  ! {sp['nombre']}: {e}")
            continue
        n = mapa.get(st.get("indicator"), 0)
        if n:
            out.append(senal(sp["nombre"], "Infraestructura", n,
                             f"{sp['nombre']}: {st.get('description')}",
                             url=sp["url"].split("/api/")[0], clave=f"sp-{sp['nombre']}-{st.get('indicator')}-{NOW:%Y%m%d}"))
    return out


def f_noticias():
    """Google News RSS: conteo de artículos de 24 h por frase de riesgo."""
    out = []
    for nq in CFG.get("noticias", []):
        es = nq.get("idioma") == "es"
        params = {"q": f'"{nq["frase"]}" when:1d',
                  "hl": "es-419" if es else "en-US", "gl": "PR" if es else "US",
                  "ceid": "PR:es-419" if es else "US:en"}
        try:
            x = get("https://news.google.com/rss/search", params=params).content
        except Exception as e:
            print(f"  ! noticias '{nq['frase']}': {e}")
            continue
        items = list(ET.fromstring(x).iter("item"))
        if len(items) >= nq["umbral"]:
            top = items[0]
            # Las noticias son ruidosas: nunca pasan de ELEVADO por sí solas.
            out.append(senal("Noticias", "Señal de noticias", min(2, nq["peso"]),
                             f"{len(items)} noticias en 24 h: “{nq['frase']}”",
                             f"Más reciente: {top.findtext('title','')}",
                             url=top.findtext("link", ""),
                             clave=f"news-{nq['frase']}-{NOW:%Y%m%d}"))
        time.sleep(1)
    return out


def f_gpsjam():
    """GPSJam: interferencia GNSS (día anterior). Indicador de guerra electrónica."""
    out = []
    man = get("https://gpsjam.org/data/manifest.csv").text.strip().splitlines()
    dia = man[-1].split(",")[0]
    rows = csv.DictReader(io.StringIO(get(f"https://gpsjam.org/data/{dia}-h3_4.csv").text))
    malos = []
    for r in rows:
        g = int(r["count_good_aircraft"]); b = int(r["count_bad_aircraft"])
        if g + b >= 5 and b / (g + b) > 0.10:
            malos.append(r["hex"])
    det = ""
    try:
        import h3
        cerca = []
        for hx in malos:
            lat, lon = h3.cell_to_latlng(hx)
            zona, dist = zona_cercana(lat, lon)
            if zona: cerca.append(f"{zona} ({dist} km)")
        if cerca:
            out.append(senal("GPSJam", "GPS/GNSS", 2, f"Interferencia GPS cerca de la familia ({dia})",
                             "; ".join(sorted(set(cerca)))[:300], url="https://gpsjam.org/",
                             clave=f"gpsjam-fam-{dia}", alcance="familia"))
    except ImportError:
        det = "(instala 'h3' para ubicar zonas)"
    n = 1 if len(malos) < 1500 else 2
    out.append(senal("GPSJam", "GPS/GNSS", n, f"{len(malos)} zonas con interferencia GPS ({dia})",
                     det, url=f"https://gpsjam.org/?date={dia}", clave=f"gpsjam-{dia}"))
    return out


FUENTES = [("Clima espacial", f_clima_espacial), ("Sismos", f_sismos), ("GDACS", f_gdacs),
           ("IODA", f_ioda), ("Apple", f_apple), ("Status pages", f_statuspages),
           ("Noticias", f_noticias), ("GPSJam", f_gpsjam)]


# ------------------------------------------------------------------ evaluación
def evaluar(senales):
    nivel = max([s["nivel"] for s in senales], default=0)
    # Convergencia: señales elevadas de CATEGORÍAS distintas (no 3 noticias iguales)
    cats = {s["categoria"] for s in senales if s["nivel"] >= 2}
    conv = CFG.get("convergencia_senales", 3)
    motivo = ""
    if len(cats) >= conv and nivel < 3:
        nivel = 3
        motivo = f"Convergencia: señales elevadas en {len(cats)} categorías ({', '.join(sorted(cats))})."
    top = sorted(senales, key=lambda s: (-s["nivel"], s["fuente"]))
    if nivel == 0:
        resumen = "Sin riesgos relevantes para las comunicaciones."
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

    escribir_salidas(nivel, resumen, senales, fuentes, estado["historial"])
    json.dump(estado, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)
    print(f"\nNIVEL: {EMOJI[nivel]} {NIVELES[nivel]} — {resumen}")


if __name__ == "__main__":
    main()
