#!/bin/bash
# Installation et lancement de l'application (macOS / Linux).
set -e
cd "$(dirname "$0")"

echo "Verification de Python..."
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo ""
    echo "ERREUR : Python n'est pas installe ou n'est pas reconnu."
    echo ""
    echo "Installer Python 3.10 ou plus recent :"
    echo "  - macOS : https://www.python.org/downloads/ (ou 'brew install python')"
    echo "  - Linux : via le gestionnaire de paquets de la distribution (ex: apt install python3 python3-venv)"
    echo ""
    read -p "Appuyer sur Entree pour fermer..."
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Premier lancement : creation de l'environnement Python local..."
    if ! "$PYTHON_BIN" -m venv .venv; then
        echo ""
        echo "ERREUR pendant la creation de l'environnement Python."
        echo ""
        read -p "Appuyer sur Entree pour fermer..."
        exit 1
    fi
fi

if [ ! -f ".setup_ok" ]; then
    echo "Installation des dependances, ca peut prendre quelques minutes la premiere fois..."
    ".venv/bin/python" -m pip install --upgrade pip >/dev/null
    if ! ".venv/bin/python" -m pip install -r requirements.txt; then
        echo ""
        echo "ERREUR pendant l'installation des dependances. Verifier la connexion"
        echo "internet et relancer ce fichier. Si le probleme persiste, contacter le support."
        echo ""
        read -p "Appuyer sur Entree pour fermer..."
        exit 1
    fi
    touch ".setup_ok"
fi

if [ ! -f "$HOME/.streamlit/credentials.toml" ]; then
    mkdir -p "$HOME/.streamlit"
    printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"
fi

echo ""
echo "Lancement de l'application, veuillez patienter quelques secondes..."
echo "Une page va s'ouvrir automatiquement dans le navigateur par defaut."
echo "(Pour arreter l'application : fermer cette fenetre, ou appuyer sur Ctrl+C)"
echo ""
".venv/bin/python" -m streamlit run app.py
