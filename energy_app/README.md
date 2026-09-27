# Analyse Energetique -- PV + Batterie (interface locale)

Interface web locale (Streamlit) pour ton moteur d'analyse "fournisseur"
(PV + batterie + dispatch Day-Ahead). Tu remplis un formulaire, l'appli
calcule (a partir de ton fichier Excel modele), et te sort un rapport PDF
propre + les graphiques + le detail annee par annee.

## Installation (une seule fois)

1. Installe Python 3.10+ si ce n'est pas deja fait : https://www.python.org/downloads/
2. Ouvre un terminal dans ce dossier et installe les dependances :

```
pip install -r requirements.txt
```

## Lancer l'appli

```
streamlit run app.py
```

Une page s'ouvre automatiquement dans ton navigateur (en general
http://localhost:8501).

## Partager l'appli avec des collegues (meme reseau / bureau)

Streamlit affiche au demarrage une ligne du type :

```
Network URL: http://192.168.1.42:8501
```

Donne cette adresse a tes collegues connectes au meme reseau (wifi/ethernet
du bureau) : ils pourront ouvrir la meme interface depuis leur navigateur,
sans rien installer. Le calcul tourne sur TON PC (c'est lui le serveur) --
s'il est eteint ou que l'appli est fermee, l'adresse ne fonctionne plus.

Si tes collegues n'y arrivent pas, verifie le pare-feu Windows : il faut
autoriser Python/Streamlit sur les reseaux "prives".

## Utilisation

1. Charge ton fichier Excel modele (.xlsm), celui qui contient les feuilles
   "DONNEES ENERGIE" (conso du client) et "PVS" (profil de production PV).
2. Charge ton fichier de prix Day-Ahead Belpex (.pkl, en EUR/kWh) si tu en as
   un. Sinon l'appli tourne en MODE DEMO (prix simules) -- pratique pour
   tester l'interface, mais NE PAS utiliser ces chiffres pour un client reel.
3. Ajuste les parametres (PV, batterie, prix, marges, hypotheses financieres).
4. Clique sur "Lancer la simulation" -- le calcul complet (jusqu'a 20 ans,
   dispatch optimise jour par jour) peut prendre plusieurs minutes selon
   l'horizon choisi. Une barre de progression indique l'avancement.
5. Regarde les resultats (metriques, graphiques, detail annuel).
6. Genere et telecharge le rapport PDF, le CSV detaille, et/ou une copie de
   l'Excel avec les nouveaux parametres (pour tes archives).

## Important -- fichier Excel

L'appli NE MODIFIE JAMAIS ton fichier Excel original. Les parametres saisis
dans le formulaire sont appliques uniquement en memoire pour le calcul (le
fichier "Excel rempli" telechargeable est une copie generee a part, pour
archive -- il s'ouvre normalement dans Excel qui recalcule tout tout seul).

## Fichiers

- `app.py` -- interface Streamlit (le point d'entree)
- `engine/` -- moteurs de calcul
  - `fournisseur_roi.py` -- dispatch Day-Ahead, ROI, VAN/TRI
  - `financial_sheet.py` -- fiche financiere (financement par emprunt, loyer, CV)
  - `finance_utils.py` -- utilitaires partages (NPV/IRR, PMT/amortissement)
- `io_excel/` -- lecture/ecriture du fichier Excel modele
  - `dataextraction.py` -- lecture des donnees/parametres depuis l'Excel
  - `excel_writer.py` -- generation de la copie Excel "archive" (optionnelle)
  - `excel_schema.py` -- carte des cellules parametres, partagee par les deux fichiers ci-dessus
- `io_sources/` -- sources de donnees externes (PV, prix de marche)
  - `extract_pv_from_excel.py` -- fige le profil PV depuis l'Excel (script a executer une fois)
  - `fetch_belpex.py` -- recupere les prix Belpex Day-Ahead (script a executer une fois par annee)
  - `pv_pvgis.py` -- recuperation du profil PV via l'API PVGIS (alternative a l'Excel)
- `reporting/` -- generation des rapports PDF
  - `pdf_report.py` -- rapport PDF principal (modele fournisseur)
  - `pdf_style.py` -- formatage et style ReportLab partages
- `tests/` -- tests de non-regression (voir section "Tests" ci-dessous)

## Tests

Suite de tests de non-regression sur les moteurs de calcul (`engine/`) --
utile avant tout refactor pour verifier qu'aucun resultat ne change
silencieusement.

```
pip install -r requirements-dev.txt
python -m pytest
```
