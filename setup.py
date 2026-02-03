"""
py2app setup script for Onion.Press menubar application
"""
import sys
import os
from setuptools import setup

# Override build directories to avoid conflicts with build/ scripts
sys.argv.extend(['--dist-dir=py2app_dist', '--bdist-base=py2app_build'])

APP = ['Onion.Press.app/Contents/Resources/scripts/menubar.py']
DATA_FILES = [
    ('', [
        'Onion.Press.app/Contents/Resources/app-icon.png',
        'Onion.Press.app/Contents/Resources/menubar-icon-stopped.png',
        'Onion.Press.app/Contents/Resources/menubar-icon-starting.png',
        'Onion.Press.app/Contents/Resources/menubar-icon-running.png',
    ]),
]

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'Onion.Press.app/Contents/Resources/AppIcon.icns',
    'plist': {
        'CFBundleName': 'menubar',
        'CFBundleDisplayName': 'Onion.Press',
        'CFBundleIdentifier': 'press.onion.app',
        'CFBundleVersion': '2.2.19',
        'CFBundleShortVersionString': '2.2.19',
        'LSUIElement': True,  # Run as menu bar app (no dock icon)
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSAppleEventsUsageDescription': 'Onion.Press needs to control System Events to display native confirmation dialogs for important actions like uninstalling the app.',
    },
    'packages': ['rumps', 'objc', 'AppKit'],
    'includes': ['subprocess', 'threading', 'os', 'time', 'json'],
    'excludes': ['tkinter', 'test', 'unittest'],
    'arch': 'universal2',  # Build for both Intel and Apple Silicon
    'strip': True,  # Strip debug symbols to reduce size
    'optimize': 2,  # Optimize Python bytecode
}

setup(
    name='Onion.Press',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
