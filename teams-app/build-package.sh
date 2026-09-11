#!/usr/bin/env bash
# Phase 13: zip the Teams app package for sideloading.
#
# The three files must sit at the ROOT of the zip, not inside a folder --
# Teams rejects the package outright if manifest.json is nested, and the
# error it gives ("app package is invalid") does not say why.
set -euo pipefail
cd "$(dirname "$0")"

python3 -c "import json;json.load(open('manifest.json'));print('manifest.json: valid JSON')"

rm -f translation-panel.zip
zip -q -j translation-panel.zip manifest.json color.png outline.png
echo "built $(pwd)/translation-panel.zip"
unzip -l translation-panel.zip
