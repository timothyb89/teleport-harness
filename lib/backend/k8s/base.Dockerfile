# The teleport image's base on the k8s backend: the same runtime the docker backend builds
# (lib/backend/docker.sh), minus the binaries, which `crane append` adds as one layer each.
# Built by buildkitd and tagged by this file's sha256, so editing it rebuilds the base.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl openssl bash jq \
 && rm -rf /var/lib/apt/lists/*
