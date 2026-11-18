#!/usr/bin/env bash
# Instala un MOTOR DE RESPALDO en un servidor Ubuntu (Oracle Cloud Always Free:
# Zúrich, Tokio, Singapur, Seúl...). Uso, ya conectado por SSH:
#   curl -fsSL https://raw.githubusercontent.com/garta15/centinela/main/servidor/instalar_servidor.sh | bash
# (o copia este archivo y ejecuta: bash instalar_servidor.sh)
set -e
REPOS=(
  "https://github.com/garta15/centinela.git"
  "https://codeberg.org/garta15/centinela.git"
  "https://gitlab.com/garta15/centinela.git"
  "https://huggingface.co/garta15/centinela-codigo"
)
sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv lftp nginx rsync
cd ~
if [ ! -d centinela ]; then
  for r in "${REPOS[@]}"; do git clone --quiet "$r" centinela && break || true; done
fi
cd centinela
python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
chmod +x servidor/*.sh
if [ ! -f .env ]; then
cat > .env <<'ENV'
# ---- Rellena estos valores ----
MOTOR=oracle-zurich
ROL=respaldo
PRIMARIOS=https://garta15.github.io/centinela/pulso.json
MAX_SILENCIO_MIN=45
NTFY_TOPIC=tu-tema-secreto
FAMILY_KEY=la-misma-frase-clave-familiar
PAISES_FAMILIA=PR,US
ZONAS_FAMILIA=[{"nombre":"Base","lat":18.22,"lon":-66.59,"radio_km":600}]
SHORTCUT_NAME=Alerta Familiar
# Opcional: FTP de tu dominio (host|usuario|clave|carpeta; varios con ';')
FTP_DESTINOS=
# Opcional (gratis): nombre DuckDNS para tener HTTPS sin comprar dominio
DUCKDNS_SUBDOMINIO=
DUCKDNS_TOKEN=
ENV
  echo ">>> Edita ~/centinela/.env (nano .env) y vuelve a ejecutar este script."
fi
# nginx: permitir que el usuario escriba el panel
sudo chown -R "$USER" /var/www/html
# abrir puerto 80 en el firewall del sistema (Oracle Ubuntu usa iptables)
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
sudo netfilter-persistent save 2>/dev/null || true
# cron cada 15 min
( crontab -l 2>/dev/null | grep -v centinela ; echo "*/15 * * * * $HOME/centinela/servidor/ciclo.sh >> $HOME/centinela/ciclo.log 2>&1" ) | crontab -
# DuckDNS (gratis): mantiene tu-nombre.duckdns.org apuntando a este servidor
set -a; . ./.env; set +a
if [ -n "$DUCKDNS_SUBDOMINIO" ] && [ -n "$DUCKDNS_TOKEN" ]; then
  ( crontab -l 2>/dev/null | grep -v duckdns ; echo "*/30 * * * * curl -s 'https://www.duckdns.org/update?domains=$DUCKDNS_SUBDOMINIO&token=$DUCKDNS_TOKEN&ip=' >/dev/null" ) | crontab -
  curl -s "https://www.duckdns.org/update?domains=$DUCKDNS_SUBDOMINIO&token=$DUCKDNS_TOKEN&ip=" ; echo
  sudo apt-get install -y -qq certbot python3-certbot-nginx
  sudo certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email -d "$DUCKDNS_SUBDOMINIO.duckdns.org" || true
fi
./servidor/ciclo.sh || true
echo "✓ Motor de respaldo instalado. Panel: http://$(curl -s ifconfig.me)/"
