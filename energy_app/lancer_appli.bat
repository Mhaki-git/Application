@echo off
cd /d "%~dp0"
echo Lancement de l'appli, patiente quelques secondes...
python -m streamlit run app.py
pause
