import io
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib import colors


def create_corner_watermark_page(width, height, company_name):
    """
    Generate an in-memory single-page PDF with a light, semi-transparent
    watermark containing the company name in the corner.
    """
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(width, height))
    c.saveState()

    # Semi-transparent primary slate/navy tone
    watermark_color = colors.HexColor("#1e293b")
    c.setFillColor(watermark_color, alpha=0.22)
    c.setStrokeColor(watermark_color, alpha=0.22)

    w = float(width)
    h = float(height)

    # 1. Bottom-Right Corner Box & Text
    box_width = 240
    box_height = 36
    margin = 16

    x = max(margin, w - box_width - margin)
    y = max(margin, margin)

    # Rounded rectangle badge in corner
    c.setLineWidth(1)
    c.roundRect(x, y, box_width, box_height, 4, stroke=1, fill=0)

    # Header label in box
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x + 10, y + 22, company_name.upper())

    # Sub-text
    c.setFont("Helvetica", 7)
    c.drawString(x + 10, y + 10, "OFFICIAL ENGINEERING DRAWING LIBRARY")

    # 2. Subtle diagonal watermark across top margin or upper corner
    c.saveState()
    top_x = max(margin, w - 260)
    top_y = max(margin, h - 28)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(top_x, top_y, f"PROPERTY OF {company_name.upper()}")
    c.restoreState()

    c.restoreState()
    c.save()
    packet.seek(0)
    return packet


def apply_company_watermark(pdf_file_path_or_bytes, company_name):
    """
    Apply a light, semi-transparent watermark to every page of the given PDF.
    Returns BytesIO object containing the watermarked PDF.
    """
    if isinstance(pdf_file_path_or_bytes, (str, bytes)):
        if isinstance(pdf_file_path_or_bytes, str):
            reader = PdfReader(pdf_file_path_or_bytes)
        else:
            reader = PdfReader(io.BytesIO(pdf_file_path_or_bytes))
    elif hasattr(pdf_file_path_or_bytes, 'read'):
        pdf_file_path_or_bytes.seek(0)
        reader = PdfReader(pdf_file_path_or_bytes)
    else:
        raise ValueError("Invalid PDF input source.")

    writer = PdfWriter()

    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)

        watermark_packet = create_corner_watermark_page(width, height, company_name)
        watermark_reader = PdfReader(watermark_packet)
        watermark_page = watermark_reader.pages[0]

        page.merge_page(watermark_page)
        writer.add_page(page)

    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)
    return output_stream
