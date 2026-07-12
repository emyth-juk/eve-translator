"""Collect only the Windows Qt GUI plugin required by this widget application.

PyInstaller's stock QtGui hook collects every ``platforminputcontexts`` plugin.
It also collects image codecs, SVG/PDF support, touch input, and alternate
platform backends. EVE Translator loads its bundled PNG through Qt's built-in
PNG support and runs only as a native Windows Widgets application, so the
``qwindows`` platform plugin is the sole QtGui plugin it needs. The Windows
style plugin is collected separately by PyInstaller's QtWidgets hook.
"""

from pathlib import PurePath

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


ALLOWED_QT_GUI_PLUGINS = frozenset({"qwindows.dll"})


def _is_allowed_binary(binary: tuple[str, str]) -> bool:
    source = str(binary[0]).replace("\\", "/")
    if "/plugins/" not in source.casefold():
        return True
    return PurePath(source).name.casefold() in ALLOWED_QT_GUI_PLUGINS


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
binaries = [binary for binary in binaries if _is_allowed_binary(binary)]
