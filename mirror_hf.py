"""Copia docs/ a un Space estático de Hugging Face (respaldo si GitHub falla)."""
import os
from huggingface_hub import HfApi

token = os.environ["HF_TOKEN"]
space = os.environ.get("HF_SPACE") or "TU-USUARIO/centinela-familiar"
api = HfApi(token=token)
api.create_repo(space, repo_type="space", space_sdk="static", exist_ok=True)
readme = """---
title: Centinela Familiar
emoji: 🛰️
colorFrom: gray
colorTo: red
sdk: static
pinned: false
---
Respaldo del panel. Datos: status.json · feed.xml
"""
api.upload_file(path_or_fileobj=readme.encode(), path_in_repo="README.md",
                repo_id=space, repo_type="space", commit_message="readme")
api.upload_folder(folder_path="docs", repo_id=space, repo_type="space",
                  commit_message="estado", allow_patterns=["*.json", "*.xml", "*.html", "*.txt", "*.enc"])
print("✓ Respaldo HF:", f"https://huggingface.co/spaces/{space}")

# Copia del CÓDIGO (para que los motores de respaldo puedan actualizarse
# aunque GitHub caiga). Repositorio público sin datos personales.
codigo = space.split("/")[0] + "/centinela-codigo"
api.create_repo(codigo, repo_type="model", exist_ok=True)
api.upload_folder(folder_path=".", repo_id=codigo, repo_type="model", commit_message="código",
                  allow_patterns=["monitor.py", "config.yaml", "requirements.txt", "espejos.txt",
                                  "servidor/*", "mirror_hf.py", "LEEME.md", "panel_cifrado.html"])
print("✓ Código HF:", f"https://huggingface.co/{codigo}")
