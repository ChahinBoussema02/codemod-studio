using Microsoft.EntityFrameworkCore;
using TelemetryApi.Models;

namespace TelemetryApi.Data;

/// <summary>
/// EF Core context for the telemetry store (PostgreSQL in all environments).
/// </summary>
public class TelemetryDbContext(DbContextOptions<TelemetryDbContext> options) : DbContext(options)
{
    public DbSet<AnalysisRun> Runs => Set<AnalysisRun>();

    public DbSet<NodeExecution> NodeExecutions => Set<NodeExecution>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        var run = modelBuilder.Entity<AnalysisRun>();
        run.ToTable("analysis_runs");

        // Dashboard queries are overwhelmingly "the last N runs" and "runs for
        // this file over time"; both indexes earn their write cost.
        run.HasIndex(r => r.CreatedAt).HasDatabaseName("ix_runs_created_at");
        run.HasIndex(r => new { r.FileHash, r.CreatedAt }).HasDatabaseName("ix_runs_file_created");

        run.HasMany(r => r.Nodes)
            .WithOne(n => n.Run)
            .HasForeignKey(n => n.RunId)
            .OnDelete(DeleteBehavior.Cascade);

        var node = modelBuilder.Entity<NodeExecution>();
        node.ToTable("node_executions");
        node.HasIndex(n => n.Node).HasDatabaseName("ix_nodes_name");

        base.OnModelCreating(modelBuilder);
    }
}
