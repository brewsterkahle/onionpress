.PHONY: help images images-multiarch dev-up dev-down extension icons \
        dmg deb build build-simple test test-unit doctor clean clean-cache install

help:
	@echo "OnionPress Build System"
	@echo ""
	@echo "Everything OnionPress ships can be rebuilt locally. See docs/BUILDING.md."
	@echo ""
	@echo "Containers (any OS, needs docker + buildx):"
	@echo "  make images       - Build the tor + wordpress images from this repo"
	@echo "  make dev-up       - Run the stack on the images you just built"
	@echo "  make dev-down     - Stop it (keeps your data volumes)"
	@echo ""
	@echo "Packages:"
	@echo "  make dmg          - Build the macOS installer  (macOS only)"
	@echo "  make deb          - Build the Linux package    (any OS)"
	@echo "  make extension    - Build the Chrome + Firefox extensions"
	@echo "  make icons        - Regenerate the app icons   (macOS only)"
	@echo ""
	@echo "Checks:"
	@echo "  make doctor       - Report which build tools you have and lack"
	@echo "  make test         - Check the source layout is intact"
	@echo "  make test-unit    - Run the Python unit tests"
	@echo ""
	@echo "Housekeeping:"
	@echo "  make clean        - Remove build outputs"
	@echo "  make clean-cache  - Also drop build/.cache (costs 3-5 min next build)"
	@echo "  make install      - Copy OnionPress.app to /Applications (testing)"
	@echo ""

# ─── Containers ─────────────────────────────────────────────────────────

images:
	./build/build-images.sh

# Multi-arch requires --push, because Docker cannot load a multi-platform
# result into the local image store. Cross-building the tor image runs its
# apt and mkp224o steps under QEMU (hours back when it also compiled arti) —
# CI builds on two native runners instead. Set REGISTRY to your own namespace.
images-multiarch:
	@if [ -z "$(REGISTRY)" ]; then \
		echo "ERROR: set REGISTRY, e.g. make images-multiarch REGISTRY=ghcr.io/you"; \
		echo "       (multi-arch must be pushed; Docker cannot load it locally)"; \
		exit 1; \
	fi
	./build/build-images.sh --platform linux/amd64,linux/arm64 --push --registry $(REGISTRY)

dev-up:
	./build/dev-up.sh

dev-down:
	./build/dev-up.sh --down

# ─── Packages ───────────────────────────────────────────────────────────

dmg:
	./build/build-dmg-simple.sh

deb:
	./build/build-linux.sh

extension:
	./build/build-extension.sh

icons:
	./build/make-icons.sh

# `build` and `build-simple` used to be different scripts. build/build-dmg.sh
# was dead and damaging — it packaged a bundle at the never-produced lowercase
# path onionpress.app (which resolves to the real one on case-insensitive
# APFS), thinned its universal binaries to arm64-only, and then failed on a
# missing background image. Both names now run the real builder.
build: dmg
build-simple: dmg

# ─── Checks ─────────────────────────────────────────────────────────────

doctor:
	@./build/doctor.sh

test:
	@echo "Testing source layout..."
	@echo "Checking structure..."
	@test -d app/MacOS || (echo "ERROR: app/MacOS directory missing" && exit 1)
	@test -f app/MacOS/launcher-wrapper.swift || (echo "ERROR: launcher-wrapper.swift missing" && exit 1)
	@test -f app/MacOS/onionpress || (echo "ERROR: onionpress script missing" && exit 1)
	@test -f app/Info.plist || (echo "ERROR: Info.plist missing" && exit 1)
	@test -f app/Resources/docker/docker-compose.yml || (echo "ERROR: docker-compose.yml missing" && exit 1)
	@test -f src/menubar.py || (echo "ERROR: src/menubar.py missing" && exit 1)
	@test -f src/onionpress/key_manager.py || (echo "ERROR: src/onionpress/key_manager.py missing" && exit 1)
	@echo "All required source files present"
	@echo ""
	@echo "Checking permissions..."
	@test -x app/MacOS/onionpress || (echo "ERROR: onionpress not executable" && exit 1)
	@echo "Permissions correct"
	@echo ""
	@echo "Checking image pins are in sync..."
	@./build/refresh-image-digests.sh --check >/dev/null || (echo "ERROR: image pins have drifted — see build/refresh-image-digests.sh --check" && exit 1)
	@echo "Image pins consistent"
	@echo ""
	@echo "Source layout is valid!"
	@echo "To build: make dmg"

test-unit:
	@# Run the Python unit tests under a pinned 3.14 via uv. Some modules
	@# in src/onionpress/ use `X | None` syntax that fails to import on
	@# stock /usr/bin/python3 (3.9) on macOS — uv fetches an isolated
	@# 3.14 into ~/.local/share/uv/ without touching system Python.
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "ERROR: 'uv' is not installed."; \
		echo "Install with:  curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		echo "          or:  brew install uv"; \
		echo ""; \
		echo "Or run directly against any Python 3.10+:"; \
		echo "  python3 -m unittest discover tests -p 'test_*.py'"; \
		exit 1; \
	fi
	uv run --python 3.14 python -m unittest discover tests -p 'test_*.py'

# ─── Housekeeping ───────────────────────────────────────────────────────

clean:
	@echo "Cleaning build artifacts..."
	rm -f build/*.dmg build/temp.dmg build/*.deb
	rm -rf OnionPress.app build/dist py2app_build py2app_dist
	@echo "Build artifacts cleaned"
	@echo "(build/.cache kept — dropping it costs 3-5 min on the next DMG build;"
	@echo " use 'make clean-cache' if you really want it gone)"

clean-cache: clean
	rm -rf build/.cache
	@echo "Binary download cache cleared"

install:
	@echo "Installing to /Applications..."
	@if [ -d "/Applications/OnionPress.app" ]; then \
		echo "Removing existing installation..."; \
		rm -rf "/Applications/OnionPress.app"; \
	fi
	cp -R OnionPress.app /Applications/
	@echo "Installed to /Applications/OnionPress.app"
	@echo "You can now launch it from Applications or Spotlight"
