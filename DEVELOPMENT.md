# Development Guide

## Project Structure

```
onion.press/
├── onion.press.app/          # macOS application bundle
│   └── Contents/
│       ├── Info.plist        # App metadata
│       ├── MacOS/
│       │   ├── launcher      # Bootstrap script (launches Python app)
│       │   └── onion.press   # Container management script
│       └── Resources/
│           ├── docker/
│           │   └── docker-compose.yml  # Container configuration
│           └── scripts/
│               ├── menubar.py          # Menu bar app (Python/rumps)
│               └── requirements.txt    # Python dependencies
├── build/
│   ├── build-dmg.sh          # DMG builder with customization
│   └── build-dmg-simple.sh   # Simple DMG builder
├── Makefile                  # Build automation
└── README.md                 # User documentation
```

## Development Workflow

### Prerequisites

- macOS 11.0 or later
- Python 3.8+
- Docker Desktop, OrbStack, or Colima
- Xcode Command Line Tools: `xcode-select --install`

### Setting Up Development Environment

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/onion.press.git
   cd onion.press
   ```

2. Install Python dependencies (for testing menu bar app):
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r onion.press.app/Contents/Resources/scripts/requirements.txt
   ```

3. Ensure container runtime is running:
   ```bash
   # For OrbStack
   orbstack start

   # For Docker Desktop
   open -a Docker

   # For Colima
   colima start
   ```

### Testing Locally

#### Test the app bundle directly:
```bash
# Validate app structure
make test

# Run the app
open onion.press.app

# Or install to Applications for testing
make install
```

#### Test individual components:

1. **Test Docker Compose setup:**
   ```bash
   cd onion.press.app/Contents/Resources/docker
   docker compose up -d
   docker compose ps
   docker compose logs
   docker compose down
   ```

2. **Test container management script:**
   ```bash
   cd onion.press.app/Contents/MacOS
   ./onion.press start
   ./onion.press status
   ./onion.press address
   ./onion.press stop
   ```

3. **Test menu bar app:**
   ```bash
   cd onion.press.app/Contents/Resources/scripts
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

The DMG will be created at `build/onion.press.dmg`.

### Debugging

#### View application logs:
```bash
tail -f ~/.onion.press/onion.press.log
tail -f ~/.onion.press/launcher.log
```

#### View container logs:
```bash
cd onion.press.app/Contents/Resources/docker
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
- Creates virtual environment at `~/.onion.press/venv`
- Installs Python dependencies
- Launches menu bar app

### 2. Container Management Script (`MacOS/onion.press`)

- Detects container runtime (OrbStack/Docker Desktop/Colima)
- Offers to install OrbStack if none found
- Manages Docker Compose lifecycle
- Retrieves onion address from Tor container

### 3. Menu Bar App (`Resources/scripts/menubar.py`)

- Python app using `rumps` library
- Provides GUI for start/stop/restart
- Shows onion address
- Integrates with Tor Browser

### 4. Docker Compose Configuration (`Resources/docker/docker-compose.yml`)

Three services:
- **tor**: Hidden service container (exposes WordPress)
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
chmod +x onion.press.app/Contents/MacOS/*
```

### Python dependencies won't install
Clear venv and reinstall:
```bash
rm -rf ~/.onion.press/venv
open onion.press.app  # Will recreate venv
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

- [ ] Update version in `Info.plist`
- [ ] Update `README.md` with new features
- [ ] Test on clean macOS install
- [ ] Test both Intel and Apple Silicon Macs
- [ ] Verify OrbStack auto-install works
- [ ] Test complete WordPress setup flow
- [ ] Verify onion address generation
- [ ] Test Tor Browser integration
- [ ] Build and test DMG
- [ ] Create GitHub release with DMG

## Architecture Decisions

### Why containers?
- Isolation from host system
- Easy updates
- Consistent environment
- Cross-platform compatibility

### Why OrbStack over Docker Desktop?
- Smaller download (~100MB vs 650MB)
- Faster startup
- Lower resource usage
- Better macOS integration
- Free for personal use

### Why Python/rumps for menu bar?
- Simple API for menu bar apps
- Good macOS integration
- Easy to modify
- No compilation needed

### Why not bundle OrbStack in DMG?
- License considerations
- Smaller download
- OrbStack auto-updates separately
- Users might already have Docker

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly (see Testing section)
5. Submit a pull request

## License

MIT License - See LICENSE file
