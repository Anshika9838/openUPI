create table if not exists public.checkout_orders (
  order_id text primary key,
  txn_id text not null,
  amount numeric(12, 2) not null,
  currency text not null default 'INR',
  merchant_name text not null,
  customer_name text not null default '',
  customer_email text not null default '',
  customer_mobile text not null default '',
  upi_id text not null,
  note text,
  reference_note text not null default '',
  status text not null default 'pending',
  verification_status text not null default 'pending',
  status_message text not null default 'Awaiting payment confirmation',
  payment_reference text,
  entered_utr text,
  parsed_utr text,
  transaction_description text,
  parsed_transaction jsonb,
  checkout_token_hash text not null,
  verified_at timestamptz,
  verification_payload jsonb,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists checkout_orders_status_idx on public.checkout_orders (status);
create index if not exists checkout_orders_created_at_idx on public.checkout_orders (created_at desc);

create table if not exists public.payment_received (
  payment_id text primary key,
  utr text not null unique,
  amount numeric(12, 2),
  currency text not null default 'INR',
  transaction_description text not null,
  parsed_transaction jsonb,
  verification_status text not null default 'pending',
  status_message text not null default 'Waiting for checkout verification',
  order_id text,
  verified_order_id text,
  customer_name text not null default '',
  customer_email text not null default '',
  customer_mobile text not null default '',
  reference_note text not null default '',
  verified_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists payment_received_utr_idx on public.payment_received (utr);
create index if not exists payment_received_verified_order_idx on public.payment_received (verified_order_id);
create unique index if not exists payment_received_verified_order_unique_idx on public.payment_received (verified_order_id) where verified_order_id is not null;
