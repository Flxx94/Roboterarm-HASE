#!/usr/bin/env bash

set -Eeuo pipefail
shopt -s nullglob

DEFAULT_CONFIG=${IMAGE_BUILDER_CONFIG:-basic-config.yaml}
DEFAULT_OUTPUT=${IMAGE_BUILDER_OUTPUT:-raspios-custom.img}
DEFAULT_TEST=${IMAGE_BUILDER_TEST:-n}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  printf 'Fehler: Docker wurde nicht gefunden. Bitte Docker installieren und starten.\n' >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  printf 'Fehler: Docker Compose v2 ist nicht verfügbar.\n' >&2
  exit 1
fi

images=(input/*.img input/*.xz)
if ((${#images[@]} == 0)); then
  printf 'Keine Raspberry-Pi-OS-Images in %s/input gefunden.\n' "$SCRIPT_DIR"
  printf 'Lege dort eine .img- oder .xz-Datei ab und starte das Skript erneut.\n'
  exit 1
fi

printf 'Verfügbare Images:\n'
for i in "${!images[@]}"; do
  printf '  %d) %s\n' "$((i + 1))" "${images[$i]##*/}"
done
default_image=${IMAGE_BUILDER_INPUT:-${images[0]##*/}}
default_image_choice=
for i in "${!images[@]}"; do
  if [[ "${images[$i]##*/}" == "$default_image" ]]; then
    default_image_choice=$((i + 1))
    break
  fi
done
if [[ -z "$default_image_choice" ]]; then
  printf 'Fehler: Standard-Image %s liegt nicht in image-builder/input.\n' "$default_image" >&2
  exit 1
fi
read -r -p "Image auswählen [$default_image_choice]: " image_choice
image_choice=${image_choice:-$default_image_choice}
if [[ ! "$image_choice" =~ ^[0-9]+$ ]] || ((image_choice < 1 || image_choice > ${#images[@]})); then
  printf 'Fehler: ungültige Image-Auswahl.\n' >&2
  exit 1
fi
input_image=${images[$((image_choice - 1))]##*/}

printf '\nKonfigurationsdateien:\n'
config_candidates=(basic-config.yaml minimal-config.yaml examples/*.yaml)
for config in "${config_candidates[@]}"; do
  [[ -f "$config" ]] && printf '  - %s\n' "$config"
done
read -r -p "Config-Pfad relativ zu image-builder [$DEFAULT_CONFIG]: " config_path
config_path=${config_path:-$DEFAULT_CONFIG}
if [[ "$config_path" = /* || "/$config_path/" == *"/../"* || ! -f "$config_path" ]]; then
  printf 'Fehler: Config muss eine vorhandene Datei innerhalb von image-builder sein.\n' >&2
  exit 1
fi

read -r -p "Name der Ausgabedatei [$DEFAULT_OUTPUT]: " output_name
output_name=${output_name:-$DEFAULT_OUTPUT}
if [[ "$output_name" == */* || "$output_name" == *\\* || "$output_name" != *.img ]]; then
  printf 'Fehler: Bitte einen Dateinamen ohne Verzeichnispfad mit der Endung .img angeben.\n' >&2
  exit 1
fi

mkdir -p output input work
if [[ -e "output/$output_name" ]]; then
  read -r -p "${output_name} existiert bereits. Überschreiben? [J/n]: " overwrite
  case "$overwrite" in
    n|N|nein|Nein|NEIN|no|No|NO) printf 'Abgebrochen.\n'; exit 0 ;;
    *) ;;
  esac
fi

read -r -p "Vor dem Fertigstellen eine Test-Shell starten? [$DEFAULT_TEST]: " test_choice
test_choice=${test_choice:-$DEFAULT_TEST}
build_args=(build --input "/input/$input_image" --config "/project/$config_path" --output "/output/$output_name")
case "$test_choice" in
  j|J|ja|Ja|JA|y|Y|yes|Yes|YES) build_args+=(--test) ;;
esac

printf '\nStarte Image-Build:\n  Input:  %s\n  Config: %s\n  Output: %s\n\n' \
  "$input_image" "$config_path" "output/$output_name"

# MSYS_NO_PATHCONV keeps Git Bash from rewriting the container's /input and
# /project paths on Windows. It is harmless on macOS and Linux.
MSYS_NO_PATHCONV=1 docker compose -f compose.yaml run --rm --build builder \
  "${build_args[@]}"
