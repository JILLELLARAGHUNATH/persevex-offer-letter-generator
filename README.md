# Persevex Offer Letter & Certificate Generator

A robust, production-grade Flask web application designed for generating, managing, verifying, and email-delivering Persevex student, intern, and campus ambassador documents.

The application overlays dynamic recipient details onto master PDF templates using PyMuPDF, provides real-time PDF previews, automates secure SMTP email delivery, manages unified delivery history in Supabase (with resilient local SQLite fallback), and provides public verification pages for issued certificates.

---

## Features & Capabilities

- **Four Core Issuance Workflows**:
  - **Internship Offer Letter**: Support for "With Hours" and "Without Hours", predefined domains or custom domain entry via searchable combobox, duration, stipend, and automated hour calculations.
  - **Campus Ambassador Offer Letter**: Customized appointment letters with candidate tenure and ambassador roles.
  - **Internship Certificate**: Issue completion certificates embedded with scannable QR verification codes.
  - **Campus Ambassador Certificate**: Issue ambassador recognition certificates with verification QR codes.
- **Bulk Processing & Job Management**:
  - Spreadsheet parsing for `.xlsx`, `.xls`, and `.csv` files with intelligent header mapping.
  - Candidate pre-validation, duplicate detection against existing database history, and invalid row isolation.
  - Cooperative background job management (`BulkJobManager`) with live progress counters, active job polling, and safe mid-flight cancellation.
  - Targeted delivery controls: **Send New Mails Only** (skips previously sent candidates) vs. **Send All Mails** (explicit resend).
- **Delivery Safety & Idempotency**:
  - `send_count` tracking and status lifecycle (`SENT`, `FAILED`, `UNCERTAIN`, `PENDING`).
  - Automatic rollback on transient SMTP failures to prevent false delivery claims.
  - Protection against concurrent duplicate dispatches.
- **Unified History & Audit**:
  - Filterable, searchable audit table covering all four document categories.
  - Per-record and batch deletion.
  - Export audit logs to Microsoft Excel (`.xlsx` formatted with headers) and CSV.
- **Public Certificate Verification Portal**:
  - QR code link to `/verify/<certificate_id>` rendering live certificate preview, high-resolution image thumbnail, and PDF download.
- **Resilient Multi-Database Architecture**:
  - Primary cloud persistence via Supabase (PostgreSQL).
  - Automatic fallback to local SQLite when running offline or in restricted environments.
  - Read-only filesystem resilience for serverless runtimes.
- **Responsive Modern UI**:
  - Fluid clamp-based responsive layout optimized from mobile screens (360px) to ultra-wide displays.
  - Balanced side-by-side forms and live PDF preview.

---

## Technology Stack

- **Backend**: Python 3.12+, Flask 3.1.3
- **PDF Processing**: PyMuPDF (`fitz` 1.28.2)
- **Database & Storage**: Supabase (`supabase-py` 2.31.0, PostgreSQL) with local SQLite3 fallback
- **Spreadsheet & Data**: `openpyxl`, `xlrd`, standard `csv`
- **QR Codes & Imaging**: `qrcode`, Pillow (`PIL`)
- **Email & IMAP**: Python `smtplib` (SSL/TLS), `email.message`, `imaplib` (Sent folder sync)
- **Frontend**: Semantic HTML5, Vanilla CSS (CSS Grid, Flexbox, Fluid Clamp Typography), Vanilla JavaScript (Fetch API, asynchronous polling)
- **Deployment**: Vercel Serverless Functions (`@vercel/python`)
- **Testing**: Pytest 9.x

---

## Major Workflows

### 1. Internship Offer Letter
- **Purpose**: Issues official internship offers.
- **Inputs**: Student Name, Email, Domain (23 predefined tracks or custom entry), Duration (2–6 months), Start Date, End Date, optional Stipend, and Hours breakdown.
- **PDF Engine**: Overlays dynamic text on `pdf_templates/with_hours.pdf` or `pdf_templates/without_hours.pdf`.
- **Modes**: Single issuance with live PDF preview and bulk batch processing via spreadsheet upload.

### 2. Campus Ambassador Offer Letter
- **Purpose**: Appoints campus representatives and ambassadors.
- **Inputs**: Ambassador Name, Email, College/University, Start Date, End Date.
- **PDF Engine**: Overlays dynamic text on `pdf_templates/campus_ambassador_template.pdf`.
- **Modes**: Single issuance and bulk upload with candidate duplicate check.

### 3. Internship Certificate
- **Purpose**: Awards verified completion certificates.
- **Inputs**: Student Name, Email, Domain, Duration/Track, Start Date, End Date, Issue Date.
- **Security**: Embeds a unique certificate ID and QR code linking directly to the public verification portal.
- **Modes**: Single generation and bulk batch processing.

### 4. Campus Ambassador Certificate
- **Purpose**: Awards verified recognition certificates for campus ambassadors.
- **Inputs**: Ambassador Name, Email, Tenured Dates, Issue Date.
- **Security**: Unique ID and QR code verification overlay on `pdf_templates/campus_ambassador_certificate.pdf`.
- **Modes**: Single generation and asynchronous bulk job execution.

### 5. Unified History & Audit
- **Purpose**: Centralized dashboard to view, audit, and manage all issued documents.
- **Capabilities**: Filter by document type, search by name/email/ID, resend emails (with `send_count` increment), single or bulk record deletion, and export to Excel/CSV.

---

## User-Facing Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | `GET` | Internship Offer Letter generator & bulk dashboard |
| `/campus-ambassador` | `GET` | Campus Ambassador workflow menu |
| `/campus-ambassador/single` | `GET` | Single CA Offer Letter generator |
| `/campus-ambassador/bulk` | `GET` | Bulk CA Offer Letter spreadsheet processor |
| `/certificate` | `GET` | Internship Certificate generator & bulk processor |
| `/ca-certificate` | `GET` | Single CA Certificate generator |
| `/ca-certificate/bulk` | `GET` | Bulk CA Certificate asynchronous processor |
| `/history` | `GET` | Unified history, audit logs, and export dashboard |
| `/verify/<certificate_id>` | `GET` | Public certificate verification portal |
| `/login` | `GET`, `POST` | Admin authentication |
| `/logout` | `GET` | Clear session and log out |
| `/history/export` | `GET` | Export audit records to `.xlsx` or `.csv` |
| `/generated/<filename>` | `GET` | Preview/download temporary generated PDFs |

---

## Project Structure

```text
persevex-offer-letter-generator/
├── app.py                     # Main Flask application and route controllers
├── certificate_service.py     # PDF drawing, QR generation, and email dispatch
├── environment_config.py      # Environment resolution (dev vs. prod vs. serverless)
├── requirements.txt           # Production Python dependencies
├── vercel.json                # Vercel serverless deployment specification
├── pytest.ini                 # Pytest configuration (targets tests/ directory)
├── api/
│   └── index.py               # Vercel serverless entry point
├── database/
│   ├── config.py              # Database configuration and connection credentials
│   ├── models.py              # Data structures and schemas
│   └── repository.py          # Unified data access layer (Supabase + SQLite fallback)
├── services/
│   ├── bulk_job_manager.py    # In-memory bulk job coordinator and cancellation handler
│   ├── bulk_offer_letter_service.py # Bulk processing for internship offer letters
│   ├── bulk_certificate_service.py  # Bulk processing for internship certificates
│   ├── ca_bulk_service.py     # Bulk processing for CA offer letters
│   └── ca_certificate_service.py    # Bulk processing for CA certificates
├── pdf_templates/             # Master PDF templates
│   ├── with_hours.pdf
│   ├── without_hours.pdf
│   ├── certificate_template.pdf
│   └── campus_ambassador_certificate.pdf
├── templates/                 # Jinja2 HTML templates
│   ├── index.html             # Offer letter page
│   ├── campus_ambassador.html # Single CA letter page
│   ├── campus_ambassador_bulk.html # Bulk CA letter page
│   ├── certificate.html       # Internship certificate page
│   ├── ca_certificate.html    # Single CA certificate page
│   ├── ca_certificate_bulk.html # Bulk CA certificate page
│   ├── history.html           # Unified history page
│   └── verify.html            # Public certificate verification page
├── static/                    # CSS, images, and fonts
│   ├── style.css              # Main responsive styling
│   ├── persevex-icon.png      # Brand assets
│   └── fonts/                 # Embedded TTF fonts
├── migrations/                # SQL schema migrations for Supabase (PostgreSQL)
└── tests/                     # Automated regression test suite
    ├── test_workflow_regressions.py
    └── test_history_synchronization.py
```

---

## Setup & Local Development

### Prerequisites
- Python 3.12+
- Git

### 1. Clone the repository
```bash
git clone https://github.com/JILLELLARAGHUNATH/persevex-offer-letter-generator.git
cd persevex-offer-letter-generator
```

### 2. Set up virtual environment
```powershell
python -m venv venv
.\venv\Scripts\activate
```

### 3. Install dependencies
```powershell
pip install -r requirements.txt
```

### 4. Configure environment variables
Create a `.env.development` file in the project root:

```env
PERSEVEX_ENV=development
PERSEVEX_SESSION_SECRET=your-secure-session-secret
ADMIN_PASSWORD=your-admin-password

# Email / SMTP Configuration
SENDER_EMAIL=your-email@persevex.com
SENDER_PASSWORD=your-email-password

# Database (Supabase) - Optional for local dev (falls back to local SQLite)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-service-role-or-anon-key
```

### 5. Run the application
```powershell
python app.py
```
Open your browser at `http://127.0.0.1:5000`.

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `PERSEVEX_ENV` | Yes | Runtime mode: `development`, `production`, or `test`. |
| `PERSEVEX_SESSION_SECRET` | Recommended | Secret key for signing Flask user sessions. |
| `ADMIN_PASSWORD` | Recommended | Password used for admin dashboard access. |
| `SENDER_EMAIL` | For Email | SMTP username / sender address. |
| `SENDER_PASSWORD` | For Email | SMTP password or app password. |
| `SUPABASE_URL` | Optional | Supabase project API URL. If omitted, local SQLite is used. |
| `SUPABASE_KEY` | Optional | Supabase service or anon API key. |
| `PERSEVEX_SQLITE_DB_PATH` | Optional | Custom path for SQLite database file (defaults to `certificates_local.db`). |

---

## Running Tests

The test suite covers workflow regressions, delivery safety, cancellation, duplicate isolation, and history synchronization.

Run all tests using the project virtual environment:

```powershell
.\venv\Scripts\python.exe -m pytest -v
```

`pytest.ini` automatically directs discovery to the `tests/` directory.

---

## Deployment (Vercel)

The application is configured for deployment as a Vercel Serverless Function via `vercel.json`:

```json
{
  "version": 2,
  "builds": [
    {"src": "api/index.py", "use": "@vercel/python"}
  ],
  "routes": [
    {"src": "/(.*)", "dest": "api/index.py"}
  ]
}
```

### Serverless Considerations:
- **Filesystem**: Serverless runtimes have read-only filesystems (with temporary `/tmp` storage). All file writing operations gracefully fallback to `/tmp` or memory (`io.BytesIO`).
- **Database**: Production deployment requires configuring `SUPABASE_URL` and `SUPABASE_KEY` in Vercel environment variables to maintain persistence across serverless invocations.
- **Environment**: Set `PERSEVEX_ENV=production` in the Vercel dashboard.
