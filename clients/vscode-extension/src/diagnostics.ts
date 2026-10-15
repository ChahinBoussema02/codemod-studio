/**
 * Translates orchestrator output into native VS Code diagnostics.
 *
 * Findings and complexity warnings land in the Problems panel rather than in a
 * custom tree, so they behave like every other linter the user already has:
 * keyboard navigation, filtering, and `Go to next problem` all work for free.
 */

import * as vscode from 'vscode';
import type { ModuleProfile, SecurityFinding, Severity } from './api/types';

const SEVERITY_MAP: Record<Severity, vscode.DiagnosticSeverity> = {
  critical: vscode.DiagnosticSeverity.Error,
  high: vscode.DiagnosticSeverity.Error,
  medium: vscode.DiagnosticSeverity.Warning,
  low: vscode.DiagnosticSeverity.Information,
  info: vscode.DiagnosticSeverity.Hint,
};

/** Convert 1-based inclusive line numbers into a full-line range. */
function lineRange(document: vscode.TextDocument, start: number, end: number): vscode.Range {
  const lastLine = Math.max(document.lineCount - 1, 0);
  const from = Math.min(Math.max(start - 1, 0), lastLine);
  const to = Math.min(Math.max(end - 1, from), lastLine);
  return new vscode.Range(from, 0, to, document.lineAt(to).text.length);
}

export function findingToDiagnostic(
  document: vscode.TextDocument,
  finding: SecurityFinding,
): vscode.Diagnostic {
  const diagnostic = new vscode.Diagnostic(
    lineRange(document, finding.line_start, finding.line_end),
    `${finding.title}\n\n${finding.description}\n\nFix: ${finding.remediation}`,
    SEVERITY_MAP[finding.severity],
  );

  diagnostic.source = `CodeMod Studio (${finding.detector})`;
  if (finding.cwe_id) {
    diagnostic.code = {
      value: finding.cwe_id,
      target: vscode.Uri.parse(
        `https://cwe.mitre.org/data/definitions/${finding.cwe_id.replace('CWE-', '')}.html`,
      ),
    };
  }
  // A low-confidence agent finding should look provisional, not authoritative.
  if (finding.detector === 'llm' && finding.confidence < 0.5) {
    diagnostic.tags = [vscode.DiagnosticTag.Unnecessary];
  }
  return diagnostic;
}

export function complexityDiagnostics(
  document: vscode.TextDocument,
  module: ModuleProfile,
  threshold: number,
): vscode.Diagnostic[] {
  return module.functions
    .filter((fn) => fn.metrics.cyclomatic >= threshold)
    .map((fn) => {
      const { metrics, signature } = fn;
      const diagnostic = new vscode.Diagnostic(
        lineRange(document, signature.line_start, signature.line_start),
        `${signature.qualified_name} is complex: cyclomatic ${metrics.cyclomatic}, ` +
          `cognitive ${metrics.cognitive}, nesting depth ${metrics.max_nesting} ` +
          `(maintainability index ${metrics.maintainability_index.toFixed(1)})`,
        vscode.DiagnosticSeverity.Information,
      );
      diagnostic.source = 'CodeMod Studio (metrics)';
      diagnostic.code = 'complexity';
      return diagnostic;
    });
}

/** Owns the diagnostic collection for the whole extension. */
export class DiagnosticsManager implements vscode.Disposable {
  private readonly collection: vscode.DiagnosticCollection;

  constructor() {
    this.collection = vscode.languages.createDiagnosticCollection('codemodStudio');
  }

  replace(document: vscode.TextDocument, diagnostics: vscode.Diagnostic[]): void {
    this.collection.set(document.uri, diagnostics);
  }

  clear(document: vscode.TextDocument): void {
    this.collection.delete(document.uri);
  }

  dispose(): void {
    this.collection.dispose();
  }
}
