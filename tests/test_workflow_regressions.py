import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch


_test_storage = tempfile.TemporaryDirectory(prefix="persevex-workflow-tests-")
os.environ.update({
    "PERSEVEX_ENV": "test",
    "PERSEVEX_SESSION_SECRET": "test-only-session-secret",
    "SUPABASE_URL": "https://test-project.invalid",
    "SUPABASE_KEY": "test-only-server-key",
    "PERSEVEX_SQLITE_DB_PATH": str(Path(_test_storage.name) / "test.sqlite3"),
})

import app as application
import environment_config
from database import repository
from services.ca_bulk_service import parse_ca_file

unittest.addModuleCleanup(_test_storage.cleanup)


def history_result():
    return {
        "records": [],
        "all_filtered_records": [],
        "total_records": 0,
        "total_offer_letters": 0,
        "total_ca_letters": 0,
        "total_certificates": 0,
        "total_ca_certificates": 0,
        "total_sent": 0,
        "total_failed": 0,
        "total_pending": 0,
        "page": 1,
        "per_page": 25,
        "total_pages": 1,
        "available_months": [],
    }


class EnvironmentSelectionTests(unittest.TestCase):
    def test_development_loads_only_development_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.development").write_text("SUPABASE_URL=https://dev.invalid\n", encoding="utf-8")
            (root / ".env").write_text("SUPABASE_URL=https://prod.invalid\n", encoding="utf-8")
            with patch.dict(os.environ, {"PERSEVEX_ENV": "development", "VERCEL": ""}, clear=True):
                with patch.object(environment_config, "load_dotenv") as load:
                    self.assertEqual(environment_config.load_application_environment(root), "development")
                    load.assert_called_once_with(root / ".env.development", override=True)

    def test_production_does_not_load_any_dotenv(self):
        with patch.dict(os.environ, {"PERSEVEX_ENV": "production", "VERCEL": ""}, clear=True):
            with patch.object(environment_config, "load_dotenv") as load:
                self.assertEqual(environment_config.load_application_environment(), "production")
                load.assert_not_called()

    def test_development_requires_development_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"PERSEVEX_ENV": "development", "VERCEL": ""}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "explicit .env.development"):
                    environment_config.load_application_environment(directory)

    def test_missing_session_secret_has_no_fallback(self):
        with patch.dict(os.environ, {"PERSEVEX_SESSION_SECRET": ""}):
            with self.assertRaisesRegex(ValueError, "PERSEVEX_SESSION_SECRET must be configured"):
                environment_config.require_environment_variable("PERSEVEX_SESSION_SECRET")
        self.assertNotEqual(application.app.secret_key, "local-development-secret-change-me")


class SharedCampusAmbassadorParserTests(unittest.TestCase):
    SOURCE = b"Candidate Name,Email\nAlice,alice@example.invalid\n,\nBob,bob@example.invalid\n"

    def test_ca_letter_default_skips_completely_blank_rows(self):
        result = parse_ca_file(self.SOURCE, "participants.csv")
        self.assertTrue(result["success"])
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(result["valid_count"], 2)
        self.assertEqual(result["invalid_count"], 0)

    def test_ca_certificate_can_validate_blank_rows_without_changing_ca_letter(self):
        result = parse_ca_file(self.SOURCE, "participants.csv", preserve_blank_rows=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["total_rows"], 3)
        self.assertEqual(result["valid_count"], 2)
        self.assertEqual(result["invalid_count"], 1)
        self.assertIn("Candidate Name is missing", result["invalid_rows"][0]["reason"])

    def test_ca_letter_and_ca_certificate_upload_routes_select_parser_mode(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
        with patch.object(application, "parse_ca_file", wraps=parse_ca_file) as parse:
            ca_letter = client.post(
                "/api/campus-ambassador/parse-upload",
                data={"file": (BytesIO(self.SOURCE), "participants.csv")},
                content_type="multipart/form-data",
            )
            self.assertEqual(ca_letter.status_code, 200)
            self.assertEqual(parse.call_args.kwargs, {})
            ca_certificate = client.post(
                "/api/ca-certificate/bulk/parse-upload",
                data={"file": (BytesIO(self.SOURCE), "participants.csv")},
                content_type="multipart/form-data",
            )
            self.assertEqual(ca_certificate.status_code, 200)
            self.assertEqual(parse.call_args.kwargs, {"preserve_blank_rows": True})


class DashboardRegressionTests(unittest.TestCase):
    def test_existing_and_ca_certificate_dashboard_routes_render(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
        with patch.object(repository, "get_unified_history", return_value=history_result()):
            for path in (
                "/",
                "/campus-ambassador",
                "/campus-ambassador/bulk",
                "/certificate",
                "/ca-certificate",
                "/ca-certificate/bulk",
                "/history",
            ):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 200)

    def test_single_certificate_generation_does_not_require_email(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
        with patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "26-09-2026")):
            response = client.post("/api/ca-certificate/generate", json={
                "participant_name": "Synthetic Person",
                "participant_email": "",
                "program_date": "2026-09-26",
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["document_id"].startswith("CA-CERT-"))


class SingleDeliverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True
        self.payload = {
            "participant_name": "Synthetic Single",
            "participant_email": "single@example.invalid",
            "program_date": "2026-09-26",
            "document_id": "CA-CERT-" + "a" * 32,
        }

    def test_concurrent_or_repeated_single_send_cannot_send_twice(self):
        claim = MagicMock(side_effect=[
            {"claimed": True, "record_id": 12, "send_count": 1},
            {"claimed": False, "email_status": "sending"},
        ])
        smtp = MagicMock()
        with patch.object(repository, "claim_ca_certificate_single_email", claim), \
             patch.object(repository, "finish_ca_certificate_single_email",
                          return_value={"saved": True, "status": "supabase_and_local"}) as finish, \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL", return_value=smtp):
            smtp.__enter__.return_value = smtp
            first = self.client.post("/api/ca-certificate/send-email", json=self.payload)
            second = self.client.post("/api/ca-certificate/send-email", json=self.payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(smtp.send_message.call_count, 1)
        self.assertEqual(
            finish.call_args.args[2]["send_count"],
            1,
        )

    def test_single_rpc_incremented_send_count_is_not_incremented_again(self):
        response = MagicMock()
        response.data = [{
            "claimed": True,
            "record_id": 32,
            "document_id": self.payload["document_id"],
            "email_status": "sending",
            "send_count": 7,
        }]
        rpc_client = MagicMock()
        rpc_client.rpc.return_value.execute.return_value = response
        with patch.object(repository, "get_supabase_client", return_value=rpc_client):
            claimed = repository.claim_ca_certificate_single_email({
                "participant_name": self.payload["participant_name"],
                "participant_email": self.payload["participant_email"],
                "program_date": "26-09-2026",
                "document_id": self.payload["document_id"],
                "pdf_filename": "Synthetic_CA_Certificate.pdf",
            }, claim_token="claim-token")
        self.assertEqual(claimed["send_count"], 7)
        self.assertEqual(rpc_client.rpc.call_args.args[0], "claim_ca_certificate_single_email")
        self.assertEqual(set(rpc_client.rpc.call_args.args[1]), {
            "p_participant_name", "p_participant_email", "p_program_date",
            "p_document_id", "p_pdf_filename", "p_claim_token", "p_allow_resend",
        })

    def test_uncertain_single_delivery_is_not_retried(self):
        claim = MagicMock(side_effect=[
            {"claimed": True, "record_id": 14, "send_count": 1},
            {"claimed": False, "email_status": "uncertain"},
        ])
        finalized = []

        def finish(record_id, token, values):
            finalized.append(values["email_status"])
            return {"saved": True, "status": "supabase_and_local"}

        smtp = MagicMock()
        smtp.send_message.side_effect = TimeoutError("synthetic SMTP timeout")
        with patch.object(repository, "claim_ca_certificate_single_email", claim), \
             patch.object(repository, "finish_ca_certificate_single_email", side_effect=finish), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL", return_value=smtp):
            smtp.__enter__.return_value = smtp
            first = self.client.post("/api/ca-certificate/send-email", json=self.payload)
            second = self.client.post("/api/ca-certificate/send-email", json=self.payload)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.get_json()["email_status"], "uncertain")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(finalized, ["uncertain"])
        self.assertEqual(smtp.send_message.call_count, 1)


class BulkClaimBoundTests(unittest.TestCase):
    def test_email_claim_is_hard_limited_to_one_recipient(self):
        response = MagicMock()
        response.data = []
        client = MagicMock()
        client.rpc.return_value.execute.return_value = response
        with patch.object(repository, "get_supabase_client", return_value=client):
            self.assertEqual(
                repository.claim_ca_certificate_email_items("job-1", 25),
                [],
            )
        self.assertEqual(client.rpc.call_args.args[0], "claim_ca_certificate_email_items")
        self.assertEqual(client.rpc.call_args.args[1]["batch_size"], 1)


class BulkDeliveryHistoryTests(unittest.TestCase):
    def test_history_upsert_reuses_stable_document_id(self):
        record = {
            "participant_name": "Synthetic Bulk",
            "participant_email": "bulk@example.invalid",
            "program_date": "26-09-2026",
            "document_id": "CA-CERT-" + "c" * 32,
            "pdf_filename": "Synthetic_Bulk_CA_Certificate.pdf",
            "email_status": "sending",
            "send_count": 0,
        }
        with patch.object(repository, "get_supabase_client", return_value=None):
            repository.save_ca_certificate_record(record)
            repository.save_ca_certificate_record({**record, "email_status": "uncertain"})
        import sqlite3
        connection = sqlite3.connect(repository.SQLITE_DB_PATH)
        try:
            rows = connection.execute(
                "SELECT document_id, email_status FROM campus_ambassador_certificate_history "
                "WHERE document_id = ?",
                (record["document_id"],),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [(record["document_id"], "uncertain")])

    def test_failed_and_uncertain_bulk_send_upserts_stable_history_record(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
        item = {
            "id": "item-1",
            "source_row": 1,
            "participant_name": "Synthetic Bulk",
            "participant_email": "bulk@example.invalid",
            "program_date": "26-09-2026",
            "document_id": "CA-CERT-" + "b" * 32,
            "claim_token": "claim-token",
            "attempt_count": 0,
            "send_count": 0,
        }
        saved = []
        updates = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          side_effect=lambda record: saved.append(dict(record)) or {"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item",
                          side_effect=lambda item_id, values, claim_token=None: updates.append(dict(values)) or True), \
             patch.object(repository, "reconcile_ca_certificate_job", return_value={"status": "completed_with_errors"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "job-1"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL") as smtp_factory:
            smtp = smtp_factory.return_value.__enter__.return_value
            smtp.send_message.side_effect = TimeoutError("synthetic timeout")
            response = client.post("/api/ca-certificate/bulk/jobs/job-1/send", json={"batch_size": 1})
        statuses = [row["email_status"] for row in saved]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(statuses, ["sending", "uncertain"])
        self.assertEqual({row["document_id"] for row in saved}, {item["document_id"]})
        self.assertEqual(updates[-1]["email_status"], "uncertain")

    def test_known_bulk_smtp_failure_is_saved_as_failed(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
        item = {
            "id": "item-2",
            "source_row": 1,
            "participant_name": "Synthetic Known Failure",
            "participant_email": "known-failure@example.invalid",
            "program_date": "26-09-2026",
            "document_id": "CA-CERT-" + "d" * 32,
            "claim_token": "claim-token",
            "attempt_count": 0,
            "send_count": 0,
        }
        saved = []
        updates = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          side_effect=lambda record: saved.append(dict(record)) or {"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item",
                          side_effect=lambda item_id, values, claim_token=None: updates.append(dict(values)) or True), \
             patch.object(repository, "reconcile_ca_certificate_job", return_value={"status": "completed_with_errors"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "job-2"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL", side_effect=ConnectionError("synthetic connect failure")):
            response = client.post("/api/ca-certificate/bulk/jobs/job-2/send", json={"batch_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["email_status"] for row in saved], ["sending", "failed"])
        self.assertEqual(updates[-1]["email_status"], "failed")


class UnifiedHistoryRegressionTests(unittest.TestCase):
    def test_existing_record_types_and_uncertain_ca_delivery_are_counted(self):
        patchers = [
            patch.object(repository, "_fetch_all_raw_offer_letters", return_value=[{
                "id": 1, "email_status": "sent", "offer_letter_type": "standard",
                "student_name": "Offer", "created_at": "2026-09-26",
            }]),
            patch.object(repository, "_fetch_all_raw_campus_ambassador", return_value=[{
                "id": 2, "email_status": "sent", "student_name": "CA Letter",
                "created_at": "2026-09-26",
            }]),
            patch.object(repository, "_fetch_all_raw_certificates", return_value=[{
                "id": 3, "email_status": "sent", "student_name": "Intern",
                "created_at": "2026-09-26",
            }]),
            patch.object(repository, "_fetch_all_raw_ca_certificates", return_value=[{
                "id": 4, "email_status": "uncertain", "participant_name": "CA Certificate",
                "participant_email": "uncertain@example.invalid", "document_id": "CA-CERT-X",
                "error_message": "Delivery outcome unknown after interrupted send.",
                "created_at": "2026-09-26",
            }]),
        ]
        for patcher in patchers:
            patcher.start()
        try:
            result = repository.get_unified_history(record_type="all", status_filter="all")
            self.assertEqual({row["record_type"] for row in result["records"]},
                             {"offer_letter", "ca_letter", "certificate", "ca_certificate"})
            self.assertEqual(result["total_ca_certificates"], 1)
            self.assertEqual(result["total_sent"], 3)
            self.assertEqual(result["total_failed"], 1)
            failed = repository.get_unified_history(record_type="ca_certificate", status_filter="failed")
            self.assertEqual(failed["records"][0]["email_status"], "uncertain")
        finally:
            for patcher in reversed(patchers):
                patcher.stop()


class MigrationReleaseTests(unittest.TestCase):
    def test_complete_ca_certificate_migration_has_tables_and_claim_rpcs(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "0003_campus_ambassador_certificate_history.sql"
        sql = migration.read_text(encoding="utf-8")
        for table in (
            "campus_ambassador_certificate_history",
            "campus_ambassador_certificate_jobs",
            "campus_ambassador_certificate_job_items",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS public.{table}", sql)
        for function in (
            "claim_ca_certificate_job_items",
            "claim_ca_certificate_email_items",
            "claim_ca_certificate_single_email",
        ):
            self.assertIn(f"FUNCTION public.{function}", sql)

    def test_supplement_matches_single_rpc_contract_and_serializes_normalized_email(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "0004_ca_certificate_delivery_supplement.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("PERFORM pg_advisory_xact_lock(hashtextextended(normalized_email, 0))", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS claim_token UUID", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ", sql)
        self.assertIn("claim_token = NULL,\n           claimed_at = NULL", sql)
        self.assertIn("FUNCTION public.claim_ca_certificate_single_email(\n    TEXT, TEXT, TEXT, TEXT, TEXT, UUID, BOOLEAN", sql)
        self.assertIn("send_count = existing_send_count + 1", sql)
        self.assertIn("email_status = 'uncertain'", sql)
        self.assertIn("claim_token = NULL,\n           claimed_at = NULL,\n           updated_at", sql)
        self.assertIn("WHEN h.email_status IN ('sent', 'failed', 'uncertain') THEN h.email_status", sql)
        self.assertIn("send_count = GREATEST(i.send_count, COALESCE(h.send_count, 0))", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertIn("SET search_path = pg_catalog, public", sql)
        self.assertNotIn("email_status = 'uncertain'\n", sql[sql.index("RETURN QUERY"):])



class ReadonlyFilesystemPersistenceTests(unittest.TestCase):
    def test_finish_ca_certificate_single_email_handles_readonly_sqlite_safely(self):
        sb_mock = MagicMock()
        sb_mock.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": 99, "email_status": "sent"}]
        )
        with patch.object(repository, "get_supabase_client", return_value=sb_mock), \
             patch("sqlite3.connect", side_effect=sqlite3.OperationalError("attempt to write a readonly database")):
            result = repository.finish_ca_certificate_single_email(
                99,
                "claim-token-123",
                {
                    "participant_name": "Readonly Test",
                    "participant_email": "readonly@example.invalid",
                    "program_date": "29-09-2026",
                    "document_id": "CA-CERT-readonly-test",
                    "pdf_filename": "Readonly_CA_Certificate.pdf",
                    "email_status": "sent",
                    "send_count": 1,
                },
            )
        self.assertTrue(result["saved"])
        self.assertTrue(result["supabase_saved"])
        self.assertFalse(result["local_saved"])
        self.assertEqual(result["status"], "supabase_only")
        self.assertIsNone(result["error"])

    def test_save_ca_certificate_record_handles_readonly_sqlite_safely(self):
        sb_mock = MagicMock()
        sb_mock.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[{"id": 101, "document_id": "CA-CERT-upsert"}]
        )
        with patch.object(repository, "get_supabase_client", return_value=sb_mock), \
             patch("sqlite3.connect", side_effect=sqlite3.OperationalError("attempt to write a readonly database")):
            result = repository.save_ca_certificate_record({
                "participant_name": "Readonly Save",
                "participant_email": "readonly_save@example.invalid",
                "program_date": "29-09-2026",
                "document_id": "CA-CERT-upsert",
                "pdf_filename": "Readonly_Save.pdf",
                "email_status": "sent",
                "send_count": 1,
            })
        self.assertTrue(result["saved"])
        self.assertTrue(result["supabase_saved"])
        self.assertFalse(result["local_saved"])
        self.assertEqual(result["status"], "supabase_only")
        self.assertIsNone(result["error"])

    def test_single_send_api_with_readonly_sqlite_returns_success_without_warning(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        claim_data = {"claimed": True, "record_id": 88, "send_count": 1}
        smtp = MagicMock()
        payload = {
            "participant_name": "API Readonly",
            "participant_email": "api_readonly@example.invalid",
            "program_date": "2026-09-29",
            "document_id": "CA-CERT-" + "c" * 32,
        }

        with patch.object(repository, "claim_ca_certificate_single_email", return_value=claim_data), \
             patch.object(repository, "finish_ca_certificate_single_email",
                          return_value={"saved": True, "supabase_saved": True, "local_saved": False, "status": "supabase_only", "error": None}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "29-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL", return_value=smtp):
            smtp.__enter__.return_value = smtp
            response = client.post("/api/ca-certificate/send-email", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["history_saved"])
        self.assertEqual(data["history_status"], "supabase_only")
        self.assertNotIn("warning", data)

    def test_sqlite_connection_closed_safely_on_execution_exception(self):
        sb_mock = MagicMock()
        sb_mock.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": 105, "email_status": "sent"}]
        )
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error during insert")
        with patch.object(repository, "get_supabase_client", return_value=sb_mock), \
             patch("sqlite3.connect", return_value=mock_conn):
            result = repository.finish_ca_certificate_single_email(
                105,
                "claim-token-xyz",
                {
                    "participant_name": "Close Test",
                    "participant_email": "close_test@example.invalid",
                    "program_date": "29-09-2026",
                    "document_id": "CA-CERT-close-test",
                    "pdf_filename": "Close_Test.pdf",
                    "email_status": "sent",
                    "send_count": 1,
                },
            )
        self.assertTrue(result["saved"])
        self.assertTrue(result["supabase_saved"])
        self.assertFalse(result["local_saved"])
        mock_conn.close.assert_called_once()

    def test_genuine_supabase_failure_produces_warning_and_reports_not_saved(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        claim_data = {"claimed": True, "record_id": 99, "send_count": 1}
        smtp = MagicMock()
        payload = {
            "participant_name": "Genuine Failure",
            "participant_email": "genuine_fail@example.invalid",
            "program_date": "2026-09-29",
            "document_id": "CA-CERT-" + "e" * 32,
        }

        with patch.object(repository, "claim_ca_certificate_single_email", return_value=claim_data), \
             patch.object(repository, "finish_ca_certificate_single_email",
                          side_effect=RuntimeError("Supabase network failure")), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "29-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL", return_value=smtp):
            smtp.__enter__.return_value = smtp
            response = client.post("/api/ca-certificate/send-email", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["history_saved"])
        self.assertEqual(data["history_status"], "not_saved")
        self.assertIn("warning", data)
        self.assertEqual(
            data["warning"],
            "Email delivered, but the history record could not be saved. Do not resend this email; reconcile this delivery before retrying."
        )


if __name__ == "__main__":
    unittest.main()
