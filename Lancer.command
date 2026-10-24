#!/bin/zsh
# Double-cliquez sur ce fichier pour ouvrir l'outil de réparation.
# Le mot de passe du Mac sera demandé au moment de lire la clé.
cd "$(dirname "$0")"
"$(command -v python3)" gui.py
