#!/bin/sh
# Opens the sandbox account page in your browser.
cd "$(dirname "$0")" || exit 1
exec python3 -m webapp
