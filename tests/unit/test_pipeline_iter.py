from pathlib import Path

import pytest

from ingstr.exceptions import ConfigError
from ingstr.pipeline import iter_source_files


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


def test_yields_only_files_not_directories(tmp_path: Path) -> None:
    _touch(tmp_path / "a.txt")
    (tmp_path / "subdir").mkdir()
    _touch(tmp_path / "subdir" / "b.txt")

    paths = list(iter_source_files(tmp_path, follow_symlinks=False, exclude_patterns=[]))
    names = sorted(p.name for p in paths)

    assert names == ["a.txt", "b.txt"]


def test_recursive_walk(tmp_path: Path) -> None:
    _touch(tmp_path / "a.txt")
    _touch(tmp_path / "x" / "y" / "z" / "deep.pdf")

    paths = list(iter_source_files(tmp_path, follow_symlinks=False, exclude_patterns=[]))
    rels = sorted(p.relative_to(tmp_path).as_posix() for p in paths)

    assert rels == ["a.txt", "x/y/z/deep.pdf"]


def test_exclude_pattern_filters_files(tmp_path: Path) -> None:
    _touch(tmp_path / "keep.pdf")
    _touch(tmp_path / "Thumbs.db")
    _touch(tmp_path / "deep" / "Thumbs.db")
    _touch(tmp_path / "deep" / "ok.pdf")

    paths = list(
        iter_source_files(
            tmp_path,
            follow_symlinks=False,
            exclude_patterns=["**/Thumbs.db"],
        )
    )
    rels = sorted(p.relative_to(tmp_path).as_posix() for p in paths)

    assert rels == ["deep/ok.pdf", "keep.pdf"]


def test_exclude_pattern_prunes_directory_descent(tmp_path: Path) -> None:
    _touch(tmp_path / "keep.pdf")
    _touch(tmp_path / ".tmp" / "junk.pdf")
    _touch(tmp_path / ".tmp" / "nested" / "more.pdf")
    _touch(tmp_path / "real" / "doc.pdf")

    paths = list(
        iter_source_files(
            tmp_path,
            follow_symlinks=False,
            exclude_patterns=["**/.tmp/**"],
        )
    )
    rels = sorted(p.relative_to(tmp_path).as_posix() for p in paths)

    assert rels == ["keep.pdf", "real/doc.pdf"]


def test_exclude_pattern_filename_glob(tmp_path: Path) -> None:
    _touch(tmp_path / "real.docx")
    _touch(tmp_path / "~$lockfile.docx")
    _touch(tmp_path / "deep" / "~$other.xlsx")

    paths = list(
        iter_source_files(
            tmp_path,
            follow_symlinks=False,
            exclude_patterns=["**/~$*"],
        )
    )
    rels = sorted(p.relative_to(tmp_path).as_posix() for p in paths)

    assert rels == ["real.docx"]


def test_root_must_be_directory(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "not_a_dir.txt"
    not_a_dir.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        list(iter_source_files(not_a_dir, follow_symlinks=False, exclude_patterns=[]))


def test_empty_exclude_patterns_yields_everything(tmp_path: Path) -> None:
    _touch(tmp_path / "a.txt")
    _touch(tmp_path / "b.txt")
    paths = list(iter_source_files(tmp_path, follow_symlinks=False, exclude_patterns=[]))
    assert {p.name for p in paths} == {"a.txt", "b.txt"}
