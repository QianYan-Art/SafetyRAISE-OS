#!/bin/sh
set -eu

RUNTIME_ROOT="/app/backend/data"

mkdir -p "${RUNTIME_ROOT}/backup"
mkdir -p "${RUNTIME_ROOT}/chat_sessions"
mkdir -p "${RUNTIME_ROOT}/input_generation"
mkdir -p "${RUNTIME_ROOT}/output"
mkdir -p "${RUNTIME_ROOT}/runtime/uploads"

if [ ! -f "${RUNTIME_ROOT}/input_accident.json" ]; then
  printf '{\n}\n' > "${RUNTIME_ROOT}/input_accident.json"
fi

exec "$@"
