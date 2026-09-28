-- =============================================================================
-- Migration 0007: Deterministic CA Certificate Reconciliation & Batch Cap
-- Target: Development Supabase (vaqleosqgsmnsppnxqbh)
--
-- 1. Fixes claim_ca_certificate_job_items with Candidate CTE to eliminate
--    subquery re-evaluation volatility under FOR UPDATE SKIP LOCKED.
-- 2. Fixes claim_ca_certificate_email_items with deterministic 3-phase
--    history reconciliation (3A exact document, 3B uncertain recipient, 3C sent recipient)
--    and strict candidate exclusion for sent/uncertain deliveries.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. RPC: claim_ca_certificate_job_items (Candidate CTE Batch Cap)
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
    UPDATE public.campus_ambassador_certificate_job_items
       SET generation_status = 'pending',
           claim_token = NULL,
           claimed_at = NULL,
           updated_at = timezone('utc'::text, now())
     WHERE job_id = target_job
       AND generation_status = 'processing'
       AND claimed_at < timezone('utc'::text, now()) - INTERVAL '10 minutes';

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
-- 2. RPC: claim_ca_certificate_email_items (Deterministic Reconciliation & Cap)
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

COMMIT;
