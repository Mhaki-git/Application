@echo off
cd /d "%~dp0"
setlocal

echo Verification de Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERREUR : Python n'est pas installe ou n'est pas reconnu.
    echo.
    echo Installer Python 3.10 ou plus recent depuis https://www.python.org/downloads/
    echo Important : lors de l'installation, cocher la case "Add python.exe to PATH".
    echo Relancer ensuite ce fichier.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Premier lancement : creation de l'environnement Python local...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERREUR pendant la creation de l'environnement Python.
        echo.
        pause
        exit /b 1
    )
)

if not exist ".setup_ok" (
    echo Installation des dependances, ca peut prendre quelques minutes la premiere fois...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERREUR pendant l'installation des dependances. Verifier la connexion
        echo internet et relancer ce fichier. Si le probleme persiste, contacter le support.
        echo.
        pause
        exit /b 1
    )
    echo. > ".setup_ok"
)

if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
    (
        echo [general]
        echo email = ""
    ) > "%USERPROFILE%\.streamlit\credentials.toml"
)

echo.
echo Lancement de l'application, veuillez patienter quelques secondes...
echo Une page va s'ouvrir automatiquement dans le navigateur par defaut.
echo (Pour arreter l'application : fermer cette fenetre, ou appuyer sur Ctrl+C)
echo.
".venv\Scripts\python.exe" -m streamlit run app.py

pause
