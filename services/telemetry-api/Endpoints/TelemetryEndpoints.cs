using Microsoft.EntityFrameworkCore;
using TelemetryApi.Data;
using TelemetryApi.Models;

namespace TelemetryApi.Endpoints;

/// <summary>
/// Minimal-API endpoint group for run ingestion and dashboard aggregates.
/// </summary>
public static class TelemetryEndpoints
{
    public static IEndpointRouteBuilder MapTelemetryEndpoints(this IEndpointRouteBuilder app)
    {
        var group = app.MapGroup("/api/v1").WithTags("telemetry");

        group.MapPost("/runs", IngestRun)
            .WithName("IngestRun")
            .WithSummary("Record a completed analysis run.")
            .Produces(StatusCodes.Status201Created)
            .Produces(StatusCodes.Status200OK);

        group.MapGet("/runs", ListRuns)
            .WithName("ListRuns")
            .WithSummary("Most recent runs, newest first.");

        group.MapGet("/runs/{runId}", GetRun)
            .WithName("GetRun")
            .Produces<AnalysisRun>()
            .Produces(StatusCodes.Status404NotFound);

        group.MapGet("/summary", GetSummary)
            .WithName("GetSummary")
            .WithSummary("Rolling aggregates over a trailing window.");

        return app;
    }

    /// <summary>
    /// Upserts a run. The orchestrator may retry a push after a transient
    /// network failure, so ingestion is idempotent on <c>RunId</c> rather
    /// than returning a conflict the caller would have to special-case.
    /// </summary>
    private static async Task<IResult> IngestRun(
        RunIngestRequest request,
        TelemetryDbContext db,
        ILogger<Program> logger,
        CancellationToken ct)
    {
        var existing = await db.Runs
            .Include(r => r.Nodes)
            .FirstOrDefaultAsync(r => r.RunId == request.RunId, ct);

        if (existing is not null)
        {
            db.NodeExecutions.RemoveRange(existing.Nodes);
            existing.RiskScore = request.RiskScore;
            existing.Grade = request.Grade;
            existing.TotalFindings = request.TotalFindings;
            existing.TotalProposals = request.TotalProposals;
            existing.DurationMs = request.DurationMs;
            existing.Nodes = MapNodes(request);
            await db.SaveChangesAsync(ct);
            logger.LogInformation("Updated telemetry for run {RunId}", request.RunId);
            return Results.Ok(new { request.RunId, updated = true });
        }

        var run = new AnalysisRun
        {
            RunId = request.RunId,
            FileHash = request.FileHash,
            Language = request.Language,
            RiskScore = request.RiskScore,
            Grade = request.Grade,
            TotalFindings = request.TotalFindings,
            TotalProposals = request.TotalProposals,
            DurationMs = request.DurationMs,
            CreatedAt = DateTimeOffset.UtcNow,
            Nodes = MapNodes(request),
        };

        db.Runs.Add(run);
        await db.SaveChangesAsync(ct);
        logger.LogInformation(
            "Recorded run {RunId}: grade {Grade}, {Findings} findings",
            run.RunId, run.Grade, run.TotalFindings);

        return Results.Created($"/api/v1/runs/{run.RunId}", new { run.RunId });
    }

    private static List<NodeExecution> MapNodes(RunIngestRequest request) =>
        [.. request.Nodes.Select(n => new NodeExecution
        {
            RunId = request.RunId,
            Node = n.Node,
            Status = n.Status,
            DurationMs = n.DurationMs,
            Model = n.Model,
            PromptTokens = n.PromptTokens,
            CompletionTokens = n.CompletionTokens,
        })];

    private static async Task<IResult> ListRuns(
        TelemetryDbContext db,
        CancellationToken ct,
        int limit = 50,
        string? language = null)
    {
        var query = db.Runs.AsNoTracking().OrderByDescending(r => r.CreatedAt).AsQueryable();

        if (!string.IsNullOrWhiteSpace(language))
        {
            query = query.Where(r => r.Language == language);
        }

        var runs = await query.Take(Math.Clamp(limit, 1, 500)).ToListAsync(ct);
        return Results.Ok(runs);
    }

    private static async Task<IResult> GetRun(
        string runId, TelemetryDbContext db, CancellationToken ct)
    {
        var run = await db.Runs
            .AsNoTracking()
            .Include(r => r.Nodes)
            .FirstOrDefaultAsync(r => r.RunId == runId, ct);

        return run is null ? Results.NotFound() : Results.Ok(run);
    }

    /// <summary>
    /// Aggregates the trailing <paramref name="days"/> window.
    /// </summary>
    private static async Task<IResult> GetSummary(
        TelemetryDbContext db, CancellationToken ct, int days = 30)
    {
        var since = DateTimeOffset.UtcNow.AddDays(-Math.Clamp(days, 1, 365));
        var runs = await db.Runs.AsNoTracking().Where(r => r.CreatedAt >= since).ToListAsync(ct);

        if (runs.Count == 0)
        {
            return Results.Ok(new TelemetrySummary(
                0, 0, 0, 0, 0, 0,
                new Dictionary<string, int>(),
                new Dictionary<string, int>(),
                []));
        }

        var runIds = runs.Select(r => r.RunId).ToHashSet();
        var nodes = await db.NodeExecutions
            .AsNoTracking()
            .Where(n => runIds.Contains(n.RunId))
            .ToListAsync(ct);

        var slowest = nodes
            .GroupBy(n => n.Node)
            .Select(g => new NodeTiming(
                g.Key,
                Math.Round(g.Average(n => n.DurationMs), 2),
                g.Count(),
                g.Sum(n => (long)((n.PromptTokens ?? 0) + (n.CompletionTokens ?? 0)))))
            .OrderByDescending(t => t.AverageDurationMs)
            .ToList();

        var summary = new TelemetrySummary(
            TotalRuns: runs.Count,
            AverageRiskScore: Math.Round(runs.Average(r => r.RiskScore), 2),
            AverageDurationMs: Math.Round(runs.Average(r => r.DurationMs), 2),
            TotalFindings: runs.Sum(r => r.TotalFindings),
            TotalProposals: runs.Sum(r => r.TotalProposals),
            TotalTokens: slowest.Sum(t => t.TotalTokens),
            RunsByGrade: runs.GroupBy(r => r.Grade).ToDictionary(g => g.Key, g => g.Count()),
            RunsByLanguage: runs.GroupBy(r => r.Language).ToDictionary(g => g.Key, g => g.Count()),
            SlowestNodes: slowest);

        return Results.Ok(summary);
    }
}
