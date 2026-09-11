"""The packaging contract.

None of this runs PyInstaller: a build takes a minute and produces a third of
a gigabyte, which is not a unit test. What is checked here is the set of
decisions the build depends on, each of which is silent when it breaks. A spec
that stopped bundling ffmpeg still builds, still starts, and fails the first
time the user exports anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "build.spec"
BAT = ROOT / "build.bat"
ICON = ROOT / "assets" / "videditor.ico"


@pytest.fixture(scope="module")
def spec_source() -> str:
    return SPEC.read_text(encoding="utf-8")


class TestTheSpec:
    def test_it_exists(self) -> None:
        assert SPEC.is_file()

    def test_it_is_a_onedir_build(self, spec_source: str) -> None:
        # onefile unpacks the whole payload, ffmpeg included, into a temp
        # directory on every single launch.
        assert "COLLECT(" in spec_source
        assert "exclude_binaries=True" in spec_source

    def test_both_binaries_are_bundled_into_bin(self, spec_source: str) -> None:
        assert '"bin" / "ffmpeg.exe"' in spec_source
        assert '"bin" / "ffprobe.exe"' in spec_source
        # The destination folder matters: core.binaries looks under bin/.
        assert spec_source.count('"bin"),') >= 2

    def test_the_icon_is_bundled_and_used(self, spec_source: str) -> None:
        assert "videditor.ico" in spec_source
        assert "icon=" in spec_source

    def test_the_heavy_unused_qt_modules_are_excluded(
        self, spec_source: str
    ) -> None:
        for module in (
            "QtWebEngine",
            "QtQuick",
            "QtQml",
            "QtCharts",
            "QtNetworkAuth",
            "QtBluetooth",
            "QtPositioning",
            "QtTest",
            "QtSql",
        ):
            assert f"PySide6.{module}" in spec_source, module

    def test_the_multimedia_modules_are_not_excluded(
        self, spec_source: str
    ) -> None:
        """The video stage is QtMultimedia. Excluding it builds and then
        fails at the first frame."""
        tree = ast.parse(spec_source)
        excluded: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "EXCLUDES" for t in node.targets
            ):
                excluded = [
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant)
                ]
        assert excluded, "EXCLUDES was not found in the spec"
        for needed in (
            "PySide6.QtCore",
            "PySide6.QtGui",
            "PySide6.QtWidgets",
            "PySide6.QtMultimedia",
            "PySide6.QtMultimediaWidgets",
        ):
            assert needed not in excluded, needed

    def test_the_software_opengl_fallback_is_kept(self, spec_source: str) -> None:
        """20MB of insurance for a machine with no usable GL driver."""
        assert "opengl32sw.dll" in spec_source
        assert "opengl32sw.dll" not in _prune_list(spec_source)


def _prune_list(spec_source: str) -> set[str]:
    tree = ast.parse(spec_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "PRUNE_BINARIES" for t in node.targets
        ):
            return {
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant)
            }
    return set()


class TestThePruneList:
    def test_nothing_the_application_uses_is_pruned(self) -> None:
        pruned = _prune_list(SPEC.read_text(encoding="utf-8"))
        assert pruned, "the prune list was not found"
        for kept in (
            "qt6core.dll",
            "qt6gui.dll",
            "qt6widgets.dll",
            "qt6multimedia.dll",
            "qt6multimediawidgets.dll",
            "qt6network.dll",
            "avcodec-61.dll",
            "avformat-61.dll",
        ):
            assert kept not in pruned, kept

    def test_the_names_are_lowercase(self) -> None:
        # They are compared against a lowercased filename.
        pruned = _prune_list(SPEC.read_text(encoding="utf-8"))
        assert all(name == name.lower() for name in pruned)


class TestTheBatchFile:
    def test_it_runs_the_spec(self) -> None:
        text = BAT.read_text(encoding="utf-8")
        assert ".venv\\Scripts\\activate" in text
        assert "pyinstaller build.spec --noconfirm" in text


class TestTheIcon:
    def test_it_exists_and_is_a_real_ico(self) -> None:
        assert ICON.is_file()
        assert ICON.read_bytes()[:4] == b"\x00\x00\x01\x00", "not an ICO header"

    def test_it_carries_the_small_sizes_windows_actually_shows(self) -> None:
        from PIL import Image

        with Image.open(ICON) as image:
            sizes = set(image.info.get("sizes", []))
        assert (16, 16) in sizes and (32, 32) in sizes

    def test_the_application_sets_it(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        assert "setWindowIcon" in source

    def test_a_missing_icon_does_not_stop_the_application(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import app as app_module

        monkeypatch.setattr(app_module, "ICON_PATH", tmp_path / "nothing.ico")
        assert app_module.application_icon().isNull()


class TestTheCommandLine:
    def test_a_project_path_is_opened(self, tmp_path: Path) -> None:
        import app as app_module

        opened: list[Path] = []
        target = tmp_path / "a.vedit"
        target.write_text("{}", encoding="utf-8")

        class FakeWindow:
            def load_project_file(self, path, prompt=True):
                opened.append(path)
                return True

        assert app_module._open_argument(FakeWindow(), [str(target)]) is True
        assert opened == [target]

    def test_something_that_is_not_a_file_is_ignored(self) -> None:
        import app as app_module

        class FakeWindow:
            def load_project_file(self, path, prompt=True):  # pragma: no cover
                raise AssertionError("should not have been called")

        assert app_module._open_argument(FakeWindow(), ["--debug"]) is False
        assert app_module._open_argument(FakeWindow(), []) is False
