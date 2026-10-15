using System.ComponentModel.DataAnnotations;

namespace TelemetryApi.Models;

/// <summary>
/// One completed analysis run, as reported by the Python orchestrator.
/// </summary>
/// <remarks>
/// This table deliberately holds no source code, no file paths and no finding
/// text. <see cref="FileHash"/> is a truncated SHA-256 of the path, which is
/// enough to count repeat analyses of the same file without being able to
/// recover which file it was. That property is what lets telemetry stay on by
/// default without weakening the product's zero-leakage guarantee.
/// </remarks>
public class AnalysisRun
{
    [Key]
    [MaxLength(64)]
    public string RunId { get; set; } = string.Empty;

    /// <summary>Truncated SHA-256 of the analysed file's path. Not reversible.</summary>
    [Required]
    [MaxLength(64)]
    public string FileHash { get; set; } = string.Empty;

    [Required]
    [MaxLength(32)]
    public string Language { get; set; } = "unknown";

    /// <summary>Aggregate risk, 0-100.</summary>
    public double RiskScore { get; set; }

    [MaxLength(1)]
    public string Grade { get; set; } = "A";

    public int TotalFindings { get; set; }

    public int TotalProposals { get; set; }

    /// <summary>Wall-clock duration of the whole pipeline.</summary>
    public double DurationMs { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;

    /// <summary>Per-node execution records. Cascade-deleted with the run.</summary>
    public List<NodeExecution> Nodes { get; set; } = [];
}

/// <summary>
/// Timing and token accounting for a single agent node within a run.
/// </summary>
public class NodeExecution
{
    [Key]
    public long Id { get; set; }

    [MaxLength(64)]
    public string RunId { get; set; } = string.Empty;

    [Required]
    [MaxLength(64)]
    public string Node { get; set; } = string.Empty;

    [MaxLength(16)]
    public string Status { get; set; } = "ok";

    public double DurationMs { get; set; }

    [MaxLength(128)]
    public string? Model { get; set; }

    public int? PromptTokens { get; set; }

    public int? CompletionTokens { get; set; }

    public AnalysisRun? Run { get; set; }
}
