# syntax=docker/dockerfile:1

# ---- build ----------------------------------------------------------------
FROM mcr.microsoft.com/dotnet/sdk:9.0-alpine AS build
WORKDIR /src

# Restore on the project file alone so the NuGet layer survives source edits.
COPY TelemetryApi.csproj ./
RUN dotnet restore

COPY . ./
RUN dotnet publish -c Release -o /app/publish --no-restore /p:UseAppHost=false

# ---- runtime --------------------------------------------------------------
FROM mcr.microsoft.com/dotnet/aspnet:9.0-alpine AS runtime
WORKDIR /app

# The .NET runtime images already ship a non-root `app` user (UID 1654)
# since .NET 8, so creating one here fails with "group 'app' in use".
COPY --from=build --chown=app:app /app/publish ./
USER app

ENV ASPNETCORE_URLS=http://+:8080 \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD wget -qO- http://localhost:8080/health || exit 1

ENTRYPOINT ["dotnet", "TelemetryApi.dll"]