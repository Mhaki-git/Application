# Analyse Energetique -- PV + Batterie (interface locale)

Interface web locale (Streamlit) pour le moteur d'analyse "fournisseur"
(PV + batterie + dispatch Day-Ahead). L'utilisateur renseigne un
formulaire (consommation, production PV, prix de marche, hypotheses),
l'application effectue le calcul et produit un rapport PDF, des
graphiques et le detail annuel.

## Installation et lancement

**Etape 1 -- Installation de Python (une seule fois)**

1. Se rendre sur https://www.python.org/downloads/ et telecharger Python.
2. Executer le fichier telecharge.
3. **Windows** : sur le premier ecran de l'installateur, cocher la case
   *"Add python.exe to PATH"* avant de cliquer sur "Install Now".
4. **macOS** : l'installateur officiel convient, ou `brew install python`
   pour les utilisateurs de Homebrew.
5. **Linux** : Python 3 est generalement deja installe ; sinon, utiliser le
   gestionnaire de paquets de la distribution (ex : `apt install python3
   python3-venv` sur Debian/Ubuntu).

**Etape 2 -- Lancement de l'application**

- **Windows** : double-cliquer sur `startup.bat` (dans ce dossier).
- **macOS** : double-cliquer sur `startup.command`. Si le systeme bloque
  l'ouverture (fichier telecharge depuis une source non identifiee),
  clic droit puis "Ouvrir".
- **Linux** : executer `./startup.sh` depuis un terminal ouvert dans ce
  dossier (`chmod +x startup.sh` si necessaire au prealable).

Dans tous les cas :

- Au premier lancement, une fenetre de terminal s'ouvre et installe les
  dependances necessaires (quelques minutes).
- Aux lancements suivants, une page s'ouvre automatiquement dans le
  navigateur par defaut.
- Pour arreter l'application : fermer la fenetre de terminal.

Si un message indique que Python n'est pas installe, verifier l'etape 1
(sous Windows, la case "Add to PATH" en particulier), puis relancer.

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

En cas d'echec de connexion, verifier le pare-feu du poste hote : Python /
Streamlit doit etre autorise sur le reseau local (reseaux "prives" sous
Windows).

## Utilisation

1. Renseigner la consommation du client : soit un releve CSV (horodatage +
   valeur, pas de temps quelconque detecte automatiquement), soit un
   profil-type integre mis a l'echelle sur une consommation annuelle
   cible.
2. Renseigner la production PV : profil fige integre (extrait au
   prealable depuis l'Excel modele) ou recuperation en direct via
   l'API PVGIS.
3. En mode "Fournisseur principal", renseigner les prix Day-Ahead
   Belpex : annee integree, fichier CSV personnel (horodatage + prix en
   EUR/kWh), ou mode demonstration (prix simules -- a ne pas utiliser
   pour un client reel).
4. Ajuster les parametres (PV, batterie, prix, marges, hypotheses
   financieres).
5. Lancer la simulation. Le calcul complet (jusqu'a 20 ans, dispatch
   optimise jour par jour) peut prendre plusieurs minutes selon l'horizon
   choisi ; une barre de progression indique l'avancement.
6. Consulter les resultats (indicateurs, graphiques, detail annuel).
7. En mode "Fournisseur secondaire" uniquement : completer et generer la
   "Fiche financiere" (financement par emprunt, loyer, maintenance,
   certificats verts -- reproduit l'onglet Excel correspondant, champs
   pre-remplis depuis la simulation).
8. Generer et telecharger le rapport PDF et le CSV detaille.

Le fichier Excel modele (.xlsm, feuilles "DONNEES ENERGIE" et "PVS")
n'est pas charge depuis l'interface : il sert de source, une seule fois
en amont, aux scripts `io_sources/extract_pv_from_excel.py` et
`io_sources/extract_conso_profiles_from_excel.py` qui figent les profils
utilises ensuite par l'application (voir "Structure du projet").

## Fichier Excel source

L'application ne modifie jamais le fichier Excel original. Les parametres
saisis dans le formulaire sont appliques uniquement en memoire pour le
calcul et ne sont jamais reecrits dans le fichier source.

## Structure du projet

- `app.py` -- interface Streamlit (point d'entree)
- `startup.bat` / `startup.sh` / `startup.command` -- installation et lancement (Windows / Linux / macOS)
- `engine/` -- moteurs de calcul
  - `fournisseur_roi.py` -- dispatch Day-Ahead, ROI, VAN/TRI
  - `financial_sheet.py` -- fiche financiere (financement par emprunt, loyer, CV)
  - `finance_utils.py` -- utilitaires partages (NPV/IRR, PMT/amortissement)
- `io_excel/` -- lecture/ecriture du fichier Excel modele
  - `dataextraction.py` -- lecture des donnees/parametres depuis l'Excel
  - `excel_writer.py` -- generation d'une copie Excel parametree ; non utilise par l'interface actuelle (voir docstring du module)
  - `excel_schema.py` -- carte des cellules parametres, partagee par les deux modules ci-dessus
- `io_sources/` -- sources de donnees externes (PV, consommation, prix de marche)
  - `extract_pv_from_excel.py` -- extraction du profil PV depuis l'Excel (script a executer une fois)
  - `extract_conso_profiles_from_excel.py` -- extraction des profils-types de consommation depuis l'Excel (script a executer une fois)
  - `fetch_belpex.py` -- recuperation des prix Belpex Day-Ahead (script a executer une fois par an)
  - `pv_pvgis.py` -- recuperation du profil PV via l'API PVGIS (alternative a l'Excel)
- `reporting/` -- generation des rapports PDF
  - `pdf_report.py` -- rapport PDF principal, adapte au mode de vente actif
  - `pdf_style.py` -- formatage et style ReportLab partages
- `tests/` -- tests de non-regression (voir section "Tests")

## Tests

Suite de tests de non-regression sur les moteurs de calcul (`engine/`),
destinee a verifier l'absence de regression silencieuse avant tout
refactoring. Necessite l'environnement Python cree par le script de
lancement (`.venv/`).

Windows :
```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

macOS / Linux :
```
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
