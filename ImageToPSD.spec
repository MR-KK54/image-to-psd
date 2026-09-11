# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Base directory
base_dir = os.path.dirname(os.path.abspath('app.py'))

# Datas
datas = [
    (os.path.join(base_dir, 'templates'), 'templates'),
    (os.path.join(base_dir, 'static'), 'static'),
    (os.path.join(base_dir, 'assets'), 'assets'),
    (os.path.join(base_dir, 'groq_matcher.py'), '.'),
    (os.path.join(base_dir, 'app.ico'), '.'),
]

# Include .env if it exists
env_file = os.path.join(base_dir, '.env')
if os.path.exists(env_file):
    datas.append((env_file, '.'))

# Include pre-downloaded EasyOCR models if available on system
easyocr_user_dir = os.path.expanduser(r'~/.EasyOCR/model')
if os.path.exists(easyocr_user_dir):
    datas.append((easyocr_user_dir, 'easyocr_models'))

# Collect data files for easyocr and torch if needed
try:
    datas += collect_data_files('easyocr')
except Exception:
    pass

hiddenimports = [
    'flask',
    'flask_cors',
    'werkzeug',
    'werkzeug.routing',
    'jinja2',
    'cv2',
    'numpy',
    'PIL',
    'PIL.Image',
    'PIL.ImageFont',
    'PIL.ImageDraw',
    'easyocr',
    'easyocr.easyocr',
    'pytesseract',
    'groq',
    'dotenv',
    'requests',
    'torch',
    'torchvision',
    'psd_tools',
    'scipy',
    'scipy.ndimage',
    'shapely',
    'pyclipper',
    'webview',
    'webview.platforms.winforms',
    'clr',
]

# Collect submodules for heavy libraries
for pkg in ['easyocr', 'groq', 'psd_tools', 'webview']:
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

a = Analysis(
    ['app.py'],
    pathex=[base_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'tensorflow', 'transformers', 'pyarrow', 'pandas', 'tensorboard', 'IPython', 'jupyter', 'notebook', 'curses'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ImageToPSD',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app.ico',
)
