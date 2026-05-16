VERSION ?= $(shell git describe --tags --abbrev=0 2>/dev/null || echo dev)
MODULE := github.com/beastoin/claudecode-telegram/forge

BINARY_NAME := worker-forge
WORKER_TEMPLATE := worker
BUILD_DIR := build

GO := go
PROTOC := protoc

LDFLAGS_RELEASE := -s -w -X $(MODULE).Version=$(VERSION)

PLATFORMS := linux/amd64 linux/arm64 darwin/arm64

.PHONY: build build-worker build-all build-release test test-e2e test-e2e-full test-e2e-fresh test-acceptance test-all clean proto

build:
	@mkdir -p $(BUILD_DIR)
	$(GO) build -o $(BUILD_DIR)/$(BINARY_NAME) ./cmd/worker-forge

build-worker:
	@mkdir -p $(BUILD_DIR)
	$(GO) build -o $(BUILD_DIR)/$(WORKER_TEMPLATE) ./cmd/worker

build-all:
	@mkdir -p $(BUILD_DIR)
	@for platform in $(PLATFORMS); do \
		GOOS=$${platform%/*}; \
		GOARCH=$${platform#*/}; \
		echo "Building $(BINARY_NAME) $$GOOS/$$GOARCH"; \
		CGO_ENABLED=0 GOOS=$$GOOS GOARCH=$$GOARCH $(GO) build \
			-o $(BUILD_DIR)/$(BINARY_NAME)-$$GOOS-$$GOARCH ./cmd/worker-forge; \
		echo "Building $(WORKER_TEMPLATE) $$GOOS/$$GOARCH"; \
		CGO_ENABLED=0 GOOS=$$GOOS GOARCH=$$GOARCH $(GO) build \
			-o $(BUILD_DIR)/$(WORKER_TEMPLATE)-$$GOOS-$$GOARCH ./cmd/worker; \
	done

build-release:
	@mkdir -p $(BUILD_DIR)
	@for platform in $(PLATFORMS); do \
		GOOS=$${platform%/*}; \
		GOARCH=$${platform#*/}; \
		echo "Release $(BINARY_NAME) $$GOOS/$$GOARCH ($(VERSION))"; \
		CGO_ENABLED=0 GOOS=$$GOOS GOARCH=$$GOARCH $(GO) build \
			-ldflags '$(LDFLAGS_RELEASE)' \
			-o $(BUILD_DIR)/$(BINARY_NAME)-$$GOOS-$$GOARCH ./cmd/worker-forge; \
		echo "Release $(WORKER_TEMPLATE) $$GOOS/$$GOARCH ($(VERSION))"; \
		CGO_ENABLED=0 GOOS=$$GOOS GOARCH=$$GOARCH $(GO) build \
			-ldflags '$(LDFLAGS_RELEASE)' \
			-o $(BUILD_DIR)/$(WORKER_TEMPLATE)-$$GOOS-$$GOARCH ./cmd/worker; \
	done

test:
	$(GO) test ./...

test-e2e:
	$(GO) test -tags e2e -run TestE2E ./...

test-e2e-full:
	bash test-e2e-full.sh

test-e2e-fresh:
	bash test-e2e-fresh-home.sh

test-acceptance:
	bash test-acceptance.sh

test-all: test test-e2e-full test-e2e-fresh test-acceptance

clean:
	rm -rf $(BUILD_DIR)

proto:
	$(PROTOC) \
		--go_out=. --go_opt=paths=source_relative \
		--go-grpc_out=. --go-grpc_opt=paths=source_relative \
		proto/bridge.proto
