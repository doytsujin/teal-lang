#!/usr/bin/env bash
# Clean venv and re-install all packages
#
# Useful to confirm that everything is correctly represented in pyproject.toml

set -x
set -e

pip freeze | grep -v "^-e" | xargs pip uninstall -y
pip uninstall teal

poetry install
