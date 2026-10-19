#!/usr/bin/env bash
# Verify the deployed login page and CSS without signing in or changing data.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
web_check
