/**
 * HTTP client for the local orchestrator.
 *
 * Uses the global `fetch` shipped with the Node runtime inside VS Code, so the
 * extension has no runtime dependencies at all.
 *
 * The streaming method is the important one: a refactor pass against a 14B
 * model on CPU can take a minute, and an IDE that shows nothing for a minute
 * looks broken. `streamAnalysis` yields findings as the agents produce them.
 */

import type {
  AnalyzeResponse,
  HealthResponse,
  ParseResponse,
  StreamEvent,
} from './types';

export interface ClientOptions {
  baseUrl: string;
  auditModel?: string;
  refactorModel?: string;
}

export class OrchestratorError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = 'OrchestratorError';
  }
}

export class OrchestratorClient {
  constructor(private readonly options: ClientOptions) {}

  private get baseUrl(): string {
    return this.options.baseUrl.replace(/\/+$/, '');
  }

  private pipelineConfig(): Record<string, unknown> | undefined {
    const { auditModel, refactorModel } = this.options;
    if (!auditModel && !refactorModel) {
      return undefined;
    }
    return {
      ...(auditModel ? { audit_model: auditModel } : {}),
      ...(refactorModel ? { refactor_model: refactorModel } : {}),
    };
  }

  /** Readiness check, including which configured models are missing. */
  async health(signal?: AbortSignal): Promise<HealthResponse> {
    return this.getJson<HealthResponse>('/health/ready', signal);
  }

  /** Structural metrics only. Cheap enough to run on every save. */
  async parse(
    filePath: string,
    source: string,
    signal?: AbortSignal,
  ): Promise<ParseResponse> {
    return this.postJson<ParseResponse>(
      '/api/v1/analysis/parse',
      { file_path: filePath, source_code: source, language: 'python' },
      signal,
    );
  }

  /** Full pipeline, awaited to completion. */
  async analyze(
    filePath: string,
    source: string,
    signal?: AbortSignal,
  ): Promise<AnalyzeResponse> {
    return this.postJson<AnalyzeResponse>(
      '/api/v1/analysis/run',
      {
        file_path: filePath,
        source_code: source,
        language: 'python',
        config: this.pipelineConfig(),
      },
      signal,
    );
  }

  /**
   * Full pipeline as an async iterator over server-sent events.
   *
   * Parses the SSE framing by hand (the browser `EventSource` API is not
   * available in the extension host, and it cannot issue a POST anyway).
   */
  async *streamAnalysis(
    filePath: string,
    source: string,
    signal?: AbortSignal,
  ): AsyncGenerator<StreamEvent> {
    const response = await fetch(`${this.baseUrl}/api/v1/analysis/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({
        file_path: filePath,
        source_code: source,
        language: 'python',
        config: this.pipelineConfig(),
      }),
      ...(signal ? { signal } : {}),
    });

    if (!response.ok || !response.body) {
      throw new OrchestratorError(
        `Stream failed: ${response.status} ${response.statusText}`,
        response.status,
      );
    }

    const decoder = new TextDecoder();
    let buffer = '';

    // @ts-expect-error -- Node's ReadableStream is async-iterable at runtime.
    for await (const chunk of response.body) {
      buffer += decoder.decode(chunk as Uint8Array, { stream: true });

      // Events are separated by a blank line; a partial tail stays buffered.
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseFrame(frame);
        if (parsed) {
          yield parsed;
        }
        boundary = buffer.indexOf('\n\n');
      }
    }
  }

  private async getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...(signal ? { signal } : {}),
    });
    return this.unwrap<T>(response);
  }

  private async postJson<T>(
    path: string,
    body: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      ...(signal ? { signal } : {}),
    });
    return this.unwrap<T>(response);
  }

  private async unwrap<T>(response: Response): Promise<T> {
    if (response.ok) {
      return (await response.json()) as T;
    }
    // The orchestrator returns {code, message, detail} for domain errors;
    // surface `message` so the notification is actionable.
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { message?: string; detail?: string };
      detail = payload.message ?? payload.detail ?? detail;
    } catch {
      // Non-JSON error body; the status text will have to do.
    }
    throw new OrchestratorError(detail, response.status);
  }
}

/** Decode one `event:`/`data:` SSE frame. Returns null for comments/keepalives. */
function parseFrame(frame: string): StreamEvent | null {
  const dataLine = frame.split('\n').find((line) => line.startsWith('data:'));
  if (!dataLine) {
    return null;
  }
  try {
    return JSON.parse(dataLine.slice(5).trim()) as StreamEvent;
  } catch {
    return null;
  }
}
