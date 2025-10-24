from flask import Flask, render_template, request, send_file, redirect, url_for
import io
import os
import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Font
import re
from docx import Document
import requests # To talk to the AI
import json     # To handle the AI's JSON response
import time     # For exponential backoff

# This regex finds illegal XML characters that crash openpyxl
ILLEGAL_CHAR_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F\uE000-\uF8FF]')

# --- AI Configuration ---
# This is the (key-less) API endpoint we will use.
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key=AIzaSyD8UiE99zIwqLlUur1gQboSLFj3dfBHnz8"

# This is the JSON "template" we'll ask the AI to fill for each resume.
RESUME_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "name": {"type": "STRING"},
        "email": {"type": "STRING"},
        "phone": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "experience": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "company": {"type": "STRING"},
                    "duration": {"type": "STRING"},
                }
            }
        },
        "education": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "degree": {"type": "STRING"},
                    "institution": {"type": "STRING"},
                    "year": {"type": "STRING"},
                }
            }
        },
        "skills": {
            "type": "ARRAY",
            "items": {"type": "STRING"}
        }
    }
}


app = Flask(__name__)

def clean_text(text):
    """Cleans text of illegal characters for XML."""
    if not text:
        return ""
    return ILLEGAL_CHAR_RE.sub('', text)

def extract_text_from_pdf(pdf_stream):
    """Extracts text from a PDF file stream."""
    text = ""
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text
    return clean_text(text)

def extract_text_from_docx(docx_stream):
    """Extracts text from a DOCX file stream."""
    doc = Document(docx_stream)
    text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
    return clean_text(text)

def get_structured_data_from_ai(resume_text):
    """
    Sends resume text to the Gemini API and asks for structured JSON output.
    """
    system_prompt = (
        "You are an expert resume parser. Your job is to extract key information "
        "from a resume and return it in a structured JSON format. "
        "Do not return any text outside of the JSON object. "
        "If information is not found, return an empty string or empty array."
    )
    user_prompt = f"Please extract the information from this resume:\n\n{resume_text}"

    payload = {
        "contents": [{"parts": [{"text": user_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESUME_SCHEMA
        }
    }

    retries = 3
    delay = 1
    for i in range(retries):
        try:
            # Added a timeout to the request for robustness
            response = requests.post(GEMINI_API_URL, json=payload, timeout=45)
            response.raise_for_status() # Raise an error for bad responses (4xx, 5xx)
            
            result = response.json()
            
            if 'candidates' in result and result['candidates'][0].get('content', {}).get('parts', [{}])[0].get('text'):
                json_text = result['candidates'][0]['content']['parts'][0]['text']
                # The response is a JSON string, so we parse it into a Python dict
                return json.loads(json_text)
            else:
                print(f"AI response was valid but empty or malformed. Response: {result}")
                
        except requests.exceptions.RequestException as e:
            print(f"Error calling Gemini API: {e}. Retrying in {delay}s...")
        except Exception as e:
            # Catch other errors like JSON parsing
            print(f"Error processing AI response: {e}. Response text: {response.text if 'response' in locals() else 'No response'}")

        time.sleep(delay)
        delay *= 2 # Exponential backoff
    
    # If all retries fail, return a default object
    return {"name": "Error: Could not parse resume", "email": "", "phone": "", "summary": f"Failed to process resume text: {resume_text[:100]}..."}


def generate_excel_in_memory(all_resume_data):
    """
    Generates an Excel file in memory from the list of structured resume data.
    """
    wb = Workbook()
    ws = wb.active
    
    # Define headers - these match your new requirements
    headers = [
        "File", "Name", "Email", "Phone", "Skills", 
        "Recent Experience", "All Experience", "Education", "Summary"
    ]
    ws.append(headers)
    
    # Make header bold
    for cell in ws[1]:
        cell.font = Font(bold=True)

    # Populate data
    for data in all_resume_data:
        filename = data.get('filename', 'N/A')
        profile = data.get('profile', {})

        # Flatten skills list into a string
        skills_str = ", ".join(profile.get('skills', []))
        
        # Flatten experience list
        exp_list = []
        for exp in profile.get('experience', []):
            exp_list.append(
                f"{exp.get('title', 'N/A')} at {exp.get('company', 'N/A')} ({exp.get('duration', 'N/A')})"
            )
        exp_str = "\n".join(exp_list) # Use newline for readability in Excel
        recent_exp = exp_list[0] if exp_list else "N/A"
        
        # Flatten education list
        edu_list = []
        for edu in profile.get('education', []):
            edu_list.append(
                f"{edu.get('degree', 'N/A')} from {edu.get('institution', 'N/A')} ({edu.get('year', 'N/A')})"
            )
        edu_str = "\n".join(edu_list)

        row = [
            clean_text(filename),
            clean_text(profile.get('name', 'N/A')),
            clean_text(profile.get('email', 'N/A')),
            clean_text(profile.get('phone', 'N/A')),
            clean_text(skills_str),
            clean_text(recent_exp),
            clean_text(exp_str),
            clean_text(edu_str),
            clean_text(profile.get('summary', 'N/A'))
        ]
        ws.append(row)

    # Autofit column widths
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                # Adjust for multi-line content
                cell_length = max(len(str(line)) for line in str(cell.value).split('\n'))
                if cell_length > max_length:
                    max_length = cell_length
            except:
                pass
        # Set a reasonable max width
        adjusted_width = min((max_length + 2) * 1.2, 60)
        ws.column_dimensions[column_letter].width = max(15, adjusted_width) # Min width of 15

    # Save to memory
    memory_file = io.BytesIO()
    wb.save(memory_file)
    memory_file.seek(0)
    
    print("Excel file generated in memory with AI data.")
    return memory_file

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            return render_template('index.html', error='No file part')
            
        # Get the list of files from the form
        files = request.files.getlist('file')

        if not files or files[0].filename == '':
            return render_template('index.html', error='No file(s) selected')

        all_resume_data = []

        for file in files:
            if file and (file.filename.endswith('.pdf') or file.filename.endswith('.docx')):
                filename = file.filename
                print(f"Processing file: {filename}")
                
                try:
                    if filename.endswith('.pdf'):
                        text = extract_text_from_pdf(file.stream)
                    elif filename.endswith('.docx'):
                        text = extract_text_from_docx(file.stream)
                    else:
                        # This case should be rare but good to have
                        continue 
                    
                    # This is the new AI step!
                    profile_data = get_structured_data_from_ai(text)
                    
                    all_resume_data.append({
                        "filename": filename,
                        "profile": profile_data
                    })
                    
                except Exception as e:
                    print(f"Error processing file {filename}: {e}")
                    all_resume_data.append({
                        "filename": filename,
                        "profile": {"name": f"Error processing file: {e}"}
                    })
            else:
                # This catches any non-pdf/docx files if the browser check fails
                return render_template('index.html', error='Unsupported file format. Please upload .pdf or .docx files only.')

        if not all_resume_data:
             return render_template('index.html', error='No valid files were processed.')

        # Generate the Excel file in memory with all the AI-parsed data
        memory_file = generate_excel_in_memory(all_resume_data)
        
        return send_file(
            memory_file,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            # New filename for the AI-powered version
            download_name='AI_Resumes_Extract.xlsx',
            as_attachment=True
        )
            
    # For GET requests
    return render_template('index.html')

