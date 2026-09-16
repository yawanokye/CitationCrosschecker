-- purchases_schema_multi_currency.sql
-- CiteIntegrity commercial tables for GHS, NGN and USD pay-as-you-go access.

CREATE TABLE IF NOT EXISTS purchases (
    id SERIAL PRIMARY KEY,
    user_email TEXT NOT NULL,
    payment_type TEXT DEFAULT 'one_off',
    package_key TEXT,
    document_tier TEXT,
    review_type TEXT DEFAULT 'full',
    amount NUMERIC,
    currency TEXT DEFAULT 'GHS',
    status TEXT,
    payment_provider TEXT,
    provider_reference TEXT UNIQUE,
    access_token_hash TEXT,

    preview_job_id TEXT,
    preview_file_name TEXT,
    preview_reference_count INTEGER DEFAULT 0,
    preview_citation_count INTEGER DEFAULT 0,
    market TEXT,
    billing_country TEXT,

    analyses_total INTEGER DEFAULT 2,
    analyses_used INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMPTZ,
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchase_runs (
    id SERIAL PRIMARY KEY,
    purchase_id INTEGER REFERENCES purchases(id) ON DELETE CASCADE,
    job_id TEXT UNIQUE,
    file_name TEXT,
    reference_count INTEGER DEFAULT 0,
    citation_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_purchases_user_email ON purchases(user_email);
CREATE INDEX IF NOT EXISTS idx_purchases_provider_reference ON purchases(provider_reference);
CREATE INDEX IF NOT EXISTS idx_purchases_access_token_hash ON purchases(access_token_hash);
CREATE INDEX IF NOT EXISTS idx_purchase_runs_purchase_id ON purchase_runs(purchase_id);
CREATE INDEX IF NOT EXISTS idx_purchase_runs_job_id ON purchase_runs(job_id);
