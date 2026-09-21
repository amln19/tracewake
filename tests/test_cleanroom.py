from __future__ import annotations

from pathlib import Path

import pytest

from bench import cleanroom, prepare_cleanroom, score_cleanroom, score_shipped
from tracewake.align import Step


def test_scaffold_writes_only_cleanroom_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "cleanroom"
    written: list[Path] = []

    def fake_export(path: Path) -> dict[str, str]:
        written.append(path)
        path.parent.mkdir(parents=True)
        path.write_text('{"id": "A001"}\n', encoding="utf-8")
        return {"A001": "nebius/source-id"}

    map_written: list[dict[str, str]] = []
    monkeypatch.setattr(prepare_cleanroom, "export", fake_export)
    monkeypatch.setattr(
        prepare_cleanroom,
        "write_id_map",
        lambda mapping: map_written.append(mapping) or Path("id-map.json"),
    )

    assert prepare_cleanroom.scaffold(destination) == destination.resolve()
    assert written == [destination / "data" / "train.jsonl"]
    assert map_written == [{"A001": "nebius/source-id"}]
    assert (destination / "TASK.md").is_file()
    assert (destination / "score.py").is_file()
    assert (destination / "SUBMISSION.md").is_file()
    assert not (destination / "predictor.py").exists()


def test_scaffold_never_overwrites_an_existing_cleanroom(tmp_path: Path) -> None:
    destination = tmp_path / "cleanroom"
    destination.mkdir()
    (destination / "predictor.py").write_text("# independent work\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        prepare_cleanroom.scaffold(destination)


def test_export_uses_only_the_committed_development_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partition = tmp_path / "partition.json"
    partition.write_text(
        '{"nebius": {"dev": ["neb-old/N02"]}, '
        '"openhands": {"dev": ["oh-old/O01"]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(cleanroom, "PARTITION", partition)
    monkeypatch.setattr(cleanroom, "_nebius_rows", lambda: [("N01", 1, []), ("N02", 2, [])])
    monkeypatch.setattr(cleanroom, "_openhands_rows", lambda: [("O01", 3, []), ("O02", 4, [])])

    mapping = cleanroom.export(tmp_path / "data" / "train.jsonl")

    assert mapping == {"A001": "neb-old/N02", "B001": "oh-old/O01"}


def test_cleanroom_score_requires_an_independent_submission(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="independently authored predictor"):
        score_cleanroom.main(["--cleanroom", str(tmp_path)])


def test_shipped_score_imports_tracewakes_localizer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [Step(name="read", args={}, target="src/a.py")]
    pools = {name: [("case", 1, steps)] for name in ("nebius", "openhands", "rootse")}
    monkeypatch.setattr(score_shipped, "_load_test", lambda: pools)

    score_shipped.main()

    assert "SHIPPED TRACEWAKE LOCALIZER" in capsys.readouterr().out
