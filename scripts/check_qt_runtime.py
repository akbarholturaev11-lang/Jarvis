#!/usr/bin/env python3
"""Qt launcher preflight with an optional narrow macOS hidden-flag repair."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


PLATFORM_PLUGIN_NAMES = {
    "darwin": "libqcocoa.dylib",
    "win32": "qwindows.dll",
    "linux": "libqxcb.so",
}
FORBIDDEN_QT_ENVIRONMENT = (
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_PLATFORM",
)


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    expected_venv: Path
    python_prefix: Path
    pyqt_package: Path
    qt_prefix: Path
    plugins_path: Path


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_macos_hidden(path: Path) -> bool:
    hidden_flag = getattr(stat, "UF_HIDDEN", 0)
    if not hidden_flag:
        return False
    try:
        return bool(path.stat().st_flags & hidden_flag)
    except (AttributeError, OSError):
        return False


def _plugin_tree_paths(root: Path):
    """Yield every plugin entry, including paths carrying macOS hidden flags."""
    stack = [root]
    while stack:
        path = stack.pop()
        yield path
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            children = [Path(entry.path) for entry in os.scandir(path)]
        except OSError:
            continue
        stack.extend(children)


def _ancestor_chain(path: Path, root: Path):
    """Yield path and its parents up to the canonical venv root."""
    current = path
    while _inside(current, root) or current == root:
        yield current
        if current == root:
            return
        current = current.parent


def normalize_macos_hidden_flags(
    paths: RuntimePaths,
    *,
    platform: str = sys.platform,
) -> int:
    """Clear only the macOS hidden bit that makes Qt skip plugin files."""
    if platform != "darwin":
        return 0

    if not _inside(paths.qt_prefix, paths.expected_venv) or not _inside(
        paths.plugins_path,
        paths.expected_venv,
    ):
        raise RuntimeError(
            "Refusing to change flags outside the project virtual environment / "
            "Отказ от изменения флагов вне виртуального окружения проекта."
        )
    if not hasattr(os, "chflags"):
        return 0

    candidates = list(_ancestor_chain(paths.qt_prefix, paths.expected_venv))
    if paths.plugins_path.exists():
        candidates.extend(_plugin_tree_paths(paths.plugins_path))

    changed = 0
    hidden_flag = stat.UF_HIDDEN
    for path in candidates:
        if path.is_symlink():
            continue
        try:
            current_flags = path.stat().st_flags
            if current_flags & hidden_flag:
                os.chflags(path, current_flags & ~hidden_flag)
                changed += 1
        except OSError as exc:
            raise RuntimeError(
                "Could not clear the macOS hidden flag / "
                f"Не удалось снять флаг hidden в macOS: {path}: {exc}"
            ) from exc
    return changed


def build_runtime_paths(
    *,
    project_root: str | Path,
    python_prefix: str | Path,
    pyqt_package: str | Path,
    qt_prefix: str | Path,
    plugins_path: str | Path,
) -> RuntimePaths:
    root = _resolved(project_root)
    return RuntimePaths(
        project_root=root,
        expected_venv=_resolved(root / ".venv"),
        python_prefix=_resolved(python_prefix),
        pyqt_package=_resolved(pyqt_package),
        qt_prefix=_resolved(qt_prefix),
        plugins_path=_resolved(plugins_path),
    )


def validate_runtime_paths(
    paths: RuntimePaths,
    *,
    platform: str = sys.platform,
    check_hidden: bool = True,
) -> list[str]:
    errors: list[str] = []

    if paths.python_prefix != paths.expected_venv:
        errors.append(
            "Python virtual environment does not match this project. / "
            "Виртуальное окружение Python не соответствует этому проекту."
        )

    for label, path in (
        ("PyQt6 package / пакет PyQt6", paths.pyqt_package),
        ("Qt prefix / префикс Qt", paths.qt_prefix),
        ("Qt plugins / плагины Qt", paths.plugins_path),
    ):
        if not _inside(path, paths.expected_venv):
            errors.append(
                f"{label} is outside the project virtual environment / "
                f"находится вне виртуального окружения проекта: {path}"
            )

    if not paths.plugins_path.is_dir():
        errors.append(
            "Qt plugins directory is missing / Каталог плагинов Qt отсутствует: "
            f"{paths.plugins_path}"
        )
    else:
        plugin_name = PLATFORM_PLUGIN_NAMES.get(platform)
        if plugin_name:
            plugin_file = paths.plugins_path / "platforms" / plugin_name
            if not plugin_file.is_file():
                errors.append(
                    "Qt platform plugin is missing / Плагин платформы Qt отсутствует: "
                    f"{plugin_file}"
                )
            elif plugin_file.is_symlink() or not _inside(
                _resolved(plugin_file),
                paths.expected_venv,
            ):
                errors.append(
                    "Qt platform plugin must be a real file inside the project "
                    "virtual environment / Плагин платформы Qt должен быть обычным "
                    f"файлом внутри виртуального окружения проекта: {plugin_file}"
                )

    if check_hidden and platform == "darwin":
        candidates = list(_ancestor_chain(paths.qt_prefix, paths.expected_venv))
        if paths.plugins_path.exists():
            candidates.extend(_plugin_tree_paths(paths.plugins_path))
        hidden_paths = [
            path for path in candidates if path.exists() and _is_macos_hidden(path)
        ]
        if hidden_paths:
            preview = ", ".join(str(path) for path in hidden_paths[:3])
            errors.append(
                "Qt plugin files have the macOS hidden flag, so Qt cannot discover "
                "them / Файлы плагинов Qt имеют флаг hidden в macOS, поэтому Qt не "
                f"может их обнаружить: {preview}"
            )

    return errors


def validate_qt_environment(
    environ: Mapping[str, str] = os.environ,
) -> list[str]:
    errors: list[str] = []
    for name in FORBIDDEN_QT_ENVIRONMENT:
        value = environ.get(name, "").strip()
        if value:
            errors.append(
                "Qt path/platform override is not allowed / "
                f"Переопределение пути/платформы Qt запрещено: {name}={value}"
            )
    return errors


def run_gui_smoke_test() -> None:
    """Verify that the configured Qt platform can construct an application."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication(["jarvis-qt-preflight"])
    if sys.platform == "darwin" and app.platformName().lower() != "cocoa":
        platform_name = app.platformName()
        app.quit()
        raise RuntimeError(
            "Qt did not load the Cocoa platform / "
            f"Qt не загрузил платформу Cocoa: {platform_name}"
        )
    app.processEvents()
    app.quit()


def _print_diagnostics(paths: RuntimePaths, errors: list[str]) -> None:
    print("ERROR / ОШИБКА: JARVIS Qt runtime preflight failed.", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    print(
        f"Expected venv / Ожидаемое окружение: {paths.expected_venv}",
        file=sys.stderr,
    )
    print(f"Python prefix / Префикс Python: {paths.python_prefix}", file=sys.stderr)
    print(f"Qt plugins / Плагины Qt: {paths.plugins_path}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument(
        "--paths-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--repair-hidden-flags",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    try:
        import PyQt6
        from PyQt6.QtCore import QLibraryInfo
    except Exception as exc:
        print(
            "ERROR / ОШИБКА: PyQt6 could not be imported / "
            f"не удалось импортировать PyQt6: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    paths = build_runtime_paths(
        project_root=args.project_root,
        python_prefix=sys.prefix,
        pyqt_package=Path(PyQt6.__file__).parent,
        qt_prefix=QLibraryInfo.path(QLibraryInfo.LibraryPath.PrefixPath),
        plugins_path=QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath),
    )
    structural_errors = validate_qt_environment()
    structural_errors.extend(validate_runtime_paths(paths, check_hidden=False))
    if structural_errors:
        _print_diagnostics(paths, structural_errors)
        return 2

    if args.repair_hidden_flags:
        try:
            changed = normalize_macos_hidden_flags(paths)
        except RuntimeError as exc:
            print(f"ERROR / ОШИБКА: {exc}", file=sys.stderr)
            return 2
        if changed:
            print(
                "Cleared macOS hidden flags from Qt plugins / "
                f"Сняты флаги hidden с плагинов Qt: {changed}"
            )

    errors = validate_runtime_paths(paths)
    if errors:
        _print_diagnostics(paths, errors)
        return 2

    if not args.paths_only:
        run_gui_smoke_test()

    print("Qt runtime preflight: OK / Проверка среды Qt: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
