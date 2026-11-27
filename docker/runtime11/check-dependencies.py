"""Check actual Agent imports without launching the server or touching the desktop."""
import importlib
import json
import sys

MODULES = ('psutil', 'win32con', 'win32gui', 'win32process', 'win32clipboard',
           'win32security', 'pywinauto', 'comtypes', 'pyperclip', 'pygetwindow',
           'pyautogui', 'PIL', 'requests', 'flask', 'numpy', 'lxml.etree',
           'screeninfo', 'send2trash', 'Xlib', 'tkinter')

def check(modules=MODULES):
    failures = {}
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures[name] = f'{type(exc).__name__}: {exc}'
    return failures

if __name__ == '__main__':
    failures = check()
    print(json.dumps({'python': sys.executable, 'failures': failures}))
    sys.exit(bool(failures))
