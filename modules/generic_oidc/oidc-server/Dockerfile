# Trivial OIDC server for testing Teleport generic_oidc joining.
# Pure stdlib, so the build needs no module downloads.
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod .
COPY main.go .
RUN CGO_ENABLED=0 go build -o /oidc-server .

FROM gcr.io/distroless/static-debian12
COPY --from=build /oidc-server /oidc-server
VOLUME ["/data"]
EXPOSE 8443
ENTRYPOINT ["/oidc-server"]
