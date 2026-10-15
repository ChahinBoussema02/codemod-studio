/**
 * Webview showing the live run: agent timeline, findings and refactor diffs.
 *
 * The panel is a singleton -- re-running analysis reuses the open tab rather
 * than stacking editors. All styling comes from VS Code's own CSS variables so
 * the panel follows the user's theme without a theme-detection dance.
 */

import * as vscode from 'vscode';
import type {
  AgentTrace,
  RefactorProposal,
  RiskSummary,
  SecurityFinding,
} from '../api/types';

interface PanelState {
  filePath: string;
  phase: string;
  findings: SecurityFinding[];
  proposals: RefactorProposal[];
  traces: AgentTrace[];
  summary: RiskSummary | null;
  warnings: string[];
}

/** Message sent from the webview back to the extension host. */
type InboundMessage =
  | { type: 'reveal'; line: number }
  | { type: 'applyProposal'; proposalId: string };

export class ResultsPanel {
  private static instance: ResultsPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];
  private state: PanelState;

  private constructor(
    private readonly extensionUri: vscode.Uri,
    filePath: string,
    private readonly onApply: (proposalId: string) => void,
  ) {
    this.state = {
      filePath,
      phase: 'pending',
      findings: [],
      proposals: [],
      traces: [],
      summary: null,
      warnings: [],
    };

    this.panel = vscode.window.createWebviewPanel(
      'codemodStudio.results',
      'CodeMod Studio',
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      },
    );

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(
      (message: InboundMessage) => this.handleMessage(message),
      null,
      this.disposables,
    );

    this.panel.webview.html = this.renderShell();
  }

  static show(
    extensionUri: vscode.Uri,
    filePath: string,
    onApply: (proposalId: string) => void,
  ): ResultsPanel {
    if (ResultsPanel.instance) {
      ResultsPanel.instance.reset(filePath);
      ResultsPanel.instance.panel.reveal(vscode.ViewColumn.Beside, true);
      return ResultsPanel.instance;
    }
    ResultsPanel.instance = new ResultsPanel(extensionUri, filePath, onApply);
    return ResultsPanel.instance;
  }

  // -- incremental updates, driven by the SSE stream ------------------------ //

  reset(filePath: string): void {
    this.state = {
      filePath,
      phase: 'pending',
      findings: [],
      proposals: [],
      traces: [],
      summary: null,
      warnings: [],
    };
    this.push();
  }

  setPhase(phase: string): void {
    this.state.phase = phase;
    this.push();
  }

  addFinding(finding: SecurityFinding): void {
    this.state.findings.push(finding);
    this.state.findings.sort((a, b) => severityRank(b.severity) - severityRank(a.severity));
    this.push();
  }

  addProposal(proposal: RefactorProposal): void {
    this.state.proposals.push(proposal);
    this.push();
  }

  setSummary(summary: RiskSummary): void {
    this.state.summary = summary;
    this.push();
  }

  setTraces(traces: AgentTrace[]): void {
    this.state.traces = traces;
    this.push();
  }

  addWarning(warning: string): void {
    this.state.warnings.push(warning);
    this.push();
  }

  findProposal(id: string): RefactorProposal | undefined {
    return this.state.proposals.find((p) => p.id === id);
  }

  private push(): void {
    void this.panel.webview.postMessage({ type: 'state', state: this.state });
  }

  private async handleMessage(message: InboundMessage): Promise<void> {
    switch (message.type) {
      case 'reveal': {
        const document = await vscode.workspace.openTextDocument(this.state.filePath);
        const editor = await vscode.window.showTextDocument(document, vscode.ViewColumn.One);
        const position = new vscode.Position(Math.max(message.line - 1, 0), 0);
        editor.selection = new vscode.Selection(position, position);
        editor.revealRange(
          new vscode.Range(position, position),
          vscode.TextEditorRevealType.InCenter,
        );
        break;
      }
      case 'applyProposal':
        this.onApply(message.proposalId);
        break;
    }
  }

  private renderShell(): string {
    const { webview } = this.panel;
    const script = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, 'media', 'panel.js'),
    );
    const style = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, 'media', 'panel.css'),
    );
    const nonce = createNonce();

    // A strict CSP is mandatory for a webview that renders model output:
    // only our nonce-tagged script may run, and nothing may be fetched.
    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none';
                 style-src ${webview.cspSource};
                 script-src 'nonce-${nonce}';
                 font-src ${webview.cspSource};">
  <link href="${style}" rel="stylesheet">
  <title>CodeMod Studio</title>
</head>
<body>
  <header id="summary" class="summary">
    <div class="grade" id="grade">-</div>
    <div class="headline">
      <h1 id="file">Waiting for analysis…</h1>
      <p id="headline">The agents have not reported yet.</p>
    </div>
  </header>

  <section class="timeline" id="timeline"></section>
  <section id="warnings" class="warnings" hidden></section>

  <nav class="tabs">
    <button class="tab active" data-tab="findings">Findings <span id="findings-count">0</span></button>
    <button class="tab" data-tab="proposals">Refactors <span id="proposals-count">0</span></button>
  </nav>

  <section id="findings" class="tab-body"></section>
  <section id="proposals" class="tab-body" hidden></section>

  <script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }

  dispose(): void {
    ResultsPanel.instance = undefined;
    this.panel.dispose();
    while (this.disposables.length) {
      this.disposables.pop()?.dispose();
    }
  }
}

function severityRank(severity: string): number {
  return { info: 0, low: 1, medium: 2, high: 3, critical: 4 }[severity] ?? 0;
}

function createNonce(): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from({ length: 32 }, () =>
    alphabet.charAt(Math.floor(Math.random() * alphabet.length)),
  ).join('');
}
