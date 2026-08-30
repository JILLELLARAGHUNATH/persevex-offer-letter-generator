# Persevex Offer Letter Generator

A simple local Flask application that edits the original Persevex PDF templates instead of recreating them.

## Features

- With Hours / Without Hours
- Student details form
- Uses the original PDF as the master document
- Changes only dynamic text
- Actual PDF preview
- Download
- Send the same PDF by Gmail SMTP
- No login
- No database
- No admin panel

## Windows setup

Open Command Prompt/PowerShell in this folder:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open:

http://127.0.0.1:5000

## Email testing

Edit the sender settings near the top of `app.py`:

```python
SENDER_EMAIL = "YOUR_GMAIL@gmail.com"
SENDER_APP_PASSWORD = "YOUR_GMAIL_APP_PASSWORD"
```

Use a Gmail App Password, not your normal Gmail password.

Do not commit a real App Password to GitHub.

## Master templates

The exact uploaded original PDFs are stored in:

- `pdf_templates/with_hours.pdf`
- `pdf_templates/without_hours.pdf`
