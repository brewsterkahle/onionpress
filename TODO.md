# TODO

## Future Features

### High Priority

- **Support replication and service backup** - Enable users to replicate their WordPress content and backup their onion service. Potential approaches:
  - Integration with Wayback Machine for content archiving
  - Client-to-onionpress server-to-server communication/coordination
  - Distributed backup across multiple onion services
  - Automatic snapshots to Internet Archive
  - Cross-service replication protocol

### Medium Priority

- **Code signing and notarization** - Investigate Apple Developer account for reducing security warnings (Note: May not eliminate all warnings due to virtualization/containers)

- **Multi-site support** - Allow running multiple WordPress instances with different onion addresses from a single installation

- **Automated updates** - Check for new WordPress/container image versions and prompt user to update

- **Custom vanity prefix UI** - GUI for configuring vanity address prefix instead of editing config file

### Low Priority

- **Tor Browser bundling** - Option to download/bundle Tor Browser with the app for one-click setup

- **Performance monitoring** - Built-in dashboard showing Tor bandwidth, visitor stats, resource usage

- **Plugin marketplace** - Curated list of privacy-focused WordPress plugins safe for Tor

- **Themes for Tor** - Lightweight themes optimized for Tor Browser's security settings

- **Bridge relay support** - Optional contribution to Tor network by running a bridge relay

## Retro Features

- **90's visitor counter** - Add a retro-style hit counter with local state tracking
- **Webring support** - Discover and connect with other OnionPress sites:
  - Add one or more OnionPress URLs to your webring
  - Crawl homepages at session start to test which are active
  - Rotate through active sites (need graphic design)
  - Auto-discover other homepage URLs to expand the ring
  - Maintain local state of webring members so that it is not a hosted service that someone else can see rings, also maintain the availability status of the other members so your site only shows OnionPress services that are up
  - **Webring reliability**: When adding an OnionPress to your webring, support adding keys to replicate their content and serve it on their address for robustness (backup hosting)
- **Animated GIF from gifcities.org** - Add "hello?hello?" animated GIF for retro aesthetic

## Technical Improvements

- **Sleep/wake handling** - Detect when Mac wakes from sleep, change menubar icon to yellow, test Tor URL connectivity, and return to purple when connection confirmed

- **Reduce disk usage** - Optimize Colima VM size and Docker image caching
- **Faster startup** - Pre-warm containers or improve initialization time
- **Better error messages** - User-friendly explanations for common issues
- **Health checks** - Automated diagnostics for Tor connectivity and WordPress health

## Documentation

- **Video tutorial** - Screen recording showing installation and setup
- **Troubleshooting guide** - Comprehensive FAQ with solutions
- **Architecture diagram** - Visual explanation of how components interact
- **Security best practices** - Guide for hardening WordPress on Tor

## Community

- **Homebrew tap submission** - Submit to official Homebrew cask repository
- **Package for other platforms** - Linux and Windows support (long-term)

---

**Contributing**: Feel free to tackle any of these items! Open an issue to discuss your approach before starting major work.
