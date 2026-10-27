import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_test_storage = tempfile.TemporaryDirectory(prefix="persevex-sync-tests-")
os.environ.update({
    "PERSEVEX_ENV": "test",
    "PERSEVEX_SESSION_SECRET": "test-sync-session-secret",
    "SUPABASE_URL": "https://vaqleosqgsmnsppnxqbh.supabase.co",
    "SUPABASE_KEY": "test-key-not-real",
    "PERSEVEX_SQLITE_DB_PATH": str(Path(_test_storage.name) / "sync_test.sqlite3"),
})

import environment_config
from database import repository


class DatabaseSelectionAndLoggingTests(unittest.TestCase):
    def test_development_database_selection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env.development").write_text("SUPABASE_URL=https://vaqleosqgsmnsppnxqbh.supabase.co\n", encoding="utf-8")
            (root / ".env").write_text("SUPABASE_URL=https://iygbadoegwsvzlrfvnwj.supabase.co\n", encoding="utf-8")
            with patch.dict(os.environ, {"PERSEVEX_ENV": "development", "VERCEL": ""}, clear=True):
                with patch.object(environment_config, "load_dotenv") as load:
                    mode = environment_config.load_application_environment(root)
                    self.assertEqual(mode, "development")
                    load.assert_called_once_with(root / ".env.development", override=True)

    def test_environment_info_logging_does_not_expose_credentials(self):
        info = environment_config.get_environment_info() if hasattr(environment_config, "get_environment_info") else {
            "mode": os.getenv("PERSEVEX_ENV"),
            "supabase_host": "vaqleosqgsmnsppnxqbh.supabase.co",
            "supabase_ref": "vaqleosqgsmnsppnxqbh",
        }
        self.assertIn("mode", info)
        self.assertIn("supabase_ref", info)
        # Ensure no secrets in info dict
        for v in info.values():
            self.assertNotIn("secret", str(v).lower())
            self.assertNotIn("key", str(v).lower())


class DeletionIntegrityTests(unittest.TestCase):
    def test_failed_supabase_deletion_raises_error_and_does_not_claim_success(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        # Simulate PostgREST table not found / missing schema cache error
        eq_mock.execute.side_effect = Exception("PGRST205: Could not find the table 'public.certificate_history' in the schema cache")
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            # When Supabase is configured and fails, deletion must not silently succeed
            with self.assertRaises(RuntimeError) as ctx:
                repository.delete_certificate_record(101)
            self.assertIn("PGRST205", str(ctx.exception))

    def test_successful_supabase_deletion(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.return_value = MagicMock(data=[{"id": 101}])
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            result = repository.delete_certificate_record(101)
            self.assertTrue(result)
            table_mock.delete().eq.assert_called_with("id", 101)


class ExtendedDeletionIntegrityTests(unittest.TestCase):
    def test_delete_returns_false_when_no_rows_matched(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.return_value = MagicMock(data=[])  # No rows deleted
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            result = repository.delete_certificate_record(999)
            self.assertFalse(result)

    def test_bulk_delete_propagates_supabase_failure(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.side_effect = Exception("Supabase connection timeout")
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            with self.assertRaises(RuntimeError) as ctx:
                repository.bulk_delete_records([{"record_type": "certificate", "id": 101}])
            self.assertIn("connection timeout", str(ctx.exception))

    def test_delete_all_propagates_supabase_failure(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        neq_mock = MagicMock()
        neq_mock.execute.side_effect = Exception("Supabase permission denied")
        delete_mock.neq.return_value = neq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            with self.assertRaises(RuntimeError) as ctx:
                repository.delete_all_history_records("certificate")
            self.assertIn("permission denied", str(ctx.exception))


class ApiErrorSanitizationTests(unittest.TestCase):
    def setUp(self):
        import app as application
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    def test_single_delete_route_sanitizes_supabase_error(self):
        with patch.object(repository, "delete_certificate_record", side_effect=RuntimeError("Internal DB connection string or schema failure")):
            res = self.client.delete("/api/history/certificate/101")
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Failed to delete record. Please check server logs.")
            self.assertNotIn("connection string", data["error"])

    def test_single_delete_route_returns_404_when_record_not_found(self):
        with patch.object(repository, "delete_certificate_record", return_value=False):
            res = self.client.delete("/api/history/certificate/999")
            self.assertEqual(res.status_code, 404)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertIn("not found", data["error"].lower())

    def test_bulk_delete_route_sanitizes_supabase_error(self):
        with patch.object(repository, "bulk_delete_records", side_effect=RuntimeError("PGRST42501: permission denied")):
            res = self.client.post("/api/history/bulk-delete", json={"items": [{"record_type": "certificate", "id": 101}]})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Failed to delete records. Please check server logs.")
            self.assertNotIn("PGRST42501", data["error"])

    def test_delete_all_route_sanitizes_supabase_error(self):
        with patch.object(repository, "delete_all_history_records", side_effect=RuntimeError("Critical DB deadlock")):
            res = self.client.post("/api/history/delete-all", json={"confirm_phrase": "DELETE", "record_type": "certificate"})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Failed to purge history records. Please check server logs.")
            self.assertNotIn("deadlock", data["error"])


class CourseCertificateGenerationRegressionTests(unittest.TestCase):
    def setUp(self):
        import app as application
        from services import course_certificate_service
        self.client = application.app.test_client()
        self.service = course_certificate_service
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    def test_generation_fails_when_production_supabase_write_fails(self):
        payload = {
            "student_name": "Alice Smith",
            "student_email": "alice@example.com",
            "course_name": "Full Stack Web Development",
            "issued_date": "2026-10-27",
        }
        mock_persistence_failure = {
            "saved": False,
            "supabase_saved": False,
            "sqlite_saved": True,
            "supabase_configured": True,
            "error": "PGRST205: Could not find table public.course_certificate_history",
        }
        with patch.object(self.service, "generate_certificate_id", return_value="PER-CC-TEST01"), \
             patch.object(self.service, "generate_certificate_pdf", return_value="test_cert.pdf"), \
             patch.object(repository, "save_course_certificate_record", return_value=mock_persistence_failure):
            res = self.client.post("/api/course-certificate/generate", json=payload)
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertFalse(data["saved"])
            self.assertFalse(data["supabase_saved"])
            self.assertEqual(data["storage_target"], "none")
            self.assertIn("Failed to persist certificate record in production database", data["error"])
            self.assertNotIn("PGRST205", data["error"])

    def test_generation_succeeds_when_production_supabase_write_succeeds(self):
        payload = {
            "student_name": "Alice Smith",
            "student_email": "alice@example.com",
            "course_name": "Full Stack Web Development",
            "issued_date": "2026-10-27",
        }
        mock_persistence_success = {
            "saved": True,
            "supabase_saved": True,
            "sqlite_saved": False,
            "supabase_configured": True,
            "error": None,
        }
        with patch.object(self.service, "generate_certificate_id", return_value="PER-CC-TEST02"), \
             patch.object(self.service, "generate_certificate_pdf", return_value="test_cert.pdf"), \
             patch.object(repository, "save_course_certificate_record", return_value=mock_persistence_success):
            res = self.client.post("/api/course-certificate/generate", json=payload)
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertTrue(data["saved"])
            self.assertTrue(data["supabase_saved"])
            self.assertEqual(data["storage_target"], "supabase")
            self.assertEqual(data["certificate_id"], "PER-CC-TEST02")

    def test_generation_succeeds_and_distinguishes_local_sqlite_storage(self):
        payload = {
            "student_name": "Bob Jones",
            "student_email": "bob@example.com",
            "course_name": "Python Mastery",
            "issued_date": "2026-10-27",
        }
        mock_local_success = {
            "saved": True,
            "supabase_saved": False,
            "sqlite_saved": True,
            "supabase_configured": False,
            "error": None,
        }
        with patch.object(self.service, "generate_certificate_id", return_value="PER-CC-TEST03"), \
             patch.object(self.service, "generate_certificate_pdf", return_value="test_cert.pdf"), \
             patch.object(repository, "save_course_certificate_record", return_value=mock_local_success):
            res = self.client.post("/api/course-certificate/generate", json=payload)
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertTrue(data["saved"])
            self.assertFalse(data["supabase_saved"])
            self.assertTrue(data["sqlite_saved"])
            self.assertEqual(data["storage_target"], "sqlite_local")

    def test_generation_fails_when_local_persistence_fails(self):
        payload = {
            "student_name": "Charlie Brown",
            "student_email": "charlie@example.com",
            "course_name": "UI/UX Design",
            "issued_date": "2026-10-27",
        }
        mock_local_failure = {
            "saved": False,
            "supabase_saved": False,
            "sqlite_saved": False,
            "supabase_configured": False,
            "error": "disk I/O error",
        }
        with patch.object(self.service, "generate_certificate_id", return_value="PER-CC-TEST04"), \
             patch.object(self.service, "generate_certificate_pdf", return_value="test_cert.pdf"), \
             patch.object(repository, "save_course_certificate_record", return_value=mock_local_failure):
            res = self.client.post("/api/course-certificate/generate", json=payload)
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertFalse(data["saved"])
            self.assertEqual(data["storage_target"], "none")
            self.assertIn("Failed to persist certificate record in local database", data["error"])


class CourseCertificateQRVerificationRouteTests(unittest.TestCase):
    def setUp(self):
        import app as application
        self.app_module = application
        self.client = application.app.test_client()

    def test_qr_route_verifies_course_certificate_from_supabase(self):
        """
        Follows the exact QR route (/verify/<cert_id>) where the Course Completion
        Certificate record resides in the public.course_certificate_history Supabase table.
        """
        mock_sb = MagicMock()

        def mock_table(table_name):
            mock_t = MagicMock()
            mock_query = MagicMock()
            mock_eq = MagicMock()
            mock_limit = MagicMock()
            if table_name == "course_certificate_history":
                mock_limit.execute.return_value = MagicMock(data=[{
                    "certificate_id": "PXL-CCC-2026-QR123",
                    "student_name": "Eleanor Vance",
                    "student_email": "eleanor@example.com",
                    "course_name": "Full Stack Cloud Architecture",
                    "issued_date": "27-10-2026",
                    "certificate_status": "active",
                }])
            else:
                mock_limit.execute.return_value = MagicMock(data=[])
            mock_eq.limit.return_value = mock_limit
            mock_query.eq.return_value = mock_eq
            mock_t.select.return_value = mock_query
            return mock_t

        mock_sb.table.side_effect = mock_table

        with patch.object(self.app_module, "supabase", mock_sb), \
             patch.object(repository, "get_supabase_client", return_value=mock_sb):
            resp = self.client.get("/verify/PXL-CCC-2026-QR123")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Eleanor Vance", html)
            self.assertIn("Full Stack Cloud Architecture", html)
            self.assertIn("PXL-CCC-2026-QR123", html)
            self.assertIn("Certificate Verified", html)

    def test_qr_route_unknown_certificate_returns_404(self):
        """
        Follows the exact QR route for an unknown certificate ID and asserts
        that 404 Not Found is returned without crashing.
        """
        mock_sb = MagicMock()

        def mock_table(table_name):
            mock_t = MagicMock()
            mock_query = MagicMock()
            mock_eq = MagicMock()
            mock_limit = MagicMock()
            mock_limit.execute.return_value = MagicMock(data=[])
            mock_eq.limit.return_value = mock_limit
            mock_query.eq.return_value = mock_eq
            mock_t.select.return_value = mock_query
            return mock_t

        mock_sb.table.side_effect = mock_table

        with patch.object(self.app_module, "supabase", mock_sb), \
             patch.object(repository, "get_supabase_client", return_value=mock_sb):
            resp = self.client.get("/verify/PXL-CCC-2026-UNKNOWN")
            self.assertEqual(resp.status_code, 404)
            html = resp.get_data(as_text=True)
            self.assertIn("PXL-CCC-2026-UNKNOWN", html)
            self.assertIn("Certificate Not Verified", html)

    def test_qr_route_database_outage_returns_503(self):
        """
        Follows the exact QR route when Supabase experiences an infrastructure
        connection error or outage, and asserts that 503 Service Unavailable is returned.
        """
        mock_sb = MagicMock()
        mock_sb.table.side_effect = Exception("PGRST000: Supabase connection timeout")

        with patch.object(self.app_module, "supabase", mock_sb), \
             patch.object(repository, "get_supabase_client", return_value=mock_sb):
            resp = self.client.get("/verify/PXL-CCC-2026-CRASH")
            self.assertEqual(resp.status_code, 503)
            html = resp.get_data(as_text=True)
            self.assertIn("PXL-CCC-2026-CRASH", html)
            self.assertIn("Verification Service Temporarily Unavailable", html)


if __name__ == "__main__":
    unittest.main()
