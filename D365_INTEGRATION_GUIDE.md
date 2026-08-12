# D365 Integration – Merchandiser AI Enterprise v9

## What this package provides

- Microsoft Entra ID OAuth 2.0 client-credential authentication
- D365 OData pagination and error handling
- Configurable D365 entity names
- Editable D365-to-Merchandiser field mappings
- Read-only connection test
- Dry-run synchronization with mapping preview and validation errors
- Controlled upsert into Stores, Suppliers, Items, Sales, Stock and Purchase Orders
- Approved-decision PO payload preview for future D365 write-back

## Important safety rule

Start **read-only**. Do not enable purchase-order POST until a D365 administrator confirms the legal entity, number sequence, vendor, warehouse/site, currency, delivery terms, tax and workflow requirements.

## 1. Create the Entra ID application

Ask your Microsoft/D365 administrator to:

1. Register an application in Microsoft Entra ID.
2. Create a client secret or, preferably for production, a certificate.
3. Add the application/user in D365 under **System administration > Setup > Microsoft Entra applications**.
4. Assign a D365 service account and only the security roles required for the selected data entities.
5. Confirm the Finance & Operations environment URL and legal entity code.

## 2. Configure the application

Copy:

```text
backend/.env.example
```

to:

```text
backend/.env
```

Fill in tenant ID, client ID, secret, environment URL and company.

Never commit `.env` or share the client secret by email/chat. Use Azure Key Vault for production.

## 3. Install dependencies

From the backend folder:

```bat
python -m pip install -r requirements.txt
```

## 4. Start the backend

```bat
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open Swagger:

```text
http://127.0.0.1:8000/docs
```

## 5. Test connection

Use:

```text
POST /api/integrations/d365/test
```

## 6. Run dry-run previews first

Recommended master-data order:

1. stores
2. suppliers
3. items
4. stock
5. purchase_orders
6. sales

Example:

```text
POST /api/integrations/d365/sync/items?dry_run=true&top=100
```

Review the `preview` and `errors`. If D365 field names differ, edit:

```text
backend/integrations/d365/mappings.py
```

If entity names differ, change them in `.env`.

## 7. Commit a validated sync

Only after the dry run is correct:

```text
POST /api/integrations/d365/sync/items?dry_run=false&top=100
```

Then call the existing application refresh endpoint:

```text
POST /api/refresh
```

## 8. Purchase-order write-back preparation

For a decision already at `po_ready`:

```text
GET /api/integrations/d365/po-payload/{decision_id}
```

This generates a payload preview only. It intentionally does **not** POST to D365 yet.

## Recommended first pilot

Use a UAT/sandbox D365 environment and one legal entity, one store/warehouse, one supplier and 10–20 SKUs. Reconcile record counts and values before expanding the scope.
