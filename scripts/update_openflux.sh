#!/bin/bash
# Build and ship the OpenFlux core from OUR fork (yegor251/OpenFlux).
#
# The fork's main mirrors upstream and is NEVER built from; our work
# lives in branches (e.g. feature/direct-lane-guard = PR #212).
#
#   update_openflux.sh sync  <branch>  fetch upstream/main, rebase the
#                                      branch onto it, force-push to the
#                                      fork (an open PR updates itself);
#                                      conflicts abort loudly - resolve by
#                                      hand in ~/PycharmProjects/OpenFlux-contrib
#   update_openflux.sh build <branch>  refresh the throwaway OpenFlux/
#                                      checkout from fork@branch, build
#                                      bin/openflux(+.linux), re-fork the
#                                      installer, stamp bin/.core-build-info
#   update_openflux.sh deploy          upload the built core, switch the
#                                      openflux symlink (previous target =
#                                      rollback), restart all channel
#                                      units, verify
#   update_openflux.sh all   <branch>  sync + build (deploy stays explicit)
set -euo pipefail
cd "$(dirname "$0")/.."

FORK="${OFLUX_FORK:-yegor251/OpenFlux}"
UPSTREAM="p1neappleXpress/OpenFlux"
CONTRIB="${HOME}/PycharmProjects/OpenFlux-contrib"
VPS="${OFLUX_VPS:-root@159.194.227.248}"
NODE_DIR="/opt/openflux-node/bin"

ensure_contrib() {
    if [ ! -d "$CONTRIB/.git" ]; then
        git clone "https://github.com/${FORK}.git" "$CONTRIB"
        git -C "$CONTRIB" remote add upstream "https://github.com/${UPSTREAM}.git"
    fi
    git -C "$CONTRIB" fetch origin --prune
    git -C "$CONTRIB" fetch upstream
}

cmd_sync() {
    local branch="$1"
    ensure_contrib
    git -C "$CONTRIB" checkout "$branch" 2>/dev/null \
        || git -C "$CONTRIB" checkout -b "$branch" "origin/$branch"
    if ! git -C "$CONTRIB" rebase upstream/main; then
        git -C "$CONTRIB" rebase --abort || true
        echo "REBASE CONFLICT: '$branch' vs upstream/main." >&2
        echo "Resolve by hand: cd $CONTRIB && git rebase upstream/main" >&2
        exit 1
    fi
    git -C "$CONTRIB" push --force-with-lease origin "$branch"
    echo "sync ok: $branch = $(git -C "$CONTRIB" rev-parse --short HEAD) on upstream/main $(git -C "$CONTRIB" rev-parse --short upstream/main)"
}

cmd_build() {
    local branch="$1"
    rm -rf OpenFlux
    git clone --depth 1 -b "$branch" "https://github.com/${FORK}.git" OpenFlux
    echo "source: $(git -C OpenFlux log --oneline -1)"
    (cd OpenFlux && go build -o ../bin/openflux .)
    (cd OpenFlux && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -trimpath -o ../bin/openflux-linux .)
    python3 deploy/make_fork.py
    sh -n deploy/node-install.sh
    {
        echo "branch=$branch"
        echo "commit=$(git -C OpenFlux rev-parse HEAD)"
        echo "built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > bin/.core-build-info
    echo "build ok: bin/openflux, bin/openflux-linux ($(./bin/openflux --version 2>/dev/null | head -1 || echo built))"
}

cmd_deploy() {
    if [ ! -f bin/.core-build-info ]; then
        echo "nothing built yet - run: $0 build <branch>" >&2
        exit 1
    fi
    local sha stamp prev
    sha="$(sed -n 's/^commit=//p' bin/.core-build-info | cut -c1-8)"
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    echo "deploying: $(tr '\n' ' ' < bin/.core-build-info)"
    scp -q bin/openflux-linux "$VPS:$NODE_DIR/openflux-$sha"
    scp -q bin/.core-build-info "$VPS:/opt/openflux-node/CORE-VERSION"
    ssh "$VPS" "
        set -e
        cd $NODE_DIR
        prev=\$(readlink openflux 2>/dev/null || echo '(plain file)')
        chmod 755 openflux-$sha
        ln -sfn openflux-$sha openflux
        echo \"symlink: openflux -> openflux-$sha (was: \$prev)\"
        systemctl list-units 'openflux-node@*' --no-legend | awk '{print \$1}' | xargs -r systemctl restart
        sleep 2
        systemctl list-units 'openflux-node@*' --no-legend
        echo \"rollback: ssh $VPS 'ln -sfn \$prev openflux && systemctl restart openflux-node@*' (prev target this deploy: \$prev)\"
    "
}

case "${1:-}" in
    sync)   shift; cmd_sync "${1:?usage: $0 sync <branch>}" ;;
    build)  shift; cmd_build "${1:?usage: $0 build <branch>}" ;;
    deploy) cmd_deploy ;;
    all)    shift; branch="${1:?usage: $0 all <branch>}"; cmd_sync "$branch"; cmd_build "$branch" ;;
    *)
        echo "usage: $0 sync <branch> | build <branch> | deploy | all <branch>" >&2
        exit 2
        ;;
esac
