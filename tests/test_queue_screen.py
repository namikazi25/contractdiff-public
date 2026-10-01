"""Offline queue-screen tests."""
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototype"


class PrototypeHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.stylesheets = []
        self.headers = []
        self._in_header = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"])
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href"))
        if tag == "th":
            self._in_header = True

    def handle_endtag(self, tag):
        if tag == "th":
            self._in_header = False

    def handle_data(self, data):
        if self._in_header and data.strip():
            self.headers.append(data.strip())


def run_queue_app(program):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    sources = [
        "global.window = {};",
        (PROTOTYPE / "data" / "contracts.js").read_text(encoding="utf-8"),
        (PROTOTYPE / "data" / "routing.js").read_text(encoding="utf-8"),
        (PROTOTYPE / "app.js").read_text(encoding="utf-8"),
        program,
    ]
    result = subprocess.run(
        [node, "--input-type=commonjs"],
        input="\n".join(sources),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_index_is_a_self_contained_filesystem_entry_point():
    html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
    parser = PrototypeHTML()
    parser.feed(html)

    assert parser.stylesheets == ["styles.css"]
    assert parser.scripts == [
        "data/contracts.js",
        "data/templates.js",
        "data/playbook.js",
        "data/routing.js",
        "data/evaluation.js",
        "app.js",
    ]
    for relative_path in parser.stylesheets + parser.scripts:
        assert not relative_path.startswith(("http://", "https://", "//", "/"))
        assert (PROTOTYPE / relative_path).is_file()


def test_queue_has_every_required_column():
    html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
    parser = PrototypeHTML()
    parser.feed(html)

    assert parser.headers == [
        "Document ID",
        "Counterparty",
        "Contract type",
        "Risk level",
        "Findings",
        "Highest severity",
        "Route",
        "Analysis status",
        "Duplicate status",
        "Analysis path",
        "Open review",
    ]


def test_prototype_uses_no_network_resources_or_apis():
    for path in [
        PROTOTYPE / "index.html",
        PROTOTYPE / "app.js",
        PROTOTYPE / "styles.css",
    ]:
        content = path.read_text(encoding="utf-8")
        assert "http://" not in content
        assert "https://" not in content
        assert "fetch(" not in content
        assert "XMLHttpRequest" not in content
        assert "WebSocket" not in content


def test_frozen_routing_builds_the_queue_and_features_nda_003_first():
    output = run_queue_app(
        """
        const rows = window.QueueApp.sortQueueRows(
          window.QueueApp.buildQueueRows(window.DATA)
        );
        console.log(JSON.stringify(rows));
        """
    )
    rows = json.loads(output)

    assert len(rows) == 11
    assert rows[0]["documentId"] == "NDA-003"
    first_non_high = next(
        (index for index, row in enumerate(rows) if row["risk"] != "High"),
        len(rows),
    )
    assert all(row["risk"] == "High" for row in rows[:first_non_high])

    unknown = next(row for row in rows if row["documentId"] == "UNKNOWN-001")
    assert unknown["risk"] == "Manual"
    assert unknown["route"] == "manual_review"
    assert unknown["routeLabel"] == "Manual review"

    duplicate = next(row for row in rows if row["documentId"] == "NDA-001-DUP")
    assert duplicate["risk"] == "Duplicate"
    assert duplicate["duplicateOf"] == "NDA-001"
    assert duplicate["duplicateStatus"] == "Duplicate of NDA-001"


def test_risk_route_and_severity_are_derived_from_routing_data():
    output = run_queue_app(
        """
        const routing = JSON.parse(JSON.stringify(window.DATA.routing));
        const record = routing.find(item => item.document_id === "NDA-003");
        record.final_route = "legal_review";
        record.union_findings = [{candidate_issue: "test", severity: "medium"}];
        const row = window.QueueApp.buildQueueRows({
          contracts: window.DATA.contracts,
          routing
        }).find(item => item.documentId === "NDA-003");
        console.log(JSON.stringify(row));
        """
    )
    row = json.loads(output)

    assert row["risk"] == "Medium"
    assert row["route"] == "legal_review"
    assert row["routeLabel"] == "Legal review"
    assert row["findingCount"] == 1
    assert row["highestSeverity"] == "medium"


def test_operational_findings_count_without_inventing_a_severity():
    output = run_queue_app(
        """
        const row = window.QueueApp.buildQueueRows(window.DATA)
          .find(item => item.documentId === "NDA-006");
        console.log(JSON.stringify(row));
        """
    )
    row = json.loads(output)

    assert row["findingCount"] == 1  # telemetry is not a scored finding
    assert row["injectionSignal"] is True
    assert row["highestSeverity"] == "high"


def test_injection_signal_is_flagged_only_where_telemetry_exists():
    output = run_queue_app(
        """
        const rows = window.QueueApp.buildQueueRows(window.DATA);
        console.log(JSON.stringify(rows.map(row => [row.documentId, row.injectionSignal])));
        """
    )
    flagged = {doc: signal for doc, signal in json.loads(output)}
    assert flagged["NDA-006"] is True
    assert not any(signal for doc, signal in flagged.items() if doc != "NDA-006")


def test_row_links_open_the_review_route_for_the_selected_document():
    output = run_queue_app(
        'console.log(window.QueueApp.reviewHref("NDA-003"));'
    )
    assert output == "index.html?document=NDA-003"

    app = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
    html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
    assert "data-review-href" in app
    assert "window.location.href = row.dataset.reviewHref" in app
    assert "data-review-screen" in html
    assert 'parameters.get("document")' in app
