.PHONY: build test lint install clean

build:
	go build -o bin/gdelt-pp-cli ./cmd/gdelt-pp-cli

test:
	go test ./...

lint:
	golangci-lint run

install:
	go install ./cmd/gdelt-pp-cli

clean:
	rm -rf bin/

build-mcp:
	go build -o bin/gdelt-pp-mcp ./cmd/gdelt-pp-mcp

install-mcp:
	go install ./cmd/gdelt-pp-mcp

build-all: build build-mcp
