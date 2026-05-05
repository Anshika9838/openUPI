# Secure Checkout Sandbox

This project is a local Flask checkout sandbox with:

- Supabase-backed order storage
- session-protected checkout access
- Socket.IO status updates for instant verification feedback
- a cleaner checkout UI with real transaction details

## Run

Install dependencies:

```bash
pip install -r requirements.txt
```

Set these environment variables when using Supabase:

- `FLASK_SECRET_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `TRANSACTION_INGEST_API_KEY` for the protected transaction-description ingest endpoint

Run the app:

```bash
python app.py
```

## Database

Apply [`supabase_schema.sql`](./supabase_schema.sql) in your Supabase project to create the `checkout_orders` table.

## Flow

1. `POST /api/orders` creates an order and returns a checkout URL with a one-time token.
2. Visiting `/checkout/<order_id>?token=...` validates the token and stores an authenticated Flask session.
3. The checkout page accepts a user-entered UTR and calls `/api/orders/<order_id>/verify-utr`.
4. A separate server sends the transaction description to `POST /api/payments/receive` with an `X-API-Key` header.
5. The message is parsed and stored in `payment_received`.
6. When the parsed UTR and entered UTR match and the amount also matches the order, the order is marked `success`.
