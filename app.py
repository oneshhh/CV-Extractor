from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify
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
# We no longer need uuid or a file_cache
# import uuid  <- REMOVED

# --- Read API Key from Environment ---
API_KEY = os.environ.get('GOOGLE_API_KEY', '')
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"

# This regex finds illegal XML characters that crash openpyxl
# We must re-create this properly
ILLEGAL_CHAR_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F\uE000-\uF8FF]')

# This is the JSON "template" we'll ask the AI to fill for each resume.
RESUME_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "name": { "type": "STRING" },
        "email": { "type": "STRING" },
        "phone": { "type": "STRING" },
        "summary": { "type": "STRING", "description": "A 2-3 sentence summary of the candidate." },
        "skills": { 
            "type": "ARRAY", 
            "items": { "type": "STRING" },
            "description": "A list of key skills and technologies."
        },
        "experience": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": { "type": "STRING" },
                    "company": { "type": "STRING" },
                    "duration": { "type": "STRING", "description": "e.g., 'Jan 2020 - Present' or '3 years'" }
                }
            },
            "description": "A list of relevant work experiences."
        },
        "education": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "degree": { "type": "STRING" },
                    "institution": { "type": "STRING" }
                }
            },
            "description": "A list of educational qualifications."
        }
    }
}


app = Flask(__name__)
# file_cache = {} <- REMOVED, no longer needed


def clean_text(text):
    """
    Removes illegal XML characters from a string.
    """
    if not isinstance(text, str):
        return ""
    return ILLEGAL_CHAR_RE.sub('', text)

def extract_text_from_pdf(pdf_stream):
    """
    Extracts text from a PDF file stream and cleans it.
    """
    text = ""
    try:
        with pdfplumber.open(pdf_stream) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        print(f"Error reading PDF: {e}")
        return "" # Return empty string on failure
    return clean_text(text)

def extract_text_from_docx(docx_stream):
    """
    Extracts text from a DOCX file stream and cleans it.
    """
    text = ""
    try:
        doc = Document(docx_stream)
        for para in doc.paragraphs:
            text += para.text + "\n"
    except Exception as e:
        print(f"Error reading DOCX: {e}")
        return "" # Return empty string on failure
    return clean_text(text)

def get_structured_data_from_ai(resume_text):
    """
    Sends resume text to the Gemini API and asks for structured JSON data.
    """
    if not resume_text or not API_KEY:
        print("Skipping AI call: No text or no API key.")
        return {"name": "Error: No text or API key", "email": "", "phone": "", "summary": ""}

    payload = {
        "contents": [{
            "parts": [{ "text": f"Extract the relevant information from this resume text:\n\n{resume_text}" }]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESUME_SCHEMA
        }
    }
    
    retries = 3
    for i in range(retries):
        try:
            response = requests.post(GEMINI_API_URL, json=payload, headers={'Content-Type': 'application/json'})
            
            if response.status_code == 200:
                response_json = response.json()
                json_text = response_json.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                return json.loads(json_text)
            else:
                print(f"Error calling Gemini API: {response.status_code} {response.text}. Retrying in {i+1}s...")
                time.sleep(i + 1)
        
        except requests.exceptions.RequestException as e:
            print(f"Network error calling Gemini API: {e}. Retrying in {i+1}s...")
            time.sleep(i + 1)
        except json.JSONDecodeError as e:
            print(f"Error decoding Gemini response: {e}. Response was: {json_text}")
            return {"name": "Error: Could not parse AI response", "email": "", "phone": "", "summary": ""}
        except Exception as e:
            print(f"Unknown error in get_structured_data_from_ai: {e}")
            return {"name": "Error: Unknown AI processing error", "email": "", "phone": "", "summary": ""}

    print("Gemini API call failed after all retries.")
    return {"name": "Error: AI API call failed", "email": "", "phone": "", "summary": f"Failed to process resume text: {resume_text[:100]}..."}


def generate_excel_in_memory(all_resume_data):
    """
    Generates an Excel file in memory from the structured resume data.
    """
    wb = Workbook()
    ws = wb.active
    
    # Define headers from our schema
    headers = ["Filename", "Name", "Email", "Phone", "Summary", "Skills", "Recent Experience", "Education"]
    ws.append(headers)
    
    # Apply bold font to headers
    for cell in ws["1:1"]:
        cell.font = Font(bold=True)

    # Add data rows
    for data in all_resume_data:
        if not data:
            continue
            
        # Extract skills (list to string)
        skills = ", ".join(data.get('skills', []))
        
        # Extract recent experience (list of objects to string)
        exp_list = data.get('experience', [])
        exp_str = ""
        if exp_list:
            first_exp = exp_list[0]
            exp_str = f"{first_exp.get('title', 'N/A')} at {first_exp.get('company', 'N/A')} ({first_exp.get('duration', 'N/A')})"
        
        # Extract education (list of objects to string)
        edu_list = data.get('education', [])
        edu_str = ""
        if edu_list:
            first_edu = edu_list[0]
            edu_str = f"{first_edu.get('degree', 'N/A')}, {first_edu.get('institution', 'N/A')}"

        row = [
            data.get('filename', 'N/A'), # Filename we added
            data.get('name', 'N/A'),
            data.get('email', 'N/A'),
            data.get('phone', 'N/A'),
            data.get('summary', 'N/A'),
            skills,
            exp_str,
            edu_str
        ]
        ws.append(row)

    # Autofit columns (simple version)
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter # Get column letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2)
        ws.column_dimensions[column].width = min(adjusted_width, 60) # Cap width at 60

    # Save to a memory buffer
    memory_file = io.BytesIO()
    wb.save(memory_file)
    memory_file.seek(0)
    return memory_file

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        try:
            if 'file' not in request.files:
                return jsonify({"error": "No file part in the request."}), 400
            
            files = request.files.getlist('file')
            all_resume_data = []

            if not files or all(f.filename == '' for f in files):
                return jsonify({"error": "No files selected."}), 400

            for file in files:
                if file and (file.filename.endswith('.pdf') or file.filename.endswith('.docx')):
                    print(f"Processing file: {file.filename}")
                    text = ""
                    try:
                        if file.filename.endswith('.pdf'):
                            text = extract_text_from_pdf(file.stream)
                        elif file.filename.endswith('.docx'):
                            text = extract_text_from_docx(file.stream)
                        
                        if text:
                            ai_data = get_structured_data_from_ai(text)
                            ai_data['filename'] = file.filename # Add filename for context
                            all_resume_data.append(ai_data)
                        
                    except BaseException as e: # Catch BaseException to stop Gunicorn crashes
                        print(f"CRITICAL: Failed to process file {file.filename}. Error: {e}")
                        # Don't stop the whole batch, just skip this file.
                        all_resume_data.append({"name": f"Error processing {file.filename}", "email": "", "phone": "", "summary": str(e)})

            if not all_resume_data:
                 return jsonify({"error": "No valid files were processed."}), 400

            # --- REVERTED LOGIC ---
            # Generate the file and send it directly in the response.
            memory_file = generate_excel_in_memory(all_resume_data)
            
            return send_file(
                memory_file,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                download_name="AI_Resumes_Extract.xlsx",
                as_attachment=True
            )
            # --- END OF REVERTED LOGIC ---
            
        except Exception as e:
            print(f"Error in upload_file: {e}")
            return jsonify({"error": str(e)}), 500
            
    # For GET requests, just show the HTML page
    return render_template('index.html')

@app.route('/results')
def results_page():
    """
    Renders the download/success page.
    The file data will be retrieved from sessionStorage by the page's JS.
    """
    return render_template('results.html')

# --- REMOVED /download/<id> route ---
# It's no longer needed as the file is sent directly.

