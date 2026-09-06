import tempfile
import os
import hmac
from pathlib import Path
import re
import smtplib
from datetime import datetime,timezone
from email.message import EmailMessage
import imaplib

from dotenv import load_dotenv
from supabase import create_client, Client

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

import pymupdf


# ============================================================
# APPLICATION
# ============================================================

app = Flask(__name__)

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError(
        "SUPABASE_URL and SUPABASE_KEY must be configured."
    )

supabase: Client = create_client(SUPABASE_URL,SUPABASE_KEY)


# ============================================================
# LOGIN / SESSION CONFIGURATION
# ============================================================
LOGIN_USERNAME = os.getenv("PERSEVEX_LOGIN_USERNAME", "").strip()
LOGIN_PASSWORD = os.getenv("PERSEVEX_LOGIN_PASSWORD", "").strip()
SESSION_SECRET = os.getenv("PERSEVEX_SESSION_SECRET", "").strip()

if not SESSION_SECRET:
    SESSION_SECRET = "local-development-secret-change-me"

app.secret_key = SESSION_SECRET
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.getenv("VERCEL") == "1"
)


# ============================================================
# DIRECTORIES
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

TEMPLATE_DIR = BASE_DIR / "pdf_templates"

# Vercel allows writing only inside the temporary directory.
GENERATED_DIR = Path(tempfile.gettempdir()) / "persevex_generated"
FONT_DIR = Path(tempfile.gettempdir()) / "persevex_fonts"

GENERATED_DIR.mkdir(
    parents=True,
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

SENDER_PASSWORD = os.getenv("SENDER_PASSWORD","").strip()

SMTP_HOST = "smtpout.secureserver.net"
SMTP_PORT = 465

IMAP_HOST = "imap.secureserver.net"
IMAP_PORT = 993
IMAP_SENT_FOLDER = "Sent"


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

    windows_fonts = Path(
        r"C:\Windows\Fonts"
    )

    if not windows_fonts.exists():

        raise RuntimeError(
            "Windows Fonts directory was not found."
        )

    # --------------------------------------------------------
    # Prefer fonts that normally contain the Indian Rupee
    # symbol.
    # --------------------------------------------------------

    preferred = [
        "Nirmala.ttf",
        "NirmalaUI.ttf",
        "NirmalaUI-Regular.ttf",
        "segoeui.ttf",
        "arial.ttf",
        "calibri.ttf",
    ]

    for filename in preferred:

        font_path = (
            windows_fonts
            /
            filename
        )

        if not font_path.exists():
            continue

        try:

            font = pymupdf.Font(
                fontfile=str(font_path)
            )

            # Check actual glyph support.
            if font.has_glyph(
                ord("₹")
            ):

                return str(
                    font_path
                )

        except Exception:

            continue

    # --------------------------------------------------------
    # Search every installed font as fallback.
    # --------------------------------------------------------

    for font_path in windows_fonts.iterdir():

        if not font_path.is_file():
            continue

        if font_path.suffix.lower() not in (
            ".ttf",
            ".otf",
            ".ttc",
        ):
            continue

        try:

            font = pymupdf.Font(
                fontfile=str(font_path)
            )

            if font.has_glyph(
                ord("₹")
            ):

                return str(
                    font_path
                )

        except Exception:

            continue

    raise RuntimeError(
        "No installed Windows font supporting "
        "the ₹ symbol was found."
    )


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
# REPLACE STUDENT NAME - KEEP ORIGINAL FONT SIZE
# ============================================================

def replace_student_name_line(
    page,
    line,
    new_text,
    font_file,
):
    """
    Replace the student-name line without reducing the template font size.

    Long names are wrapped onto the available space at the SAME font size.
    This helper is used only for the student-name line, so no other PDF
    formatting or replacement logic is changed.
    """

    rect = pymupdf.Rect(line["bbox"])
    original_size = float(line["size"])
    color = rgb_from_pdf_color(line["color"])

    # Remove only the original template text line.
    page.add_redact_annot(
        rect,
        fill=(1, 1, 1),
    )
    page.apply_redactions()

    # Keep the original left position and font size. Use the page width so
    # long names wrap instead of triggering the font-size reduction used by
    # replace_entire_line().
    right_margin = 30
    available = pymupdf.Rect(
        line["origin"][0],
        rect.y0 - 1,
        page.rect.width - right_margin,
        rect.y0 + max(90, original_size * 4.5),
    )

    result = page.insert_textbox(
        available,
        new_text,
        fontfile=font_file,
        fontsize=original_size,
        color=color,
        align=pymupdf.TEXT_ALIGN_LEFT,
        lineheight=1.0,
        overlay=True,
    )

    if result < 0:
        # Never shrink the name font. If an unusually long name needs more
        # vertical room, continue with the same font size in a larger box.
        available.y1 = page.rect.height - 30
        page.insert_textbox(
            available,
            new_text,
            fontfile=font_file,
            fontsize=original_size,
            color=color,
            align=pymupdf.TEXT_ALIGN_LEFT,
            lineheight=1.0,
            overlay=True,
        )


# ============================================================
# REPLACE WITH-HOURS OFFER LINE - KEEP ORIGINAL FONT SIZE
# ============================================================

def replace_hours_offer_line(
    page,
    line,
    new_text,
    font_file,
):
    """
    Replace ONLY the WITH HOURS offer line without reducing its font size.

    This function is intentionally limited to the line beginning
    "Has been offered an internship...". All other replacement logic remains
    unchanged.
    """

    rect = pymupdf.Rect(line["bbox"])
    original_size = float(line["size"])
    color = rgb_from_pdf_color(line["color"])

    # Clear only the original offer line.
    page.add_redact_annot(
        rect,
        fill=(1, 1, 1),
    )
    page.apply_redactions()

    # Use the available page width and keep the exact original font size.
    # The font size is never reduced.
    text_rect = pymupdf.Rect(
        line["origin"][0],
        rect.y0 - 1,
        page.rect.width - 24,
        rect.y1 + max(8, original_size * 1.5),
    )

    result = page.insert_textbox(
        text_rect,
        new_text,
        fontfile=font_file,
        fontsize=original_size,
        color=color,
        align=pymupdf.TEXT_ALIGN_LEFT,
        lineheight=1.0,
        overlay=True,
    )

    # If the text needs slightly more vertical room, retry without changing
    # the font size.
    if result < 0:
        text_rect.y1 = rect.y0 + max(
            original_size * 3.0,
            rect.height * 2.5,
        )
        page.insert_textbox(
            text_rect,
            new_text,
            fontfile=font_file,
            fontsize=original_size,
            color=color,
            align=pymupdf.TEXT_ALIGN_LEFT,
            lineheight=1.0,
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
    WITH HOURS ONLY: change ONLY the stipend amount.

    The existing sentence, paragraph layout, font, font size, spacing,
    and original ₹ symbol remain untouched.
    """

    stipend = clean_stipend(stipend)

    if not stipend:
        return

    line = find_stipend_line(page)

    # Keep the amount formatting clean while preserving the exact template text.
    try:
        formatted_amount = f"{int(float(stipend)):,}"
    except (TypeError, ValueError):
        formatted_amount = str(stipend)

    # --------------------------------------------------------
    # Find the amount span on the ORIGINAL stipend line.
    #
    # The Persevex template may store ₹ in a different font span
    # from the numeric amount. Therefore, replace only the number
    # whenever possible and leave the original ₹ glyph untouched.
    # --------------------------------------------------------

    amount_span = None
    line_y = line["bbox"].y0

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue

        for pdf_line in block.get("lines", []):
            if abs(pdf_line["bbox"][1] - line_y) > 2.0:
                continue

            for span in pdf_line.get("spans", []):
                span_text = str(span.get("text", "")).strip()

                # Preferred case: amount is its own span, for example
                # "15,000." or "15000.".
                if re.fullmatch(r"[\d,]+\.?", span_text):
                    amount_span = span
                    break

                # Fallback: ₹ and amount are stored together.
                if "₹" in span_text and re.search(r"\d", span_text):
                    amount_span = span
                    break

            if amount_span is not None:
                break

        if amount_span is not None:
            break

    if amount_span is None:
        raise ValueError(
            "Could not locate the original stipend amount in the PDF template."
        )

    amount_rect = pymupdf.Rect(amount_span["bbox"])
    amount_text = str(amount_span.get("text", ""))

    # Remove ONLY the original amount. Do not remove or redraw the
    # surrounding stipend sentence.
    page.add_redact_annot(
        amount_rect,
        fill=(1, 1, 1),
    )
    page.apply_redactions()

    # --------------------------------------------------------
    # Normal template case:
    # ₹ is already present in its original font, so redraw ONLY
    # the number with the same typography as the original amount.
    # --------------------------------------------------------

    if "₹" not in amount_text:

        span_font_name = str(
            amount_span.get("font", "")
        )

        try:
            amount_font = extract_font(
                template,
                span_font_name,
            )
        except Exception:
            amount_font = extract_font(
                template,
                "OpenSans-Regular",
            )

        page.insert_text(
            amount_span["origin"],
            formatted_amount + ".",
            fontfile=amount_font,
            fontsize=float(amount_span["size"]),
            color=rgb_from_pdf_color(
                amount_span["color"]
            ),
            overlay=True,
        )

        print(
            "STIPEND: Amount updated. Original ₹ symbol and formatting preserved."
        )
        return

    # --------------------------------------------------------
    # Rare fallback:
    # ₹ and the amount are stored in one span. Preserve the same
    # font whenever it supports ₹; otherwise use the template's
    # embedded NotoSans font for the combined replacement.
    # --------------------------------------------------------

    span_font_name = str(
        amount_span.get("font", "")
    )

    try:
        combined_font = extract_font(
            template,
            span_font_name,
        )
        test_font = pymupdf.Font(
            fontfile=combined_font
        )

        if not test_font.has_glyph(
            ord("₹")
        ):
            raise RuntimeError(
                "Original amount font does not support ₹."
            )

    except Exception:

        try:
            combined_font = extract_font(
                template,
                "NotoSans-Regular",
            )
        except Exception:
            combined_font = find_rupee_font()

    page.insert_text(
        amount_span["origin"],
        f"₹{formatted_amount}.",
        fontfile=combined_font,
        fontsize=float(amount_span["size"]),
        color=rgb_from_pdf_color(
            amount_span["color"]
        ),
        overlay=True,
    )

    print(
        "STIPEND: Amount updated with ₹ while preserving the original layout."
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

        replace_student_name_line(
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

        replace_hours_offer_line(
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
# WITHOUT-HOURS ONLY HELPERS
#
# IMPORTANT:
# These functions are intentionally separate from the WITH HOURS
# logic. The WITH HOURS section above is not changed.
# ============================================================

def find_without_hours_duration_block(page):
    """Find the original four-line training/internship paragraph."""

    lines = get_text_lines(page)
    start = None

    for line in lines:
        if "the total duration of the program is" in normalize_text(line["text"]):
            start = line
            break

    if start is None:
        raise ValueError(
            "Could not locate the without-hours duration paragraph."
        )

    # Collect lines immediately below the start line with the same left edge.
    candidates = [
        line for line in lines
        if line["bbox"].y0 >= start["bbox"].y0 - 0.5
        and line["bbox"].y0 <= start["bbox"].y0 + 55
        and abs(line["bbox"].x0 - start["bbox"].x0) < 2.0
    ]
    candidates.sort(key=lambda item: item["bbox"].y0)

    # The original template paragraph is exactly four visual lines.
    if len(candidates) < 4:
        raise ValueError(
            "Could not locate all four lines of the without-hours duration paragraph."
        )

    return candidates[:4]


def _replace_original_lines(page, lines, new_texts, font_file):
    """Redraw existing lines at their original positions and typography."""

    for line in lines:
        page.add_redact_annot(
            pymupdf.Rect(line["bbox"]),
            fill=(1, 1, 1),
        )

    page.apply_redactions()

    for line, text in zip(lines, new_texts):
        page.insert_text(
            line["origin"],
            text,
            fontfile=font_file,
            fontsize=line["size"],
            color=rgb_from_pdf_color(line["color"]),
            overlay=True,
        )


def replace_without_hours_duration_values(page, template, total_months):
    """
    WITHOUT HOURS ONLY.

    Preserve the original four-line paragraph exactly. Only the total-duration
    and internship-duration values are changed. This avoids the extra spaces
    caused by replacing individual words at different widths.
    """

    total_months = int(total_months)
    internship_months = total_months - 1

    total_unit = "month" if total_months == 1 else "months"
    internship_unit = "month" if internship_months == 1 else "months"

    lines = find_without_hours_duration_block(page)
    regular_font = extract_font(template, "OpenSans-Regular")

    original = [str(line["text"]) for line in lines]

    # First line: replace only "3 months" while preserving every other word.
    first_text = re.sub(
        r"\b3\s+months\b",
        f"{total_months} {total_unit}",
        original[0],
        count=1,
    )

    # Second line: replace only "2 months" while preserving every other word.
    second_text = re.sub(
        r"\b2\s+months\b",
        f"{internship_months} {internship_unit}",
        original[1],
        count=1,
    )

    new_texts = [
        first_text,
        second_text,
        original[2],
        original[3],
    ]

    _replace_original_lines(
        page,
        lines,
        new_texts,
        regular_font,
    )


def find_without_hours_stipend_parts(page):
    """Find the two visual parts of the wrapped stipend sentence."""

    lines = get_text_lines(page)
    previous = None
    stipend = None

    for line in lines:
        text = normalize_text(line["text"])

        if (
            "conduct weekly check-ins" in text
            and text.endswith("the")
        ):
            previous = line

        if (
            "internship offers a performance-based stipend" in text
            or "internship offers a performance-based stipend up to" in text
        ):
            stipend = line

    if previous is None or stipend is None:
        raise ValueError(
            "Could not locate the complete wrapped stipend sentence."
        )

    return previous, stipend


def replace_without_hours_stipend_amount(page, template, stipend):
    """WITHOUT HOURS ONLY: replace ONLY the original numeric stipend amount."""

    stipend = clean_stipend(stipend)
    _, stipend_line = find_without_hours_stipend_parts(page)

    regular_font = extract_font(
        template,
        "OpenSans-Regular",
    )

    fontsize = stipend_line["size"]
    color = rgb_from_pdf_color(stipend_line["color"])
    formatted = f"{int(stipend):,}."

    # --------------------------------------------------------
    # IMPORTANT:
    # The original Persevex template stores ₹ as NotoSans-Regular
    # and the numeric amount as OpenSans-Regular. Therefore we find
    # the NUMERIC SPAN, not the PDF word. This leaves the original
    # ₹ glyph completely untouched.
    # --------------------------------------------------------

    amount_span = None

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue

        for pdf_line in block.get("lines", []):
            if abs(pdf_line["bbox"][1] - stipend_line["bbox"].y0) > 1.0:
                continue

            for span in pdf_line.get("spans", []):
                text = str(span.get("text", ""))

                # Normal template: span is exactly "25,000.".
                if re.fullmatch(r"[\d,]+\.?", text.strip()):
                    amount_span = span
                    break

                # Fallback only if ₹ and number happen to be in one span.
                if "₹" in text and re.search(r"\d", text):
                    amount_span = span
                    break

            if amount_span is not None:
                break

        if amount_span is not None:
            break

    if amount_span is None:
        raise ValueError(
            "Could not locate the original stipend amount span."
        )

    amount_rect = pymupdf.Rect(
        amount_span["bbox"]
    )

    page.add_redact_annot(
        amount_rect,
        fill=(1, 1, 1),
    )
    page.apply_redactions()

    # Normal template: ₹ is already preserved as the original NotoSans
    # glyph, so insert only the numeric amount in OpenSans.
    if "₹" not in str(amount_span.get("text", "")):

        page.insert_text(
            (
                amount_span["origin"][0],
                stipend_line["origin"][1],
            ),
            formatted,
            fontfile=regular_font,
            fontsize=fontsize,
            color=color,
            overlay=True,
        )

        return

    # Rare fallback: the original PDF combined ₹ and the amount into one
    # span. Use the embedded NotoSans-Regular from the same template rather
    # than a system font, so the PDF remains self-contained.
    try:
        rupee_font = extract_font(
            template,
            "NotoSans-Regular",
        )
    except Exception:
        rupee_font = find_rupee_font()

    page.insert_text(
        (
            amount_span["origin"][0],
            stipend_line["origin"][1],
        ),
        f"₹{formatted}",
        fontfile=rupee_font,
        fontsize=fontsize,
        color=color,
        overlay=True,
    )

def remove_without_hours_stipend_sentence(page):
    """WITHOUT HOURS ONLY: remove the complete wrapped stipend sentence."""

    previous_line, stipend_line = find_without_hours_stipend_parts(page)
    words = page.get_text("words")

    trailing_the = None
    for word in words:
        if abs(word[1] - previous_line["bbox"].y0) <= 1.0:
            if normalize_text(word[4]) == "the":
                trailing_the = word

    if trailing_the is None:
        raise ValueError(
            "Could not locate the trailing 'The' of the stipend sentence."
        )

    page.add_redact_annot(
        pymupdf.Rect(
            trailing_the[0],
            trailing_the[1],
            trailing_the[2],
            trailing_the[3],
        ),
        fill=(1, 1, 1),
    )

    page.add_redact_annot(
        pymupdf.Rect(stipend_line["bbox"]),
        fill=(1, 1, 1),
    )

    page.apply_redactions()


def find_without_hours_date_lines(page):
    """Find the original two-line date paragraph."""

    lines = get_text_lines(page)
    first = None
    second = None

    for line in lines:
        text = normalize_text(line["text"])

        if "the duration of the internship program ranges from" in text:
            first = line
        elif "until the student completes the required number of internship hours" in text:
            second = line

    if first is None or second is None:
        raise ValueError(
            "Could not find the complete date paragraph."
        )

    return first, second


def replace_without_hours_date_paragraph(
    page,
    template,
    start_date,
    end_date,
    shift_up=0.0,
):
    """Replace only the dates and optionally move the original date paragraph."""

    first, second = find_without_hours_date_lines(page)
    regular_font = extract_font(template, "OpenSans-Regular")

    original_first = str(first["text"])
    original_second = str(second["text"])

    # Replace the two original dates in ONE operation so the newly inserted
    # start date cannot accidentally be matched again as the second date.
    matches = list(
        re.finditer(
            r"\d{2}/\d{2}/\d{4}",
            original_first,
        )
    )

    if len(matches) < 2:
        raise ValueError(
            "Could not locate both dates in the original date line."
        )

    new_first = (
        original_first[:matches[0].start()]
        + start_date
        + original_first[matches[0].end():matches[1].start()]
        + end_date
        + original_first[matches[1].end():]
    )

    # Remove the original paragraph before drawing the replacement.
    page.add_redact_annot(
        pymupdf.Rect(first["bbox"]),
        fill=(1, 1, 1),
    )
    page.add_redact_annot(
        pymupdf.Rect(second["bbox"]),
        fill=(1, 1, 1),
    )
    page.apply_redactions()

    page.insert_text(
        (
            first["origin"][0],
            first["origin"][1] - shift_up,
        ),
        new_first,
        fontfile=regular_font,
        fontsize=first["size"],
        color=rgb_from_pdf_color(first["color"]),
        overlay=True,
    )

    page.insert_text(
        (
            second["origin"][0],
            second["origin"][1] - shift_up,
        ),
        original_second,
        fontfile=regular_font,
        fontsize=second["size"],
        color=rgb_from_pdf_color(second["color"]),
        overlay=True,
    )


def replace_without_hours_date_paragraph_shifted(
    page,
    template,
    start_date,
    end_date,
    shift_up,
):
    """Compatibility wrapper for the shifted date operation."""

    replace_without_hours_date_paragraph(
        page,
        template,
        start_date,
        end_date,
        shift_up=shift_up,
    )


def replace_without_hours_offer_paragraph(page, offer_line, new_text, font_file):
    """
    Replace ONLY the Dates-section offer paragraph.

    The font size is taken from the normal body paragraph below it, never
    reduced to fit the text. The text wraps naturally while keeping the same
    font family, font size, spacing and left alignment for short and long
    student/domain values.
    """
    lines = get_text_lines(page)

    # Use the existing normal body paragraph as the formatting reference.
    duration_line = None
    for candidate in lines:
        candidate_text = normalize_text(candidate["text"])
        if "the total duration of the program is" in candidate_text:
            duration_line = candidate
            break

    if duration_line is None:
        raise ValueError("Could not locate the Dates-section body formatting reference.")

    body_size = float(duration_line["size"])
    color = rgb_from_pdf_color(duration_line["color"])

    # Clear only the existing offer paragraph area: from the original
    # 'Program in the field...' line up to the next body paragraph.
    clear_top = pymupdf.Rect(offer_line["bbox"]).y0 - 2
    clear_bottom = pymupdf.Rect(duration_line["bbox"]).y0 - 8
    clear_rect = pymupdf.Rect(
        offer_line["origin"][0] - 2,
        clear_top,
        page.rect.width - 24,
        clear_bottom,
    )

    page.add_redact_annot(clear_rect, fill=(1, 1, 1))
    page.apply_redactions()

    text_box = pymupdf.Rect(
        offer_line["origin"][0],
        clear_top,
        page.rect.width - 24,
        clear_bottom,
    )

    result = page.insert_textbox(
        text_box,
        new_text,
        fontfile=font_file,
        fontsize=body_size,
        color=color,
        align=pymupdf.TEXT_ALIGN_LEFT,
        lineheight=1.0,
        overlay=True,
    )

    if result < 0:
        raise ValueError(
            "The Dates-section offer paragraph does not fit at the original "
            "font size. The font was intentionally not reduced."
        )


# ============================================================
# WITHOUT HOURS
#
# IMPORTANT: WITH HOURS SECTION ABOVE IS LEFT UNCHANGED.
# ============================================================

def edit_without_hours(data):
    """Generate the WITHOUT HOURS letter only. WITH HOURS is untouched."""

    template = TEMPLATE_DIR / "without_hours.pdf"

    if not template.exists():
        raise FileNotFoundError(
            "WITHOUT HOURS PDF template not found:\n"
            f"{template}"
        )

    doc = pymupdf.open(template)

    try:
        page = doc[0]
        regular_font = extract_font(template, "OpenSans-Regular")

        # ----------------------------------------------------
        # STUDENT NAME - original behavior
        # ----------------------------------------------------
        line = find_line_any(
            page,
            [
                "This letter is to confirm that Mr/Ms.",
                "This letter is to confirm",
            ],
        )

        # Keep the student-name line independent from the next paragraph.
        # This prevents a long name from forcing the complete sentence into
        # the same text box and changing the visual layout.
        replace_student_name_line(
            page,
            line,
            "This letter is to confirm that Mr/Ms. "
            f"{data['student_name']}",
            regular_font,
        )

        # ----------------------------------------------------
        # DOMAIN - original behavior
        # ----------------------------------------------------
        line = find_line_any(
            page,
            [
                "Program in the field of Digital Marketing",
                "Program in the field of",
            ],
        )

        replace_without_hours_offer_paragraph(
            page,
            line,
            "Has been offered a Training and Internship Program in the field of "
            f"{data['domain']} with Persevex, under the supervision of "
            "Mr. Shanmukh Shekar K C.",
            regular_font,
        )

        # ----------------------------------------------------
        # DURATION - ONLY VALUES ARE CHANGED.
        # ----------------------------------------------------
        total_months = int(
            str(data["duration"]).split()[0]
        )

        replace_without_hours_duration_values(
            page,
            template,
            total_months,
        )

        # ----------------------------------------------------
        # STIPEND
        # ----------------------------------------------------
        stipend = clean_stipend(
            data.get("stipend", "")
        )

        if stipend:
            # Change ONLY the original amount. Keep the original ₹.
            replace_without_hours_stipend_amount(
                page,
                template,
                stipend,
            )

            # Dates stay exactly in their original position.
            replace_without_hours_date_paragraph(
                page,
                template,
                data["start_date"],
                data["end_date"],
                shift_up=0,
            )

        else:
            # Remove the entire wrapped stipend sentence, including the
            # trailing "The" on the preceding line.
            previous_line, stipend_line = find_without_hours_stipend_parts(page)

            # Date paragraph is currently below TWO removed visual lines:
            #   1) the trailing "The"
            #   2) the stipend sentence line
            # Move the date paragraph to the old stipend-line position.
            date_first, date_second = find_without_hours_date_lines(page)

            # Keep a small blank space between the paragraph above and
            # the date paragraph when no stipend is entered.
            # Only the vertical position is adjusted; all existing fonts,
            # sizes, formatting and other sections remain unchanged.
            desired_gap = 10
            shift_up = (
                date_first["origin"][1]
                - stipend_line["origin"][1]
                - desired_gap
            )

            remove_without_hours_stipend_sentence(page)

            replace_without_hours_date_paragraph(
                page,
                template,
                data["start_date"],
                data["end_date"],
                shift_up=shift_up,
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
    allowed_endpoints = {
        "login",
        "static",
    }

    if request.endpoint in allowed_endpoints:
        return None

    if session.get("authenticated") is True:
        return None

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
    try:
        response = (
            supabase
            .table("email_history")
            .select("email_status")
            .execute()
        )
        records = response.data or []

        success_count = sum(
            1
            for record in records
            if str(record.get("email_status") or "").lower() == "sent"
        )
        failed_count = sum(
            1
            for record in records
            if str(record.get("email_status") or "").lower() == "failed"
        )
    except Exception as exc:
        print("HEADER STATUS COUNT ERROR:", repr(exc))
        success_count = 0
        failed_count = 0

    return render_template(
        "index.html",
        success_count=success_count,
        failed_count=failed_count,
    )

# ============================================================
# LIVE EMAIL STATUS COUNTS
# ============================================================

@app.get("/api/email-status-counts")
def email_status_counts():

    try:

        response = (
            supabase
            .table("email_history")
            .select("email_status")
            .execute()
        )

        records = response.data or []

        success_count = sum(
            1
            for record in records
            if str(
                record.get("email_status") or ""
            ).lower() == "sent"
        )

        failed_count = sum(
            1
            for record in records
            if str(
                record.get("email_status") or ""
            ).lower() == "failed"
        )

        return jsonify({
            "success": True,
            "success_count": success_count,
            "failed_count": failed_count
        })

    except Exception as exc:

        print(
            "LIVE STATUS COUNT ERROR:",
            repr(exc)
        )

        return jsonify({
            "success": False,
            "success_count": 0,
            "failed_count": 0
        }), 500

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
# EMAIL HISTORY HELPERS
# ============================================================

def get_previous_email_record(email):

    try:

        response = (
            supabase
            .table("email_history")
            .select("*")
            .eq(
                "student_email",
                email
            )
            .order(
                "created_at",
                desc=True
            )
            .limit(1)
            .execute()
        )

        if response.data:

            return response.data[0]

        return None

    except Exception as exc:

        print(
            "DUPLICATE EMAIL CHECK ERROR:",
            repr(exc)
        )

        return None
# ============================================================
# SEND EMAIL
# ============================================================

@app.post("/send-email")
def send_email():

    data = request.get_json(force=True) or {}

    # --------------------------------------------------------
    # GET DATA
    # --------------------------------------------------------

    filename = data.get("filename") or ""

    recipient = (
        data.get("student_email") or ""
    ).strip().lower()

    student_name = (
        data.get("student_name") or "Student"
    ).strip()

    send_again = (
        data.get("send_again") is True
    )

    # --------------------------------------------------------
    # HELPER FUNCTION
    # --------------------------------------------------------

    def build_history_data(
        status,
        sent_at=None,
        send_count=0,
        error_message=None
    ):

        safe_filename = (
            Path(filename).name
            if filename
            else None
        )

        offer_letter_id = (
            data.get("offer_letter_id")
            or (
                Path(safe_filename).stem
                if safe_filename
                else None
            )
        )

        return {
            "student_name": student_name,
            "student_email": recipient,
            "phone_number": data.get("phone_number"),
            "college_name": data.get("college_name"),

            "internship_domain": (
                data.get("internship_domain")
                or data.get("domain")
            ),

            "internship_duration": (
                data.get("internship_duration")
                or data.get("duration")
            ),

            "start_date": data.get("start_date"),
            "end_date": data.get("end_date"),

            "offer_letter_type": (
                data.get("offer_letter_type")
                or data.get("letter_type")
            ),

            "email_status": status,
            "sent_at": sent_at,
            "offer_letter_id": offer_letter_id,
            "pdf_filename": safe_filename,
            "send_count": send_count,
            "error_message": error_message
        }

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    if not filename:
        return jsonify({
            "success": False,
            "error": "Generated PDF is required."
        }), 400

    if not recipient:
        return jsonify({
            "success": False,
            "error": "Student email is required."
        }), 400

    # --------------------------------------------------------
    # SAFE FILENAME
    # --------------------------------------------------------

    filename = Path(filename).name

    pdf_path = GENERATED_DIR / filename

    if not pdf_path.exists():
        return jsonify({
            "success": False,
            "error": "Generated PDF not found. Please generate the letter again."
        }), 404

    # --------------------------------------------------------
    # CHECK PREVIOUS EMAIL
    # --------------------------------------------------------

    previous_record = get_previous_email_record(recipient)

    # --------------------------------------------------------
    # DUPLICATE CHECK
    # --------------------------------------------------------

    if previous_record and not send_again:
        return jsonify({
            "success": False,
            "duplicate": True,
            "message": "An offer letter record already exists for this email address.",
            "previous_record": previous_record
        }), 409

    # --------------------------------------------------------
    # EMAIL CONFIGURATION
    # --------------------------------------------------------

    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return jsonify({
            "success": False,
            "error": "Email is not configured."
        }), 500

    # ========================================================
    # PREPARE EMAIL
    # ========================================================

    try:

        message = EmailMessage()

        message["From"] = f"Persevex LLP <{SENDER_EMAIL}>"

        message["To"] = recipient

        message["Subject"] = "Internship Acceptance Letter"

        # ----------------------------------------------------
        # PLAIN TEXT EMAIL
        # ----------------------------------------------------

        message.set_content(
f"""Dear {student_name},

Warm greetings from Persevex LLP!

We are delighted to inform you that your Internship Acceptance Letter has been attached with this email. Please review the document carefully and feel free to reach out if you need any clarification.

We are truly excited to welcome you onboard at Persevex and look forward to your active contribution and learning journey with us. Your enthusiasm and dedication will play a key role in shaping meaningful experiences throughout this internship.

Kindly acknowledge the receipt of this email and confirm your acceptance at your earliest convenience.

Wishing you a wonderful start with us!

Warm regards,
Team Persevex"""
        )

        # ----------------------------------------------------
        # HTML EMAIL
        # ----------------------------------------------------

        message.add_alternative(
f"""
<!DOCTYPE html>
<html>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 16px; line-height: 1.6; color: #333333;">

<p>Dear {student_name},</p>

<p>Warm greetings from Persevex LLP!</p>

<p>
We are delighted to inform you that your
<strong>Internship Acceptance Letter</strong>
has been attached with this email. Please review the document carefully
and feel free to reach out if you need any clarification.
</p>

<p>
We are truly excited to welcome you onboard at Persevex and look forward
to your active contribution and learning journey with us. Your enthusiasm
and dedication will play a key role in shaping meaningful experiences
throughout this internship.
</p>

<p>
Kindly acknowledge the receipt of this email and confirm your acceptance
at your earliest convenience.
</p>

<p>Wishing you a wonderful start with us!</p>

<p>
Warm regards,<br>
<strong>Team Persevex</strong>
</p>

</body>
</html>
""",
            subtype="html"
        )

        # ----------------------------------------------------
        # ATTACH PDF
        # ----------------------------------------------------

        message.add_attachment(
            pdf_path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=filename
        )

    except Exception as exc:

        print("EMAIL PREPARATION ERROR:", repr(exc))

        return jsonify({
            "success": False,
            "error": str(exc),
            "email_status": "failed"
        }), 500

    # ========================================================
    # SEND EMAIL USING SMTP
    # ========================================================

    try:

        print("========================================")
        print("CONNECTING TO SMTP")
        print("FROM:", SENDER_EMAIL)
        print("TO:", recipient)
        print("========================================")

        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30
        ) as smtp:

            smtp.login(
                SENDER_EMAIL,
                SENDER_PASSWORD
            )

            smtp.send_message(message)

        print("SMTP EMAIL SENT SUCCESSFULLY")

    except Exception as exc:

        error_message = str(exc)

        print("========================================")
        print("SMTP EMAIL FAILED")
        print("Error:", repr(exc))
        print("========================================")

        # ----------------------------------------------------
        # SAVE FAILED HISTORY
        # ----------------------------------------------------

        try:

            if previous_record:

                previous_id = previous_record.get("id")

                previous_send_count = int(
                    previous_record.get("send_count") or 0
                )

                failed_history_data = build_history_data(
                    status="failed",
                    sent_at=None,
                    send_count=previous_send_count + 1,
                    error_message=error_message
                )

                supabase.table(
                    "email_history"
                ).update(
                    failed_history_data
                ).eq(
                    "id",
                    previous_id
                ).execute()

            else:

                failed_history_data = build_history_data(
                    status="failed",
                    sent_at=None,
                    send_count=1,
                    error_message=error_message
                )

                supabase.table(
                    "email_history"
                ).insert(
                    failed_history_data
                ).execute()

        except Exception as supabase_error:

            print(
                "FAILED HISTORY SAVE ERROR:",
                repr(supabase_error)
            )

        return jsonify({
            "success": False,
            "error": error_message,
            "email_status": "failed"
        }), 500

    # ========================================================
    # EMAIL SENT SUCCESSFULLY
    # ========================================================

    sent_time = datetime.now(
        timezone.utc
    ).isoformat()

    # --------------------------------------------------------
    # SAVE SENT EMAIL TO IMAP SENT FOLDER
    # --------------------------------------------------------

    try:

        with imaplib.IMAP4_SSL(
            IMAP_HOST,
            993
        ) as imap:

            imap.login(
                SENDER_EMAIL,
                SENDER_PASSWORD
            )

            result = imap.append(
                IMAP_SENT_FOLDER,
                "\\Seen",
                None,
                message.as_bytes()
            )

            print(
                "EMAIL SAVED TO SENT FOLDER:",
                result
            )

    except Exception as imap_error:

        # Email was already sent successfully.
        # Do not mark the entire request as failed.
        print(
            "WARNING: Could not save email to Sent folder:",
            repr(imap_error)
        )

    # --------------------------------------------------------
    # RESEND EMAIL
    # --------------------------------------------------------

    if previous_record and send_again:

        try:

            previous_id = previous_record.get("id")

            previous_send_count = int(
                previous_record.get("send_count") or 0
            )

            update_data = build_history_data(
                status="sent",
                sent_at=sent_time,
                send_count=previous_send_count + 1,
                error_message=None
            )

            supabase.table(
                "email_history"
            ).update(
                update_data
            ).eq(
                "id",
                previous_id
            ).execute()

            print(
                "EMAIL RESEND HISTORY UPDATED SUCCESSFULLY"
            )

        except Exception as supabase_error:

            print(
                "SUPABASE RESEND UPDATE ERROR:",
                repr(supabase_error)
            )

        return jsonify({
            "success": True,
            "resent": True,
            "message": "Email sent again successfully."
        })

    # --------------------------------------------------------
    # NEW EMAIL - SAVE HISTORY
    # --------------------------------------------------------

    try:

        sent_history_data = build_history_data(
            status="sent",
            sent_at=sent_time,
            send_count=1,
            error_message=None
        )

        supabase.table(
            "email_history"
        ).insert(
            sent_history_data
        ).execute()

        print(
            "EMAIL HISTORY SAVED SUCCESSFULLY"
        )

    except Exception as supabase_error:

        print(
            "SUPABASE HISTORY SAVE ERROR:",
            repr(supabase_error)
        )

    # ========================================================
    # SUCCESS RESPONSE
    # ========================================================

    return jsonify({
        "success": True,
        "message": "Email sent successfully."
    })
# ============================================================
# EMAIL HISTORY
# ============================================================

@app.get("/history")
def history():

    try:

        # ----------------------------------------------------
        # GET FILTER VALUES
        # ----------------------------------------------------

        search = (

            request.args.get(
                "search",
                ""
            )

            or

            ""

        ).strip()


        date_filter = (

            request.args.get(
                "date",
                ""
            )

            or

            ""

        ).strip()


        status_filter = (

            request.args.get(
                "status",
                ""
            )

            or

            ""

        ).strip().lower()


        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        try:

            page = int(

                request.args.get(

                    "page",

                    1

                )

            )

        except (

            TypeError,

            ValueError

        ):

            page = 1


        if page < 1:

            page = 1


        PER_PAGE = 25


        # ----------------------------------------------------
        # GET RECORDS FROM SUPABASE
        # ----------------------------------------------------

        response = (

            supabase

            .table(
                "email_history"
            )

            .select(
                "*"
            )

            .order(

                "created_at",

                desc=True

            )

            .execute()

        )


        records = (

            response.data

            or

            []

        )


        # ----------------------------------------------------
        # SEARCH BY STUDENT NAME OR EMAIL
        # ----------------------------------------------------

        if search:


            search_lower = (

                search.lower()

            )


            records = [


                record

                for record in records


                if (

                    search_lower

                    in

                    str(

                        record.get(
                            "student_name"
                        )

                        or

                        ""

                    ).lower()

                    or

                    search_lower

                    in

                    str(

                        record.get(
                            "student_email"
                        )

                        or

                        ""

                    ).lower()

                )

            ]


        # ----------------------------------------------------
        # FILTER BY DATE
        # ----------------------------------------------------

        if date_filter:


            records = [


                record

                for record in records


                if (

                    str(

                        record.get(
                            "created_at"
                        )

                        or

                        ""

                    ).startswith(

                        date_filter

                    )

                    or

                    str(

                        record.get(
                            "sent_at"
                        )

                        or

                        ""

                    ).startswith(

                        date_filter

                    )

                )

            ]


        # ----------------------------------------------------
        # FILTER BY STATUS
        # ----------------------------------------------------

        if status_filter:


            records = [


                record

                for record in records


                if (

                    str(

                        record.get(
                            "email_status"
                        )

                        or

                        ""

                    ).lower()

                    ==

                    status_filter

                )

            ]


        # ----------------------------------------------------
        # GLOBAL FILTERED STATISTICS (BEFORE PAGINATION)
        # ----------------------------------------------------

        total_records = (

            len(
                records
            )

        )


        total_sent = sum(

            1

            for record in records

            if str(
                record.get(
                    "email_status"
                )

                or

                ""

            ).strip().lower()

            ==

            "sent"

        )


        total_failed = sum(

            1

            for record in records

            if str(
                record.get(
                    "email_status"
                )

                or

                ""

            ).strip().lower()

            ==

            "failed"

        )


        # ----------------------------------------------------
        # PAGINATION
        # ----------------------------------------------------

        total_pages = max(

            1,

            (

                total_records

                +

                PER_PAGE

                -

                1

            )

            //

            PER_PAGE

        )


        if page > total_pages:

            page = total_pages


        start_index = (

            page

            -

            1

        ) * PER_PAGE


        end_index = (

            start_index

            +

            PER_PAGE

        )


        history_records = (

            records[

                start_index:end_index

            ]

        )


        return render_template(


            "history.html",


            history_records=
                history_records,


            search=
                search,


            date_filter=
                date_filter,


            status_filter=
                status_filter,


            page=
                page,


            total_pages=
                total_pages,


            total_records=
                total_records,


            total_sent=
                total_sent,


            total_failed=
                total_failed,


            error=
                None

        )


    except Exception as exc:


        print(

            "HISTORY ERROR:",

            repr(
                exc
            )

        )


        return render_template(


            "history.html",


            history_records=
                [],


            search=
                "",


            date_filter=
                "",


            status_filter=
                "",


            page=
                1,


            total_pages=
                1,


            total_records=
                0,


            total_sent=
                0,


            total_failed=
                0,


            error=

                str(
                    exc
                )

        )


# ============================================================
# DELETE HISTORY RECORD
# ============================================================

@app.delete("/history/<int:record_id>")
def delete_history_record(

    record_id

):

    try:


        supabase.table(

            "email_history"

        ).delete().eq(

            "id",

            record_id

        ).execute()


        return jsonify({


            "success":
                True,


            "message":

                "History record deleted successfully."

        })


    except Exception as exc:


        print(

            "DELETE HISTORY ERROR:",

            repr(
                exc
            )

        )


        return jsonify({


            "error":

                str(
                    exc
                )

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