// @ts-check
/**
 * Webview front-end.
 *
 * Renders state pushed from the extension host. Every value that originates
 * from a language model is inserted with `textContent`, never `innerHTML` --
 * model output is untrusted input and this webview has script privileges.
 */
(function () {
  const vscode = acquireVsCodeApi();

  /** @type {Record<string, string>} */
  const SEVERITY_LABEL = {
    critical: 'Critical',
    high: 'High',
    medium: 'Medium',
    low: 'Low',
    info: 'Info',
  };

  const el = (id) => /** @type {HTMLElement} */ (document.getElementById(id));

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderSummary(state) {
    el('file').textContent = state.filePath.split(/[\\/]/).pop() || state.filePath;
    const grade = el('grade');
    if (state.summary) {
      grade.textContent = state.summary.grade;
      grade.dataset.grade = state.summary.grade;
      el('headline').textContent = state.summary.headline;
    } else {
      grade.textContent = '…';
      el('headline').textContent = `Phase: ${state.phase}`;
    }
  }

  function renderTimeline(state) {
    const container = el('timeline');
    container.replaceChildren();
    const phases = ['parsing', 'auditing', 'refactoring', 'reporting', 'completed'];
    const current = phases.indexOf(state.phase);
    phases.forEach((phase, index) => {
      const step = element('div', 'step', phase);
      if (index < current) step.classList.add('done');
      if (index === current) step.classList.add('active');
      container.appendChild(step);
    });
  }

  function renderWarnings(state) {
    const container = el('warnings');
    container.replaceChildren();
    container.hidden = state.warnings.length === 0;
    state.warnings.forEach((warning) => {
      container.appendChild(element('div', 'warning', warning));
    });
  }

  function renderFindings(state) {
    const container = el('findings');
    container.replaceChildren();
    el('findings-count').textContent = String(state.findings.length);

    if (state.findings.length === 0) {
      container.appendChild(element('p', 'empty', 'No findings reported.'));
      return;
    }

    state.findings.forEach((finding) => {
      const card = element('article', `card sev-${finding.severity}`);

      const head = element('div', 'card-head');
      head.appendChild(
        element('span', 'badge', SEVERITY_LABEL[finding.severity] || finding.severity),
      );
      head.appendChild(element('h2', null, finding.title));
      card.appendChild(head);

      const meta = element('div', 'meta');
      meta.appendChild(element('span', null, `line ${finding.line_start}`));
      if (finding.symbol) meta.appendChild(element('span', null, finding.symbol));
      if (finding.cwe_id) meta.appendChild(element('span', null, finding.cwe_id));
      meta.appendChild(element('span', 'detector', finding.detector));
      meta.appendChild(
        element('span', null, `${Math.round(finding.confidence * 100)}% confidence`),
      );
      card.appendChild(meta);

      card.appendChild(element('p', null, finding.description));
      const fix = element('p', 'remediation');
      fix.appendChild(element('strong', null, 'Fix: '));
      fix.appendChild(document.createTextNode(finding.remediation));
      card.appendChild(fix);

      const goTo = element('button', 'link', 'Go to line');
      goTo.addEventListener('click', () =>
        vscode.postMessage({ type: 'reveal', line: finding.line_start }),
      );
      card.appendChild(goTo);

      container.appendChild(card);
    });
  }

  function renderProposals(state) {
    const container = el('proposals');
    container.replaceChildren();
    el('proposals-count').textContent = String(state.proposals.length);

    if (state.proposals.length === 0) {
      container.appendChild(element('p', 'empty', 'No refactors proposed.'));
      return;
    }

    state.proposals.forEach((proposal) => {
      const card = element('article', 'card');
      card.appendChild(element('h2', null, proposal.target_symbol));

      const meta = element('div', 'meta');
      meta.appendChild(element('span', null, proposal.strategy.replace(/_/g, ' ')));
      meta.appendChild(element('span', null, `risk: ${proposal.estimated_risk}`));
      if (proposal.syntax_validated) {
        meta.appendChild(element('span', 'validated', 'syntax verified'));
      }
      card.appendChild(meta);

      card.appendChild(element('p', null, proposal.rationale));

      const pre = element('pre', 'code');
      pre.appendChild(element('code', null, proposal.proposed_code));
      card.appendChild(pre);

      const apply = element('button', 'primary', 'Preview and apply');
      apply.addEventListener('click', () =>
        vscode.postMessage({ type: 'applyProposal', proposalId: proposal.id }),
      );
      card.appendChild(apply);

      container.appendChild(card);
    });
  }

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.getAttribute('data-tab');
      el('findings').hidden = target !== 'findings';
      el('proposals').hidden = target !== 'proposals';
    });
  });

  window.addEventListener('message', (event) => {
    const message = event.data;
    if (message.type !== 'state') return;
    const { state } = message;
    renderSummary(state);
    renderTimeline(state);
    renderWarnings(state);
    renderFindings(state);
    renderProposals(state);
    vscode.setState(state);
  });

  // Restore after the webview is backgrounded and revived.
  const restored = vscode.getState();
  if (restored) {
    window.postMessage({ type: 'state', state: restored }, '*');
  }
})();
