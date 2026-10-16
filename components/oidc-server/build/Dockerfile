# Trivial OIDC server for testing Teleport generic_oidc joining.
# Pure stdlib, so the build needs no module downloads.
#
# The build stage runs on the BUILDER's own platform and cross-compiles to the target
# (linux/amd64). Running the Go toolchain itself emulated is not just slow: under the
# qemu-user emulator buildkitd ships, it crashes outright (`fatal error: lfstack.push`),
# which is what an arm64 k8s node does with an amd64 RUN step.
#
# BuildKit sets BUILDPLATFORM itself (a DEFAULT here would override it, so there is none).
# The docker backend's legacy builder (DOCKER_BUILDKIT=0) does not, so be_docker_build_side_image
# passes it as a build arg: amd64, building under the daemon's emulation (Rosetta in lima)
# as before, which Go survives.
ARG BUILDPLATFORM
FROM --platform=$BUILDPLATFORM golang:1.23-alpine AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY go.mod .
COPY main.go .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -o /oidc-server .

FROM gcr.io/distroless/static-debian12
COPY --from=build /oidc-server /oidc-server
VOLUME ["/data"]
EXPOSE 8443
ENTRYPOINT ["/oidc-server"]
