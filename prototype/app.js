(function () {
  "use strict";

  const FEATURED_DOCUMENT_ID = "NDA-003";
  const SEVERITY_RANK = { high: 3, medium: 2, low: 1 };
  const ROUTE_META = {
    senior_legal_review: { label: "Senior legal review", risk: "High", rank: 5 },
    manual_review: { label: "Manual review", risk: "Manual", rank: 4 },
    legal_review: { label: "Legal review", risk: "Medium", rank: 3 },
    quick_check: { label: "Quick check", risk: "Low", rank: 2 },
    duplicate: { label: "Duplicate", risk: "Duplicate", rank: 1 }
  };

  function humanize(value) {
    if (!value) return "—";
    return value
      .replace(/_/g, " ")
      .replace(/\b\w/g, function (letter) { return letter.toUpperCase(); })
      .replace(/\bNda\b/g, "NDA")
      .replace(/\bAi\b/g, "AI");
  }

  function routeMeta(route) {
    return ROUTE_META[route] || { label: humanize(route), risk: "Manual", rank: 4 };
  }

  function highestSeverity(findings) {
    const severities = (findings || [])
      .map(function (finding) { return finding.severity; })
      .filter(function (severity) { return severity in SEVERITY_RANK; });
    if (!severities.length) return null;
    return severities.sort(function (a, b) {
      return SEVERITY_RANK[b] - SEVERITY_RANK[a];
    })[0];
  }

  function analysisStatus(paths) {
    const statuses = Object.values(paths || {}).map(function (path) { return path.status; });
    if (!statuses.length) return "Not analysed";
    if (statuses.every(function (status) { return status === "completed"; })) return "Complete";
    if (statuses.some(function (status) {
      return status === "analysis_failed" || status === "analysis-failed";
    })) return "Needs review";
    return "In progress";
  }

  function analysisPath(paths) {
    const labels = [];
    if (paths && paths.ai) labels.push("AI");
    if (paths && paths.baseline) labels.push("Rules");
    return labels.length ? labels.join(" + ") : "—";
  }

  function buildQueueRows(data) {
    const contracts = (data && data.contracts) || [];
    const routing = (data && data.routing) || [];
    const contractsById = new Map(contracts.map(function (contract) {
      return [contract.document_id, contract];
    }));

    return routing.map(function (record) {
      const contract = contractsById.get(record.document_id);
      if (!contract) throw new Error("Missing contract data for " + record.document_id);
      const meta = routeMeta(record.final_route);
      const findings = record.union_findings || [];
      // null-severity findings are operational telemetry, not scored
      // legal findings. They stay visible but never count or rank.
      const legalFindings = findings.filter(function (finding) {
        return finding.severity != null;
      });
      const injectionSignal = findings.some(function (finding) {
        return finding.severity == null &&
          finding.candidate_issue === "prompt_injection_detected";
      });
      return {
        documentId: record.document_id,
        counterparty: contract.counterparty,
        contractType: humanize(contract.contract_type),
        risk: meta.risk,
        riskRank: meta.rank,
        findingCount: legalFindings.length,
        injectionSignal: injectionSignal,
        highestSeverity: highestSeverity(legalFindings),
        route: record.final_route,
        routeLabel: meta.label,
        analysisStatus: analysisStatus(record.paths),
        duplicateStatus: record.duplicate_of ? "Duplicate of " + record.duplicate_of : "Not duplicate",
        duplicateOf: record.duplicate_of,
        analysisPath: analysisPath(record.paths),
        receivedAt: contract.received_at || "",
        featured: record.document_id === FEATURED_DOCUMENT_ID
      };
    });
  }

  function sortQueueRows(rows) {
    return rows.slice().sort(function (a, b) {
      if (a.riskRank !== b.riskRank) return b.riskRank - a.riskRank;
      if (a.featured !== b.featured) return a.featured ? -1 : 1;
      if (a.receivedAt !== b.receivedAt) return b.receivedAt.localeCompare(a.receivedAt);
      return a.documentId.localeCompare(b.documentId);
    });
  }

  function reviewHref(documentId) {
    return "index.html?document=" + encodeURIComponent(documentId);
  }

  function pathLabel(path) {
    return path === "baseline" ? "Rules" : humanize(path);
  }

  function linkedFindings(record, unionFinding) {
    const linkedIds = new Set(unionFinding.finding_ids || []);
    const linked = [];
    Object.entries(record.paths || {}).forEach(function (entry) {
      const path = entry[0];
      const output = entry[1];
      (output.findings || []).forEach(function (finding) {
        const idMatch = linkedIds.has(finding.finding_id);
        const fallbackMatch = !linkedIds.size &&
          finding.candidate_issue === unionFinding.candidate_issue &&
          finding.rule_id === unionFinding.rule_id;
        if (idMatch || fallbackMatch) linked.push({ path: path, finding: finding });
      });
    });
    return linked;
  }

  function uniqueEvidence(linked, contractId) {
    const seen = new Set();
    const evidence = [];
    linked.forEach(function (item) {
      (item.finding.evidence || []).forEach(function (record) {
        if (contractId && (record.source !== "contract" || record.source_id !== contractId)) {
          return;
        }
        const key = [record.source_id, record.start_char, record.end_char, record.quote].join(":");
        if (!seen.has(key)) {
          seen.add(key);
          evidence.push(record);
        }
      });
    });
    return evidence;
  }

  function surroundingContext(text, evidence) {
    if (!evidence || typeof evidence.start_char !== "number") return null;
    const source = String(text || "");
    const before = source.lastIndexOf("\n\n", evidence.start_char);
    const after = source.indexOf("\n\n", evidence.end_char);
    return source.slice(before < 0 ? 0 : before + 2, after < 0 ? source.length : after).trim();
  }

  function confidenceByPath(linked) {
    const confidence = {};
    linked.forEach(function (item) {
      const value = item.finding.confidence;
      if (typeof value === "number") {
        confidence[item.path] = Math.max(confidence[item.path] || 0, value);
      }
    });
    return Object.keys(confidence).map(function (path) {
      return { path: path, label: pathLabel(path), value: confidence[path] };
    });
  }

  function buildDecisionPacket(unionFinding, record, contract, template, playbook, index) {
    const linked = linkedFindings(record, unionFinding);
    const representative = linked.find(function (item) { return item.path === "ai"; }) || linked[0];
    const detail = representative ? representative.finding : {};
    const templateClauseId = detail.template_clause_id || null;
    const templateClause = template && (template.clauses || []).find(function (clause) {
      return clause.template_clause_id === templateClauseId;
    });
    const rule = playbook && (playbook.rules || []).find(function (item) {
      return item.rule_id === unionFinding.rule_id;
    });
    const clauseId = linked.map(function (item) { return item.finding.clause_id; })
      .find(function (value) { return value; }) || null;
    const analysisPaths = unionFinding.analysis_paths || linked.map(function (item) { return item.path; });
    const fallback = detail.suggested_redline || (rule && rule.suggested_fallback_language) || null;

    const sourceEvidence = uniqueEvidence(linked, record.document_id);
    return {
      key: unionFinding.candidate_issue + "-" + index,
      title: humanize(unionFinding.candidate_issue),
      candidateIssue: unionFinding.candidate_issue,
      contractId: record.document_id,
      clauseId: clauseId,
      clauseType: detail.clause_type || (rule && rule.clause_type) || "unknown",
      templateClauseId: templateClauseId,
      sourceEvidence: sourceEvidence,
      sourceContext: surroundingContext(contract.text, sourceEvidence[0]),
      templateText: templateClause ? templateClause.text : null,
      whatChanged: detail.summary || "A routed deviation requires human confirmation.",
      whyItMatters: detail.why_it_matters || (rule && rule.preferred_position) || "Review against the approved position.",
      ruleId: unionFinding.rule_id,
      severity: unionFinding.severity,
      fallback: fallback,
      preferredPosition: rule ? rule.preferred_position : null,
      acceptableFallback: rule ? rule.acceptable_fallback : null,
      confidence: confidenceByPath(linked),
      analysisPaths: analysisPaths,
      aiOnly: analysisPaths.length === 1 && analysisPaths[0] === "ai",
      decisionNeeded: unionFinding.severity === "high"
        ? "Confirm whether the evidence supports this high-risk deviation. Reject a false alarm or escalate a confirmed issue."
        : "Confirm the deviation and accept, edit, or reject the proposed fallback."
    };
  }

  function buildTelemetryWarning(unionFinding, record) {
    const linked = linkedFindings(record, unionFinding);
    const detail = linked.length ? linked[0].finding : {};
    return {
      kind: unionFinding.candidate_issue,
      title: unionFinding.candidate_issue === "prompt_injection_detected"
        ? "Instruction-like text detected in the contract"
        : humanize(unionFinding.candidate_issue),
      summary: detail.summary || "Operational analysis telemetry requires attention.",
      evidence: uniqueEvidence(linked, record.document_id),
      analysisPaths: unionFinding.analysis_paths || linked.map(function (item) { return item.path; })
    };
  }

  function extractClause(text, heading) {
    const blocks = String(text || "").split(/\n\s*\n/);
    const index = blocks.findIndex(function (block) {
      return block.toLowerCase().includes(heading.toLowerCase());
    });
    if (index < 0) return null;
    return blocks.slice(index, index + 2).join("\n\n");
  }

  function buildRevisionExample(data, selectedContract) {
    const current = (data.contracts || []).find(function (contract) {
      return contract.parent_document_id &&
        contract.contract_record_id === selectedContract.contract_record_id;
    });
    if (!current) return null;
    const previous = (data.contracts || []).find(function (contract) {
      return contract.document_id === current.parent_document_id;
    });
    if (!previous || ![current.document_id, previous.document_id].includes(selectedContract.document_id)) {
      return null;
    }

    const currentRouting = (data.routing || []).find(function (record) {
      return record.document_id === current.document_id;
    });
    const unionFinding = currentRouting && (currentRouting.union_findings || []).find(function (finding) {
      return finding.candidate_issue === "unlimited_liability";
    });
    const linked = unionFinding ? linkedFindings(currentRouting, unionFinding) : [];
    const newEvidence = uniqueEvidence(linked, current.document_id)[0];
    const priorDecision = (current.prior_decisions || []).find(function (decision) {
      return decision.candidate_issue === "unlimited_liability";
    }) || (previous.prior_decisions || [])[0] || null;
    const currentFinding = linked.length ? linked[0].finding : {};

    return {
      previousDocumentId: previous.document_id,
      previousVersion: previous.version,
      currentDocumentId: current.document_id,
      currentVersion: current.version,
      issue: "Liability position changed between revisions",
      previousText: extractClause(previous.text, "Limitation of liability"),
      currentText: newEvidence ? newEvidence.quote : extractClause(current.text, "Limitation of liability"),
      priorDecision: priorDecision,
      whatToCheck: currentFinding.suggested_redline || "Compare the revised wording with the approved liability position."
    };
  }

  function buildReviewModel(data, documentId) {
    const contract = (data.contracts || []).find(function (item) {
      return item.document_id === documentId;
    });
    const record = (data.routing || []).find(function (item) {
      return item.document_id === documentId;
    });
    if (!contract || !record) throw new Error("Unknown document: " + documentId);

    const template = (data.templates || []).find(function (item) {
      return item.contract_type === contract.contract_type && item.status === "active";
    }) || null;
    const playbook = (data.playbook || []).find(function (item) {
      return item.playbook_id === record.playbook_id;
    }) || null;
    const queueRow = buildQueueRows(data).find(function (item) {
      return item.documentId === documentId;
    });
    const legalFindings = (record.union_findings || []).filter(function (finding) {
      return finding.severity != null;
    });
    const telemetry = (record.union_findings || []).filter(function (finding) {
      return finding.severity == null;
    });

    return {
      documentId: contract.document_id,
      counterparty: contract.counterparty,
      contractType: humanize(contract.contract_type),
      jurisdiction: contract.jurisdiction || "—",
      contractVersion: contract.version,
      receivedAt: contract.received_at,
      source: contract.source,
      template: template ? { id: template.template_id, version: template.version } : null,
      playbook: playbook ? { id: playbook.playbook_id, version: playbook.version } : null,
      analysisPath: queueRow.analysisPath,
      analysisStatus: queueRow.analysisStatus,
      route: record.final_route,
      routeLabel: queueRow.routeLabel,
      risk: queueRow.risk,
      duplicateOf: record.duplicate_of,
      findings: legalFindings.map(function (finding, index) {
        return buildDecisionPacket(finding, record, contract, template, playbook, index);
      }),
      telemetryWarnings: telemetry.map(function (finding) {
        return buildTelemetryWarning(finding, record);
      }),
      revision: buildRevisionExample(data, contract),
      falseAlarmExample: documentId === "NDA-002"
    };
  }

  function applyDecisionState(state, findingKey, action) {
    const allowed = ["accept", "edit", "reject", "escalate"];
    if (!allowed.includes(action)) throw new Error("Unknown prototype action: " + action);
    const next = Object.assign({}, state);
    next[findingKey] = action;
    return next;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function badge(value, kind) {
    const slug = String(value || "none").toLowerCase().replace(/[^a-z0-9]+/g, "-");
    return '<span class="badge badge--' + escapeHtml(kind) + "-" + escapeHtml(slug) + '">' +
      escapeHtml(value || "None") + "</span>";
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }

  function evidenceMarkup(evidence) {
    if (!evidence.length) {
      return '<p class="missing-evidence">No source quote was supplied. Confirm directly in the contract.</p>';
    }
    return evidence.map(function (item) {
      return '<blockquote class="evidence-quote">' +
        "<p>“" + escapeHtml(item.quote) + "”</p>" +
        '<footer><span>Contract source</span><span>Characters ' + escapeHtml(item.start_char) + "–" +
        escapeHtml(item.end_char) + "</span></footer>" +
        "</blockquote>";
    }).join("");
  }

  function confidenceMarkup(confidence) {
    if (!confidence.length) return '<span class="muted">Not supplied</span>';
    return confidence.map(function (item) {
      return '<span class="confidence-chip"><span>' + escapeHtml(item.label) + "</span><strong>" +
        escapeHtml(Math.round(item.value * 100)) + "%</strong></span>";
    }).join("");
  }

  function decisionPacketMarkup(packet, index) {
    const pathBadges = packet.analysisPaths.map(function (path) {
      return '<span class="path-label">' + escapeHtml(pathLabel(path)) + "</span>";
    }).join(" ");
    const aiOnly = packet.aiOnly
      ? '<span class="verify-badge">AI only · verify evidence</span>'
      : "";
    const templateText = packet.templateText || "No approved template clause is available for this finding.";
    const fallback = packet.fallback || "No fallback language supplied; draft manually.";
    const primaryQuote = packet.sourceEvidence.length ? packet.sourceEvidence[0].quote : null;
    const context = packet.sourceContext && packet.sourceContext !== primaryQuote
      ? '<div class="evidence-context"><span>Surrounding clause context</span><p>' +
        escapeHtml(packet.sourceContext) + "</p></div>"
      : "";

    return '<article class="decision-packet" id="finding-' + escapeHtml(index + 1) +
      '" data-finding-key="' + escapeHtml(packet.key) + '">' +
      '<header class="packet-header"><div class="packet-number">' + escapeHtml(index + 1) + "</div>" +
      '<div class="packet-title"><div class="packet-kicker"><span>' + escapeHtml(humanize(packet.clauseType)) +
      "</span>" + aiOnly + '</div><h3>' + escapeHtml(packet.title) + "</h3>" +
      '<p>' + escapeHtml(packet.ruleId || "No playbook rule") + "</p></div>" +
      badge(humanize(packet.severity), "severity") + "</header>" +
      '<div class="packet-facts"><span><strong>Contract</strong> ' + escapeHtml(packet.contractId) + "</span>" +
      '<span><strong>Clause ID</strong> ' + escapeHtml(packet.clauseId || "Not supplied") + "</span>" +
      '<span><strong>Analysis</strong> ' + pathBadges + "</span>" +
      '<span class="confidence-list"><strong>Confidence</strong> ' + confidenceMarkup(packet.confidence) + "</span></div>" +
      '<section class="evidence-section" aria-label="Contract evidence"><div class="section-label">' +
      '<span class="section-icon">“</span><div><strong>Contract evidence</strong>' +
      '<small>Read the source before accepting the claim</small></div></div>' +
      evidenceMarkup(packet.sourceEvidence) + context + "</section>" +
      '<div class="comparison-grid"><section><span class="section-caption">Approved template</span>' +
      '<p class="template-quote">' + escapeHtml(templateText) + "</p>" +
      '<small>' + escapeHtml(packet.templateClauseId || "No template clause ID") + "</small></section>" +
      '<section><span class="section-caption">What changed</span><p>' + escapeHtml(packet.whatChanged) +
      '</p><span class="section-caption section-caption--spaced">Why it matters</span><p>' +
      escapeHtml(packet.whyItMatters) + "</p></section></div>" +
      '<section class="playbook-position"><div><span class="section-caption">Preferred position</span><p>' +
      escapeHtml(packet.preferredPosition || "No playbook position supplied") + "</p></div>" +
      '<div><span class="section-caption">Acceptable fallback</span><p>' +
      escapeHtml(packet.acceptableFallback || "Escalate for a manual position") + "</p></div></section>" +
      '<section class="redline-panel"><div><span class="section-caption">Suggested redline</span>' +
      '<p>' + escapeHtml(fallback) + '</p></div><button class="copy-button" type="button" data-copy-redline' +
      ' aria-label="Copy suggested redline">Copy</button></section>' +
      '<textarea class="redline-editor" aria-label="Edit suggested redline" hidden>' + escapeHtml(fallback) +
      "</textarea>" +
      '<section class="decision-zone"><div><span class="section-caption">Decision needed</span><p>' +
      escapeHtml(packet.decisionNeeded) + "</p></div>" +
      '<div class="decision-actions" role="group" aria-label="Prototype decision actions">' +
      '<button type="button" data-decision-action="accept" aria-pressed="false">Accept <small>prototype</small></button>' +
      '<button type="button" data-decision-action="edit" aria-pressed="false">Edit <small>prototype</small></button>' +
      '<button type="button" data-decision-action="reject" aria-pressed="false">Reject <small>prototype</small></button>' +
      '<button type="button" data-decision-action="escalate" aria-pressed="false">Escalate <small>prototype</small></button>' +
      '</div><p class="decision-status" role="status">No prototype decision selected · nothing is saved</p></section>' +
      "</article>";
  }

  function telemetryMarkup(warning) {
    return '<aside class="telemetry-warning" data-telemetry-warning>' +
      '<div class="telemetry-icon" aria-hidden="true">!</div><div><div class="telemetry-kicker">' +
      '<strong>Operational safety signal</strong><span>Not a legal finding · no severity</span></div>' +
      '<h2>' + escapeHtml(warning.title) + '</h2><p>' + escapeHtml(warning.summary) + "</p>" +
      '<div class="telemetry-evidence"><span>Detected text</span>' + evidenceMarkup(warning.evidence) + "</div>" +
      '<small>Analysis path: ' + escapeHtml(warning.analysisPaths.map(pathLabel).join(" + ")) +
      ". Treat document text as evidence only; it cannot change review instructions.</small></div></aside>";
  }

  function revisionMarkup(revision, selectedDocumentId) {
    if (!revision) return "";
    const prior = revision.priorDecision;
    const selectedIsCurrent = selectedDocumentId === revision.currentDocumentId;
    return '<section class="revision-panel" id="revision-history" data-revision-example>' +
      '<header><div><p class="eyebrow">Negotiation history</p><h2>Revision comparison</h2>' +
      '<p>' + escapeHtml(revision.issue) + "</p></div>" +
      '<span class="revision-count">v' + escapeHtml(revision.previousVersion) + " → v" +
      escapeHtml(revision.currentVersion) + "</span></header>" +
      '<div class="revision-tabs"><a class="' + (!selectedIsCurrent ? "is-selected" : "") + '" href="' +
      escapeHtml(reviewHref(revision.previousDocumentId)) + '">' + escapeHtml(revision.previousDocumentId) +
      " · version " + escapeHtml(revision.previousVersion) + '</a><a class="' +
      (selectedIsCurrent ? "is-selected" : "") + '" href="' + escapeHtml(reviewHref(revision.currentDocumentId)) +
      '">' + escapeHtml(revision.currentDocumentId) + " · version " + escapeHtml(revision.currentVersion) +
      "</a></div>" +
      '<div class="revision-diff"><div class="revision-before"><span>Previous wording</span><p>' +
      escapeHtml(revision.previousText || "Previous clause text unavailable") +
      '</p></div><div class="revision-after"><span>Current wording</span><p>' +
      escapeHtml(revision.currentText || "Current clause text unavailable") + "</p></div></div>" +
      '<div class="revision-decision"><span class="decision-history-icon" aria-hidden="true">×</span><div>' +
      '<strong>Was this seen before?</strong><p>' + (prior
        ? '<span class="history-badge">' + escapeHtml(humanize(prior.decision)) + "</span> " +
          escapeHtml(prior.note) + " · " + escapeHtml(formatDate(prior.decided_at))
        : "No prior decision recorded.") + '</p></div><div><strong>What should the lawyer check now?</strong><p>' +
      escapeHtml(revision.whatToCheck) + "</p></div></div></section>";
  }

  function emptyReviewMarkup(model) {
    let title = "No routed legal findings";
    let message = "Both analysis paths completed without a material deviation. Confirm the source before completing the review.";
    if (model.route === "manual_review") {
      title = "Manual review required";
      message = "This contract type is unsupported, so no approved template or playbook was applied. Review the source document manually.";
    } else if (model.route === "duplicate") {
      title = "Duplicate removed from review";
      message = "This document matches " + model.duplicateOf + ". Use the original review record instead of making a second decision.";
    }
    return '<section class="empty-review" data-empty-review><span aria-hidden="true">✓</span><div><h2>' +
      escapeHtml(title) + "</h2><p>" + escapeHtml(message) + "</p>" +
      (model.duplicateOf ? '<a href="' + escapeHtml(reviewHref(model.duplicateOf)) + '">Open ' +
        escapeHtml(model.duplicateOf) + "</a>" : "") + "</div></section>";
  }

  function reviewMarkup(model) {
    const metadata = [
      ["Document ID", model.documentId],
      ["Contract type", model.contractType],
      ["Jurisdiction", model.jurisdiction],
      ["Contract version", "Version " + model.contractVersion],
      ["Template", model.template ? model.template.id + " · v" + model.template.version : "Not matched"],
      ["Playbook", model.playbook ? model.playbook.id + " · v" + model.playbook.version : "Not matched"],
      ["Analysis path", model.analysisPath],
      ["Received", formatDate(model.receivedAt)]
    ];
    const metadataMarkup = metadata.map(function (item) {
      return "<div><dt>" + escapeHtml(item[0]) + "</dt><dd>" + escapeHtml(item[1]) + "</dd></div>";
    }).join("");
    const navigation = model.findings.map(function (packet, index) {
      return '<a href="#finding-' + escapeHtml(index + 1) + '"><span>' + escapeHtml(index + 1) + "</span>" +
        escapeHtml(packet.title) + badge(humanize(packet.severity), "severity") + "</a>";
    }).join("");
    const packets = model.findings.map(decisionPacketMarkup).join("");
    const telemetry = model.telemetryWarnings.map(telemetryMarkup).join("");
    const falseAlarmBanner = model.falseAlarmExample
      ? '<aside class="false-alarm-banner" data-false-alarm-example><span aria-hidden="true">i</span><div>' +
        '<strong>False-alarm review exercise</strong><p>These three AI-only flags are known false alarms. ' +
        "Read each evidence quote—it may contradict the claim—then use Reject (prototype) to record the human check.</p></div></aside>"
      : "";

    return '<header class="review-hero"><div><p class="eyebrow">Contract review</p><div class="review-title-line">' +
      '<h1 id="review-title">' + escapeHtml(model.documentId) + '</h1><span class="status status--' +
      escapeHtml(model.analysisStatus.toLowerCase().replace(/\s+/g, "-")) + '"><span aria-hidden="true"></span>' +
      escapeHtml(model.analysisStatus) + '</span></div><p class="review-counterparty">' +
      escapeHtml(model.counterparty) + " · " + escapeHtml(model.contractType) + "</p></div>" +
      '<div class="review-route"><span>Final route</span><strong>' + escapeHtml(model.routeLabel) + "</strong>" +
      badge(model.risk, "risk") + "</div></header>" +
      '<dl class="review-metadata">' + metadataMarkup + "</dl>" +
      telemetry + falseAlarmBanner +
      '<div class="review-layout"><aside class="review-sidebar"><div class="sidebar-card sidebar-summary">' +
      '<span>Decision summary</span><strong><span id="decision-progress">0</span> / ' +
      escapeHtml(model.findings.length) + '</strong><p>prototype decisions made</p><div class="progress-track"><span id="decision-progress-bar"></span></div>' +
      '</div><nav class="finding-nav" aria-label="Findings in this review"><span>On this review</span>' +
      (navigation || '<p class="muted">No legal findings</p>') +
      (model.revision ? '<a href="#revision-history"><span>↕</span>Revision history</a>' : "") +
      '</nav><div class="prototype-disclaimer"><strong>Prototype only</strong><p>Actions update this screen only. ' +
      "Nothing is persisted or sent.</p></div></aside>" +
      '<div class="review-main">' + revisionMarkup(model.revision, model.documentId) +
      '<section class="findings-heading"><div><p class="eyebrow">Decision packets</p><h2>' +
      escapeHtml(model.findings.length) + " legal finding" + (model.findings.length === 1 ? "" : "s") +
      '</h2></div><span>Evidence · position · decision</span></section>' +
      (packets || emptyReviewMarkup(model)) + "</div></div>";
  }

  function highRiskMissCount(records, path) {
    return records.reduce(function (total, record) {
      const highRisk = new Set((record.expected_findings || [])
        .filter(function (finding) { return finding.severity === "high"; })
        .map(function (finding) { return finding.candidate_issue; }));
      return total + (record[path + "_misses"] || []).filter(function (issue) {
        return highRisk.has(issue);
      }).length;
    }, 0);
  }

  function buildEvaluationSummary(records) {
    const paths = ["ai", "baseline", "union"];
    const pathTotals = {};
    paths.forEach(function (path) {
      pathTotals[path] = {
        hits: records.reduce(function (sum, record) {
          return sum + (record[path + "_hits"] || []).length;
        }, 0),
        misses: records.reduce(function (sum, record) {
          return sum + (record[path + "_misses"] || []).length;
        }, 0),
        falseAlarms: records.reduce(function (sum, record) {
          return sum + (record[path + "_false_alarms"] || []).length;
        }, 0),
        highRiskMisses: highRiskMissCount(records, path)
      };
    });
    const knownCosts = records.map(function (record) { return record.ai_cost_usd; })
      .filter(function (value) { return typeof value === "number"; });

    return {
      documents: records.length,
      routeCorrect: records.filter(function (record) { return record.route_correct; }).length,
      highRiskMisses: records.filter(function (record) { return record.high_risk_miss; }).length,
      unsupportedRouted: records.filter(function (record) { return record.unsupported_routed; }).length,
      duplicatesDetected: records.filter(function (record) { return record.duplicate_detected; }).length,
      validationFailures: records.filter(function (record) { return record.validation_failure; }).length,
      totalTokens: records.reduce(function (sum, record) { return sum + (record.ai_total_tokens || 0); }, 0),
      totalWallTimeMs: records.reduce(function (sum, record) { return sum + (record.ai_wall_time_ms || 0); }, 0),
      totalCostUsd: knownCosts.length
        ? knownCosts.reduce(function (sum, value) { return sum + value; }, 0)
        : null,
      costCoverage: knownCosts.length,
      paths: pathTotals
    };
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("en-GB");
  }

  function formatRoute(route) {
    return routeMeta(route).label;
  }

  function issueListMarkup(issues, tone) {
    if (!issues || !issues.length) return '<span class="evaluation-none">None</span>';
    return issues.map(function (issue) {
      const value = typeof issue === "string" ? issue : issue.candidate_issue;
      const severity = typeof issue === "string" ? null : issue.severity;
      return '<span class="issue-chip issue-chip--' + escapeHtml(tone || severity || "neutral") + '">' +
        escapeHtml(humanize(value)) + (severity ? " · " + escapeHtml(humanize(severity)) : "") + "</span>";
    }).join(" ");
  }

  function pathOutcomeMarkup(record, path) {
    const hits = record[path + "_hits"] || [];
    const misses = record[path + "_misses"] || [];
    const falseAlarms = record[path + "_false_alarms"] || [];
    if (!misses.length && !falseAlarms.length) {
      return '<span class="outcome outcome--clean"><span aria-hidden="true">✓</span> ' +
        escapeHtml(hits.length) + " hit" + (hits.length === 1 ? "" : "s") + "</span>";
    }
    return '<span class="outcome outcome--miss" title="' +
      escapeHtml(misses.map(humanize).join(", ")) + '">−' + escapeHtml(misses.length) + " miss</span> " +
      '<span class="outcome outcome--false" title="' +
      escapeHtml(falseAlarms.map(humanize).join(", ")) + '">+' + escapeHtml(falseAlarms.length) + " false</span>";
  }

  function evaluationMarkup(records) {
    const summary = buildEvaluationSummary(records);
    const pathRows = [
      ["AI", "ai", "Semantic comparison"],
      ["Deterministic rules", "baseline", "Keyword + structural checks"],
      ["Union", "union", "Safer combined result"]
    ].map(function (definition) {
      const totals = summary.paths[definition[1]];
      return "<tr><td><strong>" + escapeHtml(definition[0]) + "</strong><small>" +
        escapeHtml(definition[2]) + "</small></td><td>" + escapeHtml(totals.hits) +
        "</td><td>" + escapeHtml(totals.misses) + "</td><td>" + escapeHtml(totals.falseAlarms) +
        '</td><td><strong class="' + (totals.highRiskMisses ? "metric-bad" : "metric-good") + '">' +
        escapeHtml(totals.highRiskMisses) + "</strong></td></tr>";
    }).join("");
    const failureRecords = records.filter(function (record) { return record.notes; });
    const failureMarkup = failureRecords.map(function (record) {
      return '<article class="failure-note"><header><a href="' + escapeHtml(reviewHref(record.document_id)) + '">' +
        escapeHtml(record.document_id) + '</a><span class="' + (record.route_correct ? "route-match" : "route-mismatch") + '">' +
        (record.route_correct ? "Route matched" : "Route mismatch") + "</span></header><p>" +
        escapeHtml(record.notes) + "</p></article>";
    }).join("");
    const documentRows = records.map(function (record) {
      const routeClass = record.route_correct ? "route-match" : "route-mismatch";
      return '<tr><td><a class="document-link" href="' + escapeHtml(reviewHref(record.document_id)) + '">' +
        escapeHtml(record.document_id) + "</a></td><td>" + issueListMarkup(record.expected_findings) +
        "</td><td>" + pathOutcomeMarkup(record, "ai") + "</td><td>" + pathOutcomeMarkup(record, "baseline") +
        "</td><td>" + pathOutcomeMarkup(record, "union") + '</td><td><div class="route-comparison"><span>' +
        escapeHtml(formatRoute(record.expected_route)) + '</span><span aria-hidden="true">→</span><strong>' +
        escapeHtml(formatRoute(record.actual_route)) + '</strong></div><span class="' + routeClass + '">' +
        (record.route_correct ? "Matched" : "Mismatch") + "</span></td><td>" +
        (record.ai_total_tokens == null ? '<span class="evaluation-none">Not available</span>' :
          '<strong>' + escapeHtml(formatNumber(record.ai_total_tokens)) + "</strong> tokens<br><small>" +
          escapeHtml((record.ai_wall_time_ms / 1000).toFixed(1)) + "s</small>") + "</td></tr>";
    }).join("");
    const totalCost = summary.totalCostUsd == null
      ? "Cost unavailable"
      : "$" + summary.totalCostUsd.toFixed(6);

    return '<div class="evaluation-hero"><div><p class="eyebrow">Harness validation</p>' +
      '<h1 id="evaluation-title">Evaluation smoke test</h1><p>Per-document comparison of the frozen AI, ' +
      "deterministic baseline, and safer union paths.</p></div>" +
      '<a class="back-link evaluation-back" href="index.html"><span aria-hidden="true">←</span> Review queue</a></div>' +
      '<aside class="sample-warning"><span aria-hidden="true">i</span><p>This is an 11-document synthetic smoke test. ' +
      "It is used to test the harness and expose failure modes. It is not a legal benchmark.</p></aside>" +
      '<section class="evaluation-cards" aria-label="Evaluation summary"><article class="evaluation-card evaluation-card--primary">' +
      '<span>Union high-risk misses</span><strong>' + escapeHtml(summary.highRiskMisses) +
      '</strong><p>AI ' + escapeHtml(summary.paths.ai.highRiskMisses) + " · Rules " +
      escapeHtml(summary.paths.baseline.highRiskMisses) + "</p></article>" +
      '<article class="evaluation-card"><span>Routes matched</span><strong>' + escapeHtml(summary.routeCorrect) +
      '<small> / ' + escapeHtml(summary.documents) + "</small></strong><p>Compared with expected routing</p></article>" +
      '<article class="evaluation-card"><span>Controlled edge cases</span><strong>' +
      escapeHtml(summary.unsupportedRouted + summary.duplicatesDetected) +
      '</strong><p>' + escapeHtml(summary.unsupportedRouted) + " unsupported · " +
      escapeHtml(summary.duplicatesDetected) + " duplicate</p></article>" +
      '<article class="evaluation-card"><span>Validation failures</span><strong>' +
      escapeHtml(summary.validationFailures) + "</strong><p>Fail closed to manual review</p></article></section>" +
      '<section class="evaluation-provenance"><div><span class="source-dot" aria-hidden="true"></span><div>' +
      '<strong>Generated, not hard-coded</strong><p>Rendered from <code>window.DATA.evaluation</code>. Run ' +
      '<code>python3 evaluation/evaluate.py</code> to rebuild CSV, JSON, and this browser snapshot.</p></div></div>' +
      '<div class="runtime-summary"><span><strong>' + escapeHtml(formatNumber(summary.totalTokens)) +
      "</strong> AI tokens</span><span><strong>" + escapeHtml((summary.totalWallTimeMs / 1000).toFixed(1)) +
      "s</strong> wall time</span><span><strong>" + escapeHtml(totalCost) + "</strong> recorded cost</span></div></section>" +
      '<section class="evaluation-panel"><header><div><p class="eyebrow">Path comparison</p>' +
      '<h2>What each path caught</h2></div><p>Issue counts, not percentages</p></header>' +
      '<div class="table-wrap"><table class="path-table"><thead><tr><th>Path</th><th>Hits</th><th>Misses</th>' +
      "<th>False alarms</th><th>High-risk misses</th></tr></thead><tbody>" + pathRows +
      "</tbody></table></div></section>" +
      '<section class="evaluation-panel"><header><div><p class="eyebrow">Failure analysis</p>' +
      '<h2>What failed, in plain English</h2></div><p>' + escapeHtml(failureRecords.length) +
      ' documents with notes</p></header><div class="failure-grid">' + failureMarkup + "</div></section>" +
      '<section class="evaluation-panel"><header><div><p class="eyebrow">Document detail</p>' +
      '<h2>All ' + escapeHtml(summary.documents) + ' smoke-test documents</h2></div><p>Open any ID to inspect evidence</p></header>' +
      '<div class="table-wrap"><table class="evaluation-table"><thead><tr><th>Document</th><th>Expected issues</th>' +
      "<th>AI</th><th>Rules</th><th>Union</th><th>Expected → actual route</th><th>AI run</th></tr></thead><tbody>" +
      documentRows + "</tbody></table></div></section>" +
      '<footer class="page-footer"><span>Source: evaluation/results.json</span>' +
      '<span>No aggregate precision or recall is claimed</span></footer>';
  }

  function rowMarkup(row) {
    const featured = row.featured
      ? '<span class="recommended">Start here</span>'
      : "";
    const duplicateClass = row.duplicateOf ? " duplicate-row" : "";
    return '<tr class="queue-row' + duplicateClass + '" data-document-id="' + escapeHtml(row.documentId) +
      '" data-review-href="' + escapeHtml(reviewHref(row.documentId)) + '" tabindex="0">' +
      '<td><a class="document-link" href="' + escapeHtml(reviewHref(row.documentId)) + '">' +
      escapeHtml(row.documentId) + "</a>" + featured + "</td>" +
      "<td><strong>" + escapeHtml(row.counterparty) + "</strong></td>" +
      "<td>" + escapeHtml(row.contractType) + "</td>" +
      "<td>" + badge(row.risk, "risk") + "</td>" +
      '<td class="numeric finding-count">' + escapeHtml(row.findingCount) +
      (row.injectionSignal
        ? ' <span class="badge badge--signal-injection">Injection attempt</span>'
        : "") + "</td>" +
      "<td>" + badge(row.highestSeverity ? humanize(row.highestSeverity) : "None", "severity") + "</td>" +
      "<td><span class=\"route-label\">" + escapeHtml(row.routeLabel) + "</span></td>" +
      "<td><span class=\"status status--" + escapeHtml(row.analysisStatus.toLowerCase().replace(/\s+/g, "-")) +
      '\"><span aria-hidden="true"></span>' + escapeHtml(row.analysisStatus) + "</span></td>" +
      "<td>" + (row.duplicateOf
        ? '<span class="duplicate-label">' + escapeHtml(row.duplicateStatus) + "</span>"
        : '<span class="muted">Not duplicate</span>') + "</td>" +
      "<td><span class=\"path-label\">" + escapeHtml(row.analysisPath) + "</span></td>" +
      '<td class="open-cell" aria-hidden="true">→</td>' +
      "</tr>";
  }

  function updateSummary(rows) {
    const count = function (route) {
      return rows.filter(function (row) { return row.route === route; }).length;
    };
    document.querySelector("#senior-count").textContent = count("senior_legal_review");
    document.querySelector("#manual-count").textContent = count("manual_review");
    document.querySelector("#quick-count").textContent = count("quick_check");
    document.querySelector("#duplicate-count").textContent = count("duplicate");
    document.querySelector("#queue-count").textContent = rows.length + " contracts";
  }

  function bindRowNavigation() {
    document.querySelectorAll("[data-review-href]").forEach(function (row) {
      row.addEventListener("click", function (event) {
        if (event.target.closest("a")) return;
        window.location.href = row.dataset.reviewHref;
      });
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          window.location.href = row.dataset.reviewHref;
        }
      });
    });
  }

  function setActiveNav(view) {
    document.querySelectorAll("[data-app-nav]").forEach(function (link) {
      if (link.dataset.appNav === view) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function showQueue(data) {
    const rows = sortQueueRows(buildQueueRows(data));
    document.querySelector("#queue-body").innerHTML = rows.map(rowMarkup).join("");
    updateSummary(rows);
    bindRowNavigation();
    setActiveNav("queue");
    document.documentElement.dataset.view = "queue";
  }

  function bindReviewActions(model) {
    let decisions = {};
    document.querySelectorAll("[data-decision-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        const packet = button.closest("[data-finding-key]");
        const action = button.dataset.decisionAction;
        decisions = applyDecisionState(decisions, packet.dataset.findingKey, action);
        packet.dataset.decision = action;
        packet.querySelectorAll("[data-decision-action]").forEach(function (candidate) {
          candidate.setAttribute("aria-pressed", String(candidate === button));
        });
        const editor = packet.querySelector(".redline-editor");
        editor.hidden = action !== "edit";
        if (action === "edit") editor.focus();
        packet.querySelector(".decision-status").textContent =
          "Local prototype state: " + humanize(action) + " · not saved";
        const completed = Object.keys(decisions).length;
        document.querySelector("#decision-progress").textContent = completed;
        document.querySelector("#decision-progress-bar").style.width =
          (model.findings.length ? (completed / model.findings.length) * 100 : 0) + "%";
      });
    });

    document.querySelectorAll("[data-copy-redline]").forEach(function (button) {
      button.addEventListener("click", function () {
        const packet = button.closest("[data-finding-key]");
        const text = packet.querySelector(".redline-editor").value;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            button.textContent = "Copied";
          }).catch(function () {
            button.textContent = "Select text";
          });
        } else {
          packet.querySelector(".redline-editor").hidden = false;
          packet.querySelector(".redline-editor").select();
          button.textContent = "Select text";
        }
      });
    });
  }

  function showReview(data, documentId) {
    const model = buildReviewModel(data, documentId);
    document.querySelector("#review-content").innerHTML = reviewMarkup(model);
    document.querySelector("#queue-view").hidden = true;
    document.querySelector("#review-view").hidden = false;
    bindReviewActions(model);
    setActiveNav("queue");
    document.title = model.documentId + " review · ContractDiff Demo";
    document.documentElement.dataset.view = "review";
    document.documentElement.dataset.documentId = model.documentId;
  }

  function showEvaluation(records) {
    if (!Array.isArray(records) || !records.length) {
      throw new Error("generated evaluation data is missing");
    }
    document.querySelector("#evaluation-content").innerHTML = evaluationMarkup(records);
    document.querySelector("#queue-view").hidden = true;
    document.querySelector("#review-view").hidden = true;
    document.querySelector("#evaluation-view").hidden = false;
    setActiveNav("evaluation");
    document.title = "Evaluation smoke test · ContractDiff Demo";
    document.documentElement.dataset.view = "evaluation";
  }

  function showError(error) {
    const banner = document.querySelector("#queue-error");
    banner.hidden = false;
    banner.textContent = "The queue could not be loaded: " + error.message;
    document.documentElement.dataset.view = "error";
  }

  function init() {
    try {
      const data = window.DATA || {};
      if (!Array.isArray(data.contracts) || !Array.isArray(data.templates) ||
          !Array.isArray(data.playbook) || !Array.isArray(data.routing)) {
        throw new Error("frozen contract, template, playbook, or routing data is missing");
      }
      const parameters = new URLSearchParams(window.location.search);
      const documentId = parameters.get("document");
      const view = parameters.get("view");
      if (documentId) showReview(data, documentId);
      else if (view === "evaluation") showEvaluation(data.evaluation);
      else showQueue(data);
    } catch (error) {
      showError(error);
      console.error(error);
    }
  }

  const api = {
    analysisPath: analysisPath,
    analysisStatus: analysisStatus,
    applyDecisionState: applyDecisionState,
    buildEvaluationSummary: buildEvaluationSummary,
    buildQueueRows: buildQueueRows,
    buildReviewModel: buildReviewModel,
    buildRevisionExample: buildRevisionExample,
    extractClause: extractClause,
    highestSeverity: highestSeverity,
    humanize: humanize,
    evaluationMarkup: evaluationMarkup,
    reviewMarkup: reviewMarkup,
    reviewHref: reviewHref,
    sortQueueRows: sortQueueRows
  };

  if (typeof window !== "undefined") window.QueueApp = api;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
  }
}());
