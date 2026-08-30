from pathlib import Path
import re
import smtplib
import os
import tempfile
from datetime import datetime
from email.message import EmailMessage

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_from_directory,
    redirect,
    url_for,
    session,
)
import hmac

import pymupdf


# ============================================================
# APPLICATION
# ============================================================

app = Flask(__name__)

# ============================================================
# LOGIN / SESSION CONFIGURATION
# ============================================================
# Set these in Vercel Environment Variables. For local testing,
# you can set them in PowerShell before starting the app.
LOGIN_USERNAME = os.getenv("PERSEVEX_LOGIN_USERNAME", "").strip()
LOGIN_PASSWORD = os.getenv("PERSEVEX_LOGIN_PASSWORD", "").strip()
SESSION_SECRET = os.getenv("PERSEVEX_SESSION_SECRET", "").strip()

# A secret is required for secure sessions.
if not SESSION_SECRET:
    SESSION_SECRET = "local-development-secret-change-me"

app.secret_key = SESSION_SECRET
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# HTTPS is used on Vercel. Keep local HTTP development working too.
app.config["SESSION_COOKIE_SECURE"] = False



# ============================================================
# DIRECTORIES
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

TEMPLATE_DIR = BASE_DIR / "pdf_templates"
GENERATED_DIR = Path(tempfile.gettempdir()) / "persevex_generated"
FONT_DIR = BASE_DIR / "static" / "fonts"

GENERATED_DIR.mkdir(
    exist_ok=True
)

FONT_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# EMAIL CONFIGURATION
# ============================================================

SENDER_EMAIL = os.getenv("SENDER_EMAIL", "").strip()
SENDER_APP_PASSWORD = os.getenv("PERSEVEX_GMAIL_APP_PASSWORD", "").strip()

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


# ============================================================
# DURATION
#
# 1 MONTH IS REMOVED
# ============================================================

DURATION_WEEKS = {
    "2 months": 8,
    "3 months": 12,
    "4 months": 16,
    "5 months": 20,
    "6 months": 24,
}


# ============================================================
# BASIC HELPERS
# ============================================================

def safe_filename(name):

    name = re.sub(
        r"[^\w\s-]",
        "",
        str(name),
        flags=re.UNICODE,
    )

    name = name.strip()

    name = re.sub(
        r"\s+",
        "_",
        name,
    )

    return name or "Student"


def normalize_text(value):

    value = str(value)

    value = value.replace(
        "\u00a0",
        " ",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip().lower()


def clean_stipend(value):

    if value is None:
        return ""

    value = str(value).strip()

    value = value.replace(
        "₹",
        "",
    )

    value = value.replace(
        ",",
        "",
    )

    return value.strip()


def format_date(value):

    return datetime.strptime(
        value,
        "%Y-%m-%d",
    ).strftime(
        "%d/%m/%Y"
    )


def normalize_duration(value):

    value = str(value).strip()

    if not value:
        return ""

    if re.fullmatch(
        r"\d+",
        value,
    ):

        return f"{value} months"

    return value


# ============================================================
# FONT EXTRACTION
# ============================================================

def extract_font(
    pdf_path,
    font_name,
):

    output = (
        FONT_DIR
        /
        f"{font_name}.ttf"
    )

    if output.exists():
        return str(output)

    doc = pymupdf.open(
        pdf_path
    )

    try:

        fonts = doc[0].get_fonts(
            full=True
        )

        for font in fonts:

            pdf_font_name = font[3]

            if (
                pdf_font_name == font_name
                or
                pdf_font_name.endswith(
                    "+" + font_name
                )
            ):

                extracted = doc.extract_font(
                    font[0]
                )

                if not extracted:
                    continue

                if (
                    len(extracted) >= 4
                    and
                    extracted[1].lower()
                    in (
                        "ttf",
                        "otf",
                    )
                ):

                    output.write_bytes(
                        extracted[3]
                    )

                    return str(output)

    finally:

        doc.close()

    raise RuntimeError(
        f"Could not extract "
        f"{font_name} from "
        f"{pdf_path.name}"
    )


# ============================================================
# FIND WINDOWS UNICODE FONT SUPPORTING ₹
# ============================================================

def find_rupee_font():
    """Return a bundled font that supports the Indian Rupee symbol.

    Vercel runs on Linux, so do not depend on C:\\Windows\\Fonts.
    The project already contains NotoSans-Regular.ttf.
    """
    bundled = FONT_DIR / "NotoSans-Regular.ttf"
    if bundled.exists():
        try:
            font = pymupdf.Font(fontfile=str(bundled))
            if font.has_glyph(ord("₹")):
                return str(bundled)
        except Exception:
            pass

    candidates = [
        FONT_DIR / "OpenSans-Regular.ttf",
        FONT_DIR / "NotoSans-Regular.ttf",
    ]
    for font_path in candidates:
        if not font_path.exists():
            continue
        try:
            font = pymupdf.Font(fontfile=str(font_path))
            if font.has_glyph(ord("₹")):
                return str(font_path)
        except Exception:
            continue

    raise RuntimeError("No bundled font supporting the ₹ symbol was found.")

# ============================================================
# GET TEXT LINES
# ============================================================

def get_text_lines(page):

    result = []

    blocks = page.get_text(
        "dict"
    ).get(
        "blocks",
        []
    )

    for block in blocks:

        if block.get(
            "type"
        ) != 0:

            continue

        for line in block.get(
            "lines",
            []
        ):

            spans = line.get(
                "spans",
                []
            )

            if not spans:
                continue

            text = "".join(
                span.get(
                    "text",
                    "",
                )
                for span in spans
            )

            if not text.strip():
                continue

            first = spans[0]

            result.append({

                "text": text,

                "bbox": pymupdf.Rect(
                    line["bbox"]
                ),

                "origin": first[
                    "origin"
                ],

                "font": first[
                    "font"
                ],

                "size": first[
                    "size"
                ],

                "color": first[
                    "color"
                ],

            })

    return result


# ============================================================
# FIND LINE
# ============================================================

def find_line(
    page,
    search_text,
):

    target = normalize_text(
        search_text
    )

    lines = get_text_lines(
        page
    )

    # Exact search.
    for line in lines:

        current = normalize_text(
            line["text"]
        )

        if target in current:

            return line

    # Word-based fallback.
    target_words = [
        word
        for word in re.findall(
            r"[a-zA-Z0-9]+",
            target,
        )
        if len(word) > 3
    ]

    for line in lines:

        current = normalize_text(
            line["text"]
        )

        matches = sum(
            1
            for word in target_words
            if word in current
        )

        if matches >= min(
            3,
            len(target_words),
        ):

            return line

    raise ValueError(
        f"Could not find PDF line: "
        f"{search_text}"
    )


def find_line_any(
    page,
    options,
):

    for option in options:

        try:

            return find_line(
                page,
                option,
            )

        except ValueError:

            pass

    raise ValueError(
        "Could not find PDF line: "
        +
        " / ".join(options)
    )


# ============================================================
# COLOR
# ============================================================

def rgb_from_pdf_color(
    color_value,
):

    r = (
        color_value >> 16
    ) & 255

    g = (
        color_value >> 8
    ) & 255

    b = color_value & 255

    return (
        r / 255,
        g / 255,
        b / 255,
    )


# ============================================================
# FIT FONT SIZE
# ============================================================

def fit_font_size(
    font_file,
    text,
    original_size,
    max_width,
):

    font = pymupdf.Font(
        fontfile=font_file
    )

    size = float(
        original_size
    )

    while size > 7:

        width = font.text_length(
            text,
            fontsize=size,
        )

        if width <= max_width:
            break

        size -= 0.1

    return size


# ============================================================
# REPLACE COMPLETE LINE
# ============================================================

def replace_entire_line(
    page,
    line,
    new_text,
    font_file,
):

    rect = pymupdf.Rect(
        line["bbox"]
    )

    original_size = float(
        line["size"]
    )

    color = rgb_from_pdf_color(
        line["color"]
    )

    fontsize = fit_font_size(
        font_file,
        new_text,
        original_size,
        rect.width,
    )

    page.add_redact_annot(
        rect,
        fill=(1, 1, 1),
    )

    page.apply_redactions()

    page.insert_text(
        (
            line["origin"][0],
            line["origin"][1],
        ),
        new_text,
        fontfile=font_file,
        fontsize=fontsize,
        color=color,
        overlay=True,
    )


# ============================================================
# GET PDF WORDS
# ============================================================

def get_pdf_words(page):

    words = page.get_text(
        "words"
    )

    words.sort(
        key=lambda item: (
            item[5],
            item[6],
            item[7],
        )
    )

    return words


# ============================================================
# FIND STIPEND SENTENCE
# ============================================================

def find_stipend_sentence_words(
    page,
):

    words = get_pdf_words(
        page
    )

    if not words:
        return None

    cleaned = [
        normalize_text(
            word[4]
        )
        for word in words
    ]

    # --------------------------------------------------------
    # Find "The internship"
    # --------------------------------------------------------

    start_index = None

    for i in range(
        len(cleaned) - 1
    ):

        if (
            cleaned[i] == "the"
            and
            cleaned[i + 1]
            == "internship"
        ):

            nearby = " ".join(
                cleaned[
                    i:
                    min(
                        i + 25,
                        len(cleaned),
                    )
                ]
            )

            if (
                "stipend" in nearby
                or
                "performance-based"
                in nearby
                or
                "performance"
                in nearby
            ):

                start_index = i
                break

    # --------------------------------------------------------
    # Fallback
    # --------------------------------------------------------

    if start_index is None:

        for i in range(
            len(cleaned)
        ):

            nearby = " ".join(
                cleaned[
                    i:
                    min(
                        i + 20,
                        len(cleaned),
                    )
                ]
            )

            if (
                "performance-based"
                in nearby
                and
                "stipend"
                in nearby
            ):

                start_index = i

                for j in range(
                    max(
                        0,
                        i - 8,
                    ),
                    i,
                ):

                    if (
                        cleaned[j]
                        == "the"
                        and
                        j + 1
                        <
                        len(cleaned)
                        and
                        cleaned[j + 1]
                        == "internship"
                    ):

                        start_index = j
                        break

                break

    if start_index is None:
        return None

    # --------------------------------------------------------
    # Find stipend
    # --------------------------------------------------------

    stipend_index = None

    for i in range(
        start_index,
        min(
            start_index + 25,
            len(cleaned),
        ),
    ):

        if "stipend" in cleaned[i]:

            stipend_index = i
            break

    if stipend_index is None:
        return None

    # --------------------------------------------------------
    # Find "to"
    # --------------------------------------------------------

    amount_start = None

    for i in range(
        stipend_index,
        min(
            stipend_index + 15,
            len(cleaned),
        ),
    ):

        if cleaned[i] == "to":

            amount_start = i + 1
            break

    # --------------------------------------------------------
    # Find amount
    # --------------------------------------------------------

    amount_end = None

    if amount_start is not None:

        for i in range(
            amount_start,
            min(
                amount_start + 8,
                len(cleaned),
            ),
        ):

            raw = str(
                words[i][4]
            )

            value = cleaned[i]

            if "₹" in raw:

                if re.search(
                    r"\d",
                    raw,
                ):

                    amount_end = i
                    break

            if re.fullmatch(
                r"[\d,]+\.?",
                value,
            ):

                amount_end = i
                break

            if re.fullmatch(
                r"\d+\.?",
                value,
            ):

                amount_end = i
                break

    # --------------------------------------------------------
    # Find period
    # --------------------------------------------------------

    if amount_end is None:

        for i in range(
            stipend_index,
            min(
                stipend_index + 25,
                len(words),
            ),
        ):

            raw = str(
                words[i][4]
            )

            if (
                raw.endswith(".")
                or
                cleaned[i] == "."
            ):

                amount_end = i
                break

    # --------------------------------------------------------
    # Fallback
    # --------------------------------------------------------

    if amount_end is None:

        start_block = words[
            start_index
        ][5]

        amount_end = stipend_index

        for i in range(
            stipend_index + 1,
            len(words),
        ):

            current = words[i]

            if current[5] != start_block:
                break

            amount_end = i

            raw = str(
                current[4]
            )

            if raw.endswith("."):
                break

    if amount_end < start_index:
        amount_end = start_index

    return (
        start_index,
        amount_end,
        words,
    )


# ============================================================
# REMOVE COMPLETE STIPEND SENTENCE
# ============================================================

def remove_stipend_sentence(
    page,
):

    result = find_stipend_sentence_words(
        page
    )

    if not result:

        print(
            "STIPEND REMOVAL: "
            "Stipend sentence not found."
        )

        return

    start_index, end_index, words = result

    selected = words[
        start_index:
        end_index + 1
    ]

    if not selected:
        return

    line_groups = {}

    for word in selected:

        key = (
            word[5],
            word[6],
        )

        if key not in line_groups:

            line_groups[key] = []

        line_groups[key].append(
            word
        )

    rectangles = []

    for key, line_words in line_groups.items():

        if not line_words:
            continue

        x0 = min(
            word[0]
            for word in line_words
        )

        y0 = min(
            word[1]
            for word in line_words
        )

        x1 = max(
            word[2]
            for word in line_words
        )

        y1 = max(
            word[3]
            for word in line_words
        )

        rect = pymupdf.Rect(
            x0 - 2,
            y0 - 1,
            x1 + 2,
            y1 + 1,
        )

        rectangles.append(
            rect
        )

    for rect in rectangles:

        page.add_redact_annot(
            rect,
            fill=(1, 1, 1),
        )

    page.apply_redactions()

    print(
        "STIPEND REMOVAL: "
        "Complete stipend sentence removed."
    )


# ============================================================
# FIND STIPEND LINE
# ============================================================

def find_stipend_line(
    page,
):

    options = [
        "The internship offers a performance-based stipend",
        "internship offers a performance-based stipend",
        "offers a performance-based stipend",
        "performance-based stipend",
        "stipend of up to",
    ]

    return find_line_any(
        page,
        options,
    )


# ============================================================
# REPLACE STIPEND
#
# THIS VERSION FIXES ₹.
# ============================================================

def replace_stipend(
    page,
    template,
    stipend,
):
    """
    Replace ONLY the numeric stipend amount.

    The template already contains the ₹ symbol in its own
    NotoSans span. We deliberately KEEP that original ₹ glyph
    instead of drawing a new one. This prevents PyMuPDF from
    falling back to Helvetica and displaying a missing-glyph dot.
    """

    stipend = clean_stipend(stipend)

    if not stipend:
        return

    # --------------------------------------------------------
    # Find the stipend line.
    # --------------------------------------------------------

    stipend_line = None

    blocks = page.get_text("dict").get("blocks", [])

    for block in blocks:
        if block.get("type") != 0:
            continue

        for line_data in block.get("lines", []):
            spans = line_data.get("spans", [])
            if not spans:
                continue

            line_text = "".join(
                str(span.get("text", ""))
                for span in spans
            )

            normalized = normalize_text(line_text)

            if (
                "stipend" in normalized
                and "performance" in normalized
            ):
                stipend_line = line_data
                break

        if stipend_line is not None:
            break

    if stipend_line is None:
        raise ValueError(
            "Could not find the stipend line in the PDF template."
        )

    spans = stipend_line.get("spans", [])

    # --------------------------------------------------------
    # IMPORTANT FIX:
    #
    # Keep the original ₹ span untouched.
    # Find only the numeric amount span and replace it.
    #
    # Template structure:
    #   ... "to "       OpenSans-Regular
    #   "₹"             NotoSans-Regular   <-- KEEP THIS
    #   "15,000."       OpenSans-Regular   <-- REPLACE THIS
    # --------------------------------------------------------

    rupee_span = None
    number_span = None

    for index, span in enumerate(spans):
        raw = str(span.get("text", ""))

        if "₹" in raw:
            rupee_span = span

            # Handle the unlikely case where ₹ and the number
            # are contained in the same span.
            if re.search(r"\d", raw):
                number_span = span
                break

            # Normally the numeric span is immediately after ₹.
            for next_span in spans[index + 1:]:
                next_raw = str(next_span.get("text", ""))
                next_clean = normalize_text(next_raw)

                if re.fullmatch(r"[\d,]+\.?", next_clean):
                    number_span = next_span
                    break

            break

    # Fallback if the template has no separate ₹ span.
    if number_span is None:
        for span in spans:
            raw = str(span.get("text", ""))
            cleaned = normalize_text(raw)

            if re.fullmatch(r"[\d,]+\.?", cleaned):
                number_span = span
                break

    if number_span is None:
        raise ValueError(
            "Could not find the original stipend amount in the PDF template."
        )

    # --------------------------------------------------------
    # Preserve the original numeric font, size and color.
    # --------------------------------------------------------

    original_size = float(
        number_span.get(
            "size",
            12,
        )
    )

    original_color = rgb_from_pdf_color(
        number_span.get(
            "color",
            0,
        )
    )

    try:
        number_font = extract_font(
            template,
            "OpenSans-Regular",
        )
    except Exception:
        number_font = None

    # Use the actual font from the template whenever possible.
    if number_font is None:
        number_font = extract_font(
            template,
            "OpenSans-Regular",
        )

    number_font_obj = pymupdf.Font(
        fontfile=number_font
    )

    new_number = stipend
    new_numeric_text = f"{new_number}."

    # --------------------------------------------------------
    # Fit ONLY the numeric portion if a very long amount is used.
    # The normal 13000 / 18000 / 25000 values stay at the original
    # 12 pt template size.
    # --------------------------------------------------------

    number_rect = pymupdf.Rect(
        number_span["bbox"]
    )

    fontsize = original_size

    # There is no need to shrink for normal values. We only shrink
    # if the replacement would extend beyond the PDF's right margin.
    max_width = max(
        number_rect.width,
        page.rect.width - number_rect.x0 - 60,
    )

    while fontsize > 7:
        width = number_font_obj.text_length(
            new_numeric_text,
            fontsize=fontsize,
        )

        if width <= max_width:
            break

        fontsize -= 0.1

    # --------------------------------------------------------
    # Remove ONLY the old numeric amount.
    #
    # Do NOT redact the ₹ span. This is the key fix.
    # --------------------------------------------------------

    redact_rect = pymupdf.Rect(
        number_rect.x0 - 0.5,
        number_rect.y0 - 0.5,
        number_rect.x1 + 0.5,
        number_rect.y1 + 0.5,
    )

    page.add_redact_annot(
        redact_rect,
        fill=(1, 1, 1),
    )

    page.apply_redactions()

    # --------------------------------------------------------
    # Insert ONLY the new number.
    #
    # The original ₹ remains in the PDF exactly as designed.
    # --------------------------------------------------------

    page.insert_text(
        (
            number_rect.x0,
            number_span["origin"][1],
        ),
        new_numeric_text,
        # IMPORTANT: give the embedded font an explicit custom name.
        # Without this, some PyMuPDF builds fall back to Helvetica.
        fontname="OpenSansCustom",
        fontfile=number_font,
        fontsize=fontsize,
        color=original_color,
        overlay=True,
    )

    print(
        "STIPEND: Numeric amount replaced successfully; "
        "original ₹ symbol preserved."
    )


# ============================================================
# REPLACE DATE LINE
# ============================================================

def replace_date_line(
    page,
    template,
    start_date,
    end_date,
):

    line = find_line_any(
        page,
        [
            "The duration of the internship program ranges from",
            "duration of the internship program ranges from",
        ],
    )

    regular_font = extract_font(
        template,
        "OpenSans-Regular",
    )

    new_text = (
        "The duration of the internship program ranges from "
        f"{start_date} to {end_date} or"
    )

    replace_entire_line(
        page,
        line,
        new_text,
        regular_font,
    )


# ============================================================
# WITH HOURS
# ============================================================

def edit_with_hours(
    data,
):

    template = (
        TEMPLATE_DIR
        /
        "with_hours.pdf"
    )

    if not template.exists():

        raise FileNotFoundError(
            "WITH HOURS PDF template not found:\n"
            f"{template}"
        )

    doc = pymupdf.open(
        template
    )

    try:

        page = doc[0]

        regular_font = extract_font(
            template,
            "OpenSans-Regular",
        )

        # ----------------------------------------------------
        # STUDENT NAME
        # ----------------------------------------------------

        line = find_line_any(
            page,
            [
                "This letter is to confirm that Mr/Ms.",
                "This letter is to confirm",
            ],
        )

        new_text = (
            "This letter is to confirm that Mr/Ms. "
            f"{data['student_name']}"
        )

        replace_entire_line(
            page,
            line,
            new_text,
            regular_font,
        )

        # ----------------------------------------------------
        # DOMAIN
        # ----------------------------------------------------

        line = find_line_any(
            page,
            [
                "Has been offered an internship in the field of",
                "Has been offered an internship",
            ],
        )

        new_text = (
            "Has been offered an internship in the field of "
            f"{data['domain']} with Persevex LLP,"
        )

        replace_entire_line(
            page,
            line,
            new_text,
            regular_font,
        )

        # ----------------------------------------------------
        # DURATION
        # ----------------------------------------------------

        line = find_line_any(
            page,
            [
                "Which is of 2 months duration",
                "Which is of",
            ],
        )

        new_text = (
            "Which is of "
            f"{data['duration']} duration, "
            "under the supervision of "
            "Mr. Shanmukh Shekar K C."
        )

        replace_entire_line(
            page,
            line,
            new_text,
            regular_font,
        )

        # ----------------------------------------------------
        # HOURS FIRST LINE
        # ----------------------------------------------------

        line = find_line_any(
            page,
            [
                "standard requirements. I affirm that this internship",
                "standard requirements",
            ],
        )

        new_text = (
            "standard requirements. I affirm that this internship "
            f"is a total of {data['weeks']} weeks and "
            f"{data['hours_per_week']}"
        )

        replace_entire_line(
            page,
            line,
            new_text,
            regular_font,
        )

        # ----------------------------------------------------
        # HOURS SECOND LINE
        # ----------------------------------------------------

        line = find_line_any(
            page,
            [
                "hours per week, which sums to",
                "hours per week",
            ],
        )

        new_text = (
            "hours per week, which sums to "
            f"{data['total_hours']} hours for the entire "
            "duration of each domain."
        )

        replace_entire_line(
            page,
            line,
            new_text,
            regular_font,
        )

        # ----------------------------------------------------
        # STIPEND
        # ----------------------------------------------------

        stipend = clean_stipend(
            data.get(
                "stipend",
                "",
            )
        )

        if stipend:

            replace_stipend(
                page,
                template,
                stipend,
            )

        else:

            remove_stipend_sentence(
                page
            )

        # ----------------------------------------------------
        # DATES
        # ----------------------------------------------------

        replace_date_line(
            page,
            template,
            data["start_date"],
            data["end_date"],
        )

        return doc

    except Exception:

        doc.close()

        raise


# ============================================================
# WITHOUT-HOURS DURATION NUMBER REPLACEMENT
# ============================================================
# Preserve the original multi-line paragraph formatting.
# Replace only the two numeric month values in the template.

def replace_without_hours_duration_numbers(page, template, total_months):
    """
    Replace ONLY the two dynamic numeric month characters in the original
    WITHOUT-HOURS template.

    Important: do not redraw the paragraph.  The original template already
    has the correct OpenSans font, size, baseline, spacing, and line wrapping.
    We therefore locate the individual numeric characters from rawdict and
    redraw each replacement at that character's exact original baseline.
    """

    words = page.get_text("words")
    words.sort(key=lambda item: (item[1], item[0]))

    # In the supplied original template the editable values are:
    #   "... program is 3 months ..."
    #   "followed by 2 months of internship ..."
    # The other "1" is the fixed training duration and must not be changed.
    first_number = next(
        (
            w for w in words
            if w[4].strip() == "3"
            and 330 <= w[1] <= 370
            and 250 <= w[0] <= 350
        ),
        None,
    )

    second_number = next(
        (
            w for w in words
            if w[4].strip() == "2"
            and 360 <= w[1] <= 390
            and 100 <= w[0] <= 180
        ),
        None,
    )

    if first_number is None or second_number is None:
        raise ValueError(
            "Could not locate the duration numbers in the WITHOUT HOURS template."
        )

    # Read the original characters, including their exact font, size,
    # origin and bounding box.  This is what preserves the template format.
    raw = page.get_text("rawdict")
    targets = []

    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    text = ch.get("c", "")
                    bbox = ch.get("bbox")
                    if not bbox:
                        continue

                    if (
                        first_number[0] - 0.5 <= bbox[0] <= first_number[2] + 0.5
                        and first_number[1] - 0.5 <= bbox[1] <= first_number[3] + 0.5
                        and text == "3"
                    ):
                        targets.append(
                            (ch, str(total_months))
                        )
                        continue

                    if (
                        second_number[0] - 0.5 <= bbox[0] <= second_number[2] + 0.5
                        and second_number[1] - 0.5 <= bbox[1] <= second_number[3] + 0.5
                        and text == "2"
                    ):
                        targets.append(
                            (ch, str(max(1, int(total_months) - 1)))
                        )

    if len(targets) != 2:
        raise ValueError(
            "Could not locate both editable duration characters in the WITHOUT HOURS template."
        )

    # Remove only the two old digit glyphs. Everything around them remains
    # untouched, including the original paragraph and its line formatting.
    for ch, _ in targets:
        page.add_redact_annot(
            pymupdf.Rect(ch["bbox"]),
            fill=(1, 1, 1),
        )

    page.apply_redactions()

    for ch, new_number in targets:
        span_size = float(ch.get("size", 12.004523277282715))
        origin = ch.get("origin")
        font_name = ch.get("font", "OpenSans-Regular")

        # Prefer the exact embedded template font. The file contains
        # OpenSans-Regular, so this keeps the replacement visually identical.
        font_file = extract_font(template, "OpenSans-Regular")

        page.insert_text(
            (origin[0], origin[1]),
            new_number,
            fontfile=font_file,
            fontsize=span_size,
            color=(0, 0, 0),
            overlay=True,
        )


# ============================================================
# WITHOUT HOURS
# ============================================================

def edit_without_hours(
    data,
):

    template = (
        TEMPLATE_DIR
        /
        "without_hours.pdf"
    )

    if not template.exists():

        raise FileNotFoundError(
            "WITHOUT HOURS PDF template not found:\n"
            f"{template}"
        )

    doc = pymupdf.open(
        template
    )

    try:

        page = doc[0]

        regular_font = extract_font(
            template,
            "OpenSans-Regular",
        )

        # ----------------------------------------------------
        # STUDENT NAME
        # ----------------------------------------------------

        line = find_line_any(
            page,
            [
                "This letter is to confirm that Mr/Ms.",
                "This letter is to confirm",
            ],
        )

        new_text = (
            "This letter is to confirm that Mr/Ms. "
            f"{data['student_name']} has been offered a Training and Internship"
        )

        replace_entire_line(
            page,
            line,
            new_text,
            regular_font,
        )

        # ----------------------------------------------------
        # DOMAIN
        # ----------------------------------------------------

        line = find_line_any(
            page,
            [
                "Program in the field of Digital Marketing",
                "Program in the field of",
            ],
        )

        new_text = (
            "Program in the field of "
            f"{data['domain']} with Persevex, "
            "under the supervision of Mr. Shanmukh"
        )

        replace_entire_line(
            page,
            line,
            new_text,
            regular_font,
        )

        # ----------------------------------------------------
        # TRAINING + INTERNSHIP
        # ----------------------------------------------------

        total_months = int(
            str(
                data["duration"]
            ).split()[0]
        )

        internship_months = (
            total_months - 1
        )

        if internship_months == 1:

            internship_text = "1 month"

        else:

            internship_text = (
                f"{internship_months} months"
            )

        month_word = (
            "month"
            if total_months == 1
            else "months"
        )

        # Preserve the original four-line paragraph formatting.
        # Only replace the two numeric month values.
        replace_without_hours_duration_numbers(
            page,
            template,
            total_months,
        )

        # ----------------------------------------------------
        # STIPEND
        # ----------------------------------------------------

        stipend = clean_stipend(
            data.get(
                "stipend",
                "",
            )
        )

        if stipend:

            replace_stipend(
                page,
                template,
                stipend,
            )

        else:

            remove_stipend_sentence(
                page
            )

        # ----------------------------------------------------
        # DATES
        # ----------------------------------------------------

        replace_date_line(
            page,
            template,
            data["start_date"],
            data["end_date"],
        )

        return doc

    except Exception:

        doc.close()

        raise


# ============================================================
# GENERATE PDF
# ============================================================

def generate_pdf(
    letter_type,
    data,
):

    if letter_type == "with_hours":

        doc = edit_with_hours(
            data
        )

    elif letter_type == "without_hours":

        doc = edit_without_hours(
            data
        )

    else:

        raise ValueError(
            "Invalid letter type."
        )

    filename = (
        f"{safe_filename(data['student_name'])}"
        "_Internship_Acceptance_Letter.pdf"
    )

    output = (
        GENERATED_DIR
        /
        filename
    )

    if output.exists():

        output.unlink()

    try:

        doc.save(
            output,
            garbage=4,
            deflate=True,
        )

    finally:

        doc.close()

    return filename


# ============================================================
# LOGIN PROTECTION
# ============================================================

@app.before_request
def require_login():
    """Require authentication for the application routes."""

    allowed_endpoints = {
        "login",
        "static",
    }

    if request.endpoint in allowed_endpoints:
        return None

    if session.get("authenticated") is True:
        return None

    # API requests should receive JSON instead of an HTML redirect.
    if request.path.startswith("/generate") or request.path.startswith("/send-email"):
        return jsonify({
            "error": "Login required. Please log in first."
        }), 401

    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated") is True:
        return redirect(url_for("index"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not LOGIN_USERNAME or not LOGIN_PASSWORD:
            error = "Login is not configured. Set the login environment variables."
        elif (
            hmac.compare_digest(username, LOGIN_USERNAME)
            and hmac.compare_digest(password, LOGIN_PASSWORD)
        ):
            session.clear()
            session["authenticated"] = True
            session["username"] = LOGIN_USERNAME
            return redirect(url_for("index"))
        else:
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# HOME
# ============================================================

@app.get("/")
def index():

    return render_template(
        "index.html"
    )


# ============================================================
# GENERATE API
# ============================================================

@app.post("/generate")
def generate():

    try:

        data = request.get_json(
            force=True
        )

        if not data:

            return jsonify({
                "error": "No data received."
            }), 400

        letter_type = data.get(
            "letter_type"
        )

        if letter_type not in (
            "with_hours",
            "without_hours",
        ):

            return jsonify({
                "error": "Invalid letter type."
            }), 400

        # ----------------------------------------------------
        # REQUIRED FIELDS
        #
        # STIPEND IS OPTIONAL.
        # ----------------------------------------------------

        required = [
            "student_name",
            "student_email",
            "domain",
            "duration",
            "start_date",
            "end_date",
        ]

        if letter_type == "with_hours":

            required.append(
                "hours_per_week"
            )

        missing = []

        for field in required:

            value = data.get(
                field,
                "",
            )

            if not str(
                value
            ).strip():

                missing.append(
                    field
                )

        if missing:

            return jsonify({
                "error": (
                    "Please fill: "
                    +
                    ", ".join(
                        missing
                    )
                )
            }), 400

        # ----------------------------------------------------
        # DURATION
        # ----------------------------------------------------

        duration = normalize_duration(
            data["duration"]
        )

        if duration not in DURATION_WEEKS:

            return jsonify({
                "error": (
                    "Please select a duration "
                    "between 2 and 6 months."
                )
            }), 400

        data["duration"] = duration

        # ----------------------------------------------------
        # DATE VALIDATION
        # ----------------------------------------------------

        try:

            start_date_obj = datetime.strptime(
                data["start_date"],
                "%Y-%m-%d",
            )

            end_date_obj = datetime.strptime(
                data["end_date"],
                "%Y-%m-%d",
            )

        except ValueError:

            return jsonify({
                "error": "Invalid date format."
            }), 400

        if end_date_obj < start_date_obj:

            return jsonify({
                "error": (
                    "End date cannot be before "
                    "start date."
                )
            }), 400

        # ----------------------------------------------------
        # CLEAN DATA
        # ----------------------------------------------------

        data["student_name"] = str(
            data["student_name"]
        ).strip()

        data["student_email"] = str(
            data["student_email"]
        ).strip()

        data["domain"] = str(
            data["domain"]
        ).strip()

        data["start_date"] = format_date(
            data["start_date"]
        )

        data["end_date"] = format_date(
            data["end_date"]
        )

        # ----------------------------------------------------
        # OPTIONAL STIPEND
        # ----------------------------------------------------

        data["stipend"] = clean_stipend(
            data.get(
                "stipend",
                "",
            )
        )

        # ----------------------------------------------------
        # WITH HOURS
        # ----------------------------------------------------

        if letter_type == "with_hours":

            weeks = DURATION_WEEKS[
                duration
            ]

            try:

                hours_per_week = int(
                    data["hours_per_week"]
                )

            except (
                ValueError,
                TypeError,
            ):

                return jsonify({
                    "error": (
                        "Hours per week must "
                        "be a number."
                    )
                }), 400

            if hours_per_week <= 0:

                return jsonify({
                    "error": (
                        "Hours per week must "
                        "be greater than zero."
                    )
                }), 400

            total_hours = (
                weeks
                *
                hours_per_week
            )

            data["weeks"] = str(
                weeks
            )

            data["hours_per_week"] = str(
                hours_per_week
            )

            data["total_hours"] = str(
                total_hours
            )

        # ----------------------------------------------------
        # GENERATE
        # ----------------------------------------------------

        filename = generate_pdf(
            letter_type,
            data,
        )

        return jsonify({

            "success": True,

            "filename": filename,

            "url": (
                f"/generated/{filename}"
            ),

        })

    except Exception as exc:

        print(
            "========================================"
        )

        print(
            "PDF GENERATION ERROR:"
        )

        print(
            repr(exc)
        )

        print(
            "========================================"
        )

        return jsonify({
            "error": str(exc)
        }), 500


# ============================================================
# SERVE GENERATED PDF
# ============================================================

@app.get(
    "/generated/<path:filename>"
)
def generated(
    filename,
):

    return send_from_directory(
        GENERATED_DIR,
        filename,
        as_attachment=False,
    )


# ============================================================
# SEND EMAIL
# ============================================================

@app.post("/send-email")
def send_email():

    try:

        data = request.get_json(
            force=True
        )

        filename = data.get(
            "filename"
        )

        recipient = data.get(
            "student_email"
        )

        student_name = data.get(
            "student_name",
            "Student",
        )

        if not filename:

            return jsonify({
                "error": (
                    "Generated PDF is required."
                )
            }), 400

        if not recipient:

            return jsonify({
                "error": (
                    "Student email is required."
                )
            }), 400

        filename = Path(
            filename
        ).name

        pdf_path = (
            GENERATED_DIR
            /
            filename
        )

        if not pdf_path.exists():

            return jsonify({
                "error": (
                    "Generated PDF not found."
                )
            }), 404

        # ----------------------------------------------------
        # EMAIL CONFIGURATION
        # ----------------------------------------------------

        if not SENDER_EMAIL or not SENDER_APP_PASSWORD:

            return jsonify({
                "error": (
                    "Email is not configured. Set "
                    "SENDER_EMAIL and "
                    "PERSEVEX_GMAIL_APP_PASSWORD "
                    "in the Vercel Environment Variables."
                )
            }), 400

        # ----------------------------------------------------
        # EMAIL
        # ----------------------------------------------------

        message = EmailMessage()

        message["From"] = (
            SENDER_EMAIL
        )

        message["To"] = (
            recipient
        )

        message["Subject"] = (
            "Internship Acceptance Letter - Persevex"
        )

        message.set_content(
            f"Dear {student_name},\n\n"
            "Please find attached your "
            "Internship Acceptance Letter "
            "from Persevex.\n\n"
            "Best regards,\n"
            "Persevex"
        )

        # ----------------------------------------------------
        # ATTACH PDF
        # ----------------------------------------------------

        message.add_attachment(
            pdf_path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=pdf_path.name,
        )

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
        ) as smtp:

            smtp.starttls()

            smtp.login(
                SENDER_EMAIL,
                SENDER_APP_PASSWORD,
            )

            smtp.send_message(
                message
            )

        return jsonify({
            "success": True,
            "message": (
                "Email sent successfully."
            ),
        })

    except Exception as exc:

        print(
            "EMAIL ERROR:"
        )

        print(
            repr(exc)
        )

        return jsonify({
            "error": str(exc)
        }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print(
        "========================================"
    )

    print(
        "Persevex Offer Letter Generator"
    )

    print(
        "========================================"
    )

    print(
        "Running at:"
    )

    print(
        "http://127.0.0.1:5000"
    )

    print(
        "========================================"
    )

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
    )