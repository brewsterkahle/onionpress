.PHONY: help build build-simple test clean install

help:
	@echo "onion.press Build System"
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
	@test -d onion.press.app/Contents/MacOS || (echo "ERROR: MacOS directory missing" && exit 1)
	@test -f onion.press.app/Contents/MacOS/launcher || (echo "ERROR: launcher missing" && exit 1)
	@test -f onion.press.app/Contents/MacOS/onion.press || (echo "ERROR: onion.press script missing" && exit 1)
	@test -f onion.press.app/Contents/Info.plist || (echo "ERROR: Info.plist missing" && exit 1)
	@test -f onion.press.app/Contents/Resources/docker/docker-compose.yml || (echo "ERROR: docker-compose.yml missing" && exit 1)
	@test -f onion.press.app/Contents/Resources/scripts/menubar.py || (echo "ERROR: menubar.py missing" && exit 1)
	@echo "✅ All required files present"
	@echo ""
	@echo "Checking permissions..."
	@test -x onion.press.app/Contents/MacOS/launcher || (echo "ERROR: launcher not executable" && exit 1)
	@test -x onion.press.app/Contents/MacOS/onion.press || (echo "ERROR: onion.press not executable" && exit 1)
	@echo "✅ Permissions correct"
	@echo ""
	@echo "App bundle structure is valid!"
	@echo "To run locally: open onion.press.app"

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build/*.dmg
	rm -rf build/temp.dmg
	@echo "✅ Build artifacts cleaned"

install:
	@echo "Installing to /Applications..."
	@if [ -d /Applications/onion.press.app ]; then \
		echo "Removing existing installation..."; \
		rm -rf /Applications/onion.press.app; \
	fi
	cp -R onion.press.app /Applications/
	@echo "✅ Installed to /Applications/onion.press.app"
	@echo "You can now launch it from Applications or Spotlight"
