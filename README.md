# Dofus Resource Assistant — 1.0.7

Overlay Windows pour le suivi local des ressources de Dofus et la lecture OCR des coordonnées.



Fonctionnalités

* OCR des coordonnées sur une zone configurable de l'interface Dofus.
* Validation des coordonnées avec la base locale `data/resource\\\_map.sqlite`.
* Conservation de la dernière carte confirmée pendant les lectures OCR temporaires ou les transitions de carte.
* Réinitialisation complète de la détection depuis les réglages.
* Recherche des ressources sur la carte courante et dans la distance configurée.
* Overlay déplaçable/redimensionnable et menu Réglages.
* Fenêtre Réglages centrée sur le moniteur qui contient l'overlay, y compris en double écran.
* Raccourcis globaux configurables via `config.txt`.
* Icône de zone de notification avec possibilité de quitter l'application.

## Raccourcis

Le fichier `config.txt` contient :

```text
SETTINGS\\\_KEY=F9
TOGGLE\\\_KEY=F8
```

* `SETTINGS\\\_KEY` ouvre ou ferme les réglages.
* `TOGGLE\\\_KEY` affiche ou masque l'overlay.
* Le fichier est surveillé automatiquement, sans redémarrage.
* Les touches affichées dans l'overlay sont synchronisées avec les raccourcis actifs.
* Les deux raccourcis doivent être différents.
* Les touches courantes (`F1-F12`, `A-Z`, `0-9`, `ESC`, `SPACE`, `ENTER`, `TAB`, flèches, `HOME`, `END`, pavé numérique, etc.) sont prises en charge.

L'installateur conserve un `config.txt` déjà présent lors d'une mise à jour.

## Architecture

* `overlay.py` : interface, hotkeys, réglages, affichage et boucle OCR.
* `detector/coordinates.py` : capture, prétraitement et validation OCR.
* `local\\\_resource\\\_db.py` : accès en lecture seule à la base de ressources fournie.
* `database.py` : stockage persistant des préférences de l'utilisateur uniquement.
* `data/resource\\\_map.sqlite` : dataset local immuable utilisé par l'application.
* `config.txt` : configuration utilisateur des raccourcis globaux.
* `launcher.py` : démarrage et journalisation des erreurs de lancement.

## Installation / compilation

L'installateur Inno Setup est défini dans `installer.iss`. Le script `build-installer.ps1` télécharge les runtimes nécessaires, puis génère :

`dist\\\\Dofus-Resource-Assistant-Setup-1.0.7.exe`

Le setup final embarque Python et Tesseract. Sur le PC utilisateur, l'installation ne nécessite donc **ni WinGet, ni Python, ni Tesseract préinstallés**.

Les dépendances Python sont ensuite installées dans un environnement local `.venv`. Une connexion Internet est nécessaire pendant cette étape pour récupérer les paquets Python.

### Prérequis du PC de développement

* Windows x64
* PowerShell 5.1+
* Inno Setup 7 (le script peut l'installer via WinGet si nécessaire)
* Accès Internet pour télécharger Python 3.12.10 et Tesseract 5.5.3 lors du build

