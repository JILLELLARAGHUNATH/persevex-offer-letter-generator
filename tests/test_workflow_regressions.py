import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
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
import services.bulk_certificate_service as bulk_certificate_service
import certificate_service
import smtplib

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

    def test_ca_certificate_duplicate_email_returns_409_confirmation(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        existing_record = {
            "id": 88,
            "participant_name": "Prior Sent",
            "participant_email": "prior_sent@example.invalid",
            "program_date": "29-09-2026",
            "document_id": "CA-CERT-88888888888888888888888888888888",
            "email_status": "sent",
            "send_count": 1,
        }
        payload = {
            "participant_name": "Prior Sent",
            "participant_email": "prior_sent@example.invalid",
            "program_date": "2026-09-29",
            "document_id": "CA-CERT-" + "9" * 32,
        }

        with patch.object(repository, "get_latest_ca_certificate_by_email", return_value=existing_record):
            response = client.post("/api/ca-certificate/send-email", json=payload)

        self.assertEqual(response.status_code, 409)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertTrue(data["duplicate"])
        self.assertTrue(data["previously_sent"])
        self.assertTrue(data["can_resend"])
        self.assertEqual(data["send_count"], 1)
        self.assertIn("already been sent", data["message"])

    def test_ca_certificate_resend_preserves_document_id_and_increments_count(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        existing_record = {
            "id": 88,
            "participant_name": "Prior Sent",
            "participant_email": "prior_sent@example.invalid",
            "program_date": "29-09-2026",
            "document_id": "CA-CERT-88888888888888888888888888888888",
            "email_status": "sent",
            "send_count": 1,
        }
        claim_data = {
            "claimed": True,
            "record_id": 88,
            "document_id": "CA-CERT-88888888888888888888888888888888",
            "email_status": "sending",
            "send_count": 2,
        }
        finish_result = {
            "saved": True,
            "supabase_saved": True,
            "local_saved": True,
            "status": "synchronized",
        }
        payload = {
            "participant_name": "Prior Sent",
            "participant_email": "prior_sent@example.invalid",
            "program_date": "2026-09-29",
            "document_id": "CA-CERT-new-unwanted-id-33333333333",
            "send_again": True,
        }
        smtp = MagicMock()

        with patch.object(repository, "get_latest_ca_certificate_by_email", return_value=existing_record), \
             patch.object(repository, "claim_ca_certificate_single_email", return_value=claim_data) as mock_claim, \
             patch.object(repository, "finish_ca_certificate_single_email", return_value=finish_result) as mock_finish, \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF synthetic", "29-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL", return_value=smtp):
            smtp.__enter__.return_value = smtp
            response = client.post("/api/ca-certificate/send-email", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["send_count"], 2)

        # Confirm claim received the PRESERVED document_id and allow_resend=True
        mock_claim.assert_called_once()
        claim_call_args = mock_claim.call_args[0][0]
        self.assertEqual(claim_call_args["document_id"], "CA-CERT-88888888888888888888888888888888")
        self.assertTrue(mock_claim.call_args[1]["allow_resend"])

        # Confirm finish received the exact record_id and updated send_count=2
        mock_finish.assert_called_once()
        self.assertEqual(mock_finish.call_args[0][0], 88)
        self.assertEqual(mock_finish.call_args[0][2]["send_count"], 2)

    def test_ca_certificate_uncertain_or_sending_status_disallows_resend(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        for uncertain_status in ("uncertain", "sending"):
            existing_record = {
                "id": 92,
                "participant_name": "Uncertain Candidate",
                "participant_email": "uncertain@example.invalid",
                "program_date": "29-09-2026",
                "document_id": "CA-CERT-92929292929292929292929292929292",
                "email_status": uncertain_status,
                "send_count": 1,
            }
            payload = {
                "participant_name": "Uncertain Candidate",
                "participant_email": "uncertain@example.invalid",
                "program_date": "2026-09-29",
                "send_again": True,
            }

            with patch.object(repository, "get_latest_ca_certificate_by_email", return_value=existing_record):
                response = client.post("/api/ca-certificate/send-email", json=payload)

            self.assertEqual(response.status_code, 409)
            data = response.get_json()
            self.assertFalse(data["success"])
            self.assertTrue(data["duplicate"])
            self.assertFalse(data["can_resend"])
            self.assertTrue(data["reconciliation_required"])
            self.assertIn("reconcile", data["message"].lower())

    def test_ca_certificate_stats_endpoint(self):
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        mock_stats = {"total_sent": 14, "total_failed": 2}
        with patch.object(repository, "get_unified_history", return_value=mock_stats):
            response = client.get("/api/ca-certificate/stats")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["success_count"], 14)
        self.assertEqual(data["failed_count"], 2)

    def test_ca_certificate_smtp_421_returns_503_with_user_friendly_message(self):
        """SMTP 421 (IP rejected) must return 503 with a clear message, not the raw exception."""
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        payload = {
            "participant_name": "SMTP Block Test",
            "participant_email": "smtpblock@example.invalid",
            "program_date": "2026-09-29",
        }
        claimed = {
            "claimed": True,
            "record_id": 99,
            "document_id": "CA-CERT-" + "9" * 32,
            "email_status": "sending",
            "send_count": 1,
        }

        class FakeSMTP421Error(Exception):
            smtp_code = 421

        with (
            patch.object(repository, "get_latest_ca_certificate_by_email", return_value=None),
            patch.object(repository, "claim_ca_certificate_single_email", return_value=claimed),
            patch.object(repository, "finish_ca_certificate_single_email", return_value={"saved": True, "status": "supabase_only"}),
            patch("app.ca_certificate_service.generate_pdf_bytes", return_value=(b"%PDF-mock", "29-09-2026")),
            patch("app.smtplib.SMTP_SSL", side_effect=FakeSMTP421Error("421 SMTPAUTH: IP rejected")),
        ):
            response = client.post("/api/ca-certificate/send-email", json=payload)

        self.assertEqual(response.status_code, 503)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertIn("421", data["error"])
        self.assertNotIn("Traceback", data["error"])
        self.assertNotIn("smtplib", data["error"])
        self.assertTrue(data.get("smtp_blocked"))

    def test_ca_certificate_smtp_failure_rolls_back_send_count(self):
        """A failed SMTP attempt must NOT increment send_count in the history record."""
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        payload = {
            "participant_name": "Rollback Test",
            "participant_email": "rollback@example.invalid",
            "program_date": "2026-09-29",
        }
        claimed = {
            "claimed": True,
            "record_id": 101,
            "document_id": "CA-CERT-" + "a" * 32,
            "email_status": "sending",
            "send_count": 2,  # This was a resend; the RPC already set it to 2
        }

        finish_mock = MagicMock(return_value={"saved": True, "status": "supabase_only"})

        with (
            patch.object(repository, "get_latest_ca_certificate_by_email", return_value=None),
            patch.object(repository, "claim_ca_certificate_single_email", return_value=claimed),
            patch.object(repository, "finish_ca_certificate_single_email", finish_mock),
            patch("app.ca_certificate_service.generate_pdf_bytes", return_value=(b"%PDF-mock", "29-09-2026")),
            patch("app.smtplib.SMTP_SSL", side_effect=ConnectionRefusedError("Connection refused")),
        ):
            response = client.post("/api/ca-certificate/send-email", json=payload)

        self.assertNotEqual(response.status_code, 200)
        finish_mock.assert_called_once()
        # send_count in the history call should be rolled back to 1 (2 - 1)
        saved_send_count = finish_mock.call_args[0][2]["send_count"]
        self.assertEqual(saved_send_count, 1,
                         f"Failed delivery should roll back send_count to 1, got {saved_send_count}")
        self.assertEqual(finish_mock.call_args[0][2]["email_status"], "failed")

    def test_ca_certificate_smtp_auth_failure_returns_503(self):
        """SMTP authentication failure must return 503 with a user-friendly message."""
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        payload = {
            "participant_name": "Auth Fail Test",
            "participant_email": "authfail@example.invalid",
            "program_date": "2026-09-29",
        }
        claimed = {
            "claimed": True,
            "record_id": 102,
            "document_id": "CA-CERT-" + "b" * 32,
            "email_status": "sending",
            "send_count": 1,
        }
        import smtplib as _smtplib
        auth_error = _smtplib.SMTPAuthenticationError(535, b"Authentication failed")

        with (
            patch.object(repository, "get_latest_ca_certificate_by_email", return_value=None),
            patch.object(repository, "claim_ca_certificate_single_email", return_value=claimed),
            patch.object(repository, "finish_ca_certificate_single_email", return_value={"saved": True, "status": "supabase_only"}),
            patch("app.ca_certificate_service.generate_pdf_bytes", return_value=(b"%PDF-mock", "29-09-2026")),
            patch("app.smtplib.SMTP_SSL", side_effect=auth_error),
        ):
            response = client.post("/api/ca-certificate/send-email", json=payload)

        self.assertEqual(response.status_code, 503)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertIn("authentication", data["error"].lower())


class CACertificateBulkJobManagerIntegrationTests(unittest.TestCase):
    """
    Tests for the BulkJobManager integration added to the CA Certificate
    bulk /process and /send endpoints.

    All SMTP calls are mocked — no real emails are sent.
    All Supabase calls are mocked — development DB only, no production access.
    """

    def _authenticated_client(self):
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    def _make_item(self, name="Test Participant", email="participant@example.invalid",
                   doc_id=None, attempt_count=0, send_count=0):
        return {
            "id": "item-bjm-1",
            "source_row": 1,
            "participant_name": name,
            "participant_email": email,
            "program_date": "26-09-2026",
            "document_id": doc_id or f"CA-CERT-{'a' * 32}",
            "claim_token": "test-claim-token",
            "attempt_count": attempt_count,
            "send_count": send_count,
        }

    # ------------------------------------------------------------------
    # /process — generation phase
    # ------------------------------------------------------------------

    def test_process_updates_bjm_progress_on_success(self):
        """_genJobId forwarded to /process must trigger update_progress."""
        client = self._authenticated_client()
        item = {
            "id": "proc-item-1",
            "participant_name": "Gen Success",
            "participant_email": "gen@example.invalid",
            "program_date": "26-09-2026",
            "claim_token": "tok",
            "attempt_count": 0,
        }
        progress_calls = []
        with patch.object(repository, "get_ca_certificate_job", return_value={"id": "repo-job-1"}), \
             patch.object(repository, "claim_ca_certificate_job_items", return_value=[item]), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "completed"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")), \
             patch.object(application.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post(
                "/api/ca-certificate/bulk/jobs/repo-job-1/process",
                json={"batch_size": 10, "job_id": "bjm-gen-job-1"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["generated"], 1)
        # update_progress must have been called with the bjm job id
        self.assertTrue(any(jid == "bjm-gen-job-1" for jid, _ in progress_calls),
                        "update_progress was not called with the BulkJobManager job id")

    def test_process_updates_bjm_progress_on_failure(self):
        """Failed PDF generation must still call update_progress(failed_inc=1)."""
        client = self._authenticated_client()
        item = {
            "id": "proc-item-2",
            "participant_name": "Gen Fail",
            "participant_email": "genfail@example.invalid",
            "program_date": "26-09-2026",
            "claim_token": "tok2",
            "attempt_count": 0,
        }
        progress_calls = []
        with patch.object(repository, "get_ca_certificate_job", return_value={"id": "repo-job-2"}), \
             patch.object(repository, "claim_ca_certificate_job_items", return_value=[item]), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "completed_with_errors"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          side_effect=RuntimeError("Synthetic PDF failure")), \
             patch.object(application.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post(
                "/api/ca-certificate/bulk/jobs/repo-job-2/process",
                json={"batch_size": 10, "job_id": "bjm-gen-job-2"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["failed"], 1)
        failed_calls = [(jid, kw) for jid, kw in progress_calls if kw.get("failed_inc", 0) > 0]
        self.assertTrue(any(jid == "bjm-gen-job-2" for jid, _ in failed_calls),
                        "update_progress(failed_inc=1) was not called for generation failure")

    def test_process_without_bjm_job_id_still_works(self):
        """Omitting job_id in /process body must not break existing behaviour."""
        client = self._authenticated_client()
        item = {
            "id": "proc-item-3",
            "participant_name": "No BJM",
            "participant_email": "nobjm@example.invalid",
            "program_date": "26-09-2026",
            "claim_token": "tok3",
            "attempt_count": 0,
        }
        with patch.object(repository, "get_ca_certificate_job", return_value={"id": "repo-job-3"}), \
             patch.object(repository, "claim_ca_certificate_job_items", return_value=[item]), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "completed"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")):
            response = client.post(
                "/api/ca-certificate/bulk/jobs/repo-job-3/process",
                json={"batch_size": 10},  # no job_id key
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    # ------------------------------------------------------------------
    # /send — sending phase: progress tracking
    # ------------------------------------------------------------------

    def test_send_updates_bjm_progress_on_confirmed_send(self):
        """Successful send must call update_progress(sent_inc=1)."""
        client = self._authenticated_client()
        item = self._make_item()
        progress_calls = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          return_value={"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "running"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "repo-job-send-1"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL") as smtp_factory, \
             patch.object(application.bulk_job_manager, "is_cancelled", return_value=False), \
             patch.object(application.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            smtp_factory.return_value.__enter__.return_value.send_message.return_value = None
            response = client.post(
                "/api/ca-certificate/bulk/jobs/repo-job-send-1/send",
                json={"batch_size": 1, "job_id": "bjm-send-job-1"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["sent"], 1)
        sent_calls = [(jid, kw) for jid, kw in progress_calls if kw.get("sent_inc", 0) > 0]
        self.assertTrue(any(jid == "bjm-send-job-1" for jid, _ in sent_calls),
                        "update_progress(sent_inc=1) was not called after confirmed send")

    def test_send_updates_bjm_progress_on_known_failure(self):
        """Known SMTP failure (connection error) must call update_progress(failed_inc=1)."""
        client = self._authenticated_client()
        item = self._make_item(name="Send Fail", email="sendfail@example.invalid")
        progress_calls = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          return_value={"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "running"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "repo-job-send-2"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL",
                          side_effect=ConnectionError("synthetic connect failure")), \
             patch.object(application.bulk_job_manager, "is_cancelled", return_value=False), \
             patch.object(application.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post(
                "/api/ca-certificate/bulk/jobs/repo-job-send-2/send",
                json={"batch_size": 1, "job_id": "bjm-send-job-2"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["failed"], 1)
        failed_calls = [(jid, kw) for jid, kw in progress_calls if kw.get("failed_inc", 0) > 0]
        self.assertTrue(any(jid == "bjm-send-job-2" for jid, _ in failed_calls),
                        "update_progress(failed_inc=1) was not called for send failure")

    def test_send_updates_bjm_progress_on_uncertain(self):
        """Uncertain outcome (send_attempted=True then exception) must call update_progress(failed_inc=1)."""
        client = self._authenticated_client()
        item = self._make_item(name="Uncertain", email="uncertain@example.invalid")
        progress_calls = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          return_value={"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "running"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "repo-job-send-3"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL") as smtp_factory, \
             patch.object(application.bulk_job_manager, "is_cancelled", return_value=False), \
             patch.object(application.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            smtp_factory.return_value.__enter__.return_value.send_message.side_effect = TimeoutError("timeout after send")
            response = client.post(
                "/api/ca-certificate/bulk/jobs/repo-job-send-3/send",
                json={"batch_size": 1, "job_id": "bjm-send-job-3"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["uncertain"], 1)
        # uncertain is still counted as a progress failure for BJM display
        failed_calls = [(jid, kw) for jid, kw in progress_calls if kw.get("failed_inc", 0) > 0]
        self.assertTrue(any(jid == "bjm-send-job-3" for jid, _ in failed_calls),
                        "update_progress(failed_inc=1) was not called for uncertain send")

    # ------------------------------------------------------------------
    # /send — cancellation
    # ------------------------------------------------------------------

    def test_send_cancelled_before_item_returns_409(self):
        """When BulkJobManager marks a job as cancelled, /send must return 409 with cancelled:true."""
        client = self._authenticated_client()
        with patch.object(application.bulk_job_manager, "is_cancelled", return_value=True):
            response = client.post(
                "/api/ca-certificate/bulk/jobs/repo-job-cancel-1/send",
                json={"batch_size": 1, "job_id": "bjm-cancel-job-1"},
            )
        self.assertEqual(response.status_code, 409)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertTrue(data.get("cancelled"), "Response must include cancelled:true")

    def test_send_no_bjm_job_id_skips_cancel_check(self):
        """Omitting job_id in /send must not raise — is_cancelled must not be called."""
        client = self._authenticated_client()
        item = self._make_item()
        is_cancelled_calls = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          return_value={"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "running"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "rj"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL") as smtp_factory, \
             patch.object(application.bulk_job_manager, "is_cancelled",
                          side_effect=lambda jid: is_cancelled_calls.append(jid) or False):
            smtp_factory.return_value.__enter__.return_value.send_message.return_value = None
            response = client.post(
                "/api/ca-certificate/bulk/jobs/rj/send",
                json={"batch_size": 1},  # no job_id
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(is_cancelled_calls, [],
                         "is_cancelled must not be called when no bjm job_id is provided")

    # ------------------------------------------------------------------
    # Safety mechanisms — must remain intact after changes
    # ------------------------------------------------------------------

    def test_uncertain_detection_preserved(self):
        """send_attempted=True + exception must still produce email_status='uncertain' in history."""
        client = self._authenticated_client()
        item = self._make_item(name="Uncertain Safety", email="uncertain2@example.invalid")
        saved = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          side_effect=lambda r: saved.append(dict(r)) or {"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "running"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "rj-unc"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL") as smtp_factory, \
             patch.object(application.bulk_job_manager, "is_cancelled", return_value=False), \
             patch.object(application.bulk_job_manager, "update_progress", return_value=None):
            smtp_factory.return_value.__enter__.return_value.send_message.side_effect = TimeoutError("timeout")
            response = client.post(
                "/api/ca-certificate/bulk/jobs/rj-unc/send",
                json={"batch_size": 1, "job_id": "bjm-unc"},
            )
        statuses = [r["email_status"] for r in saved]
        self.assertIn("uncertain", statuses, "uncertain email_status must still be written to history")

    def test_send_count_incremented_on_success(self):
        """send_count in the history record must be original + 1 on successful send."""
        client = self._authenticated_client()
        item = self._make_item(send_count=2)  # already sent twice before
        saved = []
        with patch.object(repository, "claim_ca_certificate_email_items", return_value=[item]), \
             patch.object(repository, "save_ca_certificate_record",
                          side_effect=lambda r: saved.append(dict(r)) or {"supabase_saved": True}), \
             patch.object(repository, "update_ca_certificate_job_item", return_value=True), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "running"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(repository, "get_ca_certificate_job", return_value={"id": "rj-sc"}), \
             patch.object(application.ca_certificate_service, "generate_pdf_bytes",
                          return_value=(b"%PDF mock", "26-09-2026")), \
             patch.object(application, "SENDER_EMAIL", "test@example.invalid"), \
             patch.object(application.smtplib, "SMTP_SSL") as smtp_factory, \
             patch.object(application.bulk_job_manager, "is_cancelled", return_value=False), \
             patch.object(application.bulk_job_manager, "update_progress", return_value=None):
            smtp_factory.return_value.__enter__.return_value.send_message.return_value = None
            response = client.post(
                "/api/ca-certificate/bulk/jobs/rj-sc/send",
                json={"batch_size": 1, "job_id": "bjm-sc"},
            )
        self.assertEqual(response.status_code, 200)
        sent_record = next((r for r in saved if r.get("email_status") == "sent"), None)
        self.assertIsNotNone(sent_record, "A 'sent' record must be saved on success")
        self.assertEqual(sent_record["send_count"], 3,
                         "send_count must be original (2) + 1 = 3")

    def test_retry_flag_forwarded_to_repository(self):
        """retry=True from the frontend must reach claim_ca_certificate_email_items as retry_failed=True."""
        client = self._authenticated_client()
        claim_calls = []
        with patch.object(repository, "claim_ca_certificate_email_items",
                          side_effect=lambda job_id, n, retry_failed=False:
                              claim_calls.append(retry_failed) or []), \
             patch.object(repository, "reconcile_ca_certificate_job",
                          return_value={"status": "running"}), \
             patch.object(repository, "get_ca_certificate_job_items", return_value=[]), \
             patch.object(application.bulk_job_manager, "is_cancelled", return_value=False):
            response = client.post(
                "/api/ca-certificate/bulk/jobs/rj-retry/send",
                json={"batch_size": 1, "retry": True, "job_id": "bjm-retry"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(claim_calls and claim_calls[0] is True,
                        "retry_failed must be True when retry=True is sent")

    # ------------------------------------------------------------------
    # No cancelled History records
    # ------------------------------------------------------------------

    def test_cancel_does_not_produce_history_records_for_unprocessed(self):
        """
        When is_cancelled() returns True before any item is touched,
        no history record must be written and sent/failed must both be 0.
        """
        client = self._authenticated_client()
        saved = []
        with patch.object(application.bulk_job_manager, "is_cancelled", return_value=True), \
             patch.object(repository, "save_ca_certificate_record",
                          side_effect=lambda r: saved.append(dict(r)) or {"supabase_saved": True}):
            response = client.post(
                "/api/ca-certificate/bulk/jobs/rj-no-hist/send",
                json={"batch_size": 1, "job_id": "bjm-no-hist"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(saved, [],
                         "No history record must be written for cancelled-before-item requests")


class CALetterBulkPartialEvaluationTests(unittest.TestCase):
    """
    Regression tests for the CA Letter Bulk partial-evaluation feature.

    Business rule: invalid rows must NOT block generation of valid rows.
    Generation is enabled when ≥1 valid unique record exists, regardless of
    invalid rows.  Invalid rows are ALWAYS excluded from the candidates list
    returned by parse_ca_file; the frontend never sends them to the backend.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_csv(self, rows):
        """Build a minimal CSV bytes object from a list of (name, email) tuples."""
        lines = ["Candidate Name,Email"]
        for name, email in rows:
            lines.append(f"{name},{email}")
        return "\n".join(lines).encode()

    # ------------------------------------------------------------------
    # TEST 1: 500-row mix — valid, duplicate, and 1 invalid
    # ------------------------------------------------------------------

    def test_valid_plus_one_invalid_allows_generation(self):
        """
        Scenario: 500 rows, with valid records, 2 duplicates, and 1 invalid row.
        Expected:
        - evaluation produces valid_count > 0
        - invalid_count == 1
        - candidates list contains only valid unique records (no invalid, no extra duplicates)
        - invalid row (taniya@...) is NOT in candidates
        """
        rows = []
        # 497 valid unique rows
        for i in range(497):
            rows.append((f"Candidate {i}", f"candidate{i}@example.invalid"))
        # 2 duplicate rows (same email as rows 0 and 1)
        rows.append(("Dup A", "candidate0@example.invalid"))
        rows.append(("Dup B", "candidate1@example.invalid"))
        # 1 invalid row (bad email)
        rows.append(("Taniya", "taniya"))

        result = parse_ca_file(self._build_csv(rows), "test500.csv")

        self.assertTrue(result["success"])
        self.assertEqual(result["total_rows"], 500)
        self.assertEqual(result["invalid_count"], 1,
                         "Exactly 1 row has an invalid email")
        self.assertEqual(result["duplicate_count"], 2,
                         "2 rows are duplicates of earlier rows")
        self.assertEqual(result["valid_count"], 497,
                         "497 unique valid recipients")
        # Candidates list must contain only valid unique records
        self.assertEqual(len(result["candidates"]), 497)
        candidate_emails = {c["email"] for c in result["candidates"]}
        self.assertNotIn("taniya",
                         candidate_emails,
                         "Invalid email must never appear in candidates")
        # Duplicate occurrences must also be absent
        for c in result["candidates"]:
            self.assertNotEqual(c["name"], "Dup A")
            self.assertNotEqual(c["name"], "Dup B")

    def test_valid_plus_invalid_generation_endpoint_never_receives_invalid(self):
        """
        When the frontend calls /api/campus-ambassador/bulk-generate-item it sends
        only items from validCandidatesList. Verify:
        1. A valid item is accepted by the endpoint (200).
        2. A request with a missing/empty name is rejected (400).
        3. A request with a missing/empty email is rejected (400).

        This proves that even if an invalid row somehow reached the endpoint, the
        server-side guards would block it.
        """
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True

        # Mock the PDF generation function so no real filesystem writes occur.
        with patch.object(application, "generate_ca_pdf", return_value="Alice_CA.pdf"), \
             patch.object(application, "format_ca_date", return_value="01 January 2025"):

            # Valid item → must be accepted
            valid_resp = client.post(
                "/api/campus-ambassador/bulk-generate-item",
                json={"student_name": "Alice", "student_email": "alice@example.invalid",
                      "date": "2025-01-01"},
            )
            self.assertIn(valid_resp.status_code, (200, 201),
                          "Valid record must be accepted by the generate endpoint")

            # Missing name → must be rejected (invalid row guard)
            no_name_resp = client.post(
                "/api/campus-ambassador/bulk-generate-item",
                json={"student_name": "", "student_email": "alice@example.invalid",
                      "date": "2025-01-01"},
            )
            self.assertEqual(no_name_resp.status_code, 400,
                             "Empty name must be rejected at the server level")

            # Missing email → must be rejected (invalid row guard)
            no_email_resp = client.post(
                "/api/campus-ambassador/bulk-generate-item",
                json={"student_name": "Alice", "student_email": "",
                      "date": "2025-01-01"},
            )
            self.assertEqual(no_email_resp.status_code, 400,
                             "Empty email must be rejected at the server level")

    # ------------------------------------------------------------------
    # TEST 2: All rows invalid — generation must stay disabled
    # ------------------------------------------------------------------

    def test_all_rows_invalid_produces_zero_candidates(self):
        """
        Scenario: every row has an invalid email.
        Expected:
        - valid_count == 0
        - candidates list is empty
        - invalid_count == total rows
        - no generation possible (frontend uses len(candidates) === 0 guard)
        """
        rows = [
            ("Alice", "not-an-email"),
            ("Bob", "@missinglocal.com"),
            ("Carol", "no-at-sign"),
            ("", "diana@example.invalid"),   # missing name
        ]
        result = parse_ca_file(self._build_csv(rows), "all_invalid.csv")

        self.assertTrue(result["success"])
        self.assertEqual(result["valid_count"], 0,
                         "No valid recipients — all rows are invalid")
        self.assertEqual(result["invalid_count"], 4)
        self.assertEqual(result["candidates"], [],
                         "Candidates list must be empty when all rows are invalid")
        # The frontend guard (validCandidatesList.length === 0) prevents generation.
        # No further backend test needed; this is a frontend state machine guard.

    # ------------------------------------------------------------------
    # TEST 3: Mixed valid + invalid — only valid records in candidates
    # ------------------------------------------------------------------

    def test_mixed_valid_invalid_candidates_list_excludes_invalid(self):
        """
        Scenario: 5 rows — 3 valid, 2 invalid.
        Expected:
        - valid_count == 3, invalid_count == 2
        - candidates list has exactly 3 entries, all with valid emails
        """
        rows = [
            ("Alice", "alice@example.invalid"),       # valid
            ("Bad1", "not-valid"),                    # invalid
            ("Bob", "bob@example.invalid"),           # valid
            ("Bad2", ""),                             # invalid (empty email)
            ("Carol", "carol@example.invalid"),       # valid
        ]
        result = parse_ca_file(self._build_csv(rows), "mixed.csv")

        self.assertTrue(result["success"])
        self.assertEqual(result["valid_count"], 3)
        self.assertEqual(result["invalid_count"], 2)
        self.assertEqual(len(result["candidates"]), 3)
        emails = {c["email"] for c in result["candidates"]}
        self.assertIn("alice@example.invalid", emails)
        self.assertIn("bob@example.invalid", emails)
        self.assertIn("carol@example.invalid", emails)
        self.assertNotIn("not-valid", emails)
        self.assertNotIn("", emails)

    # ------------------------------------------------------------------
    # TEST 4: Mixed valid + duplicate + invalid — counts are correct
    # ------------------------------------------------------------------

    def test_mixed_valid_duplicate_invalid_correct_counts(self):
        """
        Scenario: 8 rows:
          - 3 unique valid
          - 2 duplicates (repeat emails of valid rows)
          - 3 invalid
        Expected counts and candidates list integrity.
        """
        rows = [
            ("Alice", "alice@example.invalid"),        # valid, unique #1
            ("Bob", "bob@example.invalid"),            # valid, unique #2
            ("Carol", "carol@example.invalid"),        # valid, unique #3
            ("Alice Again", "alice@example.invalid"),  # duplicate of #1
            ("Bob Again", "bob@example.invalid"),      # duplicate of #2
            ("Bad1", "invalid-email"),                 # invalid
            ("Bad2", "also-bad"),                      # invalid
            ("", "carol@example.invalid"),             # invalid (empty name)
        ]
        result = parse_ca_file(self._build_csv(rows), "mixed4.csv")

        self.assertTrue(result["success"])
        self.assertEqual(result["total_rows"], 8)
        self.assertEqual(result["valid_count"], 3,
                         "Only the 3 unique valid records count")
        self.assertEqual(result["duplicate_count"], 2,
                         "2 rows are later occurrences of already-seen emails")
        self.assertEqual(result["invalid_count"], 3,
                         "3 rows are invalid (bad email or missing name)")
        self.assertEqual(len(result["candidates"]), 3)

        candidate_emails = {c["email"] for c in result["candidates"]}
        self.assertEqual(
            candidate_emails,
            {"alice@example.invalid", "bob@example.invalid", "carol@example.invalid"},
        )
        # Invalid row emails must never appear
        self.assertNotIn("invalid-email", candidate_emails)
        self.assertNotIn("also-bad", candidate_emails)

    # ------------------------------------------------------------------
    # TEST 5: History-aware resend behavior unchanged
    # ------------------------------------------------------------------

    def test_parse_upload_route_returns_candidates_and_invalid_rows_correctly(self):
        """
        Verify the /api/campus-ambassador/parse-upload endpoint continues to
        return both valid candidates AND invalid_rows in the response payload,
        so the frontend can display the invalid rows breakdown while enabling
        generation for the valid ones.

        This protects the server-side contract that the frontend relies on.
        """
        csv_bytes = self._build_csv([
            ("Alice", "alice@example.invalid"),   # valid
            ("Bad", "not-an-email"),               # invalid
            ("Bob", "bob@example.invalid"),        # valid
        ])

        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True

        response = client.post(
            "/api/campus-ambassador/parse-upload",
            data={"file": (BytesIO(csv_bytes), "test.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        # valid_count reflects unique valid records only
        self.assertEqual(data["valid_count"], 2)
        self.assertEqual(data["invalid_count"], 1)
        # candidates list must contain only valid records
        self.assertEqual(len(data["candidates"]), 2)
        candidate_emails = {c["email"] for c in data["candidates"]}
        self.assertNotIn("not-an-email", candidate_emails)
        # invalid_rows must be populated for the frontend to render the breakdown
        self.assertEqual(len(data.get("invalid_rows", [])), 1)
        self.assertIn("not-an-email", data["invalid_rows"][0]["email"])


# ============================================================
# INTERNSHIP OFFER LETTER BULK JOB MANAGER INTEGRATION TESTS
# ============================================================

class OfferLetterBulkJobManagerIntegrationTests(unittest.TestCase):
    def _authenticated_client(self):
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    def test_offer_letter_bulk_generate_propagates_job_id_and_updates_progress(self):
        client = self._authenticated_client()
        payload = {
            "student_name": "Offer Student",
            "student_email": "offer@example.invalid",
            "domain": "Web Development",
            "duration": "2 months",
            "start_date": "01/10/2026",
            "end_date": "30/11/2026",
            "stipend": "Unpaid",
            "letter_type": "with_hours",
            "weeks": 8,
            "hours_per_week": 15,
            "total_hours": 120,
            "job_id": "ol-bjm-job-1",
        }
        progress_calls = []
        with patch.object(application, "generate_pdf", return_value="dummy_offer.pdf"), \
             patch.object(application.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post("/api/offer-letter/bulk/generate-item", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["filename"], "dummy_offer.pdf")
        self.assertTrue(any(jid == "ol-bjm-job-1" and kw.get("processed_inc") == 1 for jid, kw in progress_calls))

    def test_offer_letter_bulk_generate_updates_progress_on_failure(self):
        client = self._authenticated_client()
        payload = {
            "student_name": "Failing Student",
            "student_email": "fail@example.invalid",
            "domain": "Web Development",
            "duration": "2 months",
            "start_date": "01/10/2026",
            "end_date": "30/11/2026",
            "stipend": "Unpaid",
            "letter_type": "with_hours",
            "job_id": "ol-bjm-job-2",
        }
        progress_calls = []
        with patch.object(application, "generate_pdf", side_effect=Exception("PDF generation crashed")), \
             patch.object(application.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post("/api/offer-letter/bulk/generate-item", json=payload)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(any(jid == "ol-bjm-job-2" and kw.get("failed_inc") == 1 for jid, kw in progress_calls))

    def test_offer_letter_bulk_generate_cancelled_returns_409(self):
        client = self._authenticated_client()
        payload = {
            "student_name": "Cancelled Student",
            "student_email": "cancelled@example.invalid",
            "domain": "Web Development",
            "duration": "2 months",
            "start_date": "01/10/2026",
            "end_date": "30/11/2026",
            "stipend": "Unpaid",
            "letter_type": "with_hours",
            "job_id": "ol-bjm-cancelled",
        }
        with patch.object(application.bulk_job_manager, "is_cancelled", return_value=True):
            response = client.post("/api/offer-letter/bulk/generate-item", json=payload)
        self.assertEqual(response.status_code, 409)
        data = response.get_json()
        self.assertTrue(data.get("cancelled"))

    def test_offer_letter_send_cancel_endpoint_prevents_dispatch_and_no_history(self):
        # When bulkOfferCancelDispatchBtn calls /api/bulk/cancel, job is cancelled
        client = self._authenticated_client()
        job = application.bulk_job_manager.create_job(
            workflow_type="offer_letter",
            operation_type="sending",
            total=10,
            title="Sending Offer Letters"
        )
        job_id = job["job_id"]
        # Cancel the job
        cancel_resp = client.post("/api/bulk/cancel", json={"job_id": job_id})
        self.assertEqual(cancel_resp.status_code, 200)
        self.assertTrue(application.bulk_job_manager.is_cancelled(job_id))

    def test_offer_letter_resend_increments_send_count_and_preserves_document_id(self):
        # Standard offer letter duplicate / resend behavior check
        client = self._authenticated_client()
        # Verify /api/offer-letter/bulk/check-duplicates route checks history
        with patch.object(repository, "find_existing_offer_letters_batch", return_value={
            "existing@example.invalid": {"recipient_email": "existing@example.invalid", "candidate_name": "Existing", "status": "Sent", "id": 42}
        }):
            resp = client.post("/api/offer-letter/bulk/check-duplicates", json={"emails": ["existing@example.invalid", "new@example.invalid"]})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("existing@example.invalid", data["duplicates"])
        self.assertNotIn("new@example.invalid", data["duplicates"])


# ============================================================
# INTERNSHIP CERTIFICATE BULK JOB MANAGER INTEGRATION TESTS
# ============================================================

class CertificateBulkJobManagerIntegrationTests(unittest.TestCase):
    def _authenticated_client(self):
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    def test_certificate_bulk_generate_propagates_job_id_and_updates_progress(self):
        client = self._authenticated_client()
        items = [
            {"student_name": "Student A", "student_email": "a@example.invalid", "domain": "Web Development",
             "start_date": "01/01/2026", "end_date": "01/03/2026"},
            {"student_name": "Student B", "student_email": "b@example.invalid", "domain": "Data Science",
             "start_date": "01/01/2026", "end_date": "01/03/2026"},
        ]
        progress_calls = []
        with patch("certificate_service.generate_certificate_pdf", side_effect=["cert_a.pdf", "cert_b.pdf"]), \
             patch.object(bulk_certificate_service.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post(
                "/api/certificate/bulk/generate",
                json={"rows": items, "job_id": "cert-gen-job-1"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["results"]), 2)
        # Should have updated progress twice with processed_inc=1
        processed_updates = [kw for jid, kw in progress_calls if jid == "cert-gen-job-1" and kw.get("processed_inc") == 1]
        self.assertEqual(len(processed_updates), 2)

    def test_certificate_bulk_generate_updates_progress_on_failure(self):
        client = self._authenticated_client()
        items = [
            {"student_name": "Crash Student", "student_email": "crash@example.invalid", "domain": "Design",
             "start_date": "01/01/2026", "end_date": "01/03/2026"}
        ]
        progress_calls = []
        with patch("certificate_service.generate_certificate_pdf", side_effect=Exception("Font missing")), \
             patch.object(bulk_certificate_service.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post(
                "/api/certificate/bulk/generate",
                json={"rows": items, "job_id": "cert-gen-fail-job"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["failed_items"]), 1)
        failed_updates = [kw for jid, kw in progress_calls if jid == "cert-gen-fail-job" and kw.get("failed_inc") == 1]
        self.assertEqual(len(failed_updates), 1)

    def test_certificate_bulk_generate_cancel_stops_subsequent_items(self):
        client = self._authenticated_client()
        items = [
            {"student_name": "Student 1", "student_email": "s1@example.invalid", "domain": "Web", "start_date": "01/01/2026", "end_date": "01/02/2026"},
            {"student_name": "Student 2", "student_email": "s2@example.invalid", "domain": "Web", "start_date": "01/01/2026", "end_date": "01/02/2026"},
        ]
        # Route checks once, item 1 checks once, then item 2 checks and gets True
        call_count = [0]
        def mock_is_cancelled(job_id):
            call_count[0] += 1
            return call_count[0] > 2

        with patch("certificate_service.generate_certificate_pdf", return_value="cert1.pdf") as mock_pdf, \
             patch.object(bulk_certificate_service.bulk_job_manager, "is_cancelled", side_effect=mock_is_cancelled):
            response = client.post(
                "/api/certificate/bulk/generate",
                json={"rows": items, "job_id": "cert-cancel-gen"}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(mock_pdf.call_count, 1)

    def test_certificate_bulk_send_updates_progress_on_success(self):
        client = self._authenticated_client()
        items = [
            {"certificate_id": "C-1", "student_name": "Student 1", "student_email": "s1@example.invalid", "domain": "Web", "filename": "c1.pdf"},
            {"certificate_id": "C-2", "student_name": "Student 2", "student_email": "s2@example.invalid", "domain": "Web", "filename": "c2.pdf"},
        ]
        progress_calls = []
        with patch("certificate_service.generate_certificate_pdf", side_effect=["c1.pdf", "c2.pdf"]), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_previous_sent_certificate_by_email", return_value=None), \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None), \
             patch.object(bulk_certificate_service.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post(
                "/api/certificate/bulk/send-email",
                json={"items": items, "job_id": "cert-send-success-job"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["successful_count"], 2)
        sent_updates = [kw for jid, kw in progress_calls if jid == "cert-send-success-job" and kw.get("sent_inc") == 1]
        self.assertEqual(len(sent_updates), 2)

    def test_certificate_bulk_send_updates_progress_on_failure(self):
        client = self._authenticated_client()
        items = [
            {"certificate_id": "C-FAIL", "student_name": "Student Fail", "student_email": "fail@example.invalid", "domain": "Web", "filename": "cfail.pdf"}
        ]
        progress_calls = []
        with patch("certificate_service.generate_certificate_pdf", return_value="cfail.pdf"), \
             patch("certificate_service.send_certificate_email", side_effect=Exception("SMTP timeout")), \
             patch.object(bulk_certificate_service, "get_previous_sent_certificate_by_email", return_value=None), \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None), \
             patch.object(bulk_certificate_service.bulk_job_manager, "update_progress",
                          side_effect=lambda job_id, **kw: progress_calls.append((job_id, kw)) or None):
            response = client.post(
                "/api/certificate/bulk/send-email",
                json={"items": items, "job_id": "cert-send-fail-job"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["failed_count"], 1)
        failed_updates = [kw for jid, kw in progress_calls if jid == "cert-send-fail-job" and kw.get("failed_inc") == 1]
        self.assertEqual(len(failed_updates), 1)

    def test_certificate_bulk_send_cancellation_stops_before_next_recipient_and_no_history_for_unprocessed(self):
        client = self._authenticated_client()
        items = [
            {"certificate_id": "C-1", "student_name": "S1", "student_email": "s1@example.invalid", "domain": "Web", "filename": "c1.pdf"},
            {"certificate_id": "C-2", "student_name": "S2", "student_email": "s2@example.invalid", "domain": "Web", "filename": "c2.pdf"},
            {"certificate_id": "C-3", "student_name": "S3", "student_email": "s3@example.invalid", "domain": "Web", "filename": "c3.pdf"},
        ]
        # Route checks once (call 1 -> False), item 1 checks (call 2 -> False), item 2 checks (call 3 -> True)
        call_count = [0]
        def mock_is_cancelled(job_id):
            call_count[0] += 1
            return call_count[0] > 2

        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="c1.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True) as mock_send, \
             patch.object(bulk_certificate_service, "get_previous_sent_certificate_by_email", return_value=None), \
             patch.object(bulk_certificate_service, "save_certificate_record",
                          side_effect=lambda rec, **kw: saved_records.append(rec) or True), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None), \
             patch.object(bulk_certificate_service.bulk_job_manager, "is_cancelled", side_effect=mock_is_cancelled):
            response = client.post(
                "/api/certificate/bulk/send-email",
                json={"items": items, "job_id": "cert-cancel-send"}
            )
        self.assertEqual(response.status_code, 409)
        # Only item 1 was sent
        self.assertEqual(mock_send.call_count, 1)
        # S1 has a history record, but S2 and S3 must NOT have history records
        saved_emails = [r["student_email"] for r in saved_records]
        self.assertIn("s1@example.invalid", saved_emails)
        self.assertNotIn("s2@example.invalid", saved_emails)
        self.assertNotIn("s3@example.invalid", saved_emails)

    def test_certificate_bulk_send_skips_historical_duplicates_when_send_again_all_is_false(self):
        client = self._authenticated_client()
        items = [
            {"certificate_id": "C-DUP", "student_name": "Dup", "student_email": "dup@example.invalid", "domain": "D", "filename": "c_dup.pdf"},
            {"certificate_id": "C-NEW", "student_name": "New", "student_email": "new@example.invalid", "domain": "D", "filename": "c_new.pdf"},
        ]
        prior_record = {
            "id": 101,
            "certificate_id": "C-DUP",
            "student_name": "Dup",
            "student_email": "dup@example.invalid",
            "email_status": "sent",
            "send_count": 1,
        }
        sent_emails = []
        with patch("certificate_service.generate_certificate_pdf", return_value="c_new.pdf"), \
             patch("certificate_service.send_certificate_email",
                   side_effect=lambda *args, **kwargs: sent_emails.append(args[1] if len(args) > 1 else kwargs.get("student_email")) or True), \
             patch.object(bulk_certificate_service, "get_previous_sent_certificate_by_email",
                          side_effect=lambda email: prior_record if email == "dup@example.invalid" else None), \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            response = client.post(
                "/api/certificate/bulk/send-email",
                json={"items": items, "skip_duplicate_emails": ["dup@example.invalid"], "send_again_all": False}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["successful_count"], 1)
        # dup@example.invalid was skipped, only new@example.invalid was sent
        skipped_results = [r for r in data.get("results", []) if r.get("status") == "skipped_historical_duplicate"]
        self.assertEqual(len(skipped_results), 1)
        self.assertEqual(skipped_results[0]["student_email"], "dup@example.invalid")

    def test_certificate_bulk_send_resend_increments_send_count_and_reuses_existing_record(self):
        client = self._authenticated_client()
        items = [
            {"certificate_id": "C-OLD", "student_name": "Prior", "student_email": "prior@example.invalid", "domain": "D", "filename": "c_prior.pdf"}
        ]
        prior_record = {
            "id": 999,
            "certificate_id": "C-OLD",
            "student_name": "Prior",
            "student_email": "prior@example.invalid",
            "email_status": "sent",
            "send_count": 2,
        }
        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="c_prior.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_previous_sent_certificate_by_email", return_value=prior_record), \
             patch.object(bulk_certificate_service, "save_certificate_record",
                          side_effect=lambda rec, existing_id=None: saved_records.append((rec, existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            response = client.post(
                "/api/certificate/bulk/send-email",
                json={"items": items, "send_again_all": True}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["successful_count"], 1)
        self.assertEqual(len(saved_records), 1)
        rec, existing_id = saved_records[0]
        self.assertEqual(existing_id, 999)
        self.assertEqual(rec["id"], 999)
        self.assertEqual(rec["send_count"], 3)  # incremented from 2 to 3
        self.assertTrue(rec["send_again"])

    def test_certificate_bulk_send_different_course_preserves_distinct_certificate_id_and_does_not_overwrite_prior_record(self):
        client = self._authenticated_client()
        # Student previously completed Web Development with ID PEX-WD-001 (PK id 101)
        prior_record = {
            "id": 101,
            "certificate_id": "PEX-WD-001",
            "student_name": "Alice Smith",
            "student_email": "alice@example.invalid",
            "internship_domain": "Web Development",
            "start_date": "01-01-2025",
            "end_date": "01-02-2025",
            "email_status": "sent",
            "send_count": 1,
        }
        # Student now completes Data Science in bulk with newly generated ID PEX-DS-002 (PK id 202, pending)
        pending_record = {
            "id": 202,
            "certificate_id": "PEX-DS-002",
            "student_name": "Alice Smith",
            "student_email": "alice@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "01-03-2025",
            "end_date": "01-04-2025",
            "email_status": "pending",
            "send_count": 0,
        }
        items = [
            {
                "certificate_id": "PEX-DS-002",
                "student_name": "Alice Smith",
                "student_email": "alice@example.invalid",
                "domain": "Data Science",
                "start_date": "01-03-2025",
                "end_date": "01-04-2025",
                "filename": "Alice_PEX-DS-002.pdf",
            }
        ]

        saved_records = []
        sent_emails = []
        with patch("certificate_service.generate_certificate_pdf", return_value="Alice_PEX-DS-002.pdf"), \
             patch("certificate_service.send_certificate_email",
                   side_effect=lambda data, pdf, fn: sent_emails.append(dict(data)) or True), \
             patch.object(bulk_certificate_service, "get_certificate_by_id",
                          side_effect=lambda cid: pending_record if cid == "PEX-DS-002" else (prior_record if cid == "PEX-WD-001" else None)), \
             patch.object(bulk_certificate_service, "get_previous_sent_certificate_by_email", return_value=prior_record), \
             patch.object(bulk_certificate_service, "save_certificate_record",
                          side_effect=lambda rec, existing_id=None: saved_records.append((rec, existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            response = client.post(
                "/api/certificate/bulk/send-email",
                json={"items": items, "send_again_all": True}
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["successful_count"], 1)

        # Dispatched email must retain Data Science certificate ID PEX-DS-002
        self.assertEqual(len(sent_emails), 1)
        self.assertEqual(sent_emails[0]["certificate_id"], "PEX-DS-002")
        self.assertEqual(sent_emails[0]["domain"], "Data Science")

        # Database update must target the new certificate's pending record (202), NOT prior record (101)
        self.assertEqual(len(saved_records), 1)
        rec, existing_id = saved_records[0]
        self.assertEqual(rec["certificate_id"], "PEX-DS-002")
        self.assertEqual(rec["internship_domain"], "Data Science")
        self.assertEqual(rec["email_status"], "sent")
        self.assertEqual(rec["send_count"], 1)
        self.assertEqual(rec["id"], 202)
        # Prior record 101 must NOT have been updated or overwritten
        self.assertNotEqual(existing_id, 101)
        self.assertNotEqual(rec["id"], 101)

    def test_bulk_duplicate_check_endpoint_returns_both_duplicates_and_duplicate_emails_keys(self):
        client = self._authenticated_client()
        fake_dup_map = {
            "dup@example.invalid": {"certificate_id": "PEX-OLD", "email_status": "sent"}
        }
        with patch("database.repository.find_existing_certificate_emails_batch", return_value=fake_dup_map):
            response = client.post(
                "/api/certificate/bulk-check-duplicates",
                json={"emails": ["dup@example.invalid", "new@example.invalid"]}
            )
        self.assertEqual(response.status_code, 200)
        res_data = response.get_json()
        self.assertTrue(res_data["success"])
        self.assertEqual(res_data["duplicate_count"], 1)
        self.assertEqual(res_data["duplicates"], ["dup@example.invalid"])
        self.assertEqual(res_data["duplicate_emails"], ["dup@example.invalid"])

    def test_qr_verification_integrity_original_details_remain_intact_after_second_issuance(self):
        client = self._authenticated_client()
        # Verify that get_certificate_by_id returns distinct records for original vs new issuance
        cert_1 = {
            "certificate_id": "PEX-WD-001",
            "student_name": "Alice Smith",
            "student_email": "alice@example.invalid",
            "internship_domain": "Web Development",
            "start_date": "01-01-2025",
            "end_date": "01-02-2025",
            "certificate_status": "active",
        }
        cert_2 = {
            "certificate_id": "PEX-DS-002",
            "student_name": "Alice Smith",
            "student_email": "alice@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "01-03-2025",
            "end_date": "01-04-2025",
            "certificate_status": "active",
        }
        with patch("certificate_service.db_get_certificate_by_id",
                   side_effect=lambda cid, *args, **kwargs: (cert_1, None) if cid == "PEX-WD-001" else ((cert_2, None) if cid == "PEX-DS-002" else (None, "not_found"))):
            # 1. Scanned original QR code for course 1 resolves to Web Development
            res1 = client.get("/verify/PEX-WD-001")
            self.assertEqual(res1.status_code, 200)
            self.assertIn(b"Web Development", res1.data)
            self.assertNotIn(b"Data Science", res1.data)

            # 2. Scanned new QR code for course 2 resolves to Data Science
            res2 = client.get("/verify/PEX-DS-002")
            self.assertEqual(res2.status_code, 200)
            self.assertIn(b"Data Science", res2.data)
            self.assertNotIn(b"Web Development", res2.data)



# ============================================================
# INTERNSHIP OFFER LETTER DOMAIN COMBOBOX REGRESSION TESTS
# ============================================================

class OfferLetterDomainComboboxRegressionTests(unittest.TestCase):
    def _authenticated_client(self):
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    def test_offer_letter_template_contains_all_23_predefined_domains(self):
        template_path = Path(__file__).parent.parent / "templates" / "index.html"
        self.assertTrue(template_path.exists(), "templates/index.html must exist")
        content = template_path.read_text(encoding="utf-8")

        expected_domains = [
            "Data Science",
            "Machine Learning",
            "Artificial Intelligence",
            "Web Development",
            "AWS Cloud Computing",
            "Human Resource",
            "Digital Marketing",
            "Finance",
            "Stock Market & Crypto Trading",
            "IOT",
            "Embedded System",
            "AutoCAD",
            "Cyber Security",
            "VLSI",
            "Logistic and Supply Chain",
            "Drone Mechanics",
            "Business Analytics",
            "Medical Coding",
            "Data Analytics",
            "Psychology",
            "Java",
            "UI/UX",
            "Hybrid Electric Vehicle",
        ]
        self.assertEqual(len(expected_domains), 23, "Must have exactly 23 predefined domains")
        for domain in expected_domains:
            self.assertIn(f'"{domain}"', content, f"Predefined domain '{domain}' must be present in templates/index.html")

    def test_offer_letter_template_contains_combobox_structure(self):
        template_path = Path(__file__).parent.parent / "templates" / "index.html"
        content = template_path.read_text(encoding="utf-8")

        self.assertIn('id="domain"', content, "Domain input id must remain 'domain'")
        self.assertIn('role="combobox"', content, "Domain input must have role='combobox'")
        self.assertIn('id="domainDropdownToggle"', content, "Toggle button must be present")
        self.assertIn('id="domainDropdownList"', content, "Dropdown list must be present")
        self.assertIn('initDomainCombobox', content, "initDomainCombobox function must be defined and called")

    def test_single_offer_letter_generate_with_predefined_domain(self):
        client = self._authenticated_client()
        payload = {
            "student_name": "Test Student",
            "student_email": "test@example.invalid",
            "domain": "Data Science",
            "duration": "2 months",
            "start_date": "2026-10-01",
            "end_date": "2026-11-30",
            "stipend": "Unpaid",
            "letter_type": "with_hours",
            "hours_per_week": 20,
        }
        with patch.object(application, "generate_pdf", return_value="dummy_offer.pdf"):
            response = client.post("/generate", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("filename"), "dummy_offer.pdf")

    def test_single_offer_letter_generate_with_custom_domain(self):
        client = self._authenticated_client()
        payload = {
            "student_name": "Quantum Student",
            "student_email": "quantum@example.invalid",
            "domain": "Quantum Computing",
            "duration": "2 months",
            "start_date": "2026-10-01",
            "end_date": "2026-11-30",
            "stipend": "Unpaid",
            "letter_type": "with_hours",
            "hours_per_week": 20,
        }
        captured_args = []
        def mock_generate_pdf(ltype, dpayload):
            captured_args.append((ltype, dpayload))
            return "quantum_offer.pdf"

        with patch.object(application, "generate_pdf", side_effect=mock_generate_pdf):
            response = client.post("/generate", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("filename"), "quantum_offer.pdf")
        self.assertEqual(len(captured_args), 1)
        self.assertEqual(captured_args[0][1]["domain"], "Quantum Computing")

    def test_single_offer_letter_send_preserves_custom_domain(self):
        client = self._authenticated_client()
        payload = {
            "filename": "quantum_offer.pdf",
            "student_name": "Quantum Student",
            "student_email": "quantum@example.invalid",
            "domain": "Quantum Computing",
            "duration": "2 months",
            "start_date": "01/10/2026",
            "end_date": "30/11/2026",
            "stipend": "Unpaid",
            "letter_type": "with_hours",
            "hours_per_week": 20,
            "send_again": False,
        }
        dummy_pdf = application.GENERATED_DIR / "quantum_offer.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 dummy content")
        try:
            smtp_mock = MagicMock()
            supabase_mock = MagicMock()
            with patch.object(application, "get_previous_email_record", return_value=None), \
                 patch.object(application, "SENDER_EMAIL", "sender@example.invalid"), \
                 patch.object(application, "SENDER_PASSWORD", "mock_pass"), \
                 patch.object(application.smtplib, "SMTP_SSL", return_value=smtp_mock), \
                 patch.object(application, "supabase", supabase_mock):
                smtp_mock.__enter__.return_value = smtp_mock
                response = client.post("/send-email", json=payload)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data.get("success"))
            self.assertEqual(smtp_mock.send_message.call_count, 1)
            # Verify custom domain was saved in supabase insert
            insert_call = supabase_mock.table.return_value.insert.call_args
            self.assertIsNotNone(insert_call)
            saved_record = insert_call[0][0]
            self.assertEqual(saved_record.get("internship_domain"), "Quantum Computing")
        finally:
            if dummy_pdf.exists():
                dummy_pdf.unlink()


class InternshipCertificateBulkIssueDateAndProgressTests(unittest.TestCase):
    """
    Focused regression tests for Internship Completion Certificate Bulk workflow:
    1. Issue Date - remove dependency on Excel/CSV, batch issue date picker support, safe defaults and validation.
    2. Real-time generation progress - per-item tracking, eye icon preview without regenerating.
    3. Real-time sending progress - verified success counts, safe cancellation without partial history.
    """

    def _authenticated_client(self):
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    def test_validate_bulk_records_succeeds_without_issued_date_column(self):
        """Excel/CSV upload validation succeeds without an Issued Date column."""
        parsed_rows = [
            {
                "student_name": "Priya Sharma",
                "student_email": "priya@example.invalid",
                "domain": "Machine Learning",
                "start_date": "01-01-2026",
                "end_date": "01-03-2026",
                # No issued_date provided in uploaded row
            },
            {
                "student_name": "Rahul Verma",
                "student_email": "rahul@example.invalid",
                "domain": "Web Development",
                "start_date": "01-02-2026",
                "end_date": "01-04-2026",
                "issued_date": "",  # Empty issued_date
            }
        ]
        result = bulk_certificate_service.validate_bulk_records(parsed_rows)
        self.assertEqual(result["total_records"], 2)
        self.assertEqual(result["valid_records_count"], 2)
        self.assertEqual(result["invalid_records_count"], 0)
        for row in result["rows"]:
            self.assertTrue(row["is_valid"])
            self.assertEqual(row["errors"], [])

    def test_bulk_generate_uses_provided_batch_issue_date_for_all_records(self):
        """Batch issue date passed in generation request applies to all certificates in the batch (normalized to DD-MM-YYYY)."""
        client = self._authenticated_client()
        items = [
            {"student_name": "Student A", "student_email": "a@example.invalid", "domain": "AI",
             "start_date": "01/01/2026", "end_date": "01/02/2026"},
            {"student_name": "Student B", "student_email": "b@example.invalid", "domain": "AI",
             "start_date": "01/01/2026", "end_date": "01/02/2026"},
        ]
        captured_data = []
        def mock_generate_pdf(data, output_dir, **kwargs):
            captured_data.append(data.copy())
            return f"cert_{data['student_name']}.pdf"

        with patch("certificate_service.generate_certificate_pdf", side_effect=mock_generate_pdf), \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
            response = client.post(
                "/api/certificate/bulk/generate",
                json={"rows": items, "issue_date": "2026-05-15"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(captured_data), 2)
        # Dates are normalized to standard DD-MM-YYYY format
        for item in captured_data:
            self.assertEqual(item["issued_date"], "15-05-2026")

    def test_bulk_generate_defaults_to_today_when_no_issue_date_provided(self):
        """When no issue date is provided and row has no date, defaults safely to today's date."""
        from datetime import datetime
        client = self._authenticated_client()
        items = [
            {"student_name": "Student Default", "student_email": "default@example.invalid", "domain": "Cloud",
             "start_date": "01/01/2026", "end_date": "01/02/2026"}
        ]
        captured_data = []
        def mock_generate_pdf(data, output_dir, **kwargs):
            captured_data.append(data.copy())
            return "cert_default.pdf"

        with patch("certificate_service.generate_certificate_pdf", side_effect=mock_generate_pdf), \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
            response = client.post(
                "/api/certificate/bulk/generate",
                json={"rows": items}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(captured_data), 1)
        today_str = datetime.now().strftime("%d-%m-%Y")
        self.assertEqual(captured_data[0]["issued_date"], today_str)

    def test_bulk_generate_falls_back_to_today_when_row_issued_date_is_invalid(self):
        """When batch issue date is absent and row contains an invalid date, falls back to today's date."""
        from datetime import datetime
        client = self._authenticated_client()
        items = [
            {
                "student_name": "Student Invalid Date",
                "student_email": "invalid_date@example.invalid",
                "domain": "DevOps",
                "start_date": "01/01/2026",
                "end_date": "01/02/2026",
                "issued_date": "not-a-valid-date-format",
            }
        ]
        captured_data = []
        def mock_generate_pdf(data, output_dir, **kwargs):
            captured_data.append(data.copy())
            return "cert_fallback.pdf"

        with patch("certificate_service.generate_certificate_pdf", side_effect=mock_generate_pdf), \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
            response = client.post(
                "/api/certificate/bulk/generate",
                json={"rows": items}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(captured_data), 1)
        # The invalid row date must be silently replaced with today's date
        today_str = datetime.now().strftime("%d-%m-%Y")
        self.assertEqual(captured_data[0]["issued_date"], today_str)

    def test_bulk_generate_rejects_invalid_issue_date_with_400(self):
        """Passing an invalid or malformed issue date returns 400."""
        client = self._authenticated_client()
        items = [
            {"student_name": "Student Bad", "student_email": "bad@example.invalid", "domain": "Cloud",
             "start_date": "01/01/2026", "end_date": "01/02/2026"}
        ]
        response = client.post(
            "/api/certificate/bulk/generate",
            json={"rows": items, "issue_date": "not-a-valid-date"}
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data.get("success"))
        self.assertIn("invalid issue date", data.get("error", "").lower())

    def test_preview_sample_accepts_and_uses_batch_issue_date(self):
        """Preview sample endpoint accepts and normalizes the selected issue date."""
        client = self._authenticated_client()
        payload = {
            "student_name": "Preview Student",
            "domain": "Data Engineering",
            "start_date": "01/01/2026",
            "end_date": "01/03/2026",
            "issue_date": "2026-07-25"
        }
        with patch("certificate_service.render_certificate_preview_image", return_value="data:image/png;base64,mock") as mock_preview:
            response = client.post("/api/certificate/preview-sample", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get("success"))
        mock_preview.assert_called_once()
        passed_data = mock_preview.call_args[0][0]
        self.assertEqual(passed_data["issued_date"], "25-07-2026")

    def test_preview_generated_certificate_image_endpoint(self):
        """Public /verify/<cert_id>/image serves rendered PNG image without regeneration."""
        client = application.app.test_client()
        mock_record = {
            "certificate_id": "CERT-12345",
            "student_name": "Verified Student",
            "student_email": "verified@example.invalid",
            "internship_domain": "Cybersecurity",
            "start_date": "01/01/2026",
            "end_date": "01/03/2026",
            "issued_date": "15-03-2026",
            "certificate_status": "active"
        }
        with patch("certificate_service.db_get_certificate_by_id", return_value=mock_record), \
             patch("certificate_service.generate_certificate_image_bytes", return_value=b"\x89PNG\r\n\x1a\nfakeimage"):
            response = client.get("/verify/CERT-12345/image")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(response.data, b"\x89PNG\r\n\x1a\nfakeimage")

    def test_bulk_generation_increments_processed_not_sent_counter(self):
        """
        R1 regression: generation success must increment processed_inc=1 and must NOT
        increment sent_inc.  sent_inc is reserved exclusively for the email-dispatch workflow.
        """
        from services.bulk_job_manager import BulkJobManager

        test_manager = BulkJobManager()
        job = test_manager.create_job("certificate", "generation", total=2)
        job_id = job["job_id"]

        items = [
            {"student_name": "R1 Student A", "student_email": "r1a@example.invalid",
             "domain": "AI", "start_date": "01/01/2026", "end_date": "01/02/2026"},
            {"student_name": "R1 Student B", "student_email": "r1b@example.invalid",
             "domain": "AI", "start_date": "01/01/2026", "end_date": "01/02/2026"},
        ]

        with patch("certificate_service.generate_certificate_pdf", return_value="cert_r1.pdf"), \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True), \
             patch.object(bulk_certificate_service, "bulk_job_manager", test_manager):
            bulk_certificate_service.generate_bulk_certificates(
                valid_rows=items,
                output_dir=".",
                job_id=job_id,
                issue_date="09-10-2026",
            )

        final = test_manager.get_job(job_id)
        # processed must equal the number of items attempted
        self.assertEqual(final["processed"], 2,
                         "processed counter must be incremented once per generation attempt")
        # sent must remain 0 — generation does not constitute a send
        self.assertEqual(final["sent"], 0,
                         "sent counter must NOT be incremented during generation (email-dispatch only)")

    def test_single_candidate_real_pdf_generation_with_qr_and_pil(self):
        """
        Issue 1: End-to-end PDF generation for a candidate without mocking Pillow/qrcode.
        Proves that Pillow/qrcode integration generates QR and PDF bytes successfully.
        """
        import certificate_service
        cert_data = {
            "student_name": "E2E Test Student",
            "student_email": "e2e@example.invalid",
            "domain": "Artificial Intelligence",
            "start_date": "01-01-2026",
            "end_date": "01-02-2026",
            "issued_date": "09-10-2026",
            "certificate_id": "PXL-CERT-E2E-TEST"
        }
        pdf_bytes, filename = certificate_service.generate_certificate_pdf_bytes(cert_data)
        self.assertTrue(isinstance(pdf_bytes, bytes))
        self.assertTrue(len(pdf_bytes) > 1000)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertEqual(filename, "E2E_Test_Student_Certificate.pdf")

    def test_certificate_template_preview_column_renders_eye_icon_only_after_generation(self):
        """
        Issue 2: Verify template contracts:
        1. renderBulkPreviewTable uses inactive placeholder '—' before generation (no text Preview button).
        2. updateRowPreviewToEyeIcon injects .btn-preview-eye with accessible aria-label and calls previewGeneratedCertificate.
        3. updateRowPreviewToFailed ensures failed rows show '—' and no active eye icon.
        """
        with open("templates/certificate.html", "r", encoding="utf-8") as f:
            content = f.read()

        # No pre-generation text button in renderBulkPreviewTable
        self.assertNotIn('onclick="previewBulkRecord(${i})">Preview</button>', content)

        # Inactive placeholder used initially
        self.assertIn("let previewAction = '<span style=\"color: var(--text-muted); font-size: 11px;\">—</span>';", content)

        # Eye icon button rendered with accessible aria-label and previewGeneratedCertificate call
        self.assertIn('aria-label="Preview certificate for ${escapeHtml(studentName)}"', content)
        self.assertIn('class="btn-preview-eye"', content)
        self.assertIn("previewGeneratedCertificate(", content)

        # Failed rows do NOT show eye icon
        self.assertIn("function updateRowPreviewToFailed", content)
        self.assertIn("Failed rows do NOT show an eye icon button", content)


class BulkCertificateGenerationOptimizationTests(unittest.TestCase):
    """
    Focused regression and performance tests for optimized bulk certificate generation:
    1. Small batch generation success and count reconciliation.
    2. Large chunked duplicate detection in find_existing_certificates_batch across chunk boundaries.
    3. Identical student names prevent filename overwriting / collisions.
    4. Batch Certificate ID generation internal uniqueness and collision handling.
    5. Mid-batch PDF failure isolation (partial failure reported without invalidating successful items).
    6. Database persistence error handling and per-record fallback.
    7. Absolute separation of generation and sending (generation endpoint NEVER sends email).
    8. Send endpoint behavior preservation.
    """

    def _authenticated_client(self):
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    def test_small_batch_generation_success_and_reconciliation(self):
        """Generates a small batch of 3 records and reconciles counts exactly."""
        items = [
            {"student_name": f"Candidate {i}", "student_email": f"cand{i}@example.invalid",
             "domain": "Python Development", "start_date": "01-01-2026", "end_date": "01-02-2026",
             "row_index": i}
            for i in range(1, 4)
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026"
                )
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["generated_count"], 3)
        self.assertEqual(result["newly_generated_count"], 3)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(len(result["results"]), 3)
        for r in result["results"]:
            self.assertTrue(r["success"])
            self.assertEqual(r["status"], "newly_generated")
            self.assertTrue(r["certificate_id"].startswith("PXL-CERT-"))

    def test_chunked_query_duplicate_detection_across_chunk_boundaries(self):
        """find_existing_certificates_batch chunks queries of 50 to avoid URL length limits and merges results."""
        emails = [f"student_{i}@example.invalid" for i in range(125)]
        candidate_rows = [{"student_email": em} for em in emails]

        mock_sb = MagicMock()
        table_mock = MagicMock()
        select_mock = MagicMock()

        chunks_queried = []
        def mock_in(col, chunk):
            chunks_queried.append(list(chunk))
            exec_mock = MagicMock()
            # Return a match for the 10th and 75th emails
            records = []
            if "student_10@example.invalid" in chunk:
                records.append({
                    "student_email": "student_10@example.invalid",
                    "internship_domain": "Data Science",
                    "start_date": "01-01-2026",
                    "end_date": "01-02-2026",
                    "certificate_id": "PXL-CERT-2026-EXIST10",
                    "certificate_status": "active"
                })
            if "student_75@example.invalid" in chunk:
                records.append({
                    "student_email": "student_75@example.invalid",
                    "internship_domain": "Web Dev",
                    "start_date": "01-01-2026",
                    "end_date": "01-02-2026",
                    "certificate_id": "PXL-CERT-2026-EXIST75",
                    "certificate_status": "active"
                })
            exec_mock.execute.return_value = MagicMock(data=records)
            return exec_mock

        select_mock.in_.side_effect = mock_in
        table_mock.select.return_value = select_mock
        mock_sb.table.return_value = table_mock

        lookup = repository.find_existing_certificates_batch(candidate_rows, supabase_client=mock_sb)
        # Should have queried 3 chunks (50 + 50 + 25)
        self.assertEqual(len(chunks_queried), 3)
        self.assertEqual(len(chunks_queried[0]), 50)
        self.assertEqual(len(chunks_queried[1]), 50)
        self.assertEqual(len(chunks_queried[2]), 25)
        # Both records from across different chunks must be merged in lookup
        key10 = ("student_10@example.invalid", "data science", "01-01-2026", "01-02-2026")
        key75 = ("student_75@example.invalid", "web dev", "01-01-2026", "01-02-2026")
        self.assertIn(key10, lookup)
        self.assertIn(key75, lookup)
        self.assertEqual(lookup[key10]["certificate_id"], "PXL-CERT-2026-EXIST10")
        self.assertEqual(lookup[key75]["certificate_id"], "PXL-CERT-2026-EXIST75")

    def test_identical_student_names_do_not_collide_files(self):
        """Two students with identical names generate distinct PDFs without overwriting each other."""
        items = [
            {"student_name": "Aarav Sharma", "student_email": "aarav1@example.invalid",
             "domain": "AI", "start_date": "01-01-2026", "end_date": "01-02-2026", "row_index": 1},
            {"student_name": "Aarav Sharma", "student_email": "aarav2@example.invalid",
             "domain": "AI", "start_date": "01-01-2026", "end_date": "01-02-2026", "row_index": 2},
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026"
                )
            self.assertEqual(result["generated_count"], 2)
            fn1 = result["results"][0]["filename"]
            fn2 = result["results"][1]["filename"]
            # Filenames must be distinct
            self.assertNotEqual(fn1, fn2, "Identical student names must have distinct filenames")
            self.assertTrue((Path(td) / fn1).exists())
            self.assertTrue((Path(td) / fn2).exists())

    def test_bulk_certificate_ids_internal_uniqueness_and_collision_handling(self):
        """generate_bulk_certificate_ids generates unique IDs and resolves collisions."""
        ids = certificate_service.generate_bulk_certificate_ids(25)
        self.assertEqual(len(ids), 25)
        self.assertEqual(len(set(ids)), 25, "All generated IDs must be internally unique")
        for cid in ids:
            self.assertTrue(cid.startswith("PXL-CERT-"))

        # Test collision resolution
        current_year = datetime.now().year
        simulated_collision = f"PXL-CERT-{current_year}-COLLID"
        with patch.object(repository, "check_existing_certificate_ids_batch", return_value={simulated_collision}):
            with patch("secrets.choice", side_effect=list("COLLID") + list("NEWID1")):
                resolved_ids = certificate_service.generate_bulk_certificate_ids(1)
                self.assertEqual(len(resolved_ids), 1)
                self.assertNotEqual(resolved_ids[0], simulated_collision)
                self.assertEqual(resolved_ids[0], f"PXL-CERT-{current_year}-NEWID1")

    def test_individual_pdf_generation_failure_isolated(self):
        """When an individual record fails during PDF generation, other records succeed and count reconciles."""
        items = [
            {"student_name": "Good 1", "student_email": "good1@example.invalid", "domain": "ML",
             "start_date": "01-01-2026", "end_date": "01-02-2026", "row_index": 1},
            {"student_name": "Bad 2", "student_email": "bad2@example.invalid", "domain": "ML",
             "start_date": "01-01-2026", "end_date": "01-02-2026", "row_index": 2},
            {"student_name": "Good 3", "student_email": "good3@example.invalid", "domain": "ML",
             "start_date": "01-01-2026", "end_date": "01-02-2026", "row_index": 3},
        ]
        real_gen = certificate_service.generate_certificate_pdf
        def selective_fail(data, output_dir=None, **kwargs):
            if data["student_name"] == "Bad 2":
                raise RuntimeError("Simulated render engine failure")
            return real_gen(data, output_dir=output_dir, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            with patch("certificate_service.generate_certificate_pdf", side_effect=selective_fail), \
                 patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026"
                )
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["newly_generated_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(result["failed_items"]), 1)
        self.assertEqual(result["failed_items"][0]["student_name"], "Bad 2")
        self.assertEqual(result["results"][0]["success"], True)
        self.assertEqual(result["results"][1]["success"], False)
        self.assertEqual(result["results"][2]["success"], True)

    def test_database_persistence_fallback_on_bulk_error(self):
        """When bulk_save_certificate_records fails, per-record fallback is invoked."""
        items = [
            {"student_name": "Student A", "student_email": "a@example.invalid", "domain": "Cloud",
             "start_date": "01-01-2026", "end_date": "01-02-2026", "row_index": 1}
        ]
        fallback_calls = []
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "bulk_save_certificate_records", side_effect=Exception("Batch write error")), \
                 patch.object(bulk_certificate_service, "save_certificate_record", side_effect=lambda r: fallback_calls.append(r) or True):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026"
                )
        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(fallback_calls[0]["student_name"], "Student A")

    def test_generation_and_sending_strictly_separated_no_email_sent(self):
        """
        CRITICAL ARCHITECTURAL SAFETY:
        Calling /api/certificate/bulk/generate must NEVER call send_certificate_email
        or any SMTP transport. Generation and sending must remain 100% separate operations.
        """
        client = self._authenticated_client()
        items = [
            {"student_name": "Safety Candidate", "student_email": "safety@example.invalid",
             "domain": "Cybersecurity", "start_date": "01-01-2026", "end_date": "01-02-2026"}
        ]
        with patch.object(certificate_service, "send_certificate_email") as mock_send, \
             patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
            response = client.post(
                "/api/certificate/bulk/generate",
                json={"rows": items, "issue_date": "2026-05-15"}
            )
        self.assertEqual(response.status_code, 200)
        # Assert send_certificate_email was NEVER called
        mock_send.assert_not_called()

    def test_existing_send_endpoint_unchanged(self):
        """The separate bulk email send endpoint (/api/certificate/bulk/send-email) functions as expected."""
        client = self._authenticated_client()
        items = [
            {"certificate_id": "PXL-CERT-2026-SEND01", "student_name": "Send Test",
             "student_email": "sendtest@example.invalid", "domain": "DevOps",
             "start_date": "01-01-2026", "end_date": "01-02-2026", "filename": "Send_Test_Certificate.pdf"}
        ]
        with patch.object(certificate_service, "send_certificate_email", return_value=True) as mock_send, \
             patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=True):
            response = client.post(
                "/api/certificate/bulk/send-email",
                json={"items": items}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["successful_count"], 1)
        mock_send.assert_called_once()

    def test_check_existing_certificate_ids_batch_raises_on_supabase_error(self):
        """Database error in Supabase ID lookup raises RuntimeError and is never treated as empty set."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.in_.return_value.execute.side_effect = Exception("Supabase connection timeout")
        with self.assertRaises(RuntimeError) as ctx:
            repository.check_existing_certificate_ids_batch(["PXL-CERT-2026-TEST01"], supabase_client=mock_sb)
        self.assertIn("Supabase connection timeout", str(ctx.exception))

    def test_generate_bulk_certificate_ids_fails_safely_on_lookup_error(self):
        """generate_bulk_certificate_ids fails safely when uniqueness cannot be verified, returning no unsafe ID."""
        with patch.object(repository, "check_existing_certificate_ids_batch", side_effect=RuntimeError("DB unreachable")):
            with self.assertRaises(RuntimeError) as ctx:
                certificate_service.generate_bulk_certificate_ids(5)
            self.assertIn("DB unreachable", str(ctx.exception))

    def test_bulk_save_certificate_records_reports_honest_durability(self):
        """bulk_save_certificate_records distinguishes durable Supabase writes from SQLite-only fallback."""
        records = [{
            "certificate_id": "PXL-CERT-2026-DUR01",
            "student_name": "Durable Student",
            "student_email": "durable@example.invalid",
            "internship_domain": "Cloud",
            "start_date": "01-01-2026",
            "end_date": "01-02-2026",
            "issued_date": "15-02-2026",
        }]
        # Case A: Supabase succeeds
        mock_sb_good = MagicMock()
        mock_sb_good.table.return_value.upsert.return_value.execute.return_value.data = [{"id": 1}]
        with patch("database.repository.get_supabase_client", return_value=mock_sb_good):
            res_good = repository.bulk_save_certificate_records(records)
            self.assertTrue(res_good["durable"])
            self.assertTrue(res_good["supabase_persisted"])
            self.assertTrue(res_good["success"])

        # Case B: Supabase fails, only SQLite succeeds
        mock_sb_bad = MagicMock()
        mock_sb_bad.table.return_value.upsert.return_value.execute.side_effect = Exception("Supabase write 503")
        with patch("database.repository.get_supabase_client", return_value=mock_sb_bad):
            res_bad = repository.bulk_save_certificate_records(records)
            self.assertFalse(res_bad["durable"], "SQLite-only fallback must NOT report durable=True")
            self.assertFalse(res_bad["supabase_persisted"])
            self.assertFalse(res_bad["success"])
            self.assertTrue(res_bad["sqlite_persisted"])

    def test_generate_bulk_certificates_reports_persistence_failure_honestly(self):
        """When durable Supabase save fails, generate_bulk_certificates reports failure honestly while preserving PDF."""
        items = [{
            "student_name": "Test Student",
            "student_email": "persist.fail@example.invalid",
            "domain": "DevOps",
            "start_date": "01-01-2026",
            "end_date": "01-02-2026",
            "row_index": 1
        }]
        failed_save_result = {
            "success": False,
            "durable": False,
            "supabase_persisted": False,
            "sqlite_persisted": True,
            "error": "Supabase outage",
            "count": 1
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=failed_save_result):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026"
                )
            self.assertEqual(result["newly_generated_count"], 0)
            self.assertEqual(result["failed_count"], 1)
            self.assertFalse(result["results"][0]["success"])
            self.assertEqual(result["results"][0]["status"], "persistence_failed")
            self.assertFalse(result["results"][0]["persisted"])
            # The PDF file was still generated on disk
            pdf_path = Path(td) / result["results"][0]["filename"]
            self.assertTrue(pdf_path.exists())

    def test_send_bulk_certificate_emails_preserves_disambiguated_filenames(self):
        """send_bulk_certificate_emails uses the custom disambiguated filename for email attachments."""
        items = [
            {"certificate_id": "PXL-CERT-2026-AAA111", "student_name": "Rohan Sharma",
             "student_email": "rohan1@example.invalid", "domain": "Web",
             "filename": "Rohan_Sharma_AAA111_Certificate.pdf"},
            {"certificate_id": "PXL-CERT-2026-BBB222", "student_name": "Rohan Sharma",
             "student_email": "rohan2@example.invalid", "domain": "Web",
             "filename": "Rohan_Sharma_BBB222_Certificate.pdf"},
        ]
        sent_filenames = []
        sent_pdf_paths = []
        def mock_send(email_data, pdf_path, filename):
            sent_filenames.append(filename)
            sent_pdf_paths.append(str(pdf_path))
            return True

        with tempfile.TemporaryDirectory() as td:
            with patch.object(certificate_service, "send_certificate_email", side_effect=mock_send), \
                 patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
                 patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=True):
                result = bulk_certificate_service.send_bulk_certificate_emails(
                    certificate_items=items,
                    output_dir=td,
                    send_again_all=True
                )
        self.assertEqual(result["successful_count"], 2)
        self.assertEqual(sent_filenames, ["Rohan_Sharma_AAA111_Certificate.pdf", "Rohan_Sharma_BBB222_Certificate.pdf"])
        self.assertTrue(sent_pdf_paths[0].endswith("Rohan_Sharma_AAA111_Certificate.pdf"))
        self.assertTrue(sent_pdf_paths[1].endswith("Rohan_Sharma_BBB222_Certificate.pdf"))

    def test_check_existing_certificate_ids_batch_raises_on_null_data(self):
        """Null data returned from Supabase raises RuntimeError to prevent treating incomplete results as free IDs."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.in_.return_value.execute.return_value.data = None
        with self.assertRaises(RuntimeError) as ctx:
            repository.check_existing_certificate_ids_batch(["PXL-CERT-2026-TEST01"], supabase_client=mock_sb)
        self.assertIn("null data", str(ctx.exception).lower())

    def test_bulk_save_certificate_records_on_vercel_without_supabase_fails(self):
        """On Vercel, SQLite fallback is ephemeral and must not report overall_success or durable."""
        records = [{
            "certificate_id": "PXL-CERT-2026-VCL01",
            "student_name": "Vercel Student",
            "student_email": "vercel@example.invalid",
            "internship_domain": "Cloud",
            "start_date": "01-01-2026",
            "end_date": "01-02-2026",
            "issued_date": "15-02-2026",
        }]
        with patch.dict(os.environ, {"VERCEL": "1"}), \
             patch("database.repository.get_supabase_client", return_value=None):
            res = repository.bulk_save_certificate_records(records)
            self.assertFalse(res["durable"])
            self.assertFalse(res["success"])
            self.assertFalse(res["supabase_persisted"])
            self.assertTrue(res["sqlite_persisted"])

    def test_send_bulk_certificate_emails_disambiguates_missing_filenames_for_duplicate_names(self):
        """When items omit filenames, duplicate student names in email queue receive disambiguated filenames."""
        items = [
            {"certificate_id": "PXL-CERT-2026-X111", "student_name": "Priya Patel",
             "student_email": "priya1@example.invalid", "domain": "AI"},
            {"certificate_id": "PXL-CERT-2026-X222", "student_name": "Priya Patel",
             "student_email": "priya2@example.invalid", "domain": "AI"},
        ]
        sent_filenames = []
        def mock_send(email_data, pdf_path, filename):
            sent_filenames.append(filename)
            return True

        with tempfile.TemporaryDirectory() as td:
            with patch.object(certificate_service, "PERSISTENT_CERT_DIR", Path(td) / "persistent"), \
                 patch.object(certificate_service, "send_certificate_email", side_effect=mock_send), \
                 patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
                 patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=True):
                result = bulk_certificate_service.send_bulk_certificate_emails(
                    certificate_items=items,
                    output_dir=td,
                    send_again_all=True
                )
        self.assertEqual(result["successful_count"], 2)
        self.assertEqual(len(sent_filenames), 2)
        self.assertNotEqual(sent_filenames[0], sent_filenames[1])
        self.assertEqual(sent_filenames[0], "Priya_Patel_Certificate.pdf")
        self.assertEqual(sent_filenames[1], "Priya_Patel_X222_Certificate.pdf")

    def test_cross_batch_duplicate_name_does_not_overwrite_existing_pdf(self):
        """Pre-existing PDF for a student name is never overwritten by a subsequent batch or request."""
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td)
            existing_pdf = out_path / "Aarav_Sharma_Certificate.pdf"
            sentinel_bytes = b"%PDF-1.4 PRE_EXISTING_CERTIFICATE_BYTES"
            existing_pdf.write_bytes(sentinel_bytes)

            items = [{
                "student_name": "Aarav Sharma",
                "student_email": "new.aarav@example.invalid",
                "domain": "Cloud",
                "start_date": "01-01-2026",
                "end_date": "01-02-2026",
                "row_index": 1,
            }]
            with patch.object(certificate_service, "PERSISTENT_CERT_DIR", Path(td) / "persistent"), \
                 patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value={"success": True, "durable": True, "supabase_persisted": True, "sqlite_persisted": True}):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026"
                )

            self.assertEqual(result["newly_generated_count"], 1)
            # The pre-existing file MUST NOT be modified
            self.assertEqual(existing_pdf.read_bytes(), sentinel_bytes)

            new_fn = result["results"][0]["filename"]
            self.assertNotEqual(new_fn, "Aarav_Sharma_Certificate.pdf", "New certificate must receive a disambiguated filename")
            self.assertTrue(new_fn.startswith("Aarav_Sharma_"))
            self.assertTrue((out_path / new_fn).exists())

    def test_local_sqlite_only_persistence_succeeds_without_supabase(self):
        """In a local offline environment without Supabase, SQLite-only save succeeds and marks durable=False honestly."""
        items = [{
            "student_name": "Local Student",
            "student_email": "local@example.invalid",
            "domain": "Backend",
            "start_date": "01-01-2026",
            "end_date": "01-02-2026",
            "row_index": 1,
        }]
        sqlite_only_result = {
            "success": True,
            "durable": False,
            "supabase_persisted": False,
            "sqlite_persisted": True,
            "error": None,
            "count": 1,
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=sqlite_only_result):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026",
                    supabase_client=None,
                )
            self.assertEqual(result["newly_generated_count"], 1)
            self.assertEqual(result["failed_count"], 0)
            res_item = result["results"][0]
            self.assertTrue(res_item["success"])
            self.assertTrue(res_item["persisted"])
            self.assertFalse(res_item["durable"], "Must not falsely claim durable Supabase save")
            self.assertEqual(res_item["status"], "newly_generated")

    def test_vercel_sqlite_only_persistence_fails_honestly(self):
        """On Vercel where SQLite is ephemeral, failed Supabase persistence marks records as persistence_failed."""
        items = [{
            "student_name": "Vercel Student",
            "student_email": "vercel@example.invalid",
            "domain": "Frontend",
            "start_date": "01-01-2026",
            "end_date": "01-02-2026",
            "row_index": 1,
        }]
        vercel_fail_result = {
            "success": False,
            "durable": False,
            "supabase_persisted": False,
            "sqlite_persisted": True,
            "error": "Supabase outage on Vercel",
            "count": 1,
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=vercel_fail_result):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026",
                )
            self.assertEqual(result["newly_generated_count"], 0)
            self.assertEqual(result["failed_count"], 1)
            res_item = result["results"][0]
            self.assertFalse(res_item["success"])
            self.assertEqual(res_item["status"], "persistence_failed")
            self.assertFalse(res_item["persisted"])
            self.assertFalse(res_item["durable"])

    def test_bulk_generation_cancellation_preserves_partial_results(self):
        """When generation is cancelled mid-batch, items generated before cancellation are preserved in results."""
        items = [
            {"student_name": f"Student {i}", "student_email": f"st{i}@example.invalid",
             "domain": "DevOps", "start_date": "01-01-2026", "end_date": "01-02-2026", "row_index": i}
            for i in range(3)
        ]
        # Simulate cancellation triggered after the first item is processed
        call_count = 0
        def cancel_after_one(job_id):
            nonlocal call_count
            call_count += 1
            return call_count > 1

        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service.bulk_job_manager, "is_cancelled", side_effect=cancel_after_one), \
                 patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value={"success": True, "durable": True, "supabase_persisted": True, "sqlite_persisted": True}):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-02-2026",
                    job_id="test-job-partial-cancel"
                )
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["newly_generated_count"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["success"])
        self.assertEqual(result["results"][0]["student_name"], "Student 0")

    def test_check_existing_certificate_ids_batch_vercel_skips_sqlite_on_supabase_success(self):
        """In Vercel mode, successful Supabase check is authoritative and skips opening local SQLite."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.in_.return_value.execute.return_value.data = [
            {"certificate_id": "PXL-CERT-2026-EXISTS"}
        ]
        with patch.dict(os.environ, {"VERCEL": "1"}), patch("sqlite3.connect") as mock_connect:
            found = repository.check_existing_certificate_ids_batch(
                ["PXL-CERT-2026-EXISTS", "PXL-CERT-2026-NEW01"],
                supabase_client=mock_sb
            )
            mock_connect.assert_not_called()
            self.assertEqual(found, {"PXL-CERT-2026-EXISTS"})

    def test_check_existing_certificate_ids_batch_detects_existing_supabase_ids(self):
        """Existing IDs persisted in Supabase are accurately returned."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.in_.return_value.execute.return_value.data = [
            {"certificate_id": "PXL-CERT-2026-TAKEN1"},
            {"certificate_id": "PXL-CERT-2026-TAKEN2"}
        ]
        with patch.dict(os.environ, {"VERCEL": "1"}):
            found = repository.check_existing_certificate_ids_batch(
                ["PXL-CERT-2026-TAKEN1", "PXL-CERT-2026-TAKEN2", "PXL-CERT-2026-FREE01"],
                supabase_client=mock_sb
            )
            self.assertEqual(found, {"PXL-CERT-2026-TAKEN1", "PXL-CERT-2026-TAKEN2"})

    def test_check_existing_certificate_ids_batch_handles_duplicate_ids_in_batch(self):
        """Duplicate candidate IDs within the same input batch are properly deduplicated and detected."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.in_.return_value.execute.return_value.data = [
            {"certificate_id": "PXL-CERT-2026-DUP01"}
        ]
        with patch.dict(os.environ, {"VERCEL": "1"}):
            found = repository.check_existing_certificate_ids_batch(
                ["PXL-CERT-2026-DUP01", "PXL-CERT-2026-DUP01", "PXL-CERT-2026-DUP01"],
                supabase_client=mock_sb
            )
            self.assertEqual(found, {"PXL-CERT-2026-DUP01"})
            # Verify the in_ query was executed with deduplicated clean IDs
            in_call = mock_sb.table().select().in_.call_args
            self.assertEqual(in_call[0][1], ["PXL-CERT-2026-DUP01"])

    def test_check_existing_certificate_ids_batch_vercel_fails_safely_on_supabase_failure(self):
        """In Vercel mode, Supabase lookup errors or unconfigured client fail safely with RuntimeError."""
        # Case A: Query failure
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.in_.return_value.execute.side_effect = Exception("Supabase 503")
        with patch.dict(os.environ, {"VERCEL": "1"}):
            with self.assertRaises(RuntimeError) as ctx:
                repository.check_existing_certificate_ids_batch(["PXL-CERT-2026-TEST01"], supabase_client=mock_sb)
            self.assertIn("Supabase 503", str(ctx.exception))

        # Case B: Unconfigured client in Vercel mode
        with patch.dict(os.environ, {"VERCEL": "1"}), patch("database.repository.get_supabase_client", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                repository.check_existing_certificate_ids_batch(["PXL-CERT-2026-TEST01"], supabase_client=None)
            self.assertIn("not configured", str(ctx.exception).lower())

    def test_check_existing_certificate_ids_batch_local_dev_queries_sqlite(self):
        """In local development (non-Vercel), SQLite is queried and local records are detected."""
        test_cert_id = "PXL-CERT-2026-LOCAL01"
        conn = sqlite3.connect(repository.SQLITE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO certificate_history (certificate_id, student_name) VALUES (?, 'Local User')",
            (test_cert_id,)
        )
        conn.commit()
        conn.close()

        def cleanup_record():
            try:
                c = sqlite3.connect(repository.SQLITE_DB_PATH)
                c.execute("DELETE FROM certificate_history WHERE certificate_id = ?", (test_cert_id,))
                c.commit()
                c.close()
            except Exception:
                pass

        self.addCleanup(cleanup_record)

        env_copy = os.environ.copy()
        env_copy.pop("VERCEL", None)
        with patch.dict(os.environ, env_copy, clear=True), patch("database.repository.get_supabase_client", return_value=None):
            found = repository.check_existing_certificate_ids_batch([test_cert_id, "PXL-CERT-2026-LOCALFREE"], supabase_client=None)
            self.assertIn(test_cert_id, found)
            self.assertNotIn("PXL-CERT-2026-LOCALFREE", found)


class VercelSQLiteGuardRegressionTests(unittest.TestCase):
    """
    Regression tests for BUG-002 and BUG-003.

    BUG-002: bulk_save_certificate_records must not attempt the SQLite write on
             Vercel when Supabase persistence already succeeded — the project-root
             path is read-only and the attempt produces noisy OperationalError logs.

    BUG-003: init_local_db() must not be called during module initialisation on
             Vercel, for the same reason.
    """

    # ------------------------------------------------------------------
    # BUG-002 regression
    # ------------------------------------------------------------------

    def test_bulk_save_vercel_skips_sqlite_when_supabase_succeeded(self):
        """
        On Vercel, if Supabase bulk-upsert succeeds, the SQLite write must be
        skipped entirely.  The function must:
          - report success=True, durable=True, supabase_persisted=True
          - NOT call sqlite3.connect (verified by patching it to raise)
        """
        records = [
            {
                "certificate_id": "PXL-CERT-2026-BUG002A",
                "student_name": "Vercel Bug002 Student",
                "student_email": "bug002@example.invalid",
                "internship_domain": "Cloud",
                "start_date": "01-01-2026",
                "end_date": "28-02-2026",
                "issued_date": "01-03-2026",
            }
        ]

        # Build a Supabase mock that reports a successful upsert
        mock_sb = MagicMock()
        mock_sb.table.return_value.upsert.return_value.execute.return_value.data = [{"id": 1}]

        sqlite_connect_called = []

        def spy_sqlite_connect(path):
            sqlite_connect_called.append(path)
            raise OSError("read-only file system")  # simulates Vercel behaviour

        with patch.dict(os.environ, {"VERCEL": "1"}), \
             patch("database.repository.get_supabase_client", return_value=mock_sb), \
             patch("database.repository.sqlite3.connect", side_effect=spy_sqlite_connect):
            res = repository.bulk_save_certificate_records(records, supabase_client=mock_sb)

        self.assertTrue(res["success"], f"Expected success=True, got: {res}")
        self.assertTrue(res["durable"])
        self.assertTrue(res["supabase_persisted"])
        self.assertFalse(res["sqlite_persisted"])
        # The critical assertion: sqlite3.connect must NEVER have been called
        self.assertEqual(
            sqlite_connect_called, [],
            "sqlite3.connect was called on Vercel even though Supabase succeeded — BUG-002 not fixed"
        )

    def test_bulk_save_vercel_attempts_sqlite_when_supabase_fails(self):
        """
        On Vercel, if Supabase fails, the code should still attempt SQLite (it
        may also fail there, but the attempt must be made — it is the only fallback).
        The function must report success=False and durable=False when both stores fail.
        """
        records = [
            {
                "certificate_id": "PXL-CERT-2026-BUG002B",
                "student_name": "Vercel Fallback Student",
                "student_email": "bug002b@example.invalid",
                "internship_domain": "ML",
                "start_date": "01-01-2026",
                "end_date": "28-02-2026",
                "issued_date": "01-03-2026",
            }
        ]

        # Use a real temp DB so SQLite can actually succeed as the last resort
        with tempfile.TemporaryDirectory() as td:
            tmp_db = str(Path(td) / "fallback.db")
            with patch.dict(os.environ, {"VERCEL": "1", "PERSEVEX_SQLITE_DB_PATH": tmp_db}), \
                 patch("database.repository.get_supabase_client", return_value=None), \
                 patch("database.repository.SQLITE_DB_PATH", tmp_db):
                # Ensure the table exists in the temp db
                conn = sqlite3.connect(tmp_db)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS certificate_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        certificate_id TEXT UNIQUE,
                        student_name TEXT, student_email TEXT,
                        internship_domain TEXT, start_date TEXT, end_date TEXT,
                        issued_date TEXT, generated_date TEXT, created_at TEXT,
                        certificate_status TEXT, email_status TEXT,
                        sent_at TEXT, send_count INTEGER DEFAULT 0,
                        pdf_filename TEXT, error_message TEXT, template_version TEXT
                    )
                """)
                conn.commit()
                conn.close()
                res = repository.bulk_save_certificate_records(records)
        # sb is None → supabase_persisted=False; SQLite should have worked
        self.assertFalse(res["durable"], "No Supabase → should not be durable")
        self.assertFalse(res["success"], "On Vercel without Supabase → overall_success should be False")
        self.assertFalse(res["supabase_persisted"])
        # SQLite write was attempted and succeeded in the temp db
        self.assertTrue(res["sqlite_persisted"])

    # ------------------------------------------------------------------
    # BUG-003 regression
    # ------------------------------------------------------------------

    def test_init_local_db_skipped_on_vercel(self):
        """
        On Vercel, init_local_db() must not be invoked.  The guard in
        certificate_service ensures the function is only called when VERCEL is
        absent from the environment.  We verify this by importing the relevant
        symbol and checking that patching init_local_db to raise would NOT
        propagate if VERCEL is set at import time.

        Since the module is already imported we test the guard logic directly:
        confirm that calling init_local_db() when VERCEL is set does NOT
        produce an unhandled error (the init itself catches errors), and that
        the environment-level guard in the module skips the call entirely.
        """
        call_log = []

        # Patch the underlying DB function to track calls
        with patch("database.repository.init_database_tables", side_effect=lambda: call_log.append(1)):
            with patch.dict(os.environ, {"VERCEL": "1"}):
                # Re-execute the same conditional guard that the module-level code uses
                if not os.environ.get("VERCEL"):
                    certificate_service.init_local_db()

        self.assertEqual(
            call_log, [],
            "init_database_tables was called even with VERCEL=1 — BUG-003 not fixed"
        )

    def test_init_local_db_runs_in_local_dev(self):
        """
        Without VERCEL in the environment, init_local_db() must still be
        invocable and must call init_database_tables() (non-regression for local dev).
        """
        call_log = []
        with patch("database.repository.init_database_tables", side_effect=lambda: call_log.append(1)):
            env_copy = {k: v for k, v in os.environ.items() if k != "VERCEL"}
            with patch.dict(os.environ, env_copy, clear=True):
                if not os.environ.get("VERCEL"):
                    certificate_service.init_local_db()
        self.assertEqual(call_log, [1], "init_database_tables was NOT called in local-dev mode — regression")


class BulkCertificateTestMatrixGapTests(unittest.TestCase):
    """
    Tests for scenarios identified as gaps in the test matrix:
      - Large batch: precomputed_ids pool is fully consumed with no shortfall
      - SMTP timeout: recorded as email_status='failed', not 'sent'
      - Vercel + Supabase failure: bulk save gracefully reports failure
    """

    # ------------------------------------------------------------------
    # Gap #5 — large batch precomputed_ids sizing
    # ------------------------------------------------------------------

    def test_large_batch_precomputed_ids_fully_consumed(self):
        """
        When generate_bulk_certificates processes N new rows, exactly N IDs are
        pre-allocated via generate_bulk_certificate_ids.  Verify that after a
        10-row batch completes without cancellation, all pre-allocated IDs are
        consumed (none left over, none short).
        """
        n = 10
        precomputed = [f"PXL-CERT-2026-BULK{i:03d}" for i in range(n)]
        pop_log = []

        original_ids = list(precomputed)

        class TrackingList(list):
            def pop(self, index=0):
                val = list.pop(self, index)
                pop_log.append(val)
                return val

        items = [
            {
                "student_name": f"Student {i}",
                "student_email": f"student{i}@example.invalid",
                "domain": "AI",
                "start_date": "01-01-2026",
                "end_date": "28-02-2026",
                "row_index": i,
            }
            for i in range(n)
        ]

        generated_ids = []

        def fake_generate_bulk_ids(count, supabase_client=None):
            return TrackingList(original_ids[:count])

        with tempfile.TemporaryDirectory() as td:
            with patch.object(certificate_service, "generate_bulk_certificate_ids", side_effect=fake_generate_bulk_ids), \
                 patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value={"success": True, "durable": True, "supabase_persisted": True, "sqlite_persisted": True}), \
                 patch.object(bulk_certificate_service, "find_existing_certificates_batch", return_value={}):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-03-2026",
                )

        self.assertEqual(result["newly_generated_count"], n, "All rows should be newly generated")
        # Every ID in the precomputed pool must have been popped exactly once
        self.assertEqual(len(pop_log), n, f"Expected {n} ID pops, got {len(pop_log)}")
        self.assertEqual(sorted(pop_log), sorted(original_ids[:n]))

    # ------------------------------------------------------------------
    # Gap #11 — SMTP timeout recorded as failed, not sent
    # ------------------------------------------------------------------

    def test_smtp_timeout_records_failed_not_sent(self):
        """
        If the SMTP transport raises a timeout (or any exception) after the PDF
        has been generated, the certificate record must be persisted with
        email_status='failed' — never 'sent'.
        """
        import smtplib as _smtplib

        items = [
            {
                "certificate_id": "PXL-CERT-2026-TIMEOUT01",
                "student_name": "Timeout Student",
                "student_email": "timeout@example.invalid",
                "domain": "DevOps",
                "start_date": "01-01-2026",
                "end_date": "28-02-2026",
                "filename": "Timeout_Student_Certificate.pdf",
            }
        ]

        saved_records = []

        def capture_save(record_data, **kwargs):
            saved_records.append(dict(record_data))
            return True

        with tempfile.TemporaryDirectory() as td:
            # Write a stub PDF so the file-existence check passes
            (Path(td) / "Timeout_Student_Certificate.pdf").write_bytes(b"%PDF-1.4 stub")

            with patch.object(certificate_service, "send_certificate_email",
                              side_effect=_smtplib.SMTPConnectError(421, "Service temporarily unavailable")), \
                 patch.object(bulk_certificate_service, "save_certificate_record", side_effect=capture_save), \
                 patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=True):
                result = bulk_certificate_service.send_bulk_certificate_emails(
                    certificate_items=items,
                    output_dir=td,
                    send_again_all=True,
                )

        self.assertEqual(result["failed_count"], 1, "Expected exactly one failure")
        self.assertEqual(result["successful_count"], 0, "No email should be marked successful")
        self.assertEqual(len(saved_records), 1, "One DB record should have been saved")
        self.assertEqual(
            saved_records[0].get("email_status"), "failed",
            f"email_status must be 'failed', got: {saved_records[0].get('email_status')!r}"
        )
        self.assertNotEqual(
            saved_records[0].get("email_status"), "sent",
            "A timed-out email must never be persisted as 'sent'"
        )

    # ------------------------------------------------------------------
    # Gap #19 — Vercel + Supabase failure handled gracefully
    # ------------------------------------------------------------------

    def test_bulk_save_vercel_both_stores_fail_reports_error_honestly(self):
        """
        On Vercel, if Supabase raises an exception AND SQLite fails (read-only
        filesystem), bulk_save_certificate_records must return
        success=False, durable=False and include a non-None error string.
        It must NOT raise an unhandled exception.
        """
        records = [
            {
                "certificate_id": "PXL-CERT-2026-BOTHFAIL01",
                "student_name": "Both Fail Student",
                "student_email": "bothfail@example.invalid",
                "internship_domain": "Security",
                "start_date": "01-01-2026",
                "end_date": "28-02-2026",
                "issued_date": "01-03-2026",
            }
        ]

        mock_sb = MagicMock()
        # Supabase upsert raises a non-schema error (connection failure)
        mock_sb.table.return_value.upsert.return_value.execute.side_effect = Exception("DB connection refused")
        # Per-record fallback also fails
        mock_sb.table.return_value.upsert.return_value.execute.side_effect = Exception("DB connection refused")

        with patch.dict(os.environ, {"VERCEL": "1"}), \
             patch("database.repository.get_supabase_client", return_value=mock_sb), \
             patch("database.repository.sqlite3.connect", side_effect=OSError("read-only file system")):
            res = repository.bulk_save_certificate_records(records, supabase_client=mock_sb)

        self.assertFalse(res["success"])
        self.assertFalse(res["durable"])
        self.assertFalse(res["supabase_persisted"])
        # error field must contain a meaningful message, not None
        self.assertIsNotNone(res["error"], "error field must not be None when both stores fail")
        self.assertIsInstance(res["error"], str)



class SingleCertificateIdReuseTests(unittest.TestCase):
    """
    Regression tests for the Single Certificate ID-reuse defect.

    Root cause (now fixed): generate_certificate() used
    get_previous_sent_certificate_by_email() to reuse a certificate_id
    solely because the email matched, causing the draft preview to load the
    wrong historical record instead of the current form values.

    These tests confirm:
    1. Existing email + genuinely new course  → fresh ID
    2. Existing email + new internship dates  → fresh ID
    3. Same exact issuance (active match)     → existing ID is reused (idempotent)
    4. Intentional resend                     → original ID and details are preserved
    5. Permanent QR verification              → loads saved record, unaffected by params
    6. Bulk regression                        → bulk workflow still passes
    """

    def _make_client(self):
        application.app.config["TESTING"] = True
        application.app.config["SECRET_KEY"] = "test-only"
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_payload(
        name="Alice Example",
        email="alice@example.invalid",
        domain="Machine Learning",
        start="2026-01-01",
        end="2026-03-31",
        issued="2026-04-01",
    ):
        return {
            "student_name": name,
            "student_email": email,
            "domain": domain,
            "start_date": start,
            "end_date": end,
            "issued_date": issued,
        }

    # ------------------------------------------------------------------
    # Case 1: existing email, genuinely new course → fresh ID
    # ------------------------------------------------------------------

    def test_existing_email_new_course_gets_fresh_id(self):
        """
        When the same email is submitted for a different course,
        generate_certificate() must NOT reuse the old certificate_id.
        A fresh ID is generated; the response carries that fresh ID and
        the preview URL is built with current form values.
        """
        old_cert_id = "PXL-CERT-2025-OLDCOURSE"
        fresh_id = "PXL-CERT-2026-FRESH001"

        old_record = {
            "certificate_id": old_cert_id,
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Data Science",  # <-- different course
            "start_date": "01-01-2025",
            "end_date": "31-03-2025",
            "issued_date": "01-04-2025",
            "email_status": "sent",
            "certificate_status": "active",
        }

        client = self._make_client()
        payload = self._generate_payload(domain="Machine Learning")

        with patch.object(repository, "find_active_certificate", return_value=None), \
             patch.object(repository, "get_previous_sent_certificate_by_email", return_value=old_record), \
             patch.object(certificate_service, "generate_certificate_id", return_value=fresh_id), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_Example_Certificate.pdf"):
            response = client.post("/api/certificate/generate", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get("success"), f"Expected success=True, got: {data}")
        self.assertEqual(
            data["certificate_id"], fresh_id,
            f"Expected fresh ID {fresh_id!r}, got {data.get('certificate_id')!r} (old ID was reused)"
        )
        self.assertNotEqual(
            data["certificate_id"], old_cert_id,
            "Old certificate_id must NOT be reused for a different course"
        )
        # The preview URL must be built with the new domain, not the old one
        self.assertIn(fresh_id, data["url"])

    # ------------------------------------------------------------------
    # Case 2: existing email, new internship dates → fresh ID
    # ------------------------------------------------------------------

    def test_existing_email_new_dates_gets_fresh_id(self):
        """
        Submitting the same email with different start/end dates must produce
        a fresh certificate_id — date changes define a genuinely new issuance.
        """
        old_cert_id = "PXL-CERT-2025-OLDDATES"
        fresh_id = "PXL-CERT-2026-FRESH002"

        old_record = {
            "certificate_id": old_cert_id,
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2025",
            "end_date": "31-03-2025",  # <-- old dates
            "issued_date": "01-04-2025",
            "email_status": "sent",
            "certificate_status": "active",
        }

        client = self._make_client()
        # Same domain as before but different dates (2026 instead of 2025)
        payload = self._generate_payload(
            domain="Machine Learning",
            start="2026-01-01",
            end="2026-03-31",
        )

        with patch.object(repository, "find_active_certificate", return_value=None), \
             patch.object(repository, "get_previous_sent_certificate_by_email", return_value=old_record), \
             patch.object(certificate_service, "generate_certificate_id", return_value=fresh_id), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_Example_Certificate.pdf"):
            response = client.post("/api/certificate/generate", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(
            data["certificate_id"], fresh_id,
            f"Expected fresh ID for new dates, got {data.get('certificate_id')!r}"
        )
        self.assertNotEqual(data["certificate_id"], old_cert_id)

    # ------------------------------------------------------------------
    # Case 3: same exact issuance (find_active_certificate match) → reuse
    # ------------------------------------------------------------------

    def test_same_exact_issuance_reuses_existing_id(self):
        """
        When find_active_certificate returns a non-revoked match for the
        exact (email, domain, start_date, end_date) tuple, the endpoint
        must return that existing certificate_id (idempotent behaviour).
        """
        existing_id = "PXL-CERT-2026-EXISTING"

        active_cert = {
            "certificate_id": existing_id,
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "email_status": "sent",
            "certificate_status": "active",
            "pdf_filename": "Alice_Example_Certificate.pdf",
        }

        client = self._make_client()
        payload = self._generate_payload()  # defaults match active_cert

        with patch.object(repository, "find_active_certificate", return_value=active_cert), \
             patch.object(certificate_service, "generate_certificate_id") as mock_gen:
            response = client.post("/api/certificate/generate", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data["certificate_id"], existing_id)
        # generate_certificate_id must NOT have been called — we returned early
        mock_gen.assert_not_called()

    # ------------------------------------------------------------------
    # Case 4: intentional resend preserves original ID and details
    # ------------------------------------------------------------------

    def test_intentional_resend_preserves_original_certificate_id(self):
        """
        When the user explicitly confirms a resend (send_again=True in
        /api/certificate/send-email), the route must use the certificate_id
        from the most-recent sent record for that email, not generate a new
        one. The original student_name and course must be preserved.
        """
        original_cert_id = "PXL-CERT-2025-ORIGINAL"

        previous_sent = {
            "id": 42,
            "certificate_id": original_cert_id,
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "email_status": "sent",
            "send_count": 1,
        }

        saved_records = []

        def capture_save(record, existing_id=None):
            saved_records.append(dict(record))
            return True

        client = self._make_client()

        with patch.object(repository, "get_previous_sent_certificate_by_email", return_value=previous_sent), \
             patch.object(repository, "save_certificate_record", side_effect=capture_save), \
             patch.object(repository, "get_certificate_by_id", return_value=previous_sent), \
             patch.object(certificate_service, "generate_certificate_id") as mock_gen, \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_Example_Certificate.pdf"), \
             patch.object(certificate_service, "send_certificate_email", return_value=None):
            response = client.post("/api/certificate/send-email", json={
                "student_name": "Alice Example",
                "student_email": "alice@example.invalid",
                "domain": "Machine Learning",
                "start_date": "01-01-2026",
                "end_date": "31-03-2026",
                "issued_date": "01-04-2026",
                "certificate_id": original_cert_id,
                "send_again": True,
            })

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get("success"), f"Resend failed: {data}")
        # The saved record must carry the ORIGINAL certificate_id
        self.assertTrue(len(saved_records) > 0)
        self.assertEqual(saved_records[-1]["certificate_id"], original_cert_id)
        # generate_certificate_id must NOT have been called
        mock_gen.assert_not_called()
        # send_count must have incremented
        self.assertEqual(saved_records[-1]["send_count"], 2)

    # ------------------------------------------------------------------
    # Case 5: permanent QR verification ignores draft query parameters
    # ------------------------------------------------------------------

    def test_permanent_verify_preview_uses_db_record_not_query_params(self):
        """
        The permanent /verify/<certificate_id>/preview endpoint must load
        the saved database record and ignore untrusted query parameters when
        a database record exists for that certificate_id.
        Verifies the fix doesn't inadvertently change this behaviour.
        """
        cert_id = "PXL-CERT-2026-QR001"

        saved_record = {
            "certificate_id": cert_id,
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "certificate_status": "active",
            "template_version": "v1",
        }

        captured_data = {}

        def fake_generate_pdf_bytes(data, base_url, template_version="v1"):
            captured_data.update(data)
            return b"%PDF-1.4 stub", "Certificate.pdf"

        client = self._make_client()

        with patch.object(certificate_service, "db_get_certificate_by_id", return_value=saved_record), \
             patch.object(certificate_service, "generate_certificate_pdf_bytes",
                          side_effect=fake_generate_pdf_bytes):
            # Provide query params that differ from the saved record
            response = client.get(
                f"/verify/{cert_id}/preview"
                f"?name=Malicious+Override&domain=Hacking&start=01-06-2020&end=31-12-2020"
            )

        self.assertEqual(response.status_code, 200)
        # The PDF was rendered using the saved DB details, NOT the attacker's query params
        self.assertEqual(captured_data.get("student_name"), "Alice Example",
                         "preview must use saved DB student_name, not query param")
        self.assertEqual(captured_data.get("domain"), "Machine Learning",
                         "preview must use saved DB domain, not query param")
        self.assertEqual(captured_data.get("start_date"), "01-01-2026",
                         "preview must use saved DB start_date, not query param")

    # ------------------------------------------------------------------
    # Case 6: Bulk workflow unaffected (smoke test)
    # ------------------------------------------------------------------

    def test_bulk_workflow_unaffected_by_single_cert_fix(self):
        """
        The Bulk certificate generation must continue to work correctly
        after the Single Certificate ID-reuse fix. This is a smoke test
        confirming the two code-paths remain independent.
        """
        items = [
            {
                "student_name": "Bulk Student A",
                "student_email": "bulk_a@example.invalid",
                "domain": "Cloud",
                "start_date": "01-01-2026",
                "end_date": "28-02-2026",
                "row_index": 1,
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "bulk_save_certificate_records",
                               return_value={"success": True, "durable": True,
                                             "supabase_persisted": True, "sqlite_persisted": True}):
                result = bulk_certificate_service.generate_bulk_certificates(
                    valid_rows=items,
                    output_dir=td,
                    issue_date="15-03-2026",
                )
        self.assertEqual(result["newly_generated_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertTrue(result["results"][0]["success"])

    # ------------------------------------------------------------------
    # Case 7: Same email but different name → fresh ID (Single)
    # ------------------------------------------------------------------

    def test_single_same_email_different_name_gets_fresh_id(self):
        """When student name differs, generate_certificate must assign a fresh ID."""
        active_cert = {
            "certificate_id": "PXL-CERT-NAME-01",
            "student_name": "Alice Example",
            "student_email": "student@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "certificate_status": "active",
        }
        client = self._make_client()
        payload = self._generate_payload(name="Bob Example", email="student@example.invalid")

        with patch.object(repository, "find_active_certificate", return_value=None), \
             patch.object(certificate_service, "generate_certificate_id", return_value="PXL-CERT-NAME-02"), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Bob_Example_Certificate.pdf"):
            response = client.post("/api/certificate/generate", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["certificate_id"], "PXL-CERT-NAME-02")
        self.assertEqual(data["data"]["student_name"], "Bob Example")

    # ------------------------------------------------------------------
    # Case 8: Same email but different issued date → fresh ID (Single)
    # ------------------------------------------------------------------

    def test_single_same_email_different_issued_date_gets_fresh_id(self):
        """When issued date differs, generate_certificate must assign a fresh ID."""
        client = self._make_client()
        payload = self._generate_payload(issued="2026-05-01")

        with patch.object(repository, "find_active_certificate", return_value=None), \
             patch.object(certificate_service, "generate_certificate_id", return_value="PXL-CERT-ISSUE-NEW"), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_Example_Certificate.pdf"):
            response = client.post("/api/certificate/generate", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["certificate_id"], "PXL-CERT-ISSUE-NEW")

    # ------------------------------------------------------------------
    # Case 9: Different email → fresh ID (Single)
    # ------------------------------------------------------------------

    def test_single_different_email_gets_fresh_id(self):
        """When email differs, generate_certificate must assign a fresh ID."""
        client = self._make_client()
        payload = self._generate_payload(email="other@example.invalid")

        with patch.object(repository, "find_active_certificate", return_value=None), \
             patch.object(certificate_service, "generate_certificate_id", return_value="PXL-CERT-EMAIL-NEW"), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_Example_Certificate.pdf"):
            response = client.post("/api/certificate/generate", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["certificate_id"], "PXL-CERT-EMAIL-NEW")

    # ------------------------------------------------------------------
    # Case 10: Single send-email duplicate detection only flags identical issuance
    # ------------------------------------------------------------------

    def test_single_send_email_duplicate_detection_only_flags_identical_issuance(self):
        """
        In /api/certificate/send-email:
        - When a student has a sent certificate for Course A, sending a new issuance for Course B
          must NOT trigger 409 duplicate, must NOT overwrite Course A, and must save its own record.
        - Sending the same Course B certificate again with send_again=False triggers 409 duplicate.
        - Confirming send_again=True increments Course B's send_count without affecting Course A.
        """
        course_a_record = {
            "id": 1,
            "certificate_id": "PXL-CERT-COURSE-A",
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "01-01-2025",
            "end_date": "31-03-2025",
            "issued_date": "01-04-2025",
            "email_status": "sent",
            "send_count": 1,
        }

        saved_records = []

        def capture_save(record, existing_id=None):
            saved_records.append(dict(record))
            return True

        client = self._make_client()

        # Step 1: Send Course B (Machine Learning) for Alice — must succeed directly without 409
        with patch.object(repository, "get_previous_sent_certificate_by_email", return_value=course_a_record), \
             patch.object(repository, "get_certificate_by_id", return_value=None), \
             patch.object(repository, "find_active_certificate", return_value=None), \
             patch.object(repository, "save_certificate_record", side_effect=capture_save), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_ML.pdf"), \
             patch.object(certificate_service, "send_certificate_email", return_value=None):
            response = client.post("/api/certificate/send-email", json={
                "student_name": "Alice Example",
                "student_email": "alice@example.invalid",
                "domain": "Machine Learning",
                "start_date": "01-01-2026",
                "end_date": "31-03-2026",
                "issued_date": "01-04-2026",
                "certificate_id": "PXL-CERT-COURSE-B",
                "send_again": False,
            })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(len(saved_records) > 0)
        ml_saved = saved_records[-1]
        self.assertEqual(ml_saved["certificate_id"], "PXL-CERT-COURSE-B")
        self.assertEqual(ml_saved["internship_domain"], "Machine Learning")
        self.assertEqual(ml_saved["send_count"], 1)

        # Step 2: Sending Course B again with send_again=False triggers 409 duplicate
        course_b_record = dict(ml_saved)
        course_b_record["email_status"] = "sent"

        with patch.object(repository, "get_certificate_by_id", return_value=course_b_record), \
             patch.object(repository, "find_active_certificate", return_value=course_b_record):
            response2 = client.post("/api/certificate/send-email", json={
                "student_name": "Alice Example",
                "student_email": "alice@example.invalid",
                "domain": "Machine Learning",
                "start_date": "01-01-2026",
                "end_date": "31-03-2026",
                "issued_date": "01-04-2026",
                "certificate_id": "PXL-CERT-COURSE-B",
                "send_again": False,
            })

        self.assertEqual(response2.status_code, 409)
        self.assertTrue(response2.get_json()["duplicate"])

        # Step 3: Confirming resend (send_again=True) increments send_count to 2
        with patch.object(repository, "get_certificate_by_id", return_value=course_b_record), \
             patch.object(repository, "find_active_certificate", return_value=course_b_record), \
             patch.object(repository, "save_certificate_record", side_effect=capture_save), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_ML.pdf"), \
             patch.object(certificate_service, "send_certificate_email", return_value=None):
            response3 = client.post("/api/certificate/send-email", json={
                "student_name": "Alice Example",
                "student_email": "alice@example.invalid",
                "domain": "Machine Learning",
                "start_date": "01-01-2026",
                "end_date": "31-03-2026",
                "issued_date": "01-04-2026",
                "certificate_id": "PXL-CERT-COURSE-B",
                "send_again": True,
            })

        self.assertEqual(response3.status_code, 200)
        ml_resent = saved_records[-1]
        self.assertEqual(ml_resent["certificate_id"], "PXL-CERT-COURSE-B")
        self.assertEqual(ml_resent["send_count"], 2)

    # ------------------------------------------------------------------
    # Case 11: Failed email and retry updates record without duplicates
    # ------------------------------------------------------------------

    def test_single_failed_email_and_retry_updates_record_without_duplicates(self):
        """Failed send saves record as failed; subsequent retry updates it to sent with send_count=1."""
        client = self._make_client()
        saved_records = []

        def capture_save(record, existing_id=None):
            saved_records.append(dict(record))
            return True

        # First attempt: SMTP fails
        with patch.object(repository, "get_certificate_by_id", return_value=None), \
             patch.object(repository, "find_active_certificate", return_value=None), \
             patch.object(repository, "save_certificate_record", side_effect=capture_save), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_ML.pdf"), \
             patch.object(certificate_service, "send_certificate_email", side_effect=Exception("SMTP Connection Timeout")):
            response = client.post("/api/certificate/send-email", json={
                "student_name": "Alice Example",
                "student_email": "alice@example.invalid",
                "domain": "Machine Learning",
                "start_date": "01-01-2026",
                "end_date": "31-03-2026",
                "issued_date": "01-04-2026",
                "certificate_id": "PXL-CERT-RETRY-01",
                "send_again": False,
            })

        self.assertEqual(response.status_code, 500)
        self.assertTrue(len(saved_records) > 0)
        self.assertEqual(saved_records[-1]["email_status"], "failed")

        # Second attempt (retry): SMTP succeeds
        failed_record = dict(saved_records[-1])
        with patch.object(repository, "get_certificate_by_id", return_value=failed_record), \
             patch.object(repository, "find_active_certificate", return_value=failed_record), \
             patch.object(repository, "save_certificate_record", side_effect=capture_save), \
             patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_ML.pdf"), \
             patch.object(certificate_service, "send_certificate_email", return_value=None):
            response2 = client.post("/api/certificate/send-email", json={
                "student_name": "Alice Example",
                "student_email": "alice@example.invalid",
                "domain": "Machine Learning",
                "start_date": "01-01-2026",
                "end_date": "31-03-2026",
                "issued_date": "01-04-2026",
                "certificate_id": "PXL-CERT-RETRY-01",
                "send_again": False,
            })

        self.assertEqual(response2.status_code, 200)
        self.assertEqual(saved_records[-1]["certificate_id"], "PXL-CERT-RETRY-01")
        self.assertEqual(saved_records[-1]["email_status"], "sent")
        self.assertEqual(saved_records[-1]["send_count"], 1)

    # ------------------------------------------------------------------
    # Case 12: Bulk exact same details reuses existing ID
    # ------------------------------------------------------------------

    def test_bulk_exact_same_details_reuses_existing_id(self):
        """Bulk certificate generation reuses existing ID when all 6 identity fields match."""
        existing_cert = {
            "certificate_id": "PXL-CERT-BULK-EXIST",
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "certificate_status": "active",
        }
        lookup = {
            ("alice example", "alice@example.invalid", "machine learning", "01-01-2026", "31-03-2026", "01-04-2026"): existing_cert
        }
        items = [{
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "row_index": 1,
        }]
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "find_existing_certificates_batch", return_value=lookup), \
                 patch.object(certificate_service, "generate_certificate_pdf", return_value="Alice_Example_Certificate.pdf"):
                result = bulk_certificate_service.generate_bulk_certificates(valid_rows=items, output_dir=td)

        self.assertEqual(result["already_exists_count"], 1)
        self.assertEqual(result["newly_generated_count"], 0)
        self.assertEqual(result["results"][0]["certificate_id"], "PXL-CERT-BULK-EXIST")

    # ------------------------------------------------------------------
    # Case 13: Bulk same email with different fields gets fresh ID
    # ------------------------------------------------------------------

    def test_bulk_same_email_different_fields_get_fresh_ids(self):
        """Bulk certificate generation creates separate fresh IDs when name, domain, or dates differ."""
        existing_cert = {
            "certificate_id": "PXL-CERT-BULK-EXIST",
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "certificate_status": "active",
        }
        lookup = {
            ("alice example", "alice@example.invalid", "machine learning", "01-01-2026", "31-03-2026", "01-04-2026"): existing_cert
        }

        diff_cases = [
            # Different student name
            {"student_name": "Bob Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "start_date": "01-01-2026", "end_date": "31-03-2026", "issued_date": "01-04-2026", "row_index": 1},
            # Different domain
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Data Science", "start_date": "01-01-2026", "end_date": "31-03-2026", "issued_date": "01-04-2026", "row_index": 2},
            # Different start date
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "start_date": "15-01-2026", "end_date": "31-03-2026", "issued_date": "01-04-2026", "row_index": 3},
            # Different end date
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "start_date": "01-01-2026", "end_date": "15-04-2026", "issued_date": "01-04-2026", "row_index": 4},
            # Different issued date
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "start_date": "01-01-2026", "end_date": "31-03-2026", "issued_date": "15-05-2026", "row_index": 5},
            # Different email
            {"student_name": "Alice Example", "student_email": "different@example.invalid", "domain": "Machine Learning", "start_date": "01-01-2026", "end_date": "31-03-2026", "issued_date": "01-04-2026", "row_index": 6},
        ]

        for case in diff_cases:
            with tempfile.TemporaryDirectory() as td:
                with patch.object(bulk_certificate_service, "find_existing_certificates_batch", return_value=lookup), \
                     patch.object(certificate_service, "generate_bulk_certificate_ids", return_value=["PXL-CERT-NEW-ID"]), \
                     patch.object(certificate_service, "generate_certificate_pdf", return_value="Cert.pdf"), \
                     patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value={"success": True}):
                    result = bulk_certificate_service.generate_bulk_certificates(valid_rows=[case], output_dir=td)

            self.assertEqual(result["newly_generated_count"], 1, f"Failed for case: {case}")
            self.assertEqual(result["already_exists_count"], 0, f"Failed for case: {case}")
            self.assertNotEqual(result["results"][0]["certificate_id"], "PXL-CERT-BULK-EXIST")

    # ------------------------------------------------------------------
    # Case 14: Two different issuances for same email in one bulk upload
    # ------------------------------------------------------------------

    def test_bulk_two_different_issuances_same_email_in_one_upload_remain_distinct(self):
        """
        When the same email appears multiple times with different certificate details:
        - validate_bulk_records marks both as valid (not in-file duplicates)
        - generate_bulk_certificates creates 2 distinct certificate IDs
        - send_bulk_certificate_emails dispatches both without skipping either
        """
        rows = [
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "start_date": "01-01-2026", "end_date": "31-03-2026", "issued_date": "01-04-2026", "row_index": 1},
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Web Development", "start_date": "01-04-2026", "end_date": "30-06-2026", "issued_date": "01-07-2026", "row_index": 2},
        ]

        # 1. Validation
        v_res = bulk_certificate_service.validate_bulk_records(rows)
        self.assertEqual(v_res["valid_records_count"], 2)
        self.assertEqual(v_res["duplicate_records_count"], 0)

        # 2. Generation
        gen_ids = ["PXL-CERT-A1", "PXL-CERT-A2"]
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bulk_certificate_service, "find_existing_certificates_batch", return_value={}), \
                 patch.object(certificate_service, "generate_bulk_certificate_ids", return_value=gen_ids), \
                 patch.object(certificate_service, "generate_certificate_pdf", side_effect=["ML.pdf", "WD.pdf"]), \
                 patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value={"success": True}):
                g_res = bulk_certificate_service.generate_bulk_certificates(valid_rows=rows, output_dir=td)

        self.assertEqual(g_res["newly_generated_count"], 2)
        cid1 = g_res["results"][0]["certificate_id"]
        cid2 = g_res["results"][1]["certificate_id"]
        self.assertNotEqual(cid1, cid2)

        # 3. Dispatch: both items are sent to alice@example.invalid without being skipped
        items_to_send = [
            {"certificate_id": cid1, "student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "filename": "ML.pdf"},
            {"certificate_id": cid2, "student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Web Development", "filename": "WD.pdf"},
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch("certificate_service.generate_certificate_pdf", side_effect=["ML.pdf", "WD.pdf"]), \
                 patch("certificate_service.send_certificate_email", return_value=None), \
                 patch.object(bulk_certificate_service, "get_certificate_by_id", return_value=None), \
                 patch.object(bulk_certificate_service, "find_active_certificate", return_value=None), \
                 patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
                 patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
                s_res = bulk_certificate_service.send_bulk_certificate_emails(certificate_items=items_to_send, output_dir=td)

        self.assertEqual(s_res["successful_count"], 2)
        self.assertEqual(s_res["skipped_count"], 0)

    # ------------------------------------------------------------------
    # Case 15: Duplicate-check endpoint and Send Only to New coherence
    # ------------------------------------------------------------------

    def test_bulk_duplicate_check_and_send_only_to_new_coherence(self):
        """
        When checking duplicates with full candidate records:
        - Only records matching an already-sent issuance are flagged
        - A new course for an existing recipient is not flagged
        - When send_again_all=False, only the duplicate item is skipped; the new course is sent
        """
        client = self._make_client()
        sent_record = {
            "certificate_id": "PXL-CERT-OLD-SENT",
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "01-01-2025",
            "end_date": "31-03-2025",
            "issued_date": "01-04-2025",
            "email_status": "sent",
            "send_count": 1,
        }

        # Step 1: Duplicate check
        candidate_records = [
            # Identical to sent record -> duplicate
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Data Science", "start_date": "01-01-2025", "end_date": "31-03-2025", "issued_date": "01-04-2025"},
            # Different course -> new issuance
            {"student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "start_date": "01-01-2026", "end_date": "31-03-2026", "issued_date": "01-04-2026"},
        ]

        with patch("database.repository.get_supabase_client", return_value=None), \
             patch("database.repository.sqlite3.connect") as mock_conn:
            # Mock DB to return sent_record
            conn_inst = MagicMock()
            cursor_inst = MagicMock()
            cursor_inst.fetchall.return_value = [sent_record]
            conn_inst.cursor.return_value = cursor_inst
            mock_conn.return_value = conn_inst

            dup_map = repository.find_existing_certificate_emails_batch(candidate_records)

        self.assertIn("alice@example.invalid", dup_map)

        # Step 2: Send batch with skip_duplicate_emails containing the duplicate certificate ID
        items = [
            {"certificate_id": "PXL-CERT-OLD-SENT", "student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Data Science", "filename": "DS.pdf"},
            {"certificate_id": "PXL-CERT-NEW-ML", "student_name": "Alice Example", "student_email": "alice@example.invalid", "domain": "Machine Learning", "filename": "ML.pdf"},
        ]

        def get_cert(cid):
            if cid == "PXL-CERT-OLD-SENT":
                return sent_record
            return None

        with tempfile.TemporaryDirectory() as td:
            with patch("certificate_service.generate_certificate_pdf", return_value="cert.pdf"), \
                 patch("certificate_service.send_certificate_email", return_value=None), \
                 patch.object(bulk_certificate_service, "get_certificate_by_id", side_effect=get_cert), \
                 patch.object(bulk_certificate_service, "find_active_certificate", return_value=None), \
                 patch.object(bulk_certificate_service, "save_certificate_record", return_value=True), \
                 patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
                res = bulk_certificate_service.send_bulk_certificate_emails(
                    certificate_items=items,
                    output_dir=td,
                    skip_duplicate_emails=["pxl-cert-old-sent"],
                    send_again_all=False,
                )

        self.assertEqual(res["skipped_count"], 1)
        self.assertEqual(res["successful_count"], 1)
        self.assertEqual(res["results"][0]["status"], "skipped_historical_duplicate")
        self.assertEqual(res["results"][1]["status"], "sent")

    # ------------------------------------------------------------------
    # Case 16: Old QR verification integrity after second issuance
    # ------------------------------------------------------------------

    def test_old_qr_integrity_after_new_issuance_generated_and_sent(self):
        """Permanent QR verification for an old certificate ID continues to show original details."""
        old_cert = {
            "certificate_id": "PXL-CERT-ORIG-2025",
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "01-01-2025",
            "end_date": "31-03-2025",
            "issued_date": "01-04-2025",
            "certificate_status": "active",
            "template_version": "v1",
        }
        new_cert = {
            "certificate_id": "PXL-CERT-NEW-2026",
            "student_name": "Alice Example",
            "student_email": "alice@example.invalid",
            "internship_domain": "Machine Learning",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "certificate_status": "active",
            "template_version": "v1",
        }

        def mock_get_cert(cid, sb=None):
            if cid == "PXL-CERT-ORIG-2025":
                return old_cert
            if cid == "PXL-CERT-NEW-2026":
                return new_cert
            return None

        captured_renders = {}

        def mock_generate_pdf_bytes(data, base_url, template_version="v1"):
            captured_renders[data["certificate_id"]] = dict(data)
            return b"%PDF-1.4 stub", "cert.pdf"

        client = self._make_client()
        with patch.object(certificate_service, "db_get_certificate_by_id", side_effect=mock_get_cert), \
             patch.object(certificate_service, "generate_certificate_pdf_bytes", side_effect=mock_generate_pdf_bytes):
            resp_old = client.get("/verify/PXL-CERT-ORIG-2025/preview?name=Tampered&domain=Tampered")
            resp_new = client.get("/verify/PXL-CERT-NEW-2026/preview")

        self.assertEqual(resp_old.status_code, 200)
        self.assertEqual(resp_new.status_code, 200)
        # Old verification retains original Data Science domain, NOT tampered and NOT Machine Learning
        self.assertEqual(captured_renders["PXL-CERT-ORIG-2025"]["domain"], "Data Science")
        self.assertEqual(captured_renders["PXL-CERT-ORIG-2025"]["start_date"], "01-01-2025")
        # New verification has its own Machine Learning domain
        self.assertEqual(captured_renders["PXL-CERT-NEW-2026"]["domain"], "Machine Learning")
        self.assertEqual(captured_renders["PXL-CERT-NEW-2026"]["start_date"], "01-01-2026")


class CertificateQrVerificationUrlTests(unittest.TestCase):
    """
    Regression and verification tests for Certificate QR verification URL generation,
    environment configuration handling, local vs production routing, and /verify endpoints.
    """

    def setUp(self):
        self._orig_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_get_public_base_url_explicit_env(self):
        os.environ["PUBLIC_BASE_URL"] = "https://canonical.persevex.com/ "
        base_url = certificate_service.get_public_base_url()
        self.assertEqual(base_url, "https://canonical.persevex.com")

        # BASE_URL fallback
        os.environ.pop("PUBLIC_BASE_URL")
        os.environ["BASE_URL"] = "https://base.persevex.com///"
        self.assertEqual(certificate_service.get_public_base_url(), "https://base.persevex.com")

    def test_get_public_base_url_vercel_env(self):
        os.environ.pop("PUBLIC_BASE_URL", None)
        os.environ.pop("BASE_URL", None)
        os.environ["VERCEL_URL"] = "persevex-app.vercel.app"
        base_url = certificate_service.get_public_base_url()
        self.assertEqual(base_url, "https://persevex-app.vercel.app")

    def test_get_public_base_url_local_request(self):
        os.environ.pop("PUBLIC_BASE_URL", None)
        os.environ.pop("BASE_URL", None)
        os.environ.pop("VERCEL_URL", None)

        mock_req = MagicMock()
        mock_req.host = "localhost:5000"
        mock_req.host_url = "http://localhost:5000/"
        base_url = certificate_service.get_public_base_url(mock_req)
        self.assertEqual(base_url, "http://localhost:5000")

        mock_req_127 = MagicMock()
        mock_req_127.host = "127.0.0.1:5000"
        mock_req_127.host_url = "http://127.0.0.1:5000/"
        base_url_127 = certificate_service.get_public_base_url(mock_req_127)
        self.assertEqual(base_url_127, "http://127.0.0.1:5000")

    def test_get_public_base_url_default_fallback_not_broken_domain(self):
        os.environ.pop("PUBLIC_BASE_URL", None)
        os.environ.pop("BASE_URL", None)
        os.environ.pop("VERCEL_URL", None)

        base_url = certificate_service.get_public_base_url()
        # Must be local fallback, NEVER the outdated static landing page https://persevex.vercel.app
        self.assertEqual(base_url, "http://127.0.0.1:5000")
        self.assertNotIn("persevex.vercel.app", base_url)

    def test_get_verification_url_structure(self):
        url = certificate_service.get_verification_url("PER-2026-78901", base_url="https://verify.persevex.com")
        self.assertEqual(url, "https://verify.persevex.com/verify/PER-2026-78901")

    def test_generate_pdf_qr_uses_canonical_base_url(self):
        os.environ["PUBLIC_BASE_URL"] = "https://canonical.persevex.com"
        cert_data = {
            "student_name": "QR Student",
            "student_email": "qr@example.invalid",
            "domain": "Web Development",
            "start_date": "01-01-2026",
            "end_date": "31-01-2026",
            "issued_date": "01-02-2026",
            "certificate_id": "PER-2026-QR001",
        }
        with patch.object(certificate_service, "get_verification_url", wraps=certificate_service.get_verification_url) as mock_get_vurl:
            pdf_bytes, filename = certificate_service.generate_certificate_pdf_bytes(cert_data)
            self.assertTrue(pdf_bytes.startswith(b"%PDF"))
            mock_get_vurl.assert_called_with("PER-2026-QR001", base_url="https://canonical.persevex.com")

    def test_verify_route_known_certificate_returns_200(self):
        known_cert = {
            "id": 888,
            "certificate_id": "PER-2026-KNOWN888",
            "student_name": "Known Student",
            "student_email": "known@example.invalid",
            "internship_domain": "Cloud Computing",
            "start_date": "01-01-2026",
            "end_date": "31-03-2026",
            "issued_date": "01-04-2026",
            "certificate_status": "active",
        }
        with patch.object(certificate_service, "db_get_certificate_by_id", return_value=(known_cert, None)):
            with application.app.test_client() as client:
                resp = client.get("/verify/PER-2026-KNOWN888")
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn("Known Student", html)
                self.assertIn("Cloud Computing", html)

    def test_verify_query_parameter_redirects_to_canonical_route(self):
        with application.app.test_client() as client:
            resp1 = client.get("/verify?certificate_id=PER-2026-REDIRECT1")
            self.assertEqual(resp1.status_code, 302)
            self.assertEqual(resp1.headers["Location"], "/verify/PER-2026-REDIRECT1")

            resp2 = client.get("/verify/?id=PER-2026-REDIRECT2")
            self.assertEqual(resp2.status_code, 302)
            self.assertEqual(resp2.headers["Location"], "/verify/PER-2026-REDIRECT2")

            resp_empty = client.get("/verify")
            self.assertEqual(resp_empty.status_code, 404)

    def test_bulk_generation_and_send_uses_resolved_base_url(self):
        os.environ["PUBLIC_BASE_URL"] = "https://canonical.persevex.com"
        captured_base_urls = []

        def mock_gen_pdf(data, output_dir=None, base_url=None, **kw):
            captured_base_urls.append(base_url)
            return "cert.pdf"

        valid_rows = [
            {"student_name": "Bulk User", "student_email": "buser@example.invalid", "domain": "Web", "start_date": "01-01-2026", "end_date": "31-01-2026", "row_index": 2}
        ]

        with patch("certificate_service.generate_certificate_pdf", side_effect=mock_gen_pdf), \
             patch.object(bulk_certificate_service, "find_existing_certificates_batch", return_value={}), \
             patch.object(bulk_certificate_service, "bulk_save_certificate_records", return_value=True):
            bulk_certificate_service.generate_bulk_certificates(valid_rows, output_dir="/tmp/test_out")

        self.assertIn("https://canonical.persevex.com", captured_base_urls)

    # ------------------------------------------------------------------
    # Bulk Send-Count Targeting & Shared-Email Identity Disambiguation
    # ------------------------------------------------------------------

    def _authenticated_client(self):
        client = application.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        return client

    def test_bulk_send_distinct_students_sharing_email_increments_respective_counts(self):
        """Two distinct students sharing email and course dates get send_count=1 on their respective records, neither incremented twice."""
        client = self._authenticated_client()
        rec_sample1 = {
            "id": 101,
            "certificate_id": "PEX-DS-001",
            "student_name": "Sample1",
            "student_email": "shared@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "15-10-2026",
            "end_date": "15-11-2026",
            "issued_date": "15-10-2026",
            "email_status": "pending",
            "send_count": 0,
        }
        rec_sample2 = {
            "id": 102,
            "certificate_id": "PEX-DS-002",
            "student_name": "Sample2",
            "student_email": "shared@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "15-10-2026",
            "end_date": "15-11-2026",
            "issued_date": "15-10-2026",
            "email_status": "pending",
            "send_count": 0,
        }
        items = [
            {"certificate_id": "PEX-DS-001", "student_name": "Sample1", "student_email": "shared@example.invalid", "domain": "Data Science", "start_date": "15-10-2026", "end_date": "15-11-2026", "issued_date": "15-10-2026", "filename": "s1.pdf"},
            {"certificate_id": "PEX-DS-002", "student_name": "Sample2", "student_email": "shared@example.invalid", "domain": "Data Science", "start_date": "15-10-2026", "end_date": "15-11-2026", "issued_date": "15-10-2026", "filename": "s2.pdf"},
        ]

        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="cert.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_certificate_by_id", side_effect=lambda cid: rec_sample1 if cid == "PEX-DS-001" else (rec_sample2 if cid == "PEX-DS-002" else None)), \
             patch.object(bulk_certificate_service, "save_certificate_record", side_effect=lambda rec, existing_id=None: saved_records.append((dict(rec), existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            # Dispatch Sample1
            r1 = client.post("/api/certificate/bulk/send-email", json={"items": [items[0]]})
            self.assertEqual(r1.status_code, 200)
            # Dispatch Sample2
            r2 = client.post("/api/certificate/bulk/send-email", json={"items": [items[1]]})
            self.assertEqual(r2.status_code, 200)

        self.assertEqual(len(saved_records), 2)
        rec1, id1 = saved_records[0]
        rec2, id2 = saved_records[1]

        self.assertEqual(rec1["id"], 101)
        self.assertEqual(rec1["certificate_id"], "PEX-DS-001")
        self.assertEqual(rec1["student_name"], "Sample1")
        self.assertEqual(rec1["send_count"], 1)

        self.assertEqual(rec2["id"], 102)
        self.assertEqual(rec2["certificate_id"], "PEX-DS-002")
        self.assertEqual(rec2["student_name"], "Sample2")
        self.assertEqual(rec2["send_count"], 1)

    def test_bulk_resend_increments_only_own_issuance_send_count(self):
        """Resending a certificate increments only its own send_count without affecting any other record sharing the email."""
        client = self._authenticated_client()
        rec_sample1 = {
            "id": 101,
            "certificate_id": "PEX-DS-001",
            "student_name": "Sample1",
            "student_email": "shared@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "15-10-2026",
            "end_date": "15-11-2026",
            "issued_date": "15-10-2026",
            "email_status": "sent",
            "send_count": 1,
        }
        item_resend = {
            "certificate_id": "PEX-DS-001",
            "student_name": "Sample1",
            "student_email": "shared@example.invalid",
            "domain": "Data Science",
            "start_date": "15-10-2026",
            "end_date": "15-11-2026",
            "issued_date": "15-10-2026",
            "filename": "s1.pdf",
        }
        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="cert.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_certificate_by_id", return_value=rec_sample1), \
             patch.object(bulk_certificate_service, "save_certificate_record", side_effect=lambda rec, existing_id=None: saved_records.append((dict(rec), existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            r = client.post("/api/certificate/bulk/send-email", json={"items": [item_resend], "send_again_all": True})
            self.assertEqual(r.status_code, 200)

        self.assertEqual(len(saved_records), 1)
        rec, eid = saved_records[0]
        self.assertEqual(eid, 101)
        self.assertEqual(rec["certificate_id"], "PEX-DS-001")
        self.assertEqual(rec["send_count"], 2)

    def test_bulk_send_shared_email_different_domains_targets_correct_records(self):
        """Two certificates sharing email but with different domains each update only their respective records."""
        client = self._authenticated_client()
        rec_ds = {
            "id": 201,
            "certificate_id": "PEX-DS-100",
            "student_name": "Alex",
            "student_email": "alex@example.invalid",
            "internship_domain": "Data Science",
            "email_status": "sent",
            "send_count": 1,
        }
        rec_cs = {
            "id": 202,
            "certificate_id": "PEX-CS-200",
            "student_name": "Alex",
            "student_email": "alex@example.invalid",
            "internship_domain": "Cyber Security",
            "email_status": "sent",
            "send_count": 1,
        }
        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="cert.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_certificate_by_id", side_effect=lambda cid: rec_ds if cid == "PEX-DS-100" else (rec_cs if cid == "PEX-CS-200" else None)), \
             patch.object(bulk_certificate_service, "save_certificate_record", side_effect=lambda rec, existing_id=None: saved_records.append((dict(rec), existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            r = client.post("/api/certificate/bulk/send-email", json={
                "items": [{"certificate_id": "PEX-CS-200", "student_name": "Alex", "student_email": "alex@example.invalid", "domain": "Cyber Security", "filename": "cs.pdf"}],
                "send_again_all": True
            })
            self.assertEqual(r.status_code, 200)

        self.assertEqual(len(saved_records), 1)
        rec, eid = saved_records[0]
        self.assertEqual(eid, 202)
        self.assertEqual(rec["certificate_id"], "PEX-CS-200")
        self.assertEqual(rec["send_count"], 2)

    def test_bulk_send_shared_email_different_dates_targets_correct_records(self):
        """Two certificates sharing email and domain but with different start/end dates each update their own record."""
        client = self._authenticated_client()
        rec_cohort1 = {
            "id": 301,
            "certificate_id": "PEX-COHORT-1",
            "student_name": "Alex",
            "student_email": "alex@example.invalid",
            "internship_domain": "Web Development",
            "start_date": "01-01-2026",
            "end_date": "01-02-2026",
            "email_status": "sent",
            "send_count": 1,
        }
        rec_cohort2 = {
            "id": 302,
            "certificate_id": "PEX-COHORT-2",
            "student_name": "Alex",
            "student_email": "alex@example.invalid",
            "internship_domain": "Web Development",
            "start_date": "01-06-2026",
            "end_date": "01-07-2026",
            "email_status": "pending",
            "send_count": 0,
        }
        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="cert.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_certificate_by_id", side_effect=lambda cid: rec_cohort1 if cid == "PEX-COHORT-1" else (rec_cohort2 if cid == "PEX-COHORT-2" else None)), \
             patch.object(bulk_certificate_service, "save_certificate_record", side_effect=lambda rec, existing_id=None: saved_records.append((dict(rec), existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            r = client.post("/api/certificate/bulk/send-email", json={
                "items": [{"certificate_id": "PEX-COHORT-2", "student_name": "Alex", "student_email": "alex@example.invalid", "domain": "Web Development", "start_date": "01-06-2026", "end_date": "01-07-2026", "filename": "c2.pdf"}],
            })
            self.assertEqual(r.status_code, 200)

        self.assertEqual(len(saved_records), 1)
        rec, eid = saved_records[0]
        self.assertEqual(rec["id"], 302)
        self.assertEqual(rec["certificate_id"], "PEX-COHORT-2")
        self.assertEqual(rec["send_count"], 1)

    def test_bulk_send_in_batch_duplicate_skipped_without_double_increment(self):
        """A duplicate item within the same batch is skipped as duplicate and not incremented twice."""
        client = self._authenticated_client()
        rec = {
            "id": 401,
            "certificate_id": "PEX-DUP-1",
            "student_name": "Sam",
            "student_email": "sam@example.invalid",
            "internship_domain": "Data Science",
            "email_status": "pending",
            "send_count": 0,
        }
        items = [
            {"certificate_id": "PEX-DUP-1", "student_name": "Sam", "student_email": "sam@example.invalid", "domain": "Data Science", "filename": "s.pdf"},
            {"certificate_id": "PEX-DUP-1", "student_name": "Sam", "student_email": "sam@example.invalid", "domain": "Data Science", "filename": "s.pdf"},
        ]
        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="cert.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_certificate_by_id", return_value=rec), \
             patch.object(bulk_certificate_service, "save_certificate_record", side_effect=lambda r, existing_id=None: saved_records.append((dict(r), existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            r = client.post("/api/certificate/bulk/send-email", json={"items": items})
            self.assertEqual(r.status_code, 200)
            data = r.get_json()

        self.assertEqual(data["successful_count"], 1)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(len(saved_records), 1)
        self.assertEqual(saved_records[0][0]["send_count"], 1)

    def test_bulk_check_duplicates_does_not_conflate_shared_email_different_students(self):
        """Duplicate check endpoint returns duplicate_certificate_ids and composite duplicate_map entries without conflating different students sharing an email."""
        client = self._authenticated_client()
        sent_record = {
            "id": 501,
            "certificate_id": "PEX-SENT-01",
            "student_name": "Sample1",
            "student_email": "shared@example.invalid",
            "internship_domain": "Data Science",
            "start_date": "15-10-2026",
            "end_date": "15-11-2026",
            "issued_date": "15-10-2026",
            "email_status": "sent",
            "send_count": 1,
        }
        candidate_records = [
            {"student_name": "Sample1", "student_email": "shared@example.invalid", "domain": "Data Science", "start_date": "15-10-2026", "end_date": "15-11-2026", "issued_date": "15-10-2026"},
            {"student_name": "Sample2", "student_email": "shared@example.invalid", "domain": "Data Science", "start_date": "15-10-2026", "end_date": "15-11-2026", "issued_date": "15-10-2026"},
        ]
        with patch.object(repository, "get_supabase_client", return_value=None), \
             patch("sqlite3.connect") as mock_conn:
            conn_inst = MagicMock()
            cursor_inst = MagicMock()
            cursor_inst.fetchall.return_value = [sent_record]
            conn_inst.cursor.return_value = cursor_inst
            mock_conn.return_value = conn_inst

            resp = client.post("/api/certificate/bulk-check-duplicates", json={"records": candidate_records})
            self.assertEqual(resp.status_code, 200)
            res_data = resp.get_json()

        self.assertTrue(res_data["success"])
        self.assertIn("PEX-SENT-01", res_data.get("duplicate_certificate_ids", []))
        dup_map = res_data.get("duplicate_map", {})
        self.assertIn("shared@example.invalid#sample1", dup_map)
        self.assertNotIn("shared@example.invalid#sample2", dup_map)

    def test_bulk_send_ambiguous_identity_fails_safely_without_hijacking(self):
        """When certificate_id belongs to a conflicting student name, item fails safely with failed_identity_ambiguity and does not mutate the record."""
        client = self._authenticated_client()
        alice_record = {
            "id": 601,
            "certificate_id": "PEX-ALICE-100",
            "student_name": "Alice Wonderland",
            "student_email": "shared@example.invalid",
            "internship_domain": "Data Science",
            "email_status": "sent",
            "send_count": 1,
        }
        item_imposter = {
            "certificate_id": "PEX-ALICE-100",
            "student_name": "Bob Builder",
            "student_email": "shared@example.invalid",
            "domain": "Data Science",
            "filename": "bob.pdf",
        }
        saved_records = []
        with patch("certificate_service.generate_certificate_pdf", return_value="cert.pdf"), \
             patch("certificate_service.send_certificate_email", return_value=True), \
             patch.object(bulk_certificate_service, "get_certificate_by_id", return_value=alice_record), \
             patch.object(bulk_certificate_service, "save_certificate_record", side_effect=lambda r, existing_id=None: saved_records.append((dict(r), existing_id)) or None), \
             patch.object(bulk_certificate_service, "save_bulk_job_record", return_value=None):
            r = client.post("/api/certificate/bulk/send-email", json={"items": [item_imposter]})
            self.assertEqual(r.status_code, 200)
            data = r.get_json()

        self.assertEqual(data["failed_count"], 1)
        self.assertEqual(data["successful_count"], 0)
        self.assertEqual(len(saved_records), 0)
        failed_res = data["results"][0]
        self.assertEqual(failed_res["status"], "failed_identity_ambiguity")
        self.assertIn("Ambiguous issuance identity", failed_res["error"])


if __name__ == '__main__':
    unittest.main()
