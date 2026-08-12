# Merchandiser AI Enterprise v10

## Clean D365-Integrated Edition

This package registers the Microsoft D365 connector directly in `backend/main.py`; no manual Python edits are required.

### One-click start
1. Extract the ZIP to a short path such as `C:\Merchandiser_AI_Enterprise_v10`.
2. Double-click `run_all.bat`.
3. Open `http://127.0.0.1:5500`.
4. Open **D365 Integration Hub** from the left menu.

### Configure D365
Double-click `setup_d365.bat` and complete `backend\.env`:

- `D365_TENANT_ID`
- `D365_CLIENT_ID`
- `D365_CLIENT_SECRET`
- `D365_RESOURCE_URL`
- `D365_COMPANY`

Use an Entra ID app registration with the required D365 Finance & Operations application permissions.

### Verify before connecting
Double-click `verify_d365.bat`. It must list:

- `/api/integrations/d365/status`
- `/api/integrations/d365/test`
- `/api/integrations/d365/sync/{entity}`
- `/api/integrations/d365/po-payload/{decision_id}`

### Safe sequence
1. Status
2. Test connection
3. Dry-run each entity
4. Review mappings
5. Perform controlled sync
6. Validate totals against D365
7. Enable PO write-back only after UAT approval

Real connection testing requires your D365 tenant URL, app registration and permissions.
