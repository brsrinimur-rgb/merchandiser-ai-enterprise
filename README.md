# Merchandiser AI 360 Enterprise — Final Repaired Build

This package consolidates the working merchandising engine and the Enterprise Integration Hub into one version.

## Fixed in this build

- Backend startup and stable sample database
- `/api/health` endpoint
- Frontend API connectivity to port 8000
- Removed the undefined `actionCard` JavaScript failure
- Structured Integration Hub Swagger response (`ConnectorListResponse`, not `"string"`)
- D365 configuration readiness and safe OAuth test
- Sandbox pull/push preview, D365 pull, audit logs and write-back safety gate
- One-click Windows startup

## Start on Windows

1. Extract the ZIP to a short path, for example:

   `C:\Merchandiser_AI_360_Enterprise_Final`

2. Double-click `run_all.bat`.
3. Keep the Backend and Frontend command windows open.
4. Open `http://127.0.0.1:5500`.

API documentation: `http://127.0.0.1:8000/docs`

Health check: `http://127.0.0.1:8000/api/health`

If the screen says **Failed to fetch**, look at the Backend command window. Run `verify.bat` to confirm the API.

## D365 connection

1. Double-click `setup_d365.bat`.
2. Enter Tenant ID, Client ID, Client Secret, D365 URL and company/legal entity.
3. Restart the backend.
4. Open **Enterprise Integration Hub → D365 Connector → Test Connection**.

Write-back is disabled by default. Complete UAT and approved entity field mappings before changing `D365_ENABLE_WRITEBACK=false`.

## Online deployment

For a server deployment, set the frontend API base before the main script:

```html
<script>window.MERCH_API_BASE="https://api.yourdomain.com";</script>
```

Use PostgreSQL for production by setting `DATABASE_URL`. Put the API and frontend behind HTTPS and authentication before allowing external users.
