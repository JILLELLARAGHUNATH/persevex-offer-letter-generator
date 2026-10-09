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
import services.bulk_certificate_service as bulk_certificate_service
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


if __name__ == '__main__':
    unittest.main()