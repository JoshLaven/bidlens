#!/usr/bin/env bash

# Source this file from the repository root:
#   source scripts/use-local.sh

export DATABASE_URL="sqlite:///./bidlens.db"
export AUTO_CREATE_SCHEMA=true
export ENABLE_INTERNAL_SCHEDULER=true
export BIDLENS_VALIDATE_DEPLOYMENT=false
export SESSION_COOKIE_SECURE=false

echo "BidLens developer environment"
echo "Database: Local SQLite"
echo "Scheduler: Enabled"
echo "Hosted deployment validation: Disabled"
