BEGIN;

ALTER TABLE public.campus_ambassador_certificate_history
    ADD COLUMN IF NOT EXISTS claim_token UUID,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION public.claim_ca_certificate_email_items(
    target_job UUID,
    batch_size INTEGER,
    token UUID,
    retry_failed BOOLEAN DEFAULT FALSE
) RETURNS SETOF public.campus_ambassador_certificate_job_items
LANGUAGE plpgsql
AS $$
BEGIN
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

    RETURN QUERY
    UPDATE public.campus_ambassador_certificate_job_items
       SET email_status = 'sending',
           claim_token = token,
           claimed_at = timezone('utc'::text, now()),
           updated_at = timezone('utc'::text, now())
     WHERE id IN (
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
    RETURNING *;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_ca_certificate_email_items(
    UUID, INTEGER, UUID, BOOLEAN
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_ca_certificate_email_items(
    UUID, INTEGER, UUID, BOOLEAN
) TO service_role;

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

    PERFORM pg_advisory_xact_lock(hashtextextended(normalized_email, 0));

    UPDATE public.campus_ambassador_certificate_history
       SET email_status = 'uncertain',
           error_message = 'Delivery outcome unknown after interrupted single send; manual reconciliation required.',
           claim_token = NULL,
           claimed_at = NULL
     WHERE lower(trim(participant_email)) = normalized_email
       AND email_status = 'sending'
       AND claimed_at < timezone('utc'::text, now()) - INTERVAL '10 minutes';

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

    IF NOT COALESCE(p_allow_resend, FALSE) AND EXISTS (
        SELECT 1
          FROM public.campus_ambassador_certificate_history AS h
         WHERE lower(trim(h.participant_email)) = normalized_email
           AND h.email_status = 'sent'
    ) THEN
        RETURN QUERY SELECT FALSE, NULL::BIGINT, NULL::TEXT, 'sent'::TEXT, 0;
        RETURN;
    END IF;

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

        UPDATE public.campus_ambassador_certificate_history
           SET participant_name = trim(p_participant_name),
               program_date = p_program_date,
               pdf_filename = p_pdf_filename,
               email_status = 'sending',
               send_count = existing_send_count + 1,
               error_message = NULL,
               claim_token = p_claim_token,
               claimed_at = timezone('utc'::text, now())
         WHERE id = existing_record_id;

        RETURN QUERY
            SELECT TRUE, existing_record_id, p_document_id, 'sending'::TEXT, existing_send_count + 1;
        RETURN;
    END IF;

    SELECT COALESCE(MAX(h.send_count), 0)
      INTO previous_send_count
      FROM public.campus_ambassador_certificate_history AS h
     WHERE lower(trim(h.participant_email)) = normalized_email;

    INSERT INTO public.campus_ambassador_certificate_history (
        participant_name, participant_email, program_date, document_id,
        pdf_filename, email_status, send_count, claim_token, claimed_at
    ) VALUES (
        trim(p_participant_name), normalized_email, p_program_date, p_document_id,
        p_pdf_filename, 'sending', previous_send_count + 1, p_claim_token,
        timezone('utc'::text, now())
    )
    RETURNING id INTO existing_record_id;

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
