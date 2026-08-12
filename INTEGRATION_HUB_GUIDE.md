
# Merchandiser AI Enterprise v11 — Integration Hub

## Included
- Unified connector registry
- Pull and push capability declarations
- D365 Finance & Supply Chain connector
- Sandbox connector for safe demonstrations
- Connection testing
- Dry-run pull previews
- Controlled live pulls
- Controlled push endpoint
- Persistent integration logs in `backend/integration_hub.db`
- Swagger endpoints under `/api/integration-hub`

## Safe operating sequence
1. Run the sandbox connector first.
2. Configure `backend/.env` for D365.
3. Test D365 connection.
4. Dry-run each entity and approve mappings.
5. Run live pulls in dependency order: stores, suppliers, items, stock, purchase orders, sales.
6. Keep `D365_ENABLE_WRITEBACK=false` until UAT, security roles and mandatory fields are approved.
7. Enable PO/transfer push only in a dedicated D365 UAT environment first.

## API
- `GET /api/integration-hub/connectors`
- `GET /api/integration-hub/status`
- `POST /api/integration-hub/{connector}/test`
- `POST /api/integration-hub/{connector}/pull/{entity}`
- `POST /api/integration-hub/{connector}/push/{entity}`
- `GET /api/integration-hub/logs`

## D365 write-back
Set `D365_ENABLE_WRITEBACK=true` only after validating D365 security roles, number sequences, legal entity, site/warehouse, currency, delivery terms, tax groups and all mandatory custom fields.
