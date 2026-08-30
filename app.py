from pathlib import Path
import re
import smtplib
import os
from datetime import datetime
from email.message import EmailMessage

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_from_directory,
)

import pymupdf


# ============================================================
# APPLICATION
# ============================================================

app = Flask(__name__)


# ============================================================
# DIRECTORIES
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

TEMPLATE_DIR = BASE_DIR / "pdf_templates"
GENERATED_DIR = BASE_DIR / "generated"
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
    WITH HOURS ONLY:
    Replace ONLY the numeric stipend amount.

    The original master PDF already contains the correct:
      - sentence wrapping
      - OpenSans text
      - NotoSans ₹ glyph
      - spacing between "up to", ₹, and the amount

    Therefore the sentence itself must NEVER be redrawn.
    Only the original numeric amount is redacted and replaced.
    """

    stipend = clean_stipend(stipend)

    if not stipend:
        return

    # Find the original stipend line in the WITH HOURS master.
    line = find_stipend_line(page)

    original_color = rgb_from_pdf_color(
        line["color"]
    )

    original_size = float(
        line["size"]
    )

    # --------------------------------------------------------
    # Find the original numeric span.
    #
    # The master template contains three spans on this line:
    #   OpenSans-Regular -> "offers a ... up to "
    #   NotoSans-Regular -> "₹"
    #   OpenSans-Regular -> "15,000."
    #
    # We intentionally leave the first two spans untouched.
    # --------------------------------------------------------
    amount_span = None

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue

        for pdf_line in block.get("lines", []):
            # The stipend line is identified by its vertical position.
            if abs(pdf_line["bbox"][1] - line["bbox"][1]) > 1.0:
                continue

            for span in pdf_line.get("spans", []):
                text = str(span.get("text", "")).strip()

                if re.fullmatch(r"[\d,]+\.?", text):
                    amount_span = span
                    break

            if amount_span is not None:
                break

        if amount_span is not None:
            break

    if amount_span is None:
        raise ValueError(
            "Could not locate the original numeric stipend amount "
            "in the WITH HOURS template."
        )

    # --------------------------------------------------------
    # Keep the original OpenSans font and original font size.
    # --------------------------------------------------------
    try:
        regular_font = extract_font(
            template,
            "OpenSans-Regular",
        )
    except Exception:
        regular_font = None

    fontsize = float(
        amount_span.get("size", original_size)
    )

    formatted = f"{int(stipend):,}."

    # --------------------------------------------------------
    # Redact ONLY the old numeric amount.
    #
    # IMPORTANT:
    # Do NOT redact the ₹ glyph.
    # Do NOT redact "up to".
    # Do NOT redact "The internship" from the previous line.
    # --------------------------------------------------------
    page.add_redact_annot(
        pymupdf.Rect(amount_span["bbox"]),
        fill=(1, 1, 1),
    )

    page.apply_redactions()

    # --------------------------------------------------------
    # Insert the new amount at the EXACT original numeric
    # baseline. The original ₹ remains at its original position.
    # --------------------------------------------------------
    insert_kwargs = {
        "fontsize": fontsize,
        "color": original_color,
        "overlay": True,
    }

    if regular_font:
        insert_kwargs["fontfile"] = regular_font

    page.insert_text(
        amount_span["origin"],
        formatted,
        **insert_kwargs,
    )

    print(
        "STIPEND: WITH HOURS numeric amount replaced only; "
        "original ₹ glyph and sentence formatting preserved."
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
    """
    WITHOUT HOURS ONLY:
    Replace ONLY the numeric stipend amount.

    IMPORTANT:
    The original ₹ glyph in the master PDF is deliberately left untouched.
    Redrawing the ₹ glyph was causing it to render as a dot in the generated
    WITHOUT HOURS PDF. The original template already contains the correct
    NotoSans ₹ glyph at the exact correct position, so there is no reason to
    remove or redraw it.
    """

    stipend = clean_stipend(stipend)

    _, stipend_line = find_without_hours_stipend_parts(page)

    regular_font = extract_font(
        template,
        "OpenSans-Regular",
    )

    # The original WITHOUT HOURS template contains:
    #   "internship offers a performance-based stipend up to "
    #       -> OpenSans-Regular
    #   "₹"
    #       -> NotoSans-Regular
    #   "25,000."
    #       -> OpenSans-Regular
    #
    # We therefore redact ONLY "25,000." and leave the original ₹ glyph
    # completely untouched.
    amount_span = None

    target_y = stipend_line["bbox"][1]

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue

        for pdf_line in block.get("lines", []):
            if abs(pdf_line["bbox"][1] - target_y) > 1.0:
                continue

            for span in pdf_line.get("spans", []):
                text = str(span.get("text", "")).strip()

                # The original numeric amount is a separate OpenSans span.
                if (
                    re.fullmatch(r"[\d,]+\.?", text)
                    and "₹" not in text
                ):
                    amount_span = span
                    break

            if amount_span is not None:
                break

        if amount_span is not None:
            break

    if amount_span is None:
        raise ValueError(
            "Could not locate the original numeric stipend amount "
            "span in the WITHOUT HOURS template."
        )

    formatted = f"{int(stipend):,}."

    # --------------------------------------------------------
    # IMPORTANT:
    # Do NOT redact the original ₹ span.
    # Only the numeric amount is replaced.
    # --------------------------------------------------------
    page.add_redact_annot(
        pymupdf.Rect(amount_span["bbox"]),
        fill=(1, 1, 1),
    )

    page.apply_redactions()

    # Draw the new numeric amount at the exact original numeric
    # baseline using the original template font and size.
    page.insert_text(
        amount_span["origin"],
        formatted,
        fontfile=regular_font,
        fontsize=amount_span["size"],
        color=rgb_from_pdf_color(amount_span["color"]),
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

# ============================================================
# WITHOUT HOURS
#
# IMPORTANT: WITH HOURS SECTION ABOVE IS LEFT UNCHANGED.
# ============================================================

def edit_without_hours(data):
    """
    WITHOUT HOURS ONLY.

    Uses the original Persevex WITHOUT HOURS PDF as the master.
    Only dynamic values are changed. The WITH HOURS implementation
    is intentionally untouched.

    Fixes included:
      - Keep the complete original introductory sentence.
      - Keep the complete supervision sentence, including "Shekar K C."
      - Change only the student name and domain.
      - Preserve the original duration paragraph formatting.
      - Preserve the original stipend ₹ glyph and change only the amount.
      - Remove the complete stipend sentence when stipend is empty.
      - Preserve the original paragraph gap before the date paragraph.
      - Change only the two dates.
    """

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
        # INTRODUCTION
        #
        # Original:
        # This letter is to confirm that Mr/Ms. Akanksha Priya
        # has been offered a Training and Internship
        #
        # Change ONLY the student name. The complete phrase
        # "has been offered a Training and Internship" is retained.
        # ----------------------------------------------------
        intro_lines = get_text_lines(page)

        intro_line = None
        for line in intro_lines:
            text = normalize_text(line["text"])
            if "this letter is to confirm that mr/ms." in text:
                intro_line = line
                break

        if intro_line is None:
            raise ValueError(
                "Could not find the original student introduction line "
                "in the WITHOUT HOURS template."
            )

        original_intro = str(intro_line["text"])

        # Replace only the name between Mr/Ms. and the preserved phrase.
        name_match = re.search(
            r"(?i)(Mr/Ms\.\s+)(.+?)(\s+has been offered a Training and Internship)\s*$",
            original_intro,
        )

        if not name_match:
            raise ValueError(
                "Could not locate the student name in the original "
                "WITHOUT HOURS introduction."
            )

        new_intro = (
            original_intro[:name_match.start(2)]
            + str(data["student_name"]).strip()
            + original_intro[name_match.end(2):]
        )

        replace_entire_line(
            page,
            intro_line,
            new_intro,
            regular_font,
        )

        # ----------------------------------------------------
        # DOMAIN / SUPERVISION
        #
        # The original template uses two visual lines:
        #
        # Program in the field of Digital Marketing with Persevex,
        # under the supervision of Mr. Shanmukh
        # Shekar K C.
        #
        # Replace ONLY "Digital Marketing".
        # Do not redraw/remove the supervision continuation line.
        # ----------------------------------------------------
        domain_line = None
        lines_now = get_text_lines(page)

        for line in lines_now:
            text = normalize_text(line["text"])
            if "program in the field of" in text:
                domain_line = line
                break

        if domain_line is None:
            raise ValueError(
                "Could not find the domain line in the WITHOUT HOURS template."
            )

        original_domain = str(domain_line["text"])

        domain_match = re.search(
            r"(?i)(Program in the field of\s+)(.+?)(\s+with Persevex,)",
            original_domain,
        )

        if not domain_match:
            raise ValueError(
                "Could not locate the original domain value in the "
                "WITHOUT HOURS template."
            )

        new_domain = (
            original_domain[:domain_match.start(2)]
            + str(data["domain"]).strip()
            + original_domain[domain_match.end(2):]
        )

        replace_entire_line(
            page,
            domain_line,
            new_domain,
            regular_font,
        )

        # ----------------------------------------------------
        # DURATION
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
            # Change ONLY the original numeric amount.
            # The original ₹ glyph remains untouched.
            replace_without_hours_stipend_amount(
                page,
                template,
                stipend,
            )

            replace_without_hours_date_paragraph(
                page,
                template,
                data["start_date"],
                data["end_date"],
                shift_up=0,
            )

        else:
            # Remove the complete wrapped stipend sentence.
            previous_line, stipend_line = find_without_hours_stipend_parts(page)

            # The original date paragraph is located before removal.
            find_without_hours_date_lines(page)

            # The original template has a paragraph gap after the
            # stipend paragraph. Once the two stipend visual lines
            # are removed, move the date paragraph upward by the
            # exact distance between those two visual lines.
            shift_up = (
                stipend_line["origin"][1]
                - previous_line["origin"][1]
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
                    "Email is not configured. "
                    "Set the PERSEVEX_GMAIL_APP_PASSWORD "
                    "environment variable to your Gmail App Password."
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