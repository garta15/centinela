# 🛰️ Centinela Familiar

Sistema privado que vigila cada 15 minutos los riesgos de gran escala que pueden cortar las comunicaciones. Cuando el riesgo es **ALTO** o **CRÍTICO**, avisa a cada iPhone de la familia. La notificación trae un botón **"Enviar mi ubicación"**: abre un Atajo que manda tu latitud y longitud y el riesgo detectado al grupo familiar, y te recuerda el protocolo.

```
 Fuentes abiertas ──► monitor.py (GitHub Actions, cada 15 min)
                          │
          ┌───────────────┼──────────────────────────┐
          ▼               ▼                          ▼
   status.json / RSS   Push ntfy (+SMS/Telegram)   Respaldos:
   panel index.html    ─► iPhone ─► Atajo           · GitHub Pages
                                    (ubicación +    · Hugging Face Space
                                     protocolo)     · tu dominio (FTP)
```

> **Guía completa paso a paso** (GitHub, respaldos en Suiza / Asia / Nueva Zelanda, Atajo de iPhone): ver el documento *Centinela Familiar — Guía de instalación*.
> Archivos nuevos: `servidor/` (instala un **motor de respaldo** en Oracle Cloud), `espejos.txt` (de dónde se actualiza el código si GitHub cae).
> Un motor con `ROL=respaldo` solo envía alertas si los `PRIMARIOS` llevan más de 45 min sin publicar, así que nunca se duplican las alertas.

## 🔒 Cifrado y nombres en clave
- Con el Secret `FAMILY_KEY`, el panel y el detalle se publican **cifrados** (`estado.enc`, AES-256-GCM). El panel los descifra en el iPhone con la frase clave.
- El RSS solo dice «🔒 Nueva alerta (nivel X)». `pulso.json` solo publica el nivel (0–4) y la hora.
- Los países y las zonas de la familia van en los Secrets `PAISES_FAMILIA` y `ZONAS_FAMILIA`, no en `config.yaml`.
- Atajos: *Alerta Familiar*, *Confirmo*, *Pase de lista* y *Chequeo Centinela*, con nombres en clave (Abril, Noviembre, Agosto…).

## Qué vigila y cómo puntúa

| Fuente (automática) | Qué detecta | Nivel máximo |
|---|---|---|
| **NOAA SWPC** | Tormentas geomagnéticas G4–G5, apagones de radio R3–R5 y su pronóstico | CRÍTICO (G5/R5) |
| **IODA** (Georgia Tech) | Caídas de internet a nivel país. Si es un país de la familia, el nivel es ALTO. Si son ≥10 países a la vez, es un patrón global | ALTO |
| **Apple System Status** | Caída de iMessage, FaceTime o Find My | ALTO |
| **USGS** | Sismos M6+ en el mundo, M5+ cerca de tus zonas, bandera de tsunami | CRÍTICO |
| **GDACS** (ONU/UE) | Alertas naranja/roja de huracanes, inundaciones y volcanes, más fuerte si son cerca de ti | CRÍTICO |
| **Cloudflare / Twilio (solo se lee su página de estado)** | Degradación de infraestructura de internet y SMS | ALTO |
| **Google News** | Frases como "undersea cable cut" o "nationwide internet shutdown" | ELEVADO* |
| **GPSJam** | Zonas con interferencia de GPS (guerra electrónica), resaltando las cercanas a la familia | ELEVADO |

\* Las noticias solas **nunca** disparan una alerta, porque hay demasiado ruido (lo comprobé: "nuclear detonation" aparece a diario en artículos de opinión). Las noticias solo cuentan para la **convergencia**: si hay señales ELEVADAS de 3 categorías distintas a la vez, el nivel sube a ALTO.

**Las páginas que diste sin API pública** (OSIRIS, PizzINT, Conflictly, Radware, VECERT, Iran Monitor, SkyOSINT, RF Consult, Downdetector, IsItDownRightNow) aparecen en el panel como **enlaces de consulta manual**. No las raspo porque no ofrecen datos abiertos, sus términos lo prohíben (Downdetector, por ejemplo) y se rompería con cualquier cambio de diseño. El sistema usa las **fuentes originales** de las que esos mapas se alimentan: USGS, NOAA, IODA y GPSJam.

Niveles: 🟢 NORMAL · 🔵 VIGILANCIA · 🟡 ELEVADO · 🟠 ALTO · 🔴 CRÍTICO.
Cada alerta se envía una sola vez (no repite durante 7 días). También te avisa cuando el riesgo vuelve a bajar.

---

## Instalación (≈30 min, costo $0)

### 1. Canal de alertas: ntfy (gratis, app de iPhone)
1. Cada miembro de la familia instala **ntfy** desde la App Store.
2. Inventa un tema secreto y difícil de adivinar, por ejemplo `fam-dr-7Kq2x9Lm4Pz`. Funciona como una contraseña.
3. En la app: **+ → Subscribe to topic →** escribe el tema. Activa las notificaciones **Critical/Time Sensitive** si la app lo pide.

> Si quieres más privacidad, ntfy permite usuarios con contraseña o tu propio servidor (`NTFY_SERVER` + `NTFY_TOKEN`).

### 2. GitHub (motor + panel principal)
1. Crea una cuenta en github.com. Crea un repositorio nuevo, por ejemplo `centinela`, y sube **todos** estos archivos.
2. **Settings → Secrets and variables → Actions → New repository secret:**
   - `NTFY_TOPIC` = tu tema secreto
3. **Settings → Pages →** Source: *Deploy from a branch* → `main` / `/docs`.
4. **Actions →** activa los workflows → *Centinela Familiar* → **Run workflow** → marca **"Enviar alerta de prueba"**. Debe llegar una notificación a todos los iPhone.
5. Edita `config.yaml`: pon `url_panel`, tus países (`paises_familia`) y tus zonas (`zonas_familia`).

**¿Repositorio público o privado?** Te recomiendo **público**: el repositorio no contiene ningún dato personal. Teléfonos, tema y protocolo viven en Secrets y en los iPhone. Además, un repositorio público tiene minutos de Actions ilimitados y GitHub Pages gratis. Un repositorio privado gratuito trae 2,000 min/mes, así que tendrías que cambiar el cron a `*/30` y servir el panel solo desde Hugging Face o tu dominio.

### 3. Respaldo en Hugging Face
1. Crea una cuenta en huggingface.co → **Settings → Access Tokens →** crea un token con permiso **Write**.
2. En GitHub agrega el Secret `HF_TOKEN` y, en **Variables**, `HF_SPACE` = `tu-usuario/centinela-familiar`.
3. El panel queda también en `https://tu-usuario-centinela-familiar.static.hf.space/`.

### 4. Respaldo en tu dominio
- **Opción A (recomendada):** en tu DNS crea `centinela.tudominio.com` como CNAME a `tu-usuario.github.io` y ponlo en Settings → Pages → Custom domain.
- **Opción B (copia física en tu hosting):** agrega los Secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PASS` y `DEPLOY_PATH` (FTP/SFTP). Con eso, cada corrida copia `docs/` a tu servidor.

Además puedes sumar Cloudflare Pages (gratis). Así tendrás **varias copias**: GitHub, Hugging Face y tu dominio. Si una cae por un ciberataque, el Atajo prueba la siguiente.

### 5. Canal extra opcional: Telegram (gratis)
Crea un bot con @BotFather y agrega los Secrets `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_IDS`.

> **Todo el sistema es 100 % gratis:** GitHub, ntfy, Hugging Face, Cloudflare Pages, Codeberg, GitLab, Oracle Always Free y DuckDNS. No uses AWS, Twilio ni hosting de pago.

---

## 📱 Atajo del iPhone: "Alerta Familiar"

Créalo en la app **Atajos → +**, con este nombre exacto (es el que abre el botón de la notificación).

| # | Acción | Configuración |
|---|---|---|
| 1 | **Lista** | `https://garta15.github.io/centinela/status.json` · `https://TU-USUARIO-centinela-familiar.static.hf.space/status.json` · `https://centinela.tudominio.com/status.json` |
| 2 | **Repetir con cada** elemento de la Lista | — |
| 3 | ↳ **Si** `Estado` *no tiene ningún valor* | — |
| 4 | ↳↳ **Obtener contenido de URL** `Elemento repetido` | — |
| 5 | ↳↳ **Obtener diccionario de** Contenido → **Definir variable** `Estado` | — |
| 6 | ↳ Fin Si · Fin Repetir | — |
| 7 | **Obtener ubicación actual** | — |
| 8 | **Obtener detalles de ubicaciones**: *Latitud* → variable `Lat`; repetir para *Longitud* → `Lon` | — |
| 9 | **Obtener valor del diccionario** `mensaje_corto` de `Estado` | Si está vacío, usa el texto "Sin datos del Centinela" |
| 10 | **Texto** (ver plantilla abajo) | — |
| 11 | **Enviar mensaje** | Destinatarios: el grupo/contactos de la familia. Desactiva *Mostrar al ejecutar* si quieres que salga solo |
| 12 | **Mostrar resultado / Vista rápida** | Tu nota del protocolo familiar (ver `PROTOCOLO_FAMILIAR.md`) |

**Plantilla del texto (paso 10):**
```
⚠️ ALERTA FAMILIAR
Riesgo: [mensaje_corto]
📍 Estoy en: [Lat], [Lon]
https://maps.apple.com/?ll=[Lat],[Lon]
🕐 [Fecha actual]
Activa PROTOCOLO: responde "OK + ubicación". Si no hay datos, usa SMS.
Punto de encuentro 1: ____  ·  Contacto fuera del área: ____
```

> En **Ajustes → Apps → Mensajes**, activa **"Enviar como SMS"**. Así, si iMessage o los datos fallan, el mensaje sale por SMS celular.

**Segunda red de seguridad (por si no llega la notificación push):** crea en **Atajos → Automatización → Hora del día** (por ejemplo 7:00 a. m. y 7:00 p. m.) un atajo "Chequeo Centinela". Ese atajo descarga `status.json` y, **solo si `nivel` ≥ 3**, muestra la notificación y ejecuta "Alerta Familiar". Marca *Ejecutar inmediatamente*.

**Sobre el RSS:** iOS no puede ejecutar un Atajo automáticamente cuando llega un RSS. Por eso el disparador es la notificación de ntfy, que trae el botón del Atajo. El `feed.xml` sigue disponible si quieres verlo en un lector RSS (Reeder, NetNewsWire).

**Sobre la ubicación:** cada iPhone obtiene **su propia** ubicación y la envía **directo** al grupo familiar. Nunca pasa por el servidor ni queda guardada en internet.

---

## Probar y ajustar
```bash
pip install -r requirements.txt
python monitor.py --dry-run     # corre todo sin enviar nada
python monitor.py --test-alert  # (con NTFY_TOPIC exportado) envía una prueba
```
- ¿Demasiadas alertas? Sube `nivel_minimo_alerta` a 4 o `convergencia_senales` a 4.
- ¿Muy pocas? Agrega frases a `noticias` o países a `paises_familia`.

## Límites honestos
- Es **alerta temprana**: si la red ya cayó donde estás, no llegará nada. Por eso existen el protocolo offline y la revisión programada.
- GitHub puede retrasar el cron de 5 a 15 min en horas pico.
- Ninguna fuente pública anticipa un ciberataque o un corte deliberado con certeza. El sistema detecta **señales y patrones**, no predice.
