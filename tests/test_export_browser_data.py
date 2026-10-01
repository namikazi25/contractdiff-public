"""Round-trip and offline-loading tests."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import export_browser_data


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PROTOTYPE_DATA_DIR = ROOT / "prototype" / "data"


def assignment_value(path: Path, data_key: str):
    content = path.read_text(encoding="utf-8")
    prefix = f"window.DATA.{data_key} = "
    assignment = next(line for line in content.splitlines() if line.startswith(prefix))
    start = content.index(prefix) + len(prefix)
    assert content.endswith(";\n")
    return json.loads(content[start:-2])


@pytest.mark.parametrize("spec", export_browser_data.DATA_EXPORTS)
def test_repository_exports_round_trip_to_canonical_json(spec):
    exported = assignment_value(
        PROTOTYPE_DATA_DIR / spec.output_name,
        spec.data_key,
    )
    canonical = json.loads(
        (DATA_DIR / spec.source_name).read_text(encoding="utf-8")
    )
    assert exported == canonical


def test_every_export_executes_as_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    for spec in export_browser_data.DATA_EXPORTS:
        path = PROTOTYPE_DATA_DIR / spec.output_name
        program = (
            "global.window = {};\n"
            + path.read_text(encoding="utf-8")
            + f"\nif (!window.DATA.{spec.data_key}) process.exit(2);\n"
        )
        result = subprocess.run(
            [node, "--input-type=commonjs"],
            input=program,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_evaluation_export_uses_results_json(tmp_path):
    output_dir = tmp_path / "browser-data"
    evaluation_path = tmp_path / "evaluation" / "results.json"
    evaluation_path.parent.mkdir()
    expected = [{"document_id": "NDA-001", "route_correct": True}]
    evaluation_path.write_text(
        json.dumps(expected),
        encoding="utf-8",
    )

    written, evaluation_present = export_browser_data.export_all(
        data_dir=DATA_DIR,
        output_dir=output_dir,
        evaluation_results=evaluation_path,
    )

    assert evaluation_present is True
    assert output_dir / "evaluation.js" in written
    assert assignment_value(output_dir / "evaluation.js", "evaluation") == expected


def test_missing_evaluation_is_deferred_not_fabricated(tmp_path):
    output_dir = tmp_path / "browser-data"
    written, evaluation_present = export_browser_data.export_all(
        data_dir=DATA_DIR,
        output_dir=output_dir,
        evaluation_results=tmp_path / "missing-results.json",
    )
    assert evaluation_present is False
    assert len(written) == 7
    assert not (output_dir / "evaluation.js").exists()


def test_required_inputs_are_loaded_before_any_output_is_written(tmp_path):
    empty_data = tmp_path / "empty-data"
    empty_data.mkdir()
    output_dir = tmp_path / "browser-data"
    with pytest.raises(export_browser_data.ExportError, match="missing"):
        export_browser_data.export_all(
            data_dir=empty_data,
            output_dir=output_dir,
            evaluation_results=tmp_path / "missing-results.json",
        )
    assert not output_dir.exists()


def test_generated_files_are_marked_and_contain_no_network_code():
    for path in sorted(PROTOTYPE_DATA_DIR.glob("*.js")):
        content = path.read_text(encoding="utf-8")
        assert "Canonical JSON is the source of truth" in content
        assert "fetch(" not in content
        assert "XMLHttpRequest" not in content


def test_smoke_harness_uses_only_relative_script_sources():
    html = (ROOT / "prototype" / "data-smoke-test.html").read_text(
        encoding="utf-8"
    )
    assert "fetch(" not in html
    assert "XMLHttpRequest" not in html
    assert "http://" not in html
    assert "https://" not in html
    for spec in export_browser_data.DATA_EXPORTS:
        assert f'src="data/{spec.output_name}"' in html
