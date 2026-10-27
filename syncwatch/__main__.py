"""Start SyncWatch:  python -m syncwatch  [--profile NAME] [--debug] [--reset-settings]

Before anything else this checks the things that would otherwise crash with
a confusing message: Python too old (SW-101), the video library missing
(SW-102), too old (SW-103) or incomplete (SW-201). It shows the same friendly
text as the in-app popups. On Windows that's a message box, because the
window library itself may be what's missing.
"""
import sys


def _message(code, technical=""):
    from syncwatch import errors
    problem = errors.problem(code)
    text = problem.explanation + "\n\nWhat you can try:\n" + "\n".join("- " + t for t in problem.tips)
    if technical:
        text += "\n\nTechnical: " + technical
    title = "SyncWatch: " + problem.title + " (" + code + ")"
    print(title + "\n" + text, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
        except Exception:
            pass


def main():
    if sys.version_info < (3, 9):
        _message("SW-101", "Python " + sys.version.split()[0])
        return 2
    try:
        import PySide6
    except ImportError as exc:
        _message("SW-102", str(exc))
        return 2
    from syncwatch import MIN_PYSIDE6
    version = tuple(int(part) for part in PySide6.__version__.split(".")[:2] if part.isdigit())
    if version < MIN_PYSIDE6:
        _message("SW-103", "PySide6 " + PySide6.__version__)
        return 2
    try:
        from PySide6 import QtMultimedia, QtMultimediaWidgets  # noqa: F401
    except ImportError as exc:
        _message("SW-201", "PySide6 is installed without its video part (" + str(exc) + ")")
        return 2
    from syncwatch.app import main as app_main
    return app_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
