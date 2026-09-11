"""
Demo Loan Dossier generator for SARFAESI Section 13(2) Legal Workspace.
Generates realistic PDF loan dossiers for instant verification and demonstration.
"""
from __future__ import annotations

import os
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors


def create_sample_dossier(target_dir: str, lan: str = "LAN987654321", borrower: str = "Ramesh Chandra Sharma") -> str:
    """Generate a realistic 3-document loan dossier in target_dir."""
    lead_dir = os.path.join(target_dir, f"Lead_{lan}_{borrower.replace(' ', '_')}")
    os.makedirs(lead_dir, exist_ok=True)
    styles = getSampleStyleSheet()

    # 1. Sanction Letter
    sanction_pdf = os.path.join(lead_dir, "01_Sanction_Letter.pdf")
    doc1 = SimpleDocTemplate(sanction_pdf, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story1 = [
        Paragraph("<b>HDFC BANK LIMITED — CREDIT SANCTION ADVICE</b>", styles["Title"]),
        Spacer(1, 12),
        Paragraph(f"<b>Loan Account No (LAN):</b> {lan}", styles["Normal"]),
        Paragraph("<b>Sanction Date:</b> 15/03/2021 | <b>Disbursal Date:</b> 22/03/2021", styles["Normal"]),
        Spacer(1, 10),
        Paragraph(f"<b>Primary Borrower:</b> {borrower}", styles["Heading2"]),
        Paragraph("<b>Applicant Address:</b> Flat 402, Shanti Kunj Apartments, MG Road, Pune, Maharashtra 411001", styles["Normal"]),
        Spacer(1, 6),
        Paragraph("<b>Co-Applicant 1:</b> Sunita Ramesh Sharma (Spouse)", styles["Normal"]),
        Paragraph("<b>Co-Applicant 1 Address:</b> Flat 402, Shanti Kunj Apartments, MG Road, Pune, Maharashtra 411001", styles["Normal"]),
        Spacer(1, 6),
        Paragraph("<b>Guarantor 1:</b> Vikramaditya Sharma", styles["Normal"]),
        Paragraph("<b>Guarantor 1 Address:</b> B-12, Sector 4, Indirapuram, Ghaziabad, UP 201014", styles["Normal"]),
        Spacer(1, 12),
        Paragraph("<b>CREDIT FACILITY DETAILS:</b>", styles["Heading3"]),
        Table([
            ["Facility Type", "Sanction Amount", "ROI (% p.a.)", "Tenure"],
            ["Housing Term Loan", "Rs. 45,00,000/- (Rupees Forty Five Lakh Only)", "8.75% Floating", "240 Months"],
        ], style=[
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f0f4f8")),
            ('GRID', (0,0), (-1,-1), 1, colors.HexColor("#cccccc")),
            ('PADDING', (0,0), (-1,-1), 6),
        ]),
        Spacer(1, 16),
        Paragraph("<b>Primary Mortgaged Security:</b> Residential Flat No. 402, 4th Floor, Shanti Kunj Apartments, Survey No. 45/2, MG Road, Pune 411001.", styles["Normal"]),
    ]
    doc1.build(story1)

    # 2. Foreclosure Notice (FCL) / Section 13(2) Notice
    fcl_pdf = os.path.join(lead_dir, "02_Foreclosure_FCL_Notice.pdf")
    doc2 = SimpleDocTemplate(fcl_pdf, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story2 = [
        Paragraph("<b>NOTICE UNDER SECTION 13(2) OF THE SARFAESI ACT, 2002</b>", styles["Title"]),
        Spacer(1, 12),
        Paragraph(f"<b>To:</b> {borrower} and Sunita Ramesh Sharma", styles["Heading3"]),
        Paragraph(f"<b>Account LAN:</b> {lan}", styles["Normal"]),
        Paragraph("<b>NPA Classification Date:</b> 30/09/2023", styles["Normal"]),
        Spacer(1, 10),
        Paragraph("<b>STATEMENT OF DUES AND TOTAL OUTSTANDING SUM (TOS) AS OF 31/10/2023:</b>", styles["Heading3"]),
        Table([
            ["Component", "Amount (INR)"],
            ["Future Principal", "38,50,000.00"],
            ["Principal Overdue", "2,45,000.00"],
            ["Interest Overdue", "1,85,600.00"],
            ["Interest on Termination", "42,300.00"],
            ["Late Payment Penal Charges", "15,800.00"],
            ["Cheque Bounce Charges (Inc. GST)", "3,540.00"],
            ["Other Charges (Inc. GST)", "4,720.00"],
            ["Foreclosure Charges", "25,000.00"],
            ["Litigation and Legal Notice Charges", "18,500.00"],
            ["Less: Excess Amount Available", "-5,000.00"],
            ["Total Outstanding Sum (TOS)", "43,85,460.00"],
        ], style=[
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f0f4f8")),
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor("#fff3cd")),
            ('GRID', (0,0), (-1,-1), 1, colors.HexColor("#cccccc")),
            ('ALIGN', (1,0), (1,-1), 'RIGHT'),
            ('PADDING', (0,0), (-1,-1), 5),
        ]),
        Spacer(1, 14),
        Paragraph("DEMAND NOTICE: You are hereby called upon to pay the Total Outstanding Sum of Rs. 43,85,460/- within 60 days from the date of this notice, failing which the Bank shall exercise all enforcement rights under Section 13(4) of the SARFAESI Act.", styles["Normal"]),
    ]
    doc2.build(story2)

    # 3. MODT Deed / Memorandum of Deposit of Title Deeds
    modt_pdf = os.path.join(lead_dir, "03_MODT_Mortgage_Deed.pdf")
    doc3 = SimpleDocTemplate(modt_pdf, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story3 = [
        Paragraph("<b>MEMORANDUM OF DEPOSIT OF TITLE DEEDS (MODT)</b>", styles["Title"]),
        Spacer(1, 12),
        Paragraph(f"<b>Mortgagor:</b> {borrower}", styles["Heading3"]),
        Paragraph(f"<b>Loan Account No:</b> {lan}", styles["Normal"]),
        Spacer(1, 10),
        Paragraph("<b>SCHEDULE OF THE MORTGAGED IMMOVABLE PROPERTY:</b>", styles["Heading3"]),
        Paragraph("All that piece and parcel of Residential Unit bearing Flat No. 402, situated on the Fourth Floor, Building known as 'Shanti Kunj Apartments', standing on Survey No. 45/2, CTS No. 1092, Village Kothrud, Taluka Haveli, District Pune, Maharashtra 411001, admeasuring 1250 sq.ft. built-up area together with one covered car parking space.", styles["Normal"]),
        Spacer(1, 12),
        Paragraph("<b>BOUNDARIES AND 4-WAY COMPASS DIRECTIONS:</b>", styles["Heading3"]),
        Table([
            ["Direction", "Boundary Description"],
            ["North", "Adjacent Society Internal Access Road 30ft wide"],
            ["South", "Open Plot bearing Survey No. 45/3"],
            ["East", "Main Entrance and Passage leading to Staircase and Lift"],
            ["West", "Flat No. 401 occupied by Mr. Joshi"],
        ], style=[
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#e8f4f8")),
            ('GRID', (0,0), (-1,-1), 1, colors.HexColor("#cccccc")),
            ('PADDING', (0,0), (-1,-1), 6),
        ]),
    ]
    doc3.build(story3)

    return lead_dir
