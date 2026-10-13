set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

uv := if path_exists(".tools/bin/uv") == "true" { ".tools/bin/uv" } else { "uv" }

default:
    @just --list

setup:
    UV_CACHE_DIR=.tools/uv-cache {{uv}} sync --locked

check:
    {{uv}} run --no-sync ruff check src tests
    {{uv}} run --no-sync ruff format --check src tests

test:
    QT_QPA_PLATFORM=offscreen {{uv}} run --no-sync pytest

inspect input:
    {{uv}} run --no-sync marker-screen inspect {{quote(input)}}

analyze input output *args:
    {{uv}} run --no-sync marker-screen analyze {{quote(input)}} --output {{quote(output)}} {{args}}

gui output *args:
    {{uv}} run --no-sync marker-web {{quote(output)}} {{args}}

gui-native output *args:
    {{uv}} run --no-sync marker-gui {{quote(output)}} {{args}}

test-web output screenshot *args:
    UV_CACHE_DIR=.tools/uv-cache {{uv}} run --locked --extra browser-test python tests/browser_review.py {{quote(output)}} --screenshot {{quote(screenshot)}} {{args}}
