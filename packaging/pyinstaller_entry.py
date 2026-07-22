"""PyInstaller entry point — a plain script wrapper around the Typer app.

PyInstaller freezes a *script*, not a console-script entry point, so this
module exists solely to give it one. Keep it dependency-light: anything
imported here is baked into the bundle's startup path.
"""

from anaplan_audit.cli import app

if __name__ == "__main__":
    app()
