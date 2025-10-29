import uuid
import os
import io
import re
import json
import time
from flask import Flask, request, render_template, jsonify, send_from_directory, redirect, url_for, send_file
from werkzeug.utils import secure_filename
import pdfplumber
from docx import Document
from openpyxl import Workbook
from openpyxl.styles import Font
import requests
from redis import Redis
import rq # Redis Queue

# --- Setup ---
app = Flask(__name__)

# --- NEW: Setup Redis & RQ (The "To-Do List") ---
# Render will provide this URL as an environment variable
REDIS_URL = os.environ.get('REDIS_URL')
if not REDIS_URL:
    raise RuntimeError("REDIS_URL environment variable not set.")

app.redis = Redis.from_url(REDIS_URL)
app.queue = rq.Queue('resume-processing', connection=app.redis)

# --- AI & Text Extraction (No changes here) ---
API_KEY = os.environ.get('GOOGLE_API_KEY', '')
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
ILLEGAL_CHAR_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F\uE000-\uF8FF]')
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

# --- This function will be run by the WORKER, not the web app ---
# We define it here so the 'app' module context is shared.

def clean_text(text):
    if not isinstance(text, str): return ""
    return ILLEGAL_CHAR_RE.sub('', text)

def extract_text_from_pdf(pdf_stream):
    text = ""
    try:
        with pdfplumber.open(pdf_stream) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        print(f"Error reading PDF: {e}")
        return ""
    return clean_text(text)

def extract_text_from_docx(docx_stream):
    text = ""
    try:
        doc = Document(docx_stream)
        for para in doc.paragraphs:
            text += para.text + "\n"
    except Exception as e:
        print(f"Error reading DOCX: {e}")
        return ""
    return clean_text(text)

def get_structured_data_from_ai(resume_text):
    if not resume_text or not API_KEY:
        print("Skipping AI call: No text or no API key.")
        return {"name": "Error: No text or API key", "email": "", "phone": "", "summary": ""}
    
    payload = {
        "contents": [{ "parts": [{ "text": f"Extract the relevant information from this resume text:\n\n{resume_text}" }] }],
        "generationConfig": { "responseMimeType": "application/json", "responseSchema": RESUME_SCHEMA }
    }
    
    retries = 3
    for i in range(retries):
        try:
            response = requests.post(GEMINI_API_URL, json=payload, headers={'Content-Type': 'application/json'}, timeout=60)
            if response.status_code == 200:
                json_text = response.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                return json.loads(json_text)
            else:
                print(f"Error calling Gemini API: {response.status_code} {response.text}. Retrying in {i+1}s...")
                time.sleep(i + 1)
        except Exception as e:
            print(f"Error in get_structured_data_from_ai: {e}")
            time.sleep(i + 1)

    print("Gemini API call failed after all retries.")
    return {"name": "Error: AI API call failed", "email": "", "phone": "", "summary": ""}

def generate_excel_in_memory(all_resume_data):
    wb = Workbook()
    ws = wb.active
    headers = ["Filename", "Name", "Email", "Phone", "Summary", "Skills", "Recent Experience", "Education"]
    ws.append(headers)
    
    for cell in ws["1:1"]: cell.font = Font(bold=True)

    for data in all_resume_data:
        if not data: continue
        skills = ", ".join(data.get('skills', []))
        exp_list = data.get('experience', [])
        exp_str = ""
        if exp_list:
            first_exp = exp_list[0]
            exp_str = f"{first_exp.get('title', 'N/A')} at {first_exp.get('company', 'N/A')} ({first_exp.get('duration', 'N/A')})"
        
        edu_list = data.get('education', [])
        edu_str = ""
        if edu_list:
            first_edu = edu_list[0]
            edu_str = f"{first_edu.get('degree', 'N/A')}, {first_edu.get('institution', 'N/A')}"

        ws.append([
            data.get('filename', 'N/A'), data.get('name', 'N/A'), data.get('email', 'N/A'),
            data.get('phone', 'N/A'), data.get('summary', 'N/A'), skills, exp_str, edu_str
        ])

    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length: max_length = len(str(cell.value))
            except: pass
        adjusted_width = (max_length + 2)
        ws.column_dimensions[column].width = min(adjusted_width, 60)
    
    # Save to a memory buffer
    memory_file = io.BytesIO()
    wb.save(memory_file)
    memory_file.seek(0)
    return memory_file

# --- NEW: This is the long-running job for the worker ---
def process_resumes_job(job_id, file_keys):
    """
    This function is run by the BACKGROUND WORKER.
    It fetches files from Redis, processes them, and saves the result to Redis.
    """
    print(f"Worker starting job {job_id} with {len(file_keys)} files.")
    all_resume_data = []
    
    # Must connect to Redis *inside the worker*
    redis_conn = Redis.from_url(os.environ.get('REDIS_URL'))
    
    file_keys_to_delete = []
    
    try:
        for key, filename in file_keys.items():
            print(f"Processing file: {filename} (key: {key})")
            file_bytes = redis_conn.get(key)
            
            if not file_bytes:
                print(f"Error: File bytes not found in Redis for key {key}")
                continue
                
            file_stream = io.BytesIO(file_bytes)
            text = ""
            
            try:
                if filename.endswith('.pdf'):
                    text = extract_text_from_pdf(file_stream)
                elif filename.endswith('.docx'):
                    text = extract_text_from_docx(file_stream)
                
                if text:
                    ai_data = get_structured_data_from_ai(text)
                    ai_data['filename'] = filename
                    all_resume_data.append(ai_data)
            
            except Exception as e:
                print(f"CRITICAL: Failed to process {filename}. Error: {e}")
                all_resume_data.append({"name": f"Error processing {filename}", "email": "", "phone": "", "summary": str(e)})
            
            finally:
                # Add key to delete list even if it failed
                file_keys_to_delete.append(key)

        if not all_resume_data:
            print(f"Job {job_id} resulted in no data.")
            redis_conn.set(f"job:{job_id}:result", "error: no data processed", ex=3600)
            return

        # Save the final Excel file *in memory*
        excel_memory_file = generate_excel_in_memory(all_resume_data)
        excel_bytes = excel_memory_file.getvalue()
        
        # Save the final Excel bytes *to Redis*
        result_key = f"job:{job_id}:result"
        redis_conn.set(result_key, excel_bytes, ex=3600) # Keep result for 1 hour
        
        print(f"Worker finished job {job_id}. Result stored in Redis key {result_key}")

    except Exception as e:
        print(f"WORKER FAILED: {e}")
        redis_conn.set(f"job:{job_id}:result", f"error: {e}", ex=3600)
    
    finally:
        # Clean up the uploaded files from Redis
        if file_keys_to_delete:
            redis_conn.delete(*file_keys_to_delete)
        print(f"Cleaned up {len(file_keys_to_delete)} file keys from Redis.")


# --- Web App Routes ---

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            return jsonify({"error": "No file part in the request."}), 400
        
        files = request.files.getlist('file')
        if not files or all(f.filename == '' for f in files):
            return jsonify({"error": "No files selected."}), 400

        job_id = str(uuid.uuid4())
        file_keys = {} # To store { 'redis_key': 'original_filename.pdf' }
        
        try:
            with app.redis.pipeline() as pipe:
                for file in files:
                    if file and (file.filename.endswith('.pdf') or file.filename.endswith('.docx')):
                        filename = secure_filename(file.filename)
                        file_bytes = file.read()
                        
                        # Check against Redis free tier limit (25MB)
                        if len(file_bytes) > 20_000_000: # 20MB limit for safety
                             return jsonify({"error": f"File '{filename}' is too large. Max 20MB."}), 413
                        
                        redis_key = f"job:{job_id}:file:{uuid.uuid4()}"
                        pipe.set(redis_key, file_bytes, ex=3600) # Expire in 1 hour
                        file_keys[redis_key] = filename
            
            pipe.execute() # Execute all file saves at once
        
        except Exception as e:
            print(f"Error saving to Redis: {e}")
            return jsonify({"error": "Failed to save files for processing. Check Redis connection."}), 500

        if not file_keys:
            return jsonify({"error": "No valid .pdf or .docx files found."}), 400

        # Add the job to the Redis "To-Do List"
        app.queue.enqueue('app.process_resumes_job', job_id, file_keys, job_timeout=1800) # 30 min timeout

        # Respond *immediately* with the job ID
        return jsonify({"success": True, "job_id": job_id})
            
    return render_template('index.html')

@app.route('/results')
def results_page():
    """Renders the results page. JS will poll for status."""
    return render_template('results.html')

@app.route('/status/<job_id>')
def job_status(job_id):
    """
    Called by the results.html page's JavaScript to check if the job is done.
    """
    result_key = f"job:{job_id}:result"
    result = app.redis.get(result_key)
    
    if result:
        # Check if the result was an error message
        if result.startswith(b"error:"):
            return jsonify({"status": "failed", "error": result.decode('utf-8')})
        
        # Success!
        return jsonify({"status": "complete", "download_url": url_for('download_file', job_id=job_id)})
    else:
        # Job not done, check if it failed in the RQ queue
        try:
            job = rq.Job.fetch(job_id, connection=app.redis)
            status = job.get_status()
            if status == 'failed':
                return jsonify({"status": "failed", "error": "Job failed during processing."})
        except:
             pass # Job not found, means it's pending or finished
        
        return jsonify({"status": "pending"})

@app.route('/download/<job_id>')
def download_file(job_id):
    """
    Serves the final Excel file from Redis.
    """
    result_key = f"job:{job_id}:result"
    excel_bytes = app.redis.get(result_key)
    
    if not excel_bytes or excel_bytes.startswith(b"error:"):
        return redirect(url_for('upload_file'))
    
    # We can delete the key now, making it a one-time download
    app.redis.delete(result_key)

    return send_file(
        io.BytesIO(excel_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name="AI_Resumes_Extract.xlsx",
        as_attachment=True
    )

