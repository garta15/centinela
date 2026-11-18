#!/usr/bin/env bash
# Lo ejecuta cron cada 15 min en el servidor de respaldo.
cd "$(dirname "$0")/.." || exit 1
set -a; [ -f .env ] && . ./.env; set +a
./servidor/actualizar.sh
./venv/bin/python monitor.py
# servir el panel localmente
rsync -a --delete docs/ /var/www/html/ 2>/dev/null || true
# copiar a tu dominio por FTP (opcional, solo si ya tienes hosting)
if [ -n "$FTP_DESTINOS" ]; then
  echo "$FTP_DESTINOS" | tr ';' '\n' | while IFS='|' read -r host user pass path; do
    [ -z "$host" ] && continue
    lftp -u "$user","$pass" "$host" -e "set ssl:verify-certificate yes; set net:timeout 20; set net:max-retries 2; mirror -R --delete docs $path; quit" \
      && echo "✓ FTP $host" || echo "✗ FTP $host"
  done
fi
