#!/usr/bin/env bash
# Actualiza el código desde el primer espejo que responda. Nunca rompe
# la instalación: si todos fallan, sigue con la versión local.
cd "$(dirname "$0")/.." || exit 0
grep -v '^#' espejos.txt | grep -v '^\s*$' | while read -r url; do
  if timeout 60 git fetch --quiet "$url" main 2>/dev/null; then
    # conserva state.json local; actualiza solo código y config
    git checkout --quiet FETCH_HEAD -- monitor.py config.yaml requirements.txt espejos.txt servidor 2>/dev/null
    echo "✓ código actualizado desde $url"
    exit 0
  else
    echo "✗ $url no responde"
  fi
done
exit 0
