-- ============================================================================
-- Migration 0006: Fix CA Certificate Bulk Email Item Claim Single-Recipient Cap
--
-- Root Cause:
-- In migration 0004 / 0003, claim_ca_certificate_email_items used:
--   UPDATE public.campus_ambassador_certificate_job_items ...
--    WHERE id IN (
--        SELECT id FROM public.campus_ambassador_certificate_job_items ...
--        LIMIT 1 FOR UPDATE SKIP LOCKED
--    )
-- Because FOR UPDATE SKIP LOCKED makes the subquery volatile, PostgreSQL
-- re-evaluates the subquery for every candidate row scanned by the outer UPDATE.
-- Consequently, each pending row in the job evaluates to TRUE in sequence,
-- causing all pending items in the job to be claimed as 'sending' instead of 1.
--
-- Fix:
-- Use a candidate Common Table Expression (CTE):
--   WITH claim_candidate AS (
--       SELECT id FROM public.campus_ambassador_certificate_job_items ...
--       LIMIT 1 FOR UPDATE SKIP LOCKED
--   )
--   UPDATE public.campus_ambassador_certificate_job_items AS i
--      SET ...
--     FROM claim_candidate
--    WHERE i.id = claim_candidate.id
--   RETURNING i.*;
--
-- This guarantees the candidate item is locked and materialized strictly once,
-- enforcing an absolute cap of at most 1 claimed item per RPC invocation.
-- ============================================================================

BEGIN;

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

    -- 3. Atomically select, lock and claim at most ONE eligible job item using a CTE
    RETURN QUERY
    WITH claim_candidate AS (
        SELECT id
          FROM public.campus_ambassador_certificate_job_items
         WHERE job_id = target_job
           AND generation_status = 'generated'
           AND (
               email_status = 'pending'
               OR (retry_failed AND email_status = 'failed')
           )
         ORDER BY source_row
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

-- Restrict execution strictly to service_role
REVOKE ALL ON FUNCTION public.claim_ca_certificate_email_items(
    UUID, INTEGER, UUID, BOOLEAN
) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.claim_ca_certificate_email_items(
    UUID, INTEGER, UUID, BOOLEAN
) TO service_role;

COMMIT;
