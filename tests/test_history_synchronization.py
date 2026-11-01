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

    def test_successful_course_certificate_supabase_deletion(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.return_value = MagicMock(data=[{"id": 202}])
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            result = repository.delete_course_certificate_record(202)
            self.assertTrue(result)
            mock_sb.table.assert_called_with("course_certificate_history")
            table_mock.delete().eq.assert_called_with("id", 202)

    def test_failed_course_certificate_supabase_deletion_raises_error(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.side_effect = Exception("PGRST205: Could not find table 'public.course_certificate_history'")
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            with self.assertRaises(RuntimeError) as ctx:
                repository.delete_course_certificate_record(202)
            self.assertIn("PGRST205", str(ctx.exception))


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

    def test_delete_course_certificate_returns_false_when_no_rows_matched(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.return_value = MagicMock(data=[])  # No rows deleted
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            result = repository.delete_course_certificate_record(999)
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

    def test_course_certificate_single_delete_route_success(self):
        with patch.object(repository, "delete_course_certificate_record", return_value=True):
            res = self.client.delete("/api/history/course_certificate/202")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertIn("Audit record deleted successfully", data["message"])

    def test_course_certificate_single_delete_route_not_found(self):
        with patch.object(repository, "delete_course_certificate_record", return_value=False):
            res = self.client.delete("/api/history/course_certificate/999")
            self.assertEqual(res.status_code, 404)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertIn("not found", data["error"].lower())

    def test_course_certificate_single_delete_route_sanitizes_error(self):
        with patch.object(repository, "delete_course_certificate_record", side_effect=RuntimeError("Supabase connection timeout")):
            res = self.client.delete("/api/history/course_certificate/202")
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Failed to delete record. Please check server logs.")
            self.assertNotIn("timeout", data["error"])

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




class UnifiedHistoryAndCounterCalculationsTests(unittest.TestCase):
    def setUp(self):
        import app as application
        self.app_module = application
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    def test_history_route_passes_total_course_certificates_to_template_and_json(self):
        """Verify that total_course_certificates is included in both JSON API and HTML template responses."""
        mock_data = {
            "records": [],
            "all_filtered_records": [],
            "total_records": 10,
            "total_offer_letters": 5,
            "total_ca_letters": 2,
            "total_certificates": 1,
            "total_ca_certificates": 1,
            "total_course_certificates": 1,
            "total_sent": 8,
            "total_failed": 1,
            "total_pending": 1,
            "page": 1,
            "per_page": 25,
            "total_pages": 1,
            "available_months": ["2026-10"],
        }
        with patch.object(repository, "get_unified_history", return_value=mock_data):
            # Test JSON response
            res_json = self.client.get("/history?format=json")
            self.assertEqual(res_json.status_code, 200)
            json_body = res_json.get_json()
            self.assertTrue(json_body["success"])
            self.assertIn("total_course_certificates", json_body)
            self.assertEqual(json_body["total_course_certificates"], 1)

            # Test HTML response
            res_html = self.client.get("/history")
            self.assertEqual(res_html.status_code, 200)
            html_body = res_html.get_data(as_text=True)
            self.assertIn('id="summaryTotalCourseCertificates"', html_body)
            self.assertIn(">1</span>", html_body)

    def test_history_route_passes_total_unknown_to_template_and_json(self):
        """Verify that total_unknown is included in JSON API and passed to the HTML template context."""
        mock_data = {
            "records": [],
            "all_filtered_records": [],
            "total_records": 10,
            "total_offer_letters": 5,
            "total_ca_letters": 2,
            "total_certificates": 1,
            "total_ca_certificates": 1,
            "total_course_certificates": 1,
            "total_sent": 7,
            "total_failed": 1,
            "total_pending": 1,
            "total_unknown": 1,
            "page": 1,
            "per_page": 25,
            "total_pages": 1,
            "available_months": ["2026-10"],
        }
        with patch.object(repository, "get_unified_history", return_value=mock_data):
            # Test JSON response
            res_json = self.client.get("/history?format=json")
            self.assertEqual(res_json.status_code, 200)
            json_body = res_json.get_json()
            self.assertTrue(json_body["success"])
            self.assertIn("total_unknown", json_body)
            self.assertEqual(json_body["total_unknown"], 1)

            # Test HTML template context
            with patch("app.render_template", wraps=self.app_module.render_template) as mock_render:
                res_html = self.client.get("/history")
                self.assertEqual(res_html.status_code, 200)
                mock_render.assert_called_once()
                self.assertIn("total_unknown", mock_render.call_args.kwargs)
                self.assertEqual(mock_render.call_args.kwargs["total_unknown"], 1)

    def test_course_certificate_count_zero_in_html_and_json(self):
        """Verify that total_course_certificates=0 is correctly passed and rendered when count is 0."""
        mock_data = {
            "records": [],
            "all_filtered_records": [],
            "total_records": 5,
            "total_offer_letters": 5,
            "total_ca_letters": 0,
            "total_certificates": 0,
            "total_ca_certificates": 0,
            "total_course_certificates": 0,
            "total_sent": 5,
            "total_failed": 0,
            "total_pending": 0,
            "page": 1,
            "per_page": 25,
            "total_pages": 1,
            "available_months": ["2026-10"],
        }
        with patch.object(repository, "get_unified_history", return_value=mock_data):
            res_json = self.client.get("/history?format=json")
            self.assertEqual(res_json.status_code, 200)
            self.assertEqual(res_json.get_json()["total_course_certificates"], 0)

            res_html = self.client.get("/history")
            self.assertEqual(res_html.status_code, 200)
            self.assertIn('id="summaryTotalCourseCertificates">0</span>', res_html.get_data(as_text=True))

    def test_pagination_across_multiple_pages(self):
        """Verify that _fetch_all_supabase_rows fetches across 3 pages (2500 records) correctly."""
        total_mock_rows = 2500
        fake_db = [{"id": i, "created_at": "2026-10-01T10:00:00Z", "student_name": f"Student {i}"} for i in range(total_mock_rows)]

        class MultiPageQueryMock:
            def __init__(self, items):
                self.items = items
                self._start = 0
                self._end = len(items)

            def order(self, *args, **kwargs):
                return self

            def range(self, start, end):
                self._start = start
                self._end = end
                return self

            def execute(self):
                data = self.items[self._start:self._end + 1]
                return MagicMock(data=data)

        class MultiPageClientMock:
            def __init__(self, items):
                self.items = items

            def table(self, name):
                t = MagicMock()
                t.select.return_value = MultiPageQueryMock(self.items)
                return t

        mock_sb = MultiPageClientMock(fake_db)
        fetched = repository._fetch_all_supabase_rows(mock_sb, "email_history", "created_at", "id")
        self.assertEqual(len(fetched), total_mock_rows)
        self.assertEqual(fetched[0]["id"], 0)
        self.assertEqual(fetched[-1]["id"], 2499)

    def test_pagination_with_identical_timestamps_uses_deterministic_secondary_key(self):
        """Verify that deterministic secondary ordering by id is applied when created_at is identical."""
        orders_called = []

        class OrderedQueryMock:
            def order(self, col, desc=True):
                orders_called.append((col, desc))
                return self

            def range(self, start, end):
                return self

            def execute(self):
                return MagicMock(data=[{"id": 1, "created_at": "2026-10-01T12:00:00Z"}])

        mock_sb = MagicMock()
        mock_table = MagicMock()
        mock_table.select.return_value = OrderedQueryMock()
        mock_sb.table.return_value = mock_table

        repository._fetch_all_supabase_rows(mock_sb, "email_history", order_col="created_at", secondary_col="id")
        self.assertIn(("created_at", True), orders_called)
        self.assertIn(("id", True), orders_called)

    def test_reaching_pagination_safety_limit_raises_runtime_error(self):
        """Verify that exceeding the 500-page safety limit raises an explicit RuntimeError rather than silently returning partial data."""
        class InfiniteQueryMock:
            def order(self, *args, **kwargs):
                return self

            def range(self, start, end):
                return self

            def execute(self):
                # Always returns a full 1000-item page, simulating an infinite stream
                return MagicMock(data=[{"id": 1}] * 1000)

        mock_sb = MagicMock()
        mock_table = MagicMock()
        mock_table.select.return_value = InfiniteQueryMock()
        mock_sb.table.return_value = mock_table

        with patch.object(repository, "_fetch_all_supabase_rows", wraps=repository._fetch_all_supabase_rows):
            with self.assertRaises(RuntimeError) as ctx:
                repository._fetch_all_supabase_rows(mock_sb, "infinite_table", "created_at", "id")
            self.assertIn("Pagination safety limit of 500 pages", str(ctx.exception))
            self.assertIn("Query aborted", str(ctx.exception))

    def test_raw_sending_status_normalizes_to_pending(self):
        """Verify that raw statuses 'sending', 'pending', 'in_transit', 'queued' normalize to 'pending'."""
        self.assertEqual(repository.normalize_email_status("sending"), "pending")
        self.assertEqual(repository.normalize_email_status("SENDING"), "pending")
        self.assertEqual(repository.normalize_email_status("pending"), "pending")
        self.assertEqual(repository.normalize_email_status("in_transit"), "pending")
        self.assertEqual(repository.normalize_email_status("queued"), "pending")

        # Verify in get_unified_history
        mock_ca_certs = [
            {"id": 10, "email_status": "sending", "participant_email": "sending@test.com"},
            {"id": 11, "email_status": "pending", "participant_email": "pending@test.com"},
        ]
        with patch.object(repository, "_fetch_all_raw_offer_letters", return_value=[]),              patch.object(repository, "_fetch_all_raw_campus_ambassador", return_value=[]),              patch.object(repository, "_fetch_all_raw_certificates", return_value=[]),              patch.object(repository, "_fetch_all_raw_ca_certificates", return_value=mock_ca_certs),              patch.object(repository, "_fetch_all_raw_course_certificates", return_value=[]):
            res = repository.get_unified_history(record_type="all")
            self.assertEqual(res["total_pending"], 2)
            self.assertEqual(res["total_sent"], 0)
            self.assertEqual(res["total_failed"], 0)
            self.assertEqual(res["total_records"], 2)

    def test_unknown_status_handling_not_classified_as_pending(self):
        """Verify that unrecognized statuses are categorized as 'unknown' and NEVER classified as pending."""
        self.assertEqual(repository.normalize_email_status("unknown_garbage"), "unknown")
        self.assertEqual(repository.normalize_email_status("corrupted_status"), "unknown")
        self.assertEqual(repository.normalize_email_status(""), "unknown")
        self.assertEqual(repository.normalize_email_status(None), "unknown")

        # In unified history, an unknown status must NOT inflate total_pending or total_sent
        mock_certs = [
            {"id": 20, "email_status": "corrupted_status", "internship_domain": "Python"},
            {"id": 21, "email_status": "sent", "internship_domain": "Java"},
        ]
        with patch.object(repository, "_fetch_all_raw_offer_letters", return_value=[]),              patch.object(repository, "_fetch_all_raw_campus_ambassador", return_value=[]),              patch.object(repository, "_fetch_all_raw_certificates", return_value=mock_certs),              patch.object(repository, "_fetch_all_raw_ca_certificates", return_value=[]),              patch.object(repository, "_fetch_all_raw_course_certificates", return_value=[]):
            res = repository.get_unified_history(record_type="all")
            # total_records includes both: the sent record AND the unknown record.
            self.assertEqual(res["total_records"], 2)
            # Only 1 is sent
            self.assertEqual(res["total_sent"], 1)
            # 0 must be pending because corrupted_status must NOT be classified as pending!
            self.assertEqual(res["total_pending"], 0)
            self.assertEqual(res["total_failed"], 0)
            # The unknown record must be counted in total_unknown
            self.assertEqual(res["total_unknown"], 1)
            # sent + failed + pending + unknown must equal total_records
            self.assertEqual(
                res["total_sent"] + res["total_failed"] + res["total_pending"] + res["total_unknown"],
                res["total_records"],
            )

    def test_unified_history_category_and_delivery_totals_consistency(self):
        """Verify that category subtotals and delivery status subtotals both sum exactly to total_records."""
        mock_raw_offers = [
            {"id": 1, "email_status": "sent", "offer_letter_type": "standard", "internship_domain": "Python"},
            {"id": 2, "email_status": "failed", "offer_letter_type": "standard", "internship_domain": "AI"},
        ]
        mock_raw_cas = [
            {"id": 3, "email_status": "sent", "internship_domain": "Campus Ambassador"},
        ]
        mock_raw_certs = [
            {"id": 4, "email_status": "sent", "internship_domain": "Data Science"},
        ]
        mock_raw_ca_certs = [
            {"id": 5, "email_status": "sent", "participant_email": "ca1@test.com"},
            {"id": 6, "email_status": "sending", "participant_email": "ca2@test.com"},  # maps to pending
        ]
        mock_raw_course_certs = [
            {"id": 7, "email_status": "sent", "course_name": "Web Dev"},
        ]

        with patch.object(repository, "_fetch_all_raw_offer_letters", return_value=mock_raw_offers),              patch.object(repository, "_fetch_all_raw_campus_ambassador", return_value=mock_raw_cas),              patch.object(repository, "_fetch_all_raw_certificates", return_value=mock_raw_certs),              patch.object(repository, "_fetch_all_raw_ca_certificates", return_value=mock_raw_ca_certs),              patch.object(repository, "_fetch_all_raw_course_certificates", return_value=mock_raw_course_certs):

            result = repository.get_unified_history(record_type="all")

            # Category subtotal sum must equal total_records
            cat_sum = (
                result["total_offer_letters"]
                + result["total_ca_letters"]
                + result["total_certificates"]
                + result["total_ca_certificates"]
                + result["total_course_certificates"]
            )
            self.assertEqual(cat_sum, result["total_records"])
            self.assertEqual(result["total_records"], 7)

            # Delivery status sum (sent + failed + pending + unknown) must equal total_records
            self.assertEqual(result["total_unknown"], 0)  # all statuses are recognized here
            status_sum = (
                result["total_sent"] + result["total_failed"]
                + result["total_pending"] + result["total_unknown"]
            )
            self.assertEqual(status_sum, result["total_records"])
            self.assertEqual(result["total_sent"], 5)
            self.assertEqual(result["total_failed"], 1)
            self.assertEqual(result["total_pending"], 1)

            # Check workflow stats breakdowns
            stats = repository.get_all_workflow_stats()
            self.assertTrue(stats["success"])
            self.assertEqual(stats["overall"]["total"], 7)
            self.assertEqual(stats["overall"]["sent"], 5)
            self.assertEqual(stats["overall"]["failed"], 1)
            self.assertEqual(stats["overall"]["pending"], 1)
            self.assertEqual(stats["overall"]["unknown"], 0)
            self.assertEqual(stats["offer_letter"]["total"], 2)
            self.assertEqual(stats["offer_letter"]["sent"], 1)
            self.assertEqual(stats["offer_letter"]["failed"], 1)
            self.assertEqual(stats["ca_certificate"]["pending"], 1)
            self.assertEqual(stats["ca_certificate"]["unknown"], 0)



    def test_unknown_status_preserved_in_all_five_workflows(self):
        """Records with unrecognized email statuses must be preserved (not silently dropped)
        in all five document workflows, counted in total_records and total_unknown,
        and excluded from total_sent, total_failed, and total_pending."""
        # One unknown-status record per workflow
        mock_raw_offers = [
            {"id": 1, "email_status": "bad_status", "offer_letter_type": "standard", "internship_domain": "Python"},
        ]
        mock_raw_cas = [
            {"id": 2, "email_status": "garbage", "internship_domain": "Campus Ambassador"},
        ]
        mock_raw_certs = [
            {"id": 3, "email_status": "", "internship_domain": "Data Science"},
        ]
        mock_raw_ca_certs = [
            {"id": 4, "email_status": None, "participant_email": "ca@test.com"},
        ]
        mock_raw_course_certs = [
            {"id": 5, "email_status": "not_a_status", "course_name": "Django"},
        ]

        with patch.object(repository, "_fetch_all_raw_offer_letters", return_value=mock_raw_offers), \
             patch.object(repository, "_fetch_all_raw_campus_ambassador", return_value=mock_raw_cas), \
             patch.object(repository, "_fetch_all_raw_certificates", return_value=mock_raw_certs), \
             patch.object(repository, "_fetch_all_raw_ca_certificates", return_value=mock_raw_ca_certs), \
             patch.object(repository, "_fetch_all_raw_course_certificates", return_value=mock_raw_course_certs):

            res = repository.get_unified_history(record_type="all")

            # All 5 records must be present — none silently dropped
            self.assertEqual(res["total_records"], 5)
            self.assertEqual(res["total_offer_letters"], 1)
            self.assertEqual(res["total_ca_letters"], 1)
            self.assertEqual(res["total_certificates"], 1)
            self.assertEqual(res["total_ca_certificates"], 1)
            self.assertEqual(res["total_course_certificates"], 1)

            # None should be classified as sent, failed, or pending
            self.assertEqual(res["total_sent"], 0)
            self.assertEqual(res["total_failed"], 0)
            self.assertEqual(res["total_pending"], 0)
            self.assertEqual(res["total_unknown"], 5)

            # Full reconciliation: sent + failed + pending + unknown == total_records
            self.assertEqual(
                res["total_sent"] + res["total_failed"] + res["total_pending"] + res["total_unknown"],
                res["total_records"],
            )

            # Check per-record email_status is stored as "unknown"
            for rec in res["records"]:
                self.assertEqual(rec["email_status"], "unknown", f"Record {rec['id']} not marked unknown")

    def test_unknown_status_counted_in_workflow_stats_not_in_sent_failed_pending(self):
        """get_all_workflow_stats must report unknown counts per workflow and in overall,
        and unknown records must not inflate sent, failed, or pending."""
        mock_raw_offers = [
            {"id": 1, "email_status": "sent", "offer_letter_type": "standard", "internship_domain": "AI"},
            {"id": 2, "email_status": "corrupted", "offer_letter_type": "standard", "internship_domain": "ML"},
        ]
        mock_raw_course_certs = [
            {"id": 3, "email_status": "unknown_val", "course_name": "Python"},
            {"id": 4, "email_status": "sent", "course_name": "Java"},
        ]

        with patch.object(repository, "_fetch_all_raw_offer_letters", return_value=mock_raw_offers), \
             patch.object(repository, "_fetch_all_raw_campus_ambassador", return_value=[]), \
             patch.object(repository, "_fetch_all_raw_certificates", return_value=[]), \
             patch.object(repository, "_fetch_all_raw_ca_certificates", return_value=[]), \
             patch.object(repository, "_fetch_all_raw_course_certificates", return_value=mock_raw_course_certs):

            stats = repository.get_all_workflow_stats()

            # Overall
            self.assertEqual(stats["overall"]["total"], 4)
            self.assertEqual(stats["overall"]["sent"], 2)
            self.assertEqual(stats["overall"]["failed"], 0)
            self.assertEqual(stats["overall"]["pending"], 0)
            self.assertEqual(stats["overall"]["unknown"], 2)

            # Per-workflow offer_letter
            self.assertEqual(stats["offer_letter"]["total"], 2)
            self.assertEqual(stats["offer_letter"]["sent"], 1)
            self.assertEqual(stats["offer_letter"]["unknown"], 1)
            self.assertEqual(stats["offer_letter"]["failed"], 0)

            # Per-workflow course_certificate
            self.assertEqual(stats["course_certificate"]["total"], 2)
            self.assertEqual(stats["course_certificate"]["sent"], 1)
            self.assertEqual(stats["course_certificate"]["unknown"], 1)
            self.assertEqual(stats["course_certificate"]["failed"], 0)

    def test_full_reconciliation_with_mixed_statuses_across_all_workflows(self):
        """sent + failed + pending + unknown must equal total_records for any mixture
        of statuses across all five workflows."""
        mock_raw_offers = [
            {"id": 1, "email_status": "sent", "offer_letter_type": "standard", "internship_domain": "AI"},
            {"id": 2, "email_status": "failed", "offer_letter_type": "standard", "internship_domain": "ML"},
            {"id": 3, "email_status": "unknown_raw", "offer_letter_type": "standard", "internship_domain": "DS"},
        ]
        mock_raw_cas = [
            {"id": 4, "email_status": "pending", "internship_domain": "Campus Ambassador"},
            {"id": 5, "email_status": "junk", "internship_domain": "Campus Ambassador"},
        ]
        mock_raw_certs = [
            {"id": 6, "email_status": "uncertain", "internship_domain": "Web"},
            {"id": 7, "email_status": "", "internship_domain": "Cloud"},
        ]
        mock_raw_ca_certs = [
            {"id": 8, "email_status": "sent", "participant_email": "a@test.com"},
            {"id": 9, "email_status": "sending", "participant_email": "b@test.com"},  # -> pending
        ]
        mock_raw_course_certs = [
            {"id": 10, "email_status": "sent", "course_name": "Flask"},
            {"id": 11, "email_status": "queued", "course_name": "Django"},  # -> pending
            {"id": 12, "email_status": "bad", "course_name": "FastAPI"},
        ]

        with patch.object(repository, "_fetch_all_raw_offer_letters", return_value=mock_raw_offers), \
             patch.object(repository, "_fetch_all_raw_campus_ambassador", return_value=mock_raw_cas), \
             patch.object(repository, "_fetch_all_raw_certificates", return_value=mock_raw_certs), \
             patch.object(repository, "_fetch_all_raw_ca_certificates", return_value=mock_raw_ca_certs), \
             patch.object(repository, "_fetch_all_raw_course_certificates", return_value=mock_raw_course_certs):

            res = repository.get_unified_history(record_type="all")

            # total_records must equal all 12 raw inputs
            self.assertEqual(res["total_records"], 12)

            # Category subtotals must add up
            cat_sum = (
                res["total_offer_letters"] + res["total_ca_letters"]
                + res["total_certificates"] + res["total_ca_certificates"]
                + res["total_course_certificates"]
            )
            self.assertEqual(cat_sum, 12)

            # Delivery totals:
            #   sent=3   (id1 sent, id8 sent, id10 sent)
            #   failed=2 (id2 failed, id6 uncertain — uncertain counts as failed)
            #   pending=3 (id4 pending, id9 sending->pending, id11 queued->pending)
            #   unknown=4 (id3 unknown_raw, id5 junk, id7 empty, id12 bad)
            self.assertEqual(res["total_sent"], 3)
            self.assertEqual(res["total_failed"], 2)   # id2 failed + id6 uncertain
            self.assertEqual(res["total_pending"], 3)  # id4 pending, id9 sending, id11 queued
            self.assertEqual(res["total_unknown"], 4)  # id3, id5, id7, id12

            # Full reconciliation
            self.assertEqual(
                res["total_sent"] + res["total_failed"] + res["total_pending"] + res["total_unknown"],
                res["total_records"],
            )

if __name__ == "__main__":
    unittest.main()
