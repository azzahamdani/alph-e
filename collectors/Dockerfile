# syntax=docker/dockerfile:1.6
#
# Single image, three binaries. Each Deployment picks its binary via `command:`.
# Keeps the build pipeline simple (one `docker build`, one push) while still
# letting us run the collectors as independent Deployments.
#
# Build:  docker build -t registry.localhost:5000/devops-collectors:latest collectors
# Run:    docker run --rm -p 8001:8001 \
#           registry.localhost:5000/devops-collectors:latest prom-collector --addr :8001

ARG GO_VERSION=1.22

FROM golang:${GO_VERSION}-alpine AS builder

WORKDIR /src

# Cache-friendly dep download.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    go mod download

COPY . .

ENV CGO_ENABLED=0 GOOS=linux

RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    set -eux; \
    for bin in prom-collector loki-collector kube-collector; do \
        go build -ldflags="-s -w" -o "/out/$bin" "./cmd/$bin"; \
    done


FROM gcr.io/distroless/static-debian12:nonroot AS runtime

COPY --from=builder /out/prom-collector /usr/local/bin/prom-collector
COPY --from=builder /out/loki-collector /usr/local/bin/loki-collector
COPY --from=builder /out/kube-collector /usr/local/bin/kube-collector

USER nonroot:nonroot

# No default ENTRYPOINT — each Deployment sets `command: [<collector-name>]`.
# When run directly, print the available binaries and exit.
ENTRYPOINT []
CMD ["/usr/local/bin/prom-collector", "--help"]
