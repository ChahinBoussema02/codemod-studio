using Microsoft.EntityFrameworkCore;
using TelemetryApi.Data;
using TelemetryApi.Endpoints;

var builder = WebApplication.CreateBuilder(args);

// --------------------------------------------------------------------------
// Services
// --------------------------------------------------------------------------

var connectionString =
    builder.Configuration.GetConnectionString("Telemetry")
    ?? Environment.GetEnvironmentVariable("ConnectionStrings__Telemetry")
    ?? "Host=postgres;Port=5432;Database=cms_telemetry;Username=cms;Password=cms";

builder.Services.AddDbContext<TelemetryDbContext>(options =>
    options.UseNpgsql(connectionString, npgsql => npgsql.EnableRetryOnFailure(3)));

builder.Services.AddEndpointsApiExplorer();
builder.Services.AddOpenApi();
builder.Services.AddHealthChecks().AddDbContextCheck<TelemetryDbContext>("database");

// Payloads are small and uniform; camelCase matches what the Python client sends.
builder.Services.ConfigureHttpJsonOptions(options =>
{
    options.SerializerOptions.PropertyNameCaseInsensitive = true;
});

var app = builder.Build();

// --------------------------------------------------------------------------
// Startup: schema
// --------------------------------------------------------------------------

// EnsureCreated keeps `docker compose up` a single command for Phase 1/2.
// Phase 3 replaces this with `await db.Database.MigrateAsync()` once the
// schema is versioned and the table has data worth preserving.
await using (var scope = app.Services.CreateAsyncScope())
{
    var db = scope.ServiceProvider.GetRequiredService<TelemetryDbContext>();
    var logger = scope.ServiceProvider.GetRequiredService<ILogger<Program>>();
    try
    {
        await db.Database.EnsureCreatedAsync();
        logger.LogInformation("Telemetry schema ready");
    }
    catch (Exception ex)
    {
        // The orchestrator treats telemetry as optional, so a database that is
        // still starting must not take this service down in a crash loop.
        logger.LogError(ex, "Could not initialise the telemetry schema");
    }
}

// --------------------------------------------------------------------------
// Pipeline
// --------------------------------------------------------------------------

if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
}

app.MapHealthChecks("/health");
app.MapTelemetryEndpoints();

app.Run();

/// <summary>Exposed so an integration test project can use WebApplicationFactory.</summary>
public partial class Program;
