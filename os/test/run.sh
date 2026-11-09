#!/usr/bin/env bash
# Build a Debian 12 container with the node's packages and run the smoke test.
# Screenshots land in os/test/out/. Needs Docker. Runs natively on arm64 (Pi-like) Macs.
set -euo pipefail
cd "$(dirname "$0")/../.."
docker build -f os/test/Dockerfile -t xos-node-test .
mkdir -p os/test/out
docker run --rm -v "$PWD/os/test/out:/out" xos-node-test
