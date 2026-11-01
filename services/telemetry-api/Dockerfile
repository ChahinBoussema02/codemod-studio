# ---- runtime --------------------------------------------------------------
FROM mcr.microsoft.com/dotnet/aspnet:9.0-alpine AS runtime
WORKDIR /app

# The .NET runtime images already ship a non-root `app` user (UID 1654)
# since .NET 8, so creating one here fails with "group 'app' in use".
COPY --from=build --chown=app:app /app/publish ./
USER app