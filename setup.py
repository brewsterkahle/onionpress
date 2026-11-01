"""
py2app setup script for OnionPress menubar application
"""
import re
import sys
import os
from setuptools import setup

# Read version from src/menubar.py (the canonical source)
_version_match = re.search(
    r'self\.version\s*=\s*"([^"]+)"',
    open(os.path.join(os.path.dirname(__file__), 'src', 'menubar.py')).read(),
)
VERSION = _version_match.group(1) if _version_match else 'unknown'

# Shown in Finder's Get Info and the About box; app/Info.plist carries the
# same line for the outer bundle (tests/test_license_declarations.py).
COPYRIGHT = ('Copyright © 2026 Internet Archive and OnionPress contributors. '
             'Licensed under the GNU AGPL v3 or later.')

# Default build directories if not specified on command line
# (avoid conflicts with build/ scripts directory)
if '--dist-dir' not in ' '.join(sys.argv):
    sys.argv.extend(['--dist-dir=py2app_dist'])
if '--bdist-base' not in ' '.join(sys.argv):
    sys.argv.extend(['--bdist-base=py2app_build'])

APP = ['src/menubar.py']

# Wordlists for local onionname suggestions — bundled from the same source
# the tor container uses, so adding/editing a language only happens in one
# place.
_WORDLISTS_DIR = 'app/Resources/docker/tor/wordlists'
_WORDLIST_FILES = [
    os.path.join(_WORDLISTS_DIR, fname)
    for fname in ('__init__.py', 'en.py', 'fr.py', 'es.py', 'de.py',
                  'nl.py', 'pt.py', 'ja.py', 'zh.py', 'ar.py')
]

DATA_FILES = [
    ('', [
        'app/Resources/app-icon.png',
        'app/Resources/menubar-icon-stopped.png',
        'app/Resources/menubar-icon-starting.png',
        'app/Resources/menubar-icon-running.png',
        'src/onion-forward.php',
    ]),
    ('assets/branding', [
        'assets/branding/logo.png',
    ]),
    ('wordlists', _WORDLIST_FILES),
]

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'app/Resources/AppIcon.icns',
    'plist': {
        'CFBundleName': 'OnionPress',
        'CFBundleDisplayName': 'OnionPress',
        'CFBundleIdentifier': 'press.onion.app',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'LSUIElement': True,  # Run as menu bar app (no dock icon)
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'NSHumanReadableCopyright': COPYRIGHT,
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSLocalNetworkUsageDescription': 'OnionPress uses localhost connections to manage Docker containers and serve a local proxy. It does not access other devices on your network.',
        'NSDocumentsFolderUsageDescription': 'OnionPress is migrating existing files from ~/Documents/OnionPress/ to ~/OnionPress/. After the one-time move, OnionPress no longer accesses your Documents folder.',
    },
    # nacl: PyNaCl (libsodium), which encrypts backups (backup format 2,
    # onionpress.backup_crypto). Its compiled half, nacl/_sodium.abi3.so,
    # loads cffi's _cffi_backend extension at import time — an import
    # py2app cannot see inside a binary — so _cffi_backend is listed in
    # includes. build/py2app-extensions.sh makes it universal2 and checks
    # the built bundle can encrypt.
    'packages': ['rumps', 'objc', 'AppKit', 'nacl'],
    # Local modules that menubar.py imports at runtime. py2app cannot
    # auto-detect these because it runs menubar.py via exec(), not import.
    # All shared code now lives in the `onionpress` package — adding a new
    # submodule means one new line here and nothing in the build scripts.
    'includes': ['subprocess', 'threading', 'os', 'time', 'json',
                 '_cffi_backend',
                 'onionpress', 'onionpress.backup', 'onionpress.backup_crypto',
                 'onionpress.hub_backup', 'onionpress.hub_record', 'onionpress.linux_status', 'onionpress.docker_versions',
                 'onionpress.backup_archive', 'onionpress.db_import',
                 'onionpress.key_manager',
                 'onionpress.setup_window', 'onionpress.platform', 'onionpress.docker',
                 'onionpress.config', 'onionpress.health', 'onionpress.containers',
                 'onionpress.image_overrides',
                 'onionpress.ports', 'onionpress.secrets_file',
                 'onionpress.ui_helpers', 'onionpress.settings_ui', 'onionpress.ui_client',
                 'onionpress.onionheaven_ui',
                 'onionpress.browser', 'onionpress.log_rotation',
                 'onionpress.analytics_sharing', 'onionpress.power',
                 'onionpress.onion_proxy', 'onionpress.onion_auth',
                 'onionpress.onion_keys',
                 'onionpress.onionheaven', 'onionpress.updater',
                 'onionpress.install_native_messaging', 'onionpress.native_messaging_host',
                 'onionpress.onion_hostname',
                 'onionpress.onionnames_client',
                 'onionpress.onionnames_registrar',
                 'onionpress.reachability_stats',
                 'onionpress.system_metrics',
                 'onionpress.multisite',
                 'onionpress.derivatives',
                 'onionpress.scrub',
                 'onionpress.mac_power',
                 'onionpress.redact',
                 'onionpress.auto_login',
                 'onionpress.splash_tips',
                 'onionpress.wp_files',
                 'onionpress.confirm', 'onionpress.handle_action',
                 'onionpress.uninstall',
                 'onionpress.wordpress_egress',
                 'onionpress.daemon_client',
                 'onionpress.thumbnails', 'onionpress.daemon',
                 'onionpress.start', 'onionpress.bundled_plugins'],
    'excludes': ['tkinter', 'test', 'unittest'],
    'arch': 'universal2',  # Build for both Intel and Apple Silicon
    'strip': True,  # Strip debug symbols to reduce size
    'optimize': 2,  # Optimize Python bytecode
}

setup(
    name='OnionPress',
    license='AGPL-3.0-or-later',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
