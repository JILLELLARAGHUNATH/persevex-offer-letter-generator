-- =============================================================================
-- PRODUCTION PATCH MIGRATION: CA Certificate Tables & Concurrency RPCs
-- Target Project: Persevex Offer Letter Generator
-- Environment: Production Supabase (iygbadoegwsvzlrfvnwj)
--
-- SAFETY ASSURANCES:
-- 1. Preserves existing public.campus_ambassador_certificate_history and all historical records.
-- 2. Non-destructive ALTER TABLE ... ADD COLUMN IF NOT EXISTS for claim_token and claimed_at.
-- 3. Creates missing job queue tables with IF NOT EXISTS.
-- 4. RPC 1 (Generation Claim): Uses Candidate CTE with LIMIT GREATEST(batch_size, 1) FOR UPDATE SKIP LOCKED
--    to prevent subquery volatility over-claiming.
-- 5. RPC 2 (Bulk Email Claim): Uses Candidate CTE with LIMIT 1 FOR UPDATE SKIP LOCKED and explicitly
--    reconciles/blocks claiming when history is already 'sent' or 'uncertain'. Never introduces a resend path for uncertain deliveries.
-- 6. RPC 3 (Single Email Claim): Retains verified 7-argument signature, advisory locking, and uncertain protection.
-- 7. Restricts all table access and RPC execution permissions strictly to service_role.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- 1. RECONCILE EXISTING campus_ambassador_certificate_history TABLE
-- -----------------------------------------------------------------------------
ALTER TABLE public.campus_ambassador_certificate_history
    ADD COLUMN IF NOT EXISTS claim_token UUID,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_ca_certificate_history_created_at
    ON public.campus_ambassador_certificate_history (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ca_certificate_history_email
    ON public.campus_ambassador_certificate_history (participant_email);

CREATE INDEX IF NOT EXISTS idx_ca_certificate_history_status
    ON public.campus_ambassador_certificate_history (email_status);

ALTER TABLE public.campus_ambassador_certificate_history ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'campus_ambassador_certificate_history'
          AND policyname = 'Allow service_role full access on ca_certificate_history'
    ) THEN
        CREATE POLICY "Allow service_role full access on ca_certificate_history"
            ON public.campus_ambassador_certificate_history
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END $$;

REVOKE ALL ON TABLE public.campus_ambassador_certificate_history FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.campus_ambassador_certificate_history TO service_role;

-- -----------------------------------------------------------------------------
-- 2. CREATE MISSING JOB QUEUE TABLES
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.campus_ambassador_certificate_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status TEXT NOT NULL DEFAULT 'queued',
    program_date TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    generated_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.campus_ambassador_certificate_job_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES public.campus_ambassador_certificate_jobs(id) ON DELETE CASCADE,
    source_row INTEGER NOT NULL,
    participant_name TEXT NOT NULL,
    participant_email TEXT NOT NULL,
    program_date TEXT NOT NULL,
    generation_status TEXT NOT NULL DEFAULT 'pending',
    email_status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    send_count INTEGER NOT NULL DEFAULT 0,
    document_id TEXT,
    error_message TEXT,
    claim_token UUID,
    claimed_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    CONSTRAINT uq_ca_cert_job_item_identity UNIQUE (job_id, participant_name, participant_email)
);

CREATE INDEX IF NOT EXISTS idx_ca_cert_job_items_job_id
    ON public.campus_ambassador_certificate_job_items (job_id);

CREATE INDEX IF NOT EXISTS idx_ca_cert_job_items_email_status
    ON public.campus_ambassador_certificate_job_items (email_status);

CREATE INDEX IF NOT EXISTS idx_ca_cert_job_items_gen_status
    ON public.campus_ambassador_certificate_job_items (generation_status);

ALTER TABLE public.campus_ambassador_certificate_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.campus_ambassador_certificate_job_items ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'campus_ambassador_certificate_jobs'
          AND policyname = 'Allow service_role full access on ca_certificate_jobs'
    ) THEN
        CREATE POLICY "Allow service_role full access on ca_certificate_jobs"
            ON public.campus_ambassador_certificate_jobs
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'campus_ambassador_certificate_job_items'
          AND policyname = 'Allow service_role full access on ca_certificate_job_items'
    ) THEN
        CREATE POLICY "Allow service_role full access on ca_certificate_job_items"
            ON public.campus_ambassador_certificate_job_items
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END $$;

REVOKE ALL ON TABLE public.campus_ambassador_certificate_jobs FROM anon, authenticated;
REVOKE ALL ON TABLE public.campus_ambassador_certificate_job_items FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.campus_ambassador_certificate_jobs TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.campus_ambassador_certificate_job_items TO service_role;

-- -----------------------------------------------------------------------------
-- 3. RPC: claim_ca_certificate_job_items (Fixed with Candidate CTE)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_ca_certificate_job_items(
    target_job UUID,
    batch_size INTEGER,
    token UUID
) RETURNS SETOF public.campus_ambassador_certificate_job_items
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
#variable_conflict use_column
BEGIN
    -- Reconcile stale processing generation attempts older than 10 minutes
    UPDATE public.campus_ambassador_certificate_job_items
       SET generation_status = 'pending',
           claim_token = NULL,
           claimed_at = NULL,
           updated_at = timezone('utc'::text, now())
     WHERE job_id = target_job
       AND generation_status = 'processing'
       AND claimed_at < timezone('utc'::text, now()) - INTERVAL '10 minutes';

    -- Candidate CTE locks and claims strictly GREATEST(batch_size, 1) rows,
    -- eliminating subquery re-evaluation volatility under FOR UPDATE SKIP LOCKED
    RETURN QUERY
    WITH claim_candidates AS (
        SELECT id
          FROM public.campus_ambassador_certificate_job_items
         WHERE job_id = target_job
           AND generation_status = 'pending'
           AND email_status = 'pending'
         ORDER BY source_row
         LIMIT GREATEST(batch_size, 1)
         FOR UPDATE SKIP LOCKED
    )
    UPDATE public.campus_ambassador_certificate_job_items AS i
       SET generation_status = 'processing',
           claim_token = token,
           claimed_at = timezone('utc'::text, now()),
           updated_at = timezone('utc'::text, now())
      FROM claim_candidates
     WHERE i.id = claim_candidates.id
    RETURNING i.*;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_ca_certificate_job_items(UUID, INTEGER, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_ca_certificate_job_items(UUID, INTEGER, UUID) TO service_role;

-- -----------------------------------------------------------------------------
-- 4. RPC: claim_ca_certificate_email_items (Candidate CTE + Mismatched History Protection)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_ca_certificate_email_items(
    target_job UUID,
    batch_size INTEGER,
    token UUID,
    retry_failed BOOLEAN DEFAULT FALSE
) RETURNS SETOF public.campus_ambassador_certificate_job_items
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
#variable_conflict use_column
BEGIN
    -- 1. Reconcile stale sending claims older than 10 minutes in certificate history
    UPDATE public.campus_ambassador_certificate_history AS h
       SET email_status = 'uncertain',
           error_message = 'Delivery outcome unknown after interrupted bulk send; manual reconciliation required.',
           claim_token = NULL,
           claimed_at = NULL
      FROM public.campus_ambassador_certificate_job_items AS i
     WHERE h.document_id = i.document_id
       AND h.email_status = 'sending'
       AND i.job_id = target_job
       AND i.email_status = 'sending'
       AND i.claimed_at < timezone('utc'::text, now()) - INTERVAL '10 minutes';

    -- 2. Reconcile stale sending job items older than 10 minutes
    UPDATE public.campus_ambassador_certificate_job_items AS i
       SET email_status = CASE
               WHEN h.email_status IN ('sent', 'failed', 'uncertain') THEN h.email_status
               ELSE 'uncertain'
           END,
           error_message = CASE
               WHEN h.email_status IN ('sent', 'failed') THEN h.error_message
               ELSE 'Delivery outcome unknown after interrupted send; manual reconciliation required.'
           END,
           send_count = GREATEST(i.send_count, COALESCE(h.send_count, 0)),
           sent_at = COALESCE(i.sent_at, h.sent_at),
           claim_token = NULL,
           claimed_at = NULL,
           updated_at = timezone('utc'::text, now())
      FROM public.campus_ambassador_certificate_job_items AS stale
      LEFT JOIN public.campus_ambassador_certificate_history AS h
        ON h.document_id = stale.document_id
     WHERE i.id = stale.id
       AND stale.job_id = target_job
       AND stale.email_status = 'sending'
       AND stale.claimed_at < timezone('utc'::text, now()) - INTERVAL '10 minutes';

    -- 3A. Deterministic exact document reconciliation (1-to-1 via UNIQUE document_id):
    -- Synchronize job item with its exact document history record if already sent or uncertain
    UPDATE public.campus_ambassador_certificate_job_items AS i
       SET email_status = h.email_status,
           error_message = CASE
               WHEN h.email_status = 'sent' THEN NULL
               ELSE h.error_message
           END,
           send_count = GREATEST(i.send_count, COALESCE(h.send_count, 0)),
           sent_at = COALESCE(i.sent_at, h.sent_at),
           claim_token = NULL,
           claimed_at = NULL,
           updated_at = timezone('utc'::text, now())
      FROM public.campus_ambassador_certificate_history AS h
     WHERE h.document_id = i.document_id
       AND i.job_id = target_job
       AND i.email_status IN ('pending', 'sending')
       AND h.email_status IN ('sent', 'uncertain');

    -- 3B. Deterministic recipient protection for uncertain deliveries:
    -- If a participant has ANY uncertain delivery in history across any document,
    -- mark this pending item as uncertain to require manual reconciliation and prevent resending.
    UPDATE public.campus_ambassador_certificate_job_items AS i
       SET email_status = 'uncertain',
           error_message = 'Recipient has an existing uncertain delivery in history; manual reconciliation required.',
           claim_token = NULL,
           claimed_at = NULL,
           updated_at = timezone('utc'::text, now())
     WHERE i.job_id = target_job
       AND i.email_status = 'pending'
       AND EXISTS (
           SELECT 1
             FROM public.campus_ambassador_certificate_history AS h
            WHERE lower(trim(h.participant_email)) = lower(trim(i.participant_email))
              AND h.email_status = 'uncertain'
       );

    -- 3C. Deterministic recipient protection for sent deliveries:
    -- If a participant has ANY sent delivery in history (and no uncertain),
    -- mark this pending item as sent to prevent duplicate dispatch.
    UPDATE public.campus_ambassador_certificate_job_items AS i
       SET email_status = 'sent',
           error_message = NULL,
           claim_token = NULL,
           claimed_at = NULL,
           updated_at = timezone('utc'::text, now())
     WHERE i.job_id = target_job
       AND i.email_status = 'pending'
       AND EXISTS (
           SELECT 1
             FROM public.campus_ambassador_certificate_history AS h
            WHERE lower(trim(h.participant_email)) = lower(trim(i.participant_email))
              AND h.email_status = 'sent'
       );

    -- 4. Atomically select, lock and claim at most ONE eligible job item using a Candidate CTE.
    -- Strict duplicate and uncertain protection:
    -- An item is never claimed if history already contains a sent or uncertain delivery for
    -- either the exact document_id or the participant_email.
    RETURN QUERY
    WITH claim_candidate AS (
        SELECT i.id
          FROM public.campus_ambassador_certificate_job_items AS i
         WHERE i.job_id = target_job
           AND i.generation_status = 'generated'
           AND (
               i.email_status = 'pending'
               OR (retry_failed AND i.email_status = 'failed')
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.campus_ambassador_certificate_history AS h
                WHERE h.document_id = i.document_id
                  AND h.email_status IN ('sent', 'uncertain')
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.campus_ambassador_certificate_history AS h
                WHERE lower(trim(h.participant_email)) = lower(trim(i.participant_email))
                  AND h.email_status IN ('sent', 'uncertain')
           )
         ORDER BY i.source_row
         LIMIT 1
         FOR UPDATE SKIP LOCKED
    )
    UPDATE public.campus_ambassador_certificate_job_items AS i
       SET email_status = 'sending',
           claim_token = token,
           claimed_at = timezone('utc'::text, now()),
           updated_at = timezone('utc'::text, now())
      FROM claim_candidate
     WHERE i.id = claim_candidate.id
    RETURNING i.*;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_ca_certificate_email_items(
    UUID, INTEGER, UUID, BOOLEAN
) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.claim_ca_certificate_email_items(
    UUID, INTEGER, UUID, BOOLEAN
) TO service_role;

-- -----------------------------------------------------------------------------
-- 5. RPC: claim_ca_certificate_single_email (Tested 7-Argument Signature)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_ca_certificate_single_email(
    p_participant_name TEXT,
    p_participant_email TEXT,
    p_program_date TEXT,
    p_document_id TEXT,
    p_pdf_filename TEXT,
    p_claim_token UUID,
    p_allow_resend BOOLEAN DEFAULT FALSE
) RETURNS TABLE (
    claimed BOOLEAN,
    record_id BIGINT,
    document_id TEXT,
    email_status TEXT,
    send_count INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
#variable_conflict use_column
DECLARE
    normalized_email TEXT := lower(trim(p_participant_email));
    blocked_status TEXT;
    previous_send_count INTEGER;
    existing_record_id BIGINT;
    existing_status TEXT;
    existing_send_count INTEGER;
BEGIN
    IF normalized_email IS NULL
       OR normalized_email = ''
       OR p_claim_token IS NULL
       OR p_document_id IS NULL
       OR p_document_id = ''
       OR trim(COALESCE(p_participant_name, '')) = ''
       OR trim(COALESCE(p_program_date, '')) = '' THEN
        RAISE EXCEPTION 'Participant email, claim token, and document ID are required.';
    END IF;

    -- Recipient-level advisory lock serializes concurrent claims for same email
    PERFORM pg_advisory_xact_lock(hashtextextended(normalized_email, 0));

    -- Mark stale sending attempts (>10 min) as uncertain (requires manual reconciliation)
    UPDATE public.campus_ambassador_certificate_history AS h
       SET email_status = 'uncertain',
           error_message = 'Delivery outcome unknown after interrupted single send; manual reconciliation required.',
           claim_token = NULL,
           claimed_at = NULL
     WHERE lower(trim(h.participant_email)) = normalized_email
       AND h.email_status = 'sending'
       AND h.claimed_at < timezone('utc'::text, now()) - INTERVAL '10 minutes';

    -- Block claim if recipient has an active sending or uncertain record
    SELECT h.email_status
      INTO blocked_status
      FROM public.campus_ambassador_certificate_history AS h
     WHERE lower(trim(h.participant_email)) = normalized_email
       AND h.email_status IN ('sending', 'uncertain')
     ORDER BY h.created_at DESC
     LIMIT 1;

    IF blocked_status IS NOT NULL THEN
        RETURN QUERY SELECT FALSE, NULL::BIGINT, NULL::TEXT, blocked_status, 0;
        RETURN;
    END IF;

    -- Block claim if recipient was already sent and resend is not requested
    IF NOT COALESCE(p_allow_resend, FALSE) AND EXISTS (
        SELECT 1
          FROM public.campus_ambassador_certificate_history AS h
         WHERE lower(trim(h.participant_email)) = normalized_email
           AND h.email_status = 'sent'
    ) THEN
        RETURN QUERY SELECT FALSE, NULL::BIGINT, NULL::TEXT, 'sent'::TEXT, 0;
        RETURN;
    END IF;

    -- Check if exact document_id record exists for this recipient
    SELECT h.id, h.email_status, h.send_count
      INTO existing_record_id, existing_status, existing_send_count
      FROM public.campus_ambassador_certificate_history AS h
     WHERE h.document_id = p_document_id
       AND lower(trim(h.participant_email)) = normalized_email
     FOR UPDATE;

    IF existing_record_id IS NOT NULL THEN
        IF existing_status NOT IN ('failed', 'sent') THEN
            RETURN QUERY
                SELECT FALSE, existing_record_id, p_document_id, existing_status, existing_send_count;
            RETURN;
        END IF;

        UPDATE public.campus_ambassador_certificate_history AS h
           SET participant_name = trim(p_participant_name),
               program_date = p_program_date,
               pdf_filename = p_pdf_filename,
               email_status = 'sending',
               send_count = existing_send_count + 1,
               error_message = NULL,
               claim_token = p_claim_token,
               claimed_at = timezone('utc'::text, now())
         WHERE h.id = existing_record_id;

        RETURN QUERY
            SELECT TRUE, existing_record_id, p_document_id, 'sending'::TEXT, existing_send_count + 1;
        RETURN;
    END IF;

    SELECT COALESCE(MAX(h.send_count), 0)
      INTO previous_send_count
      FROM public.campus_ambassador_certificate_history AS h
     WHERE lower(trim(h.participant_email)) = normalized_email;

    INSERT INTO public.campus_ambassador_certificate_history AS h (
        participant_name, participant_email, program_date, document_id,
        pdf_filename, email_status, send_count, claim_token, claimed_at
    ) VALUES (
        trim(p_participant_name), normalized_email, p_program_date, p_document_id,
        p_pdf_filename, 'sending', previous_send_count + 1, p_claim_token,
        timezone('utc'::text, now())
    )
    RETURNING h.id INTO existing_record_id;

    RETURN QUERY
        SELECT TRUE, existing_record_id, p_document_id, 'sending'::TEXT, previous_send_count + 1;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_ca_certificate_single_email(
    TEXT, TEXT, TEXT, TEXT, TEXT, UUID, BOOLEAN
) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.claim_ca_certificate_single_email(
    TEXT, TEXT, TEXT, TEXT, TEXT, UUID, BOOLEAN
) TO service_role;

COMMIT;
