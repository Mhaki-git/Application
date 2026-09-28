#!/bin/bash
# Point d'entree macOS : double-cliquable depuis le Finder (extension .command).
# Delegue a startup.sh, partage avec Linux.
cd "$(dirname "$0")"
./startup.sh
