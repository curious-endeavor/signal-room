.PHONY: build-gdelt clean-gdelt test

# Build the vendored gdelt-pp-cli binary into bin/. The signal-room fetcher
# discovers it automatically via its resolver chain.
build-gdelt:
	@mkdir -p bin
	cd vendor/gdelt-pp-cli-src && go build -o ../../bin/gdelt-pp-cli ./cmd/gdelt-pp-cli
	@echo "built: bin/gdelt-pp-cli"

clean-gdelt:
	rm -f bin/gdelt-pp-cli

test:
	python3 -m unittest discover tests
