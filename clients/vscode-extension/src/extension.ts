/**
 * Extension entry point.
 *
 * Wires four things together: the orchestrator client, the Problems-panel
 * diagnostics, the results webview and a status-bar item. The full pipeline
 * is always run through the streaming endpoint so the user sees findings as
 * the agents produce them rather than after a silent minute.
 */

import * as vscode from 'vscode';
import { OrchestratorClient, OrchestratorError } from './api/client';
import type { RefactorProposal } from './api/types';
import { DiagnosticsManager, complexityDiagnostics, findingToDiagnostic } from './diagnostics';
import { ResultsPanel } from './views/ResultsPanel';

let diagnostics: DiagnosticsManager;
let statusBar: vscode.StatusBarItem;
/** Cancels an in-flight run when the user starts another one. */
let activeRun: AbortController | undefined;

function makeClient(): OrchestratorClient {
  const config = vscode.workspace.getConfiguration('codemodStudio');
  return new OrchestratorClient({
    baseUrl: config.get<string>('orchestratorUrl', 'http://localhost:8000'),
    auditModel: config.get<string>('auditModel', 'qwen2.5-coder:7b'),
    refactorModel: config.get<string>('refactorModel', 'qwen2.5-coder:14b'),
  });
}

export function activate(context: vscode.ExtensionContext): void {
  diagnostics = new DiagnosticsManager();

  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = 'codemodStudio.analyzeFile';
  statusBar.text = '$(shield) CodeMod';
  statusBar.tooltip = 'Run the local security and refactoring agents';
  statusBar.show();

  context.subscriptions.push(
    diagnostics,
    statusBar,
    vscode.commands.registerCommand('codemodStudio.analyzeFile', analyzeActiveFile),
    vscode.commands.registerCommand('codemodStudio.parseFile', refreshMetrics),
    vscode.commands.registerCommand('codemodStudio.checkHealth', checkHealth),
    vscode.commands.registerCommand('codemodStudio.showResults', () => {
      const editor = vscode.window.activeTextEditor;
      if (editor) {
        ResultsPanel.show(context.extensionUri, editor.document.uri.fsPath, () => undefined);
      }
    }),
    vscode.workspace.onDidSaveTextDocument(async (document) => {
      if (document.languageId !== 'python') {
        return;
      }
      const config = vscode.workspace.getConfiguration('codemodStudio');
      if (config.get<boolean>('analyzeOnSave', false)) {
        await analyzeActiveFile();
      } else if (config.get<boolean>('metricsOnSave', true)) {
        await refreshMetrics();
      }
    }),
    vscode.workspace.onDidCloseTextDocument((document) => diagnostics.clear(document)),
  );

  context.subscriptions.push({
    dispose: () => activeRun?.abort(),
  });
}

export function deactivate(): void {
  activeRun?.abort();
}

// --------------------------------------------------------------------------
// Commands
// --------------------------------------------------------------------------

/** Cheap structural pass: complexity metrics only, no inference. */
async function refreshMetrics(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.languageId !== 'python') {
    return;
  }

  const threshold = vscode.workspace
    .getConfiguration('codemodStudio')
    .get<number>('complexityThreshold', 10);

  try {
    const { module } = await makeClient().parse(
      editor.document.uri.fsPath,
      editor.document.getText(),
    );
    diagnostics.replace(
      editor.document,
      complexityDiagnostics(editor.document, module, threshold),
    );
  } catch (error) {
    // Metrics run on save; a failure here must stay quiet or it becomes a
    // notification storm. The explicit commands report errors loudly.
    console.warn('CodeMod Studio: metrics refresh failed', error);
  }
}

/** Full pipeline, streamed into the panel and the Problems view. */
async function analyzeActiveFile(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.languageId !== 'python') {
    void vscode.window.showWarningMessage('CodeMod Studio: open a Python file first.');
    return;
  }

  activeRun?.abort();
  const controller = new AbortController();
  activeRun = controller;

  const document = editor.document;
  const filePath = document.uri.fsPath;
  const extensionUri = vscode.extensions.getExtension('codemod-studio.codemod-studio')!
    .extensionUri;

  const panel = ResultsPanel.show(extensionUri, filePath, (proposalId) => {
    const proposal = panel.findProposal(proposalId);
    if (proposal) {
      void applyProposal(document, proposal);
    }
  });

  const collected: vscode.Diagnostic[] = [];
  statusBar.text = '$(sync~spin) CodeMod: analysing';

  try {
    for await (const event of makeClient().streamAnalysis(
      filePath,
      document.getText(),
      controller.signal,
    )) {
      switch (event.event) {
        case 'phase':
          panel.setPhase(event.data.phase);
          statusBar.text = `$(sync~spin) CodeMod: ${event.data.phase}`;
          break;
        case 'finding':
          panel.addFinding(event.data);
          collected.push(findingToDiagnostic(document, event.data));
          diagnostics.replace(document, collected);
          break;
        case 'proposal':
          panel.addProposal(event.data);
          break;
        case 'summary':
          panel.setSummary(event.data);
          statusBar.text = `$(shield) CodeMod: ${event.data.grade} (${event.data.total_findings})`;
          break;
        case 'error':
          panel.addWarning(event.data.message);
          void vscode.window.showErrorMessage(`CodeMod Studio: ${event.data.message}`);
          break;
        case 'done':
          break;
      }
    }
  } catch (error) {
    if (controller.signal.aborted) {
      return; // Superseded by a newer run; stay silent.
    }
    statusBar.text = '$(warning) CodeMod';
    const message =
      error instanceof OrchestratorError
        ? error.message
        : 'Could not reach the orchestrator. Is it running on the configured URL?';
    void vscode.window.showErrorMessage(`CodeMod Studio: ${message}`, 'Check status').then(
      (choice) => {
        if (choice === 'Check status') {
          void vscode.commands.executeCommand('codemodStudio.checkHealth');
        }
      },
    );
  } finally {
    if (activeRun === controller) {
      activeRun = undefined;
    }
  }
}

/**
 * Show the proposal as a diff and let the user apply it.
 *
 * Generated code is never written silently: the user reviews a real diff and
 * confirms. The replacement is applied via a `WorkspaceEdit`, so a single
 * Ctrl+Z undoes it.
 */
async function applyProposal(
  document: vscode.TextDocument,
  proposal: RefactorProposal,
): Promise<void> {
  const original = new vscode.Range(
    new vscode.Position(proposal.line_start - 1, 0),
    document.lineAt(Math.min(proposal.line_end - 1, document.lineCount - 1)).range.end,
  );

  const choice = await vscode.window.showInformationMessage(
    `Replace ${proposal.target_symbol} (lines ${proposal.line_start}-${proposal.line_end})?`,
    { modal: true, detail: proposal.rationale },
    'Apply',
    'Show diff',
  );

  if (choice === 'Show diff') {
    const preview = await vscode.workspace.openTextDocument({
      content: proposal.proposed_code,
      language: document.languageId,
    });
    await vscode.window.showTextDocument(preview, { preview: true });
    return;
  }
  if (choice !== 'Apply') {
    return;
  }

  // The model returns a standalone definition at column zero; restore the
  // indentation of the line being replaced so a method stays inside its class.
  // Blank lines are left empty rather than filled with trailing whitespace.
  const indent = document.lineAt(proposal.line_start - 1).text.match(/^\s*/)?.[0] ?? '';
  const reindented = proposal.proposed_code
    .split('\n')
    .map((line) => (line.trim() === '' ? '' : indent + line))
    .join('\n');

  const edit = new vscode.WorkspaceEdit();
  edit.replace(document.uri, original, reindented.trimEnd());
  const applied = await vscode.workspace.applyEdit(edit);

  void vscode.window.showInformationMessage(
    applied
      ? `Applied refactor to ${proposal.target_symbol}. Undo with Ctrl+Z.`
      : 'The edit could not be applied; the file may have changed since the run.',
  );
}

/** Report orchestrator and Ollama status, with the missing models named. */
async function checkHealth(): Promise<void> {
  try {
    const health = await makeClient().health();
    if (!health.ollama_reachable) {
      void vscode.window.showErrorMessage(
        'CodeMod Studio: Ollama is not reachable. Start it with `ollama serve`.',
      );
      return;
    }
    if (health.missing_models.length > 0) {
      void vscode.window.showWarningMessage(
        `CodeMod Studio: missing models — run \`ollama pull ${health.missing_models.join(
          '\` and \`ollama pull ',
        )}\`.`,
      );
      return;
    }
    void vscode.window.showInformationMessage(
      `CodeMod Studio v${health.version}: ready. ${health.available_models.length} model(s) available.`,
    );
  } catch {
    void vscode.window.showErrorMessage(
      'CodeMod Studio: the orchestrator is not responding. Run `docker compose up`.',
    );
  }
}
