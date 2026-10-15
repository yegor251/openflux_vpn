#!/bin/bash
# Update the OpenFlux reference clone and rebuild everything we take from it:
#   bin/openflux        macOS client + link generator (make-link/parse-link)
#   bin/openflux-linux  Linux build (scp to servers if needed)
#   deploy/node-install.sh  re-forked from the fresh upstream (make_fork.py)
#
# The VPS node core is NOT updated here: openflux-node-update.timer does
# that on the server itself (node-v* releases, SHA-256 pinned, with
# rollback).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d OpenFlux/.git ]; then
    git clone --depth 1 https://github.com/p1neappleXpress/OpenFlux.git OpenFlux
fi
git -C OpenFlux fetch --depth 1 origin main
git -C OpenFlux reset --hard FETCH_HEAD
echo "upstream: $(git -C OpenFlux log --oneline -1)"

(cd OpenFlux && go build -o ../bin/openflux .)
(cd OpenFlux && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -trimpath -o ../bin/openflux-linux .)

python3 deploy/make_fork.py
sh -n deploy/node-install.sh

echo "OK: bin/openflux ($(./bin/openflux --version 2>/dev/null | head -1 || echo built)) and bin/openflux-linux rebuilt; installer fork refreshed"
