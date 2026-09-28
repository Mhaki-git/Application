# Analyse Energetique -- PV + Batterie (interface locale)

Interface web locale (Streamlit) pour le moteur d'analyse "fournisseur"
(PV + batterie + dispatch Day-Ahead). L'utilisateur renseigne un
formulaire, l'application effectue le calcul a partir du fichier Excel
modele, et produit un rapport PDF, des graphiques et le detail annuel.

## Installation et lancement

**Etape 1 -- Installation de Python (une seule fois)**

1. Se rendre sur https://www.python.org/downloads/ et telecharger Python.
2. Executer le fichier telecharge.
3. Sur le premier ecran de l'installateur, cocher la case *"Add python.exe
   to PATH"* avant de cliquer sur "Install Now".

**Etape 2 -- Lancement de l'application**

Double-cliquer sur `startup.bat` (dans ce dossier).

- Au premier lancement, une fenetre de terminal s'ouvre et installe les
  dependances necessaires (quelques minutes).
- Aux lancements suivants, une page s'ouvre automatiquement dans le
  navigateur par defaut.
- Pour arreter l'application : fermer la fenetre de terminal.

Si un message indique que Python n'est pas installe, verifier que la case
"Add to PATH" a bien ete cochee a l'etape 1, puis relancer.

## Partage sur le reseau local

Streamlit affiche au demarrage une ligne du type :

```
Network URL: http://192.168.1.42:8501
```

Cette adresse peut etre communiquee a des collegues connectes au meme
reseau : ils pourront acceder a la meme interface depuis leur navigateur,
sans installation locale. Le calcul s'execute sur le poste hote (qui fait
office de serveur) -- l'adresse cesse de fonctionner si ce poste est
eteint ou si l'application est fermee.

En cas d'echec de connexion, verifier le pare-feu Windows : Python /
Streamlit doit etre autorise sur les reseaux "prives".

## Utilisation

1. Charger le fichier Excel modele (.xlsm) contenant les feuilles
   "DONNEES ENERGIE" (consommation client) et "PVS" (profil de production
   PV).
2. Charger le fichier de prix Day-Ahead Belpex (.pkl, en EUR/kWh) si
   disponible. A defaut, l'application fonctionne en mode demonstration
   (prix simules) -- a ne pas utiliser pour un client reel.
3. Ajuster les parametres (PV, batterie, prix, marges, hypotheses
   financieres).
4. Lancer la simulation. Le calcul complet (jusqu'a 20 ans, dispatch
   optimise jour par jour) peut prendre plusieurs minutes selon l'horizon
   choisi ; une barre de progression indique l'avancement.
5. Consulter les resultats (indicateurs, graphiques, detail annuel).
6. Generer et telecharger le rapport PDF, le CSV detaille, et/ou une copie
   de l'Excel avec les parametres utilises (pour archivage).

## Fichier Excel source

L'application ne modifie jamais le fichier Excel original. Les parametres
saisis dans le formulaire sont appliques uniquement en memoire pour le
calcul ; le fichier "Excel rempli" telechargeable est une copie generee
separement, destinee a l'archivage.

## Structure du projet

- `app.py` -- interface Streamlit (point d'entree)
- `engine/` -- moteurs de calcul
  - `fournisseur_roi.py` -- dispatch Day-Ahead, ROI, VAN/TRI
  - `financial_sheet.py` -- fiche financiere (financement par emprunt, loyer, CV)
  - `finance_utils.py` -- utilitaires partages (NPV/IRR, PMT/amortissement)
- `io_excel/` -- lecture/ecriture du fichier Excel modele
  - `dataextraction.py` -- lecture des donnees/parametres depuis l'Excel
  - `excel_writer.py` -- generation de la copie Excel d'archive (optionnelle)
  - `excel_schema.py` -- carte des cellules parametres, partagee par les deux modules ci-dessus
- `io_sources/` -- sources de donnees externes (PV, consommation, prix de marche)
  - `extract_pv_from_excel.py` -- extraction du profil PV depuis l'Excel (script a executer une fois)
  - `extract_conso_profiles_from_excel.py` -- extraction des profils-types de consommation depuis l'Excel (script a executer une fois)
  - `fetch_belpex.py` -- recuperation des prix Belpex Day-Ahead (script a executer une fois par an)
  - `pv_pvgis.py` -- recuperation du profil PV via l'API PVGIS (alternative a l'Excel)
- `reporting/` -- generation des rapports PDF
  - `pdf_report.py` -- rapport PDF principal (modele fournisseur)
  - `pdf_style.py` -- formatage et style ReportLab partages
- `tests/` -- tests de non-regression (voir section "Tests")

## Tests

Suite de tests de non-regression sur les moteurs de calcul (`engine/`),
destinee a verifier l'absence de regression silencieuse avant tout
refactoring. Necessite l'environnement Python cree par `startup.bat`
(`.venv/`).

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```
