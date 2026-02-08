.PHONY: help build build-simple test clean install

help:
	@echo "onionpress Build System"
	@echo ""
	@echo "Available targets:"
	@echo "  make build        - Build DMG with custom window (requires UI)"
	@echo "  make build-simple - Build DMG without customization (faster)"
	@echo "  make test         - Test the app bundle locally"
	@echo "  make clean        - Clean build artifacts"
	@echo "  make install      - Install app to /Applications (for testing)"
	@echo ""

build:
	@echo "Building DMG with customization..."
	./build/build-dmg.sh

build-simple:
	@echo "Building simple DMG..."
	./build/build-dmg-simple.sh

test:
	@echo "Testing app bundle..."
	@echo "Checking structure..."
	@test -d OnionPress.app/Contents/MacOS || (echo "ERROR: MacOS directory missing" && exit 1)
	@test -f OnionPress.app/Contents/MacOS/launcher || (echo "ERROR: launcher missing" && exit 1)
	@test -f OnionPress.app/Contents/MacOS/onionpress || (echo "ERROR: onionpress script missing" && exit 1)
	@test -f OnionPress.app/Contents/Info.plist || (echo "ERROR: Info.plist missing" && exit 1)
	@test -f OnionPress.app/Contents/Resources/docker/docker-compose.yml || (echo "ERROR: docker-compose.yml missing" && exit 1)
	@test -f OnionPress.app/Contents/Resources/scripts/menubar.py || (echo "ERROR: menubar.py missing" && exit 1)
	@test -f OnionPress.app/Contents/Resources/scripts/key_manager.py || (echo "ERROR: key_manager.py missing" && exit 1)
	@test -f OnionPress.app/Contents/Resources/scripts/bip39_words.py || (echo "ERROR: bip39_words.py missing" && exit 1)
	@echo "All required source files present"
	@echo ""
	@echo "Checking MenubarApp bundle..."
	@if [ -d OnionPress.app/Contents/Resources/MenubarApp ]; then \
		test -f OnionPress.app/Contents/Resources/MenubarApp/Contents/MacOS/menubar || \
			(echo "ERROR: MenubarApp executable missing" && exit 1); \
		echo "MenubarApp bundle present"; \
	else \
		echo "NOTE: MenubarApp not yet built (run make build-simple)"; \
	fi
	@echo ""
	@echo "Checking permissions..."
	@test -x OnionPress.app/Contents/MacOS/launcher || (echo "ERROR: launcher not executable" && exit 1)
	@test -x OnionPress.app/Contents/MacOS/onionpress || (echo "ERROR: onionpress not executable" && exit 1)
	@echo "Permissions correct"
	@echo ""
	@echo "App bundle structure is valid!"
	@echo "To run locally: open OnionPress.app"

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build/*.dmg
	rm -rf build/temp.dmg
	rm -rf OnionPress.app/Contents/Resources/MenubarApp
	@echo "Build artifacts cleaned"

install:
	@echo "Installing to /Applications..."
	@if [ -d "/Applications/OnionPress.app" ]; then \
		echo "Removing existing installation..."; \
		rm -rf "/Applications/OnionPress.app"; \
	fi
	cp -R OnionPress.app /Applications/
	@echo "Installed to /Applications/OnionPress.app"
	@echo "You can now launch it from Applications or Spotlight"
