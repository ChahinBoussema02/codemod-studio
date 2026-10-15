/**
 * Mirrors of the orchestrator's Pydantic schemas.
 *
 * Kept hand-written rather than generated so the extension compiles without a
 * running backend. The orchestrator publishes OpenAPI at `/openapi.json`, so
 * `npm run generate-types` can replace this file once the contract stabilises.
 */

export type Severity = 'info' | 'low' | 'medium' | 'high' | 'critical';

export type AnalysisPhase =
  | 'pending'
  | 'parsing'
  | 'auditing'
  | 'refactoring'
  | 'reporting'
  | 'completed'
  | 'failed';

export interface ComplexityMetrics {
  cyclomatic: number;
  cognitive: number;
  max_nesting: number;
  length: number;
  parameter_count: number;
  return_points: number;
  halstead_volume: number;
  maintainability_index: number;
  has_docstring: boolean;
}

export interface FunctionSignature {
  name: string;
  qualified_name: string;
  return_annotation: string | null;
  is_async: boolean;
  is_method: boolean;
  line_start: number;
  line_end: number;
}

export interface FunctionProfile {
  signature: FunctionSignature;
  metrics: ComplexityMetrics;
}

export interface ModuleProfile {
  path: string;
  language: string;
  loc: number;
  total_lines: number;
  imports: string[];
  functions: FunctionProfile[];
  syntax_error: string | null;
}

export interface SecurityFinding {
  id: string;
  title: string;
  description: string;
  severity: Severity;
  confidence: number;
  cwe_id: string | null;
  owasp_category: string | null;
  file_path: string;
  line_start: number;
  line_end: number;
  symbol: string | null;
  remediation: string;
  detector: 'heuristic' | 'llm' | 'hybrid';
  rule_id: string | null;
}

export interface RefactorProposal {
  id: string;
  file_path: string;
  target_symbol: string;
  strategy: string;
  rationale: string;
  proposed_code: string;
  line_start: number;
  line_end: number;
  estimated_risk: Severity;
  confidence: number;
  syntax_validated: boolean;
}

export interface RiskSummary {
  risk_score: number;
  grade: 'A' | 'B' | 'C' | 'D' | 'F';
  total_findings: number;
  total_proposals: number;
  hotspot_functions: string[];
  headline: string;
}

export interface AgentTrace {
  node: string;
  status: 'ok' | 'skipped' | 'degraded' | 'failed';
  duration_ms: number;
  model: string | null;
  detail: string | null;
  error: string | null;
}

export interface AnalyzeResponse {
  run_id: string;
  phase: AnalysisPhase;
  file_path: string;
  module: ModuleProfile | null;
  findings: SecurityFinding[];
  proposals: RefactorProposal[];
  summary: RiskSummary | null;
  traces: AgentTrace[];
  warnings: string[];
  duration_ms: number;
}

export interface ParseResponse {
  module: ModuleProfile;
  duration_ms: number;
}

export interface HealthResponse {
  status: 'ok' | 'degraded';
  version: string;
  ollama_reachable: boolean;
  available_models: string[];
  missing_models: string[];
  telemetry_reachable: boolean | null;
}

/** One decoded server-sent event from `/analysis/stream`. */
export type StreamEvent =
  | { event: 'phase'; run_id: string; data: { phase: AnalysisPhase } }
  | { event: 'finding'; run_id: string; data: SecurityFinding }
  | { event: 'proposal'; run_id: string; data: RefactorProposal }
  | { event: 'summary'; run_id: string; data: RiskSummary }
  | { event: 'error'; run_id: string; data: { code: string; message: string } }
  | { event: 'done'; run_id: string; data: Record<string, never> };
