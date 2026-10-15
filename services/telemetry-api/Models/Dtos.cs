using System.ComponentModel.DataAnnotations;

namespace TelemetryApi.Models;

/// <summary>Payload posted by the orchestrator's report node.</summary>
public record RunIngestRequest
{
    [Required, MaxLength(64)]
    public string RunId { get; init; } = string.Empty;

    [Required, MaxLength(64)]
    public string FileHash { get; init; } = string.Empty;

    [MaxLength(32)]
    public string Language { get; init; } = "unknown";

    [Range(0, 100)]
    public double RiskScore { get; init; }

    [MaxLength(1)]
    public string Grade { get; init; } = "A";

    [Range(0, int.MaxValue)]
    public int TotalFindings { get; init; }

    [Range(0, int.MaxValue)]
    public int TotalProposals { get; init; }

    [Range(0, double.MaxValue)]
    public double DurationMs { get; init; }

    public List<NodeIngestRequest> Nodes { get; init; } = [];
}

/// <summary>Per-node record inside <see cref="RunIngestRequest"/>.</summary>
public record NodeIngestRequest
{
    [Required, MaxLength(64)]
    public string Node { get; init; } = string.Empty;

    [MaxLength(16)]
    public string Status { get; init; } = "ok";

    public double DurationMs { get; init; }

    [MaxLength(128)]
    public string? Model { get; init; }

    public int? PromptTokens { get; init; }

    public int? CompletionTokens { get; init; }
}

/// <summary>Rolling aggregate shown on the dashboard.</summary>
public record TelemetrySummary(
    int TotalRuns,
    double AverageRiskScore,
    double AverageDurationMs,
    int TotalFindings,
    int TotalProposals,
    long TotalTokens,
    IReadOnlyDictionary<string, int> RunsByGrade,
    IReadOnlyDictionary<string, int> RunsByLanguage,
    IReadOnlyList<NodeTiming> SlowestNodes);

/// <summary>Average cost of one node across every run in the window.</summary>
public record NodeTiming(string Node, double AverageDurationMs, int Executions, long TotalTokens);
