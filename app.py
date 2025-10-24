from flask import Flask, render_template, request, send_file, redirect, url_for
import io
import os
import pdfplumber
from openpyxl import Workbook
import re
from docx import Document

# --- NEW LINE 1 ---
# This regex finds illegal XML characters (like those in your error log)
# that cause openpyxl to crash. We will use it to clean the text.
ILLEGAL_CHAR_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F\uE000-\uF8FF]')

app = Flask(__name__)

def extract_info_from_pdf(pdf_stream):
    """Extracts info from a PDF file stream."""
    with pdfplumber.open(pdf_stream) as pdf:
        text = ""
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text

    # --- NEW LINE 2 ---
    # Clean the extracted text of any illegal characters before returning
    text = ILLEGAL_CHAR_RE.sub('', text)

    email_regex = r'[\w\.-]+@[\w\.-]+'
    phone_regex = r'[\+\(]?[1-9][0-9 .\-\(\)]{8,}[0-9]'

    emails = re.findall(email_regex, text)
    phones = re.findall(phone_regex, text)

    return emails, phones, text

def extract_info_from_docx(docx_stream):
    """Extracts info from a DOCX file stream."""
    doc = Document(docx_stream)
    text = "\n".join([paragraph.text for paragraph in doc.paragraphs])

    # --- NEW LINE 3 ---
    # Also clean the text from DOCX files, just in case
    text = ILLEGAL_CHAR_RE.sub('', text)

    email_regex = r'[\w\.-]+@[\w\.-]+'
    phone_regex = r'[\+\(]?[1-9][0-9 .\-\(\)]{8,}[0-9]'

    emails = re.findall(email_regex, text)
    phones = re.findall(phone_regex, text)

    return emails, phones, text

def generate_excel_in_memory(data):
    """Generates an Excel file in memory and returns the byte stream."""
    wb = Workbook()
    ws = wb.active
    ws.append(["File", "Email", "Phone", "Text"])

    for filename, emails, phones, text in data:
        email_str = ", ".join(set(emails)) if emails else ""
        phone_str = ", ".join(set(phones)) if phones else ""
        text_lines = text.split("\n")

        if not text_lines:
            continue
        
        # Append the first line of text with email and phone info
        # Use .strip() to avoid empty lines from just having illegal chars
        if text_lines[0].strip():
             ws.append([filename, email_str, phone_str, text_lines[0]])

        # Append subsequent lines of text
        for line in text_lines[1:]:
            if line.strip(): # Avoid adding empty lines
                ws.append(["", "", "", line])

    # Autofit column widths
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2) * 1.2
        ws.column_dimensions[column_letter].width = max(10, adjusted_width)

    memory_file = io.BytesIO()
    wb.save(memory_file)
    memory_file.seek(0)
    
    print("Excel file generated in memory.")
    return memory_file

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            return render_template('index.html', error='No file part')
            
        file = request.files['file']

        if file.filename == '':
            return render_template('index.html', error='No file selected')

        if file:
            filename = file.filename
            
            try:
                if filename.endswith('.pdf'):
                    emails, phones, text = extract_info_from_pdf(file.stream)
                elif filename.endswith('.docx'):
                    emails, phones, text = extract_info_from_docx(file.stream)
                else:
                    return render_template('index.html', error='Unsupported file format. Please upload .pdf or .docx')
                
                memory_file = generate_excel_in_memory([(filename, emails, phones, text)])
                
                return send_file(
                    memory_file,
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    download_name='cv_info.xlsx',
                    as_attachment=True
                )
            except Exception as e:
                # Log the error for debugging on Render
                print(f"An error occurred: {e}")
                return render_template('index.html', error='An error occurred while processing the file. It may be corrupted or in an unsupported format.')
            
    return render_template('index.html')

