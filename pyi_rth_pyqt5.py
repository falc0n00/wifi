# PyInstaller runtime hook to fix PyQt5 DLL loading
import os
import sys

# Get the path to PyQt5 in the bundled app
if getattr(sys, 'frozen', False):
    # Running as bundled exe
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

# Add PyQt5 Qt bin directory to PATH
qt_bin_path = os.path.join(base_path, 'PyQt5', 'Qt5', 'bin')
if os.path.exists(qt_bin_path):
    os.environ['PATH'] = qt_bin_path + os.pathsep + os.environ.get('PATH', '')
    try:
        os.add_dll_directory(qt_bin_path)
    except Exception:
        pass
