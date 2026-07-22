# PyInstaller spec for the anaplan-audit single-file binary.
#
# Build (from the repo root):
#   uv run pyinstaller packaging/anaplan-audit.spec --distpath dist --workpath build
#
# Notes on the fragile pieces:
#   * duckdb ships a compiled extension module — pyinstaller-hooks-contrib
#     has a dedicated hook (>= duckdb 1.4) that collects its metadata; the
#     frozen binary is smoke-tested via `anaplan-audit version`, which runs
#     a real DuckDB probe query.
#   * cryptography bundles OpenSSL — covered by the reworked contrib hook.
#   * anaplan_audit loads two package-data files via importlib.resources
#     (transform/queries/audit_query.sql and data/activity_events.csv) —
#     collected explicitly below so an editable install can't hide them.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

repo_root = Path(SPECPATH).parent
src_pkg = repo_root / "src" / "anaplan_audit"

# Package data accessed via importlib.resources at runtime. Explicit paths
# (not collect_data_files on the installed package) because the dev install
# is editable — the wheel metadata points back at src/, which PyInstaller's
# collector does not reliably traverse.
datas = [
    (str(src_pkg / "transform" / "queries" / "audit_query.sql"), "anaplan_audit/transform/queries"),
    (str(src_pkg / "data" / "activity_events.csv"), "anaplan_audit/data"),
    # v3.8 model-history classification vocabularies + rules.
    (str(src_pkg / "model_history" / "data" / "mh_object_types.csv"), "anaplan_audit/model_history/data"),
    (str(src_pkg / "model_history" / "data" / "mh_change_types.csv"), "anaplan_audit/model_history/data"),
    (
        str(src_pkg / "model_history" / "data" / "mh_classification_rules.csv"),
        "anaplan_audit/model_history/data",
    ),
]
# Belt-and-suspenders: anything the installed-package collector does find.
datas += collect_data_files("anaplan_audit")

a = Analysis(
    [str(repo_root / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(repo_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Big libraries pandas can pull in optionally; none are used.
        "matplotlib",
        "IPython",
        "jinja2",
        "scipy",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="anaplan-audit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
