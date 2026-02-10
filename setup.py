"""
py2app setup script for OnionPress menubar application
"""
import sys
import os
from setuptools import setup

# Default build directories if not specified on command line
# (avoid conflicts with build/ scripts directory)
if '--dist-dir' not in ' '.join(sys.argv):
    sys.argv.extend(['--dist-dir=py2app_dist'])
if '--bdist-base' not in ' '.join(sys.argv):
    sys.argv.extend(['--bdist-base=py2app_build'])

APP = ['src/menubar.py']
DATA_FILES = [
    ('', [
        'OnionPress.app/Contents/Resources/app-icon.png',
        'OnionPress.app/Contents/Resources/menubar-icon-stopped.png',
        'OnionPress.app/Contents/Resources/menubar-icon-starting.png',
        'OnionPress.app/Contents/Resources/menubar-icon-running.png',
        'src/onion-forward.php',
    ]),
]

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'OnionPress.app/Contents/Resources/AppIcon.icns',
    'plist': {
        'CFBundleName': 'menubar',
        'CFBundleDisplayName': 'OnionPress',
        'CFBundleIdentifier': 'press.onion.app',
        'CFBundleVersion': '2.2.57',
        'CFBundleShortVersionString': '2.2.57',
        'LSUIElement': True,  # Run as menu bar app (no dock icon)
        'LSMultipleInstancesProhibited': True,
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'LSApplicationCategoryType': 'public.app-category.utilities',
    },
    'packages': ['rumps', 'objc', 'AppKit', 'mnemonic'],
    # CRITICAL: Local modules that menubar.py imports at runtime.
    # py2app cannot auto-detect these because it runs menubar.py via exec(),
    # not import. If you add a new local .py module, ADD IT HERE or the build
    # will appear to succeed but the app will crash at launch with
    # "ModuleNotFoundError".
    'includes': ['subprocess', 'threading', 'os', 'time', 'json', 'key_manager', 'bip39_words',
                 'onion_proxy', 'install_native_messaging'],
    'excludes': ['tkinter', 'test', 'unittest'],
    'arch': 'universal2',  # Build for both Intel and Apple Silicon
    'strip': True,  # Strip debug symbols to reduce size
    'optimize': 2,  # Optimize Python bytecode
}

setup(
    name='OnionPress',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
