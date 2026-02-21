# Development Guide

> **Version 2.0.2** - Now bundles Colima container runtime with custom onion address prefixes!

## Project Structure

```
onionpress/
├── onionpress.app/          # macOS application bundle
│   └── Contents/
│       ├── Info.plist        # App metadata
│       ├── MacOS/
│       │   ├── launcher      # Bootstrap script (launches Python app)
│       │   └── onionpress   # Container management script
│       └── Resources/
│           ├── bin/          # Bundled binaries (Colima, Docker CLI, mkp224o, etc.)
│           ├── share/        # Lima templates and support files
│           ├── docker/
│           │   └── docker-compose.yml  # Container configuration
│           ├── config-template.txt     # Config template for address prefix
│           └── scripts/
│               ├── menubar.py          # Menu bar app (Python/rumps)
│               └── requirements.txt    # Python dependencies
├── build/
│   ├── build-dmg.sh          # DMG builder with customization
│   └── build-dmg-simple.sh   # Simple DMG builder (downloads & bundles dependencies)
├── Makefile                  # Build automation
├── README.md                 # User documentation
└── CHANGELOG.md              # Version history
```

## Development Workflow

### Prerequisites

- macOS 13.0 (Ventura) or later (for Apple Virtualization.Framework)
- Python 3.8+
- Docker Desktop or Colima for development (runtime is bundled in release builds)
- Xcode Command Line Tools: `xcode-select --install`
- For building: git, autoconf, automake, libsodium (via Homebrew)

### Setting Up Development Environment

1. Clone the repository:
   ```bash
   git clone https://github.com/brewsterkahle/onionpress.git
   cd onionpress
   ```

2. Install Python dependencies (for testing menu bar app):
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r onionpress.app/Contents/Resources/scripts/requirements.txt
   ```

3. Ensure a container runtime is running (for development testing):
   ```bash
   # Use system Docker if available
   docker info

   # Or start Docker Desktop
   open -a Docker

   # Or use Colima
   colima start
   ```

### Testing Locally

#### Test the app bundle directly:
```bash
# Validate app structure
make test

# Run the app
open onionpress.app

# Or install to Applications for testing
make install
```

#### Test individual components:

1. **Test Docker Compose setup:**
   ```bash
   cd onionpress.app/Contents/Resources/docker
   docker compose up -d
   docker compose ps
   docker compose logs
   docker compose down
   ```

2. **Test container management script:**
   ```bash
   cd onionpress.app/Contents/MacOS
   ./onionpress start
   ./onionpress status
   ./onionpress address
   ./onionpress stop
   ```

3. **Test menu bar app:**
   ```bash
   cd onionpress.app/Contents/Resources/scripts
   python3 menubar.py
   ```

### Building the DMG

#### Simple build (recommended for development):
```bash
make build-simple
```

#### Fancy build with customization:
```bash
make build
```

The DMG will be created at `build/onionpress.dmg`.

### Debugging

#### View application logs:
```bash
tail -f ~/.onionpress/onionpress.log
tail -f ~/.onionpress/launcher.log
```

#### View container logs:
```bash
cd onionpress.app/Contents/Resources/docker
docker compose logs -f
```

#### Check container status:
```bash
docker compose ps
docker ps | grep onionpress
```

#### Get onion address manually:
```bash
docker compose exec tor cat /var/lib/tor/hidden_service/wordpress/hostname
```

## Component Details

### 1. Launcher Script (`MacOS/launcher`)

- Checks for Python 3
- Creates virtual environment at `~/.onionpress/venv`
- Installs Python dependencies
- Launches menu bar app

### 2. Container Management Script (`MacOS/onionpress`)

- Uses bundled Colima container runtime (in production builds)
- Falls back to system Docker if available (useful for development)
- Generates custom onion address prefixes with mkp224o on first launch
- Manages Docker Compose lifecycle
- Retrieves onion address from Tor container

### 3. Menu Bar App (`Resources/scripts/menubar.py`)

- Python app using `rumps` library
- Provides GUI for start/stop/restart
- Shows onion address
- Integrates with Tor Browser

### 4. Docker Compose Configuration (`Resources/docker/docker-compose.yml`)

Three services:
- **tor**: Onion service container (exposes WordPress)
- **wordpress**: WordPress container
- **db**: MariaDB database

## Modifying the Docker Configuration

### Change WordPress version:
```yaml
wordpress:
  image: wordpress:6.4  # Specify version
```

### Change database:
```yaml
# Switch to MySQL
db:
  image: mysql:8.0
  environment:
    - MYSQL_ROOT_PASSWORD=...
```

### Add more containers:
```yaml
services:
  # ... existing services ...

  phpmyadmin:
    image: phpmyadmin:latest
    environment:
      - PMA_HOST=db
    ports:
      - "127.0.0.1:8081:80"
```

## Testing the Tor Integration

### Verify Tor is running:
```bash
docker compose exec tor ps aux | grep tor
```

### Check Tor logs:
```bash
docker compose logs tor
```

### Manually get onion hostname:
```bash
docker compose exec tor cat /var/lib/tor/hidden_service/wordpress/hostname
```

### Test onion service:
1. Get onion address
2. Open Tor Browser
3. Navigate to the .onion URL
4. Should see WordPress installation

## Common Issues

### "Permission denied" errors
Make scripts executable:
```bash
chmod +x onionpress.app/Contents/MacOS/*
```

### Python dependencies won't install
Clear venv and reinstall:
```bash
rm -rf ~/.onionpress/venv
open onionpress.app  # Will recreate venv
```

### Containers won't start
Check Docker runtime:
```bash
docker info
docker ps
```

### Onion address not generating
Check Tor container logs:
```bash
docker compose logs tor
```

Wait 30-60 seconds for address generation.

## Release Checklist

Before releasing a new version:

- [ ] Update version in `Info.plist` (CFBundleShortVersionString and CFBundleVersion)
- [ ] Update `CHANGELOG.md` with new features
- [ ] Update `README.md` if needed
- [ ] Test on clean macOS install (remove ~/.onionpress first)
- [ ] Test on Apple Silicon Mac (ARM64) - primary platform
- [ ] Verify bundled Colima initializes correctly
- [ ] Verify address prefix generation works (check for "op2" prefix)
- [ ] Test complete WordPress setup flow
- [ ] Test Tor Browser integration
- [ ] Verify all bundled binaries are ARM64-only (no Rosetta emulation)
- [ ] Build and test DMG with `./build/build-dmg-simple.sh`
- [ ] Verify DMG size is reasonable (~110-120MB)
- [ ] Create GitHub release with DMG and release notes

## Architecture Decisions

### Why containers?
- Isolation from host system
- Easy updates
- Consistent environment
- Reproducible deployments

### Why bundle Colima instead of requiring external Docker?
- **Single-file installation**: Users don't need to install Docker Desktop separately
- **Smaller footprint**: Colima (~8MB) + Lima (~29MB) + Docker CLI (~37MB) = ~75MB bundled
- **Privacy-focused**: No telemetry or accounts required
- **Native performance**: Uses Apple's Virtualization.Framework (VZ backend)
- **Fully isolated**: Each app has its own container environment in `~/.onionpress/`
- **Open source**: MIT/Apache licensed components

### Why mkp224o for custom address prefixes?
- **Fast generation**: Can generate 3-character prefixes in < 1 second
- **v3 onion support**: Works with modern Tor onion services
- **Customizable**: Users can configure their own prefix
- **Branding**: "op2" prefix makes onionpress services easily identifiable

### Why Python/rumps for menu bar?
- Simple API for menu bar apps
- Good macOS integration
- Easy to modify
- No compilation needed

### Why ARM64-only binaries?
- **Avoid Rosetta issues**: Running under Rosetta caused errors with Lima/Colima
- **Better performance**: Native ARM64 execution on Apple Silicon
- **Simpler debugging**: No architecture confusion
- **Future-focused**: Intel Macs are being phased out

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly (see Testing section)
5. Submit a pull request

## License

MIT License - See LICENSE file
