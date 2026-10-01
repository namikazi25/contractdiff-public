"""Decision-packet review-screen tests."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROTOTYPE = ROOT / "prototype"


def load_json(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def run_review_app(program):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    sources = ["global.window = {};"]
    for name in ["contracts", "templates", "playbook", "routing"]:
        sources.append(
            (PROTOTYPE / "data" / f"{name}.js").read_text(encoding="utf-8")
        )
    sources.extend(
        [
            (PROTOTYPE / "app.js").read_text(encoding="utf-8"),
            program,
        ]
    )
    result = subprocess.run(
        [node, "--input-type=commonjs"],
        input="\n".join(sources),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def review_models():
    output = run_review_app(
        """
        console.log(JSON.stringify(window.DATA.contracts.map(contract =>
          window.QueueApp.buildReviewModel(window.DATA, contract.document_id)
        )));
        """
    )
    return {model["documentId"]: model for model in json.loads(output)}


def test_review_model_opens_for_every_document(review_models):
    contracts = load_json("contracts.json")
    assert set(review_models) == {
        contract["document_id"] for contract in contracts
    }
    assert review_models["UNKNOWN-001"]["template"] is None
    assert review_models["UNKNOWN-001"]["playbook"] is None
    assert review_models["UNKNOWN-001"]["route"] == "manual_review"
    assert review_models["NDA-001-DUP"]["duplicateOf"] == "NDA-001"


def test_complete_review_markup_renders_for_every_document():
    output = run_review_app(
        """
        const rendered = window.DATA.contracts.map(contract => {
          const model = window.QueueApp.buildReviewModel(window.DATA, contract.document_id);
          const html = window.QueueApp.reviewMarkup(model);
          return {
            id: contract.document_id,
            length: html.length,
            hasTitle: html.includes(`<h1 id="review-title">${contract.document_id}</h1>`),
            hasRoute: html.includes(model.routeLabel),
            packets: (html.match(/class="decision-packet"/g) || []).length,
            telemetry: (html.match(/data-telemetry-warning/g) || []).length,
            revision: html.includes("data-revision-example"),
            empty: html.includes("data-empty-review"),
            hasUndefined: html.includes("undefined")
          };
        });
        console.log(JSON.stringify(rendered));
        """
    )
    rendered = {item["id"]: item for item in json.loads(output)}
    assert len(rendered) == 11
    assert all(item["length"] > 1000 for item in rendered.values())
    assert all(item["hasTitle"] for item in rendered.values())
    assert all(item["hasRoute"] for item in rendered.values())
    assert not any(item["hasUndefined"] for item in rendered.values())
    assert rendered["NDA-002"]["packets"] == 3
    assert rendered["NDA-006"]["packets"] == 1
    assert rendered["NDA-006"]["telemetry"] == 1
    assert rendered["NDA-007"]["revision"] is True
    assert rendered["NDA-008"]["revision"] is True
    assert rendered["UNKNOWN-001"]["empty"] is True


def test_every_legal_finding_is_a_complete_evidence_led_packet(review_models):
    contracts = {
        item["document_id"]: item for item in load_json("contracts.json")
    }
    required = {
        "contractId",
        "clauseId",
        "clauseType",
        "sourceEvidence",
        "sourceContext",
        "templateText",
        "whatChanged",
        "whyItMatters",
        "ruleId",
        "severity",
        "fallback",
        "confidence",
        "analysisPaths",
        "decisionNeeded",
    }
    packets = [
        packet
        for model in review_models.values()
        for packet in model["findings"]
    ]
    assert packets

    for packet in packets:
        assert required <= packet.keys()
        assert packet["ruleId"]
        assert packet["severity"] in {"low", "medium", "high"}
        assert packet["templateText"]
        assert packet["fallback"]
        assert packet["analysisPaths"]
        contract_text = contracts[packet["contractId"]]["text"]
        if not packet["sourceEvidence"]:
            # A structural missing-clause finding cannot honestly quote text
            # that is absent. The UI renders that absence explicitly.
            assert packet["candidateIssue"].startswith("missing_")
            assert packet["sourceContext"] is None
        for evidence in packet["sourceEvidence"]:
            assert evidence["source_id"] == packet["contractId"]
            assert (
                contract_text[evidence["start_char"] : evidence["end_char"]]
                == evidence["quote"]
            )

    app = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
    assert "No source quote was supplied" in app


def test_route_severity_and_rule_ids_match_routing_data(review_models):
    routing = {
        item["document_id"]: item for item in load_json("routing.json")
    }
    for document_id, model in review_models.items():
        record = routing[document_id]
        assert model["route"] == record["final_route"]
        expected = {
            item["candidate_issue"]: (item["severity"], item["rule_id"])
            for item in record["union_findings"]
            if item["severity"] is not None
        }
        actual = {
            item["candidateIssue"]: (item["severity"], item["ruleId"])
            for item in model["findings"]
        }
        assert actual == expected


def test_dv9_telemetry_is_not_rendered_as_a_legal_finding(review_models):
    model = review_models["NDA-006"]
    assert len(model["findings"]) == 1
    assert len(model["telemetryWarnings"]) == 1
    warning = model["telemetryWarnings"][0]
    assert warning["kind"] == "prompt_injection_detected"
    assert warning["evidence"]
    assert "severity" not in warning
    assert "ruleId" not in warning
    assert "decisionNeeded" not in warning

    app = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
    assert "Operational safety signal" in app
    assert "Not a legal finding · no severity" in app
    assert 'data-telemetry-warning' in app


def test_nda_002_is_an_honest_false_alarm_review_exercise(review_models):
    model = review_models["NDA-002"]
    assert model["falseAlarmExample"] is True
    assert len(model["findings"]) == 3
    assert all(packet["aiOnly"] for packet in model["findings"])
    assert all(packet["sourceEvidence"] for packet in model["findings"])

    evidence = " ".join(
        [
            item["quote"]
            for packet in model["findings"]
            for item in packet["sourceEvidence"]
        ]
        + [packet["sourceContext"] for packet in model["findings"]]
    )
    assert "thirty (30) days" in evidence
    assert "twelve months" in evidence
    assert "England and Wales" in evidence

    app = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
    assert "False-alarm review exercise" in app
    assert "Read each evidence quote" in app


def test_revision_example_connects_nda_007_and_nda_008(review_models):
    previous = review_models["NDA-007"]["revision"]
    current = review_models["NDA-008"]["revision"]
    assert previous == current
    assert current["previousDocumentId"] == "NDA-007"
    assert current["previousVersion"] == 1
    assert current["currentDocumentId"] == "NDA-008"
    assert current["currentVersion"] == 2
    assert "capped at twelve months" in current["previousText"]
    assert "shall be uncapped" in current["currentText"]
    assert current["priorDecision"]["decision"] == "rejected"
    assert "rejected unlimited liability" in current["priorDecision"]["note"]
    assert current["whatToCheck"]


def test_prototype_decisions_are_local_ephemeral_screen_state():
    output = run_review_app(
        """
        let state = {};
        state = window.QueueApp.applyDecisionState(state, "finding-1", "accept");
        state = window.QueueApp.applyDecisionState(state, "finding-2", "reject");
        state = window.QueueApp.applyDecisionState(state, "finding-1", "edit");
        console.log(JSON.stringify(state));
        """
    )
    assert json.loads(output) == {
        "finding-1": "edit",
        "finding-2": "reject",
    }

    app = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
    assert 'data-decision-action="accept"' in app
    assert 'data-decision-action="edit"' in app
    assert 'data-decision-action="reject"' in app
    assert 'data-decision-action="escalate"' in app
    assert "Local prototype state:" in app
    assert "not saved" in app
    assert "localStorage" not in app
    assert "sessionStorage" not in app
    assert "indexedDB" not in app


def test_unknown_document_fails_closed():
    output = run_review_app(
        """
        try {
          window.QueueApp.buildReviewModel(window.DATA, "NOT-A-DOCUMENT");
          console.log("unexpected-success");
        } catch (error) {
          console.log(error.message);
        }
        """
    )
    assert output == "Unknown document: NOT-A-DOCUMENT"
