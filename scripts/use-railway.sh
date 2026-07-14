#!/usr/bin/env bash

# Source this file from the repository root:
#   source scripts/use-railway.sh

railway_env_file="${BIDLENS_RAILWAY_ENV_FILE:-.env.railway.local}"

if [ ! -f "$railway_env_file" ]; then
  echo "BidLens developer environment"
  echo "Could not find $railway_env_file."
  echo "Create it from .env.railway.example and add your Railway PostgreSQL credentials:"
  echo "  cp .env.railway.example .env.railway.local"
  return 1 2>/dev/null || exit 1
fi

set -a
# shellcheck disable=SC1090
. "$railway_env_file"
set +a

if [ -z "${DATABASE_URL:-}" ]; then
  echo "BidLens developer environment"
  echo "$railway_env_file does not define DATABASE_URL."
  return 1 2>/dev/null || exit 1
fi

export DATABASE_URL
export AUTO_CREATE_SCHEMA=false
export ENABLE_INTERNAL_SCHEDULER=false

echo "BidLens developer environment"
echo "Database: Railway PostgreSQL"
echo "Scheduler: Disabled"
