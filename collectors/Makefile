SHELL := /bin/bash
BINARIES := prom-collector loki-collector kube-collector
BIN_DIR := bin

.PHONY: all lint test build run clean tidy

all: lint test build

lint:
	golangci-lint run ./...

test:
	go test ./... -race -count=1

build: $(BIN_DIR)
	@for name in $(BINARIES); do \
		echo "→ building $$name"; \
		go build -o $(BIN_DIR)/$$name ./cmd/$$name; \
	done

$(BIN_DIR):
	@mkdir -p $(BIN_DIR)

run:
	@echo "Runs the three collectors on :8001 / :8002 / :8003"
	@trap 'kill 0' INT; \
		go run ./cmd/prom-collector --addr :8001 & \
		go run ./cmd/loki-collector --addr :8002 & \
		go run ./cmd/kube-collector --addr :8003 & \
		wait

tidy:
	go mod tidy

clean:
	rm -rf $(BIN_DIR)
