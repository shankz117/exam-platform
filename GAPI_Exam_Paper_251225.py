import streamlit as st
import google.generativeai as genai
import docx
import json
import base64
import tempfile
import os
import time
import PyPDF2
import io
import urllib.parse
import zlib  # Added for compression

# ==========================================
# CONFIGURATION & SETUP
# ==========================================

st.set_page_config(
    page_title="AI Exam Manager",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ------------------------------------------------------------------
#  API KEY CONFIGURATION
# ------------------------------------------------------------------
DEFAULT_API_KEY = "" 

# ==========================================
# HELPER FUNCTIONS: ENCODING & COMPRESSION
# ==========================================

def compress_and_encode(json_data):
    """Compresses JSON data and returns a URL-safe base64 string."""
    json_str = json.dumps(json_data)
    # Compress the string
    compressed_data = zlib.compress(json_str.encode('utf-8'))
    # Encode to Base64 (URL safe)
    b64_encoded = base64.urlsafe_b64encode(compressed_data).decode('utf-8')
    return b64_encoded

def decode_and_decompress(encoded_str):
    """Decodes and decompresses the string back to JSON."""
    try:
        # 1. Try Decompressing (New Format)
        decoded_data = base64.urlsafe_b64decode(encoded_str)
        decompressed_data = zlib.decompress(decoded_data)
        return json.loads(decompressed_data.decode('utf-8'))
    except Exception:
        # 2. Fallback: Try Standard Base64 (Old Format for backward compatibility)
        try:
            decoded_bytes = base64.b64decode(encoded_str)
            return json.loads(decoded_bytes.decode('utf-8'))
        except Exception:
            return None

# ==========================================
# HELPER FUNCTIONS: LARGE FILE HANDLING
# ==========================================

def upload_to_gemini(path, mime_type, api_key):
    """
    Uploads a local file path to Gemini File API.
    Waits for the file to be processed by Google.
    """
    genai.configure(api_key=api_key)
    try:
        # Upload the file to Google's servers
        gemini_file = genai.upload_file(path, mime_type=mime_type)
        
        # Check processing state - wait for it to be 'ACTIVE'
        # Timeout after 30 seconds to prevent infinite loops
        max_retries = 30
        retry_count = 0
        
        while gemini_file.state.name == "PROCESSING":
            time.sleep(1)
            gemini_file = genai.get_file(gemini_file.name)
            retry_count += 1
            if retry_count > max_retries:
                st.error(f"Timeout processing file: {path}")
                return None
            
        if gemini_file.state.name == "FAILED":
            st.error(f"Gemini processing failed for file: {path}")
            return None
            
        return gemini_file
    except Exception as e:
        st.error(f"Upload to Gemini failed: {e}")
        return None

def split_and_upload_pdf(uploaded_file, api_key, chunk_size=10):
    """
    Splits a large PDF into smaller chunks of 'chunk_size' pages 
    and uploads them individually to Gemini.
    Returns a list of Gemini File objects.
    """
    uploaded_file.seek(0)
    try:
        pdf_reader = PyPDF2.PdfReader(uploaded_file)
        total_pages = len(pdf_reader.pages)
    except Exception as e:
        st.warning(f"Could not read PDF structure ({e}). Uploading as single file.")
        return []

    gemini_files = []
    
    # Create a temporary directory to store chunks
    with tempfile.TemporaryDirectory() as temp_dir:
        status_text = st.empty()
        progress_bar = st.progress(0)
        
        for i in range(0, total_pages, chunk_size):
            chunk_writer = PyPDF2.PdfWriter()
            end_page = min(i + chunk_size, total_pages)
            
            # Add pages to this chunk
            for page_num in range(i, end_page):
                chunk_writer.add_page(pdf_reader.pages[page_num])
            
            # Skip empty chunks
            if len(chunk_writer.pages) == 0:
                continue

            # Save chunk to a temporary file
            chunk_filename = os.path.join(temp_dir, f"chunk_{i}_{end_page}.pdf")
            with open(chunk_filename, "wb") as f:
                chunk_writer.write(f)
            
            # Upload chunk to Gemini
            status_text.text(f"Processing pages {i+1} to {end_page} of {total_pages}...")
            g_file = upload_to_gemini(chunk_filename, "application/pdf", api_key)
            if g_file:
                gemini_files.append(g_file)
            
            # Update progress
            progress_bar.progress(min((i + chunk_size) / total_pages, 1.0))
            
        status_text.empty()
        progress_bar.empty()
        
    return gemini_files

def prepare_content_for_gemini(uploaded_files, api_key):
    """
    Orchestrates file preparation.
    - Large PDFs -> Split & Upload
    - Images -> Upload
    - DOCX -> Text Extraction
    """
    content_parts = []
    
    for file in uploaded_files:
        file_type = file.type
        
        # 1. PDF Handling (With Splitting logic)
        if "pdf" in file_type:
            try:
                with st.spinner(f"Analyzing structure of {file.name}..."):
                    # Split PDF to ensure reliability
                    gemini_files = split_and_upload_pdf(file, api_key)
                    
                    if gemini_files:
                        content_parts.extend(gemini_files)
                    else:
                        # Fallback: Upload whole file via temp if splitting returned nothing
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                            tmp.write(file.getvalue())
                            tmp_path = tmp.name
                        
                        g_file = upload_to_gemini(tmp_path, "application/pdf", api_key)
                        if g_file: content_parts.append(g_file)
                        os.remove(tmp_path)
            except Exception as e:
                st.error(f"Error preparing PDF {file.name}: {e}")

        # 2. Image Handling (Direct Upload via Temp file)
        elif file_type in ["image/png", "image/jpeg", "image/webp", "image/heic"]:
            suffix = "." + file.name.split('.')[-1] if '.' in file.name else ".jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(file.getvalue())
                tmp_path = tmp.name
            
            with st.spinner(f"Uploading image {file.name}..."):
                g_file = upload_to_gemini(tmp_path, file_type, api_key)
                if g_file: content_parts.append(g_file)
            os.remove(tmp_path)

        # 3. DOCX Handling (Text Extraction)
        elif "word" in file_type or "docx" in file_type:
            try:
                doc = docx.Document(file)
                full_text = "\n".join([para.text for para in doc.paragraphs])
                content_parts.append(full_text)
            except Exception as e:
                st.error(f"Error reading Word document {file.name}: {e}")
                
    return content_parts

# ==========================================
# HELPER FUNCTIONS: AI GENERATION
# ==========================================

def generate_exam_paper(content_parts, api_key, num_mcq=10, num_short=3, num_long=3):
    """Calls Gemini API with content to generate questions."""
    
    if not api_key: return None
    genai.configure(api_key=api_key)
    
    system_instruction = f"""
    You are an expert teacher. Create an exam paper based ONLY on the provided documents.
    The documents may be split into multiple parts/pages; treat them as one continuous source.
    
    Requirements:
    1. Create {num_mcq} Multiple Choice Questions (MCQs). Each carries 1 mark. Provide 4 options and the correct answer.
    2. Create {num_short} Short Answer Questions. Each carries 2 marks.
    3. Create {num_long} Long Answer Questions. Each carries 3 marks.
    
    Output Format: return strictly valid JSON. Do not wrap in markdown code blocks.
    {{
        "mcqs": [
            {{"question": "...", "options": ["A", "B", "C", "D"], "correct": "A", "marks": 1}}
        ],
        "short": [
            {{"question": "...", "marks": 2}}
        ],
        "long": [
            {{"question": "...", "marks": 3}}
        ]
    }}
    """
    
    # FIX: Move system_instruction to model initialization
    model = genai.GenerativeModel(
        'gemini-2.5-flash-preview-09-2025',
        system_instruction=system_instruction
    ) 

    # We send only the content parts (files/text) here
    try:
        response = model.generate_content(
            content_parts, 
            generation_config={"response_mime_type": "application/json"},
            request_options={"timeout": 600} 
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"AI Generation failed. Error details: {e}")
        return None

def add_more_questions(current_exam, content_parts, api_key, q_type):
    """Adds one question of a specific type."""
    genai.configure(api_key=api_key)
    
    system_instruction = f"""
    You are an expert teacher. Based on the attached documents, generate 1 NEW {q_type} question.
    Ensure it is different from existing questions.
    """
    
    model = genai.GenerativeModel(
        'gemini-2.5-flash-preview-09-2025',
        system_instruction=system_instruction
    )
    
    prompt = f"""
    Return JSON format:
    {{
        "question": "...", 
        "marks": {1 if q_type == "MCQ" else (2 if q_type == "Short" else 3)},
        "options": ["A", "B", "C", "D"], 
        "correct": "Option"
    }}
    (Include options/correct only if MCQ).
    """
    
    full_contents = content_parts + [prompt]
    
    try:
        response = model.generate_content(
            full_contents, 
            generation_config={"response_mime_type": "application/json"}
        )
        new_q = json.loads(response.text)
        
        if q_type == "MCQ":
            current_exam['mcqs'].append(new_q)
        elif q_type == "Short":
            current_exam['short'].append(new_q)
        elif q_type == "Long":
            current_exam['long'].append(new_q)
            
        return current_exam
    except Exception as e:
        st.error(f"Failed to add question: {e}")
        return current_exam

# ==========================================
# UI COMPONENTS
# ==========================================

def teacher_dashboard(api_key):
    st.title("👨‍🏫 Teacher Dashboard")
    
    if not api_key:
        st.warning("⚠️ **Waiting for API Key:** Please enter your Google Gemini API Key in the sidebar.")
        st.stop()

    st.markdown("Upload materials (PDF, DOCX, Images). Large PDFs will be automatically split and processed.")
    
    # File Uploader
    uploaded_files = st.file_uploader(
        "Upload Study Materials", 
        accept_multiple_files=True,
        type=['pdf', 'docx', 'png', 'jpg', 'jpeg', 'webp']
    )

    # Generation Trigger
    if uploaded_files:
        if st.button("🚀 Generate Exam Paper"):
            # 1. Upload/Prepare files
            content_parts = prepare_content_for_gemini(uploaded_files, api_key)
            st.session_state.uploaded_content_parts = content_parts
            
            # 2. Generate
            if content_parts:
                with st.spinner("Generating Questions from processed files..."):
                    exam_json = generate_exam_paper(content_parts, api_key)
                    if exam_json:
                        st.session_state.exam_data = exam_json
                        st.success("Exam Generated!")
            else:
                st.warning("No valid content found.")

    # Exam Editor / Review
    if st.session_state.exam_data:
        st.divider()
        st.subheader("📝 Edit Exam Paper")
        
        exam = st.session_state.exam_data
        
        # MCQs
        st.markdown("### Multiple Choice Questions")
        for i, q in enumerate(exam['mcqs']):
            # Preview title (100 chars max)
            q_preview = q.get('question', '')[:100] + "..." if len(q.get('question', '')) > 100 else q.get('question', '')
            
            with st.expander(f"Q{i+1}: {q_preview}"):
                col1, col2 = st.columns([4, 1])
                with col1:
                    q['question'] = st.text_area("Question", value=q['question'], key=f"mcq_q_{i}", height=100)
                    while len(q['options']) < 4: q['options'].append("")
                    c_opt1, c_opt2 = st.columns(2)
                    c_opt3, c_opt4 = st.columns(2)
                    with c_opt1: q['options'][0] = st.text_input(f"Option A", value=q['options'][0], key=f"mcq_{i}_o0")
                    with c_opt2: q['options'][1] = st.text_input(f"Option B", value=q['options'][1], key=f"mcq_{i}_o1")
                    with c_opt3: q['options'][2] = st.text_input(f"Option C", value=q['options'][2], key=f"mcq_{i}_o2")
                    with c_opt4: q['options'][3] = st.text_input(f"Option D", value=q['options'][3], key=f"mcq_{i}_o3")
                    curr = q.get('correct', q['options'][0])
                    if curr not in q['options']: curr = q['options'][0]
                    q['correct'] = st.selectbox("Correct Answer", q['options'], index=q['options'].index(curr), key=f"mcq_c_{i}")
                with col2:
                    q['marks'] = st.number_input("Marks", value=q.get('marks', 1), key=f"mcq_m_{i}")

        # Short & Long Questions
        st.markdown("### Short & Long Questions")
        for q_type in ['short', 'long']:
            for i, q in enumerate(exam[q_type]):
                with st.expander(f"{q_type.title()} Q{i+1}"):
                    c1, c2 = st.columns([4,1])
                    with c1: 
                        q['question'] = st.text_area("Question", value=q['question'], key=f"{q_type}_{i}", height=100)
                    with c2: 
                        q['marks'] = st.number_input("Marks", value=q.get('marks', 2), key=f"{q_type}_m_{i}")

        # Action Buttons
        st.divider()
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("➕ Add MCQ"):
                with st.spinner("Adding..."):
                    st.session_state.exam_data = add_more_questions(exam, st.session_state.uploaded_content_parts, api_key, "MCQ")
                    st.rerun()
        with c2:
            if st.button("➕ Add Short Q"):
                with st.spinner("Adding..."):
                    st.session_state.exam_data = add_more_questions(exam, st.session_state.uploaded_content_parts, api_key, "Short")
                    st.rerun()
        with c3:
            if st.button("💾 Publish"):
                # Use new compression function
                st.session_state.exam_link = compress_and_encode(st.session_state.exam_data)
                st.rerun()

        # Sharing Section
        if st.session_state.exam_link:
            st.divider()
            st.success("✅ Exam Published Successfully!")
            st.subheader("🔗 Share Exam")

            st.markdown("""
            **How to share:**
            Enter your IP address or local URL (e.g. `http://192.168.1.5:8501` or `http://localhost:8501`) below to generate a clickable link.
            """)
            
            # Input for Base URL
            base_url = st.text_input("Enter your App URL here:", value="http://localhost:8501")
            
            # Create the full clickable link with query parameter
            encoded_id = urllib.parse.quote(st.session_state.exam_link)
            full_link = f"{base_url}/?exam_id={encoded_id}"
            
            st.write("**Method 1: Clickable Link**")
            st.code(full_link, language="text")
            
            body = f"Please click this link to take your exam:%0A%0A{full_link}"
            
            # WhatsApp and Email Buttons
            sc1, sc2 = st.columns(2)
            with sc1:
                whatsapp_url = f"https://wa.me/?text={body}"
                st.markdown(f'<a href="{whatsapp_url}" target="_blank"><button style="background-color:#25D366; color:white; border:none; padding:10px 20px; border-radius:5px; width:100%;">Share via WhatsApp</button></a>', unsafe_allow_html=True)
            with sc2:
                mailto_url = f"mailto:?subject=Exam Invitation&body={body}"
                st.markdown(f'<a href="{mailto_url}" target="_blank"><button style="background-color:#EA4335; color:white; border:none; padding:10px 20px; border-radius:5px; width:100%;">Share via Email</button></a>', unsafe_allow_html=True)
            
            st.divider()
            st.write("**Method 2: Backup File (Use if link is too long)**")
            st.write("If the link above is too long and breaks, download this file and send it to your students.")
            
            # Provide Download Button for JSON
            exam_json_str = json.dumps(st.session_state.exam_data, indent=2)
            st.download_button(
                label="📥 Download Exam File (.json)",
                data=exam_json_str,
                file_name="exam_paper.json",
                mime="application/json"
            )

def student_view(auto_exam_id=None):
    st.title("🎓 Student Portal")
    
    # Disclaimer about "localhost" for students
    if "localhost" in str(st.query_params):
        st.warning("⚠️ Note: If you are seeing 'localhost' in the address bar, ensure you are on the same computer as the teacher. Otherwise, ask for the correct network link.")

    st.markdown("### Load Exam")
    st.markdown("If you have a link, the exam ID is auto-filled below. If you have an **Exam File**, please upload it.")
    
    # Tab layout for Link vs File
    tab1, tab2 = st.tabs(["🔗 Load via Link/ID", "📂 Upload Exam File"])
    
    exam_data = None
    
    with tab1:
        # If ID came from URL, auto-fill it
        default_val = auto_exam_id if auto_exam_id else ""
        exam_id = st.text_input("Exam ID (Auto-filled from link):", value=default_val)
        
        if exam_id:
            if "%" in exam_id: exam_id = urllib.parse.unquote(exam_id)
            # Use new decompression function
            exam_data = decode_and_decompress(exam_id)
            if not exam_data:
                st.error("Invalid Exam ID. The link might be broken or incomplete.")

    with tab2:
        uploaded_exam = st.file_uploader("Upload .json Exam File (sent by teacher)", type=['json'])
        if uploaded_exam:
            try:
                exam_data = json.load(uploaded_exam)
            except Exception:
                st.error("Invalid file format.")

    # Render Exam if Data Exists
    if exam_data:
        st.divider()
        st.header("📝 Exam Paper")
        
        with st.form("exam_form"):
            answers = {}
            score = 0
            total_mcq_score = 0
            
            # MCQs
            st.subheader("Section A: Multiple Choice")
            for i, q in enumerate(exam_data['mcqs']):
                st.write(f"**Q{i+1}. {q['question']}** ({q['marks']} marks)")
                answers[f"mcq_{i}"] = st.radio("Select Answer", q['options'], key=f"s_mcq_{i}", label_visibility="collapsed")
                total_mcq_score += q['marks']
            
            # Short & Long
            st.subheader("Section B: Written Answers")
            for t in ['short', 'long']:
                if t in exam_data:
                    for i, q in enumerate(exam_data[t]):
                        st.write(f"**Q. {q['question']}** ({q['marks']} marks)")
                        st.text_area("Your Answer", key=f"s_{t}_{i}")

            # Submit
            st.divider()
            email = st.text_input("Teacher's Email (for submission)")
            submitted = st.form_submit_button("Submit Exam")
            
            if submitted:
                # Calculate MCQ score
                for i, q in enumerate(exam_data['mcqs']):
                    if answers.get(f"mcq_{i}") == q.get('correct'):
                        score += q['marks']
                        
                st.balloons()
                st.success("Exam Submitted!")
                st.info(f"**Immediate Result (MCQs):** You scored {score} / {total_mcq_score}")
                st.write(f"Answers simulated sent to {email}")


# ==========================================
# MAIN
# ==========================================

# Initialize Session State
if 'exam_data' not in st.session_state: st.session_state.exam_data = None
if 'exam_link' not in st.session_state: st.session_state.exam_link = None
if 'uploaded_content_parts' not in st.session_state: st.session_state.uploaded_content_parts = []

def main():
    st.sidebar.title("Exam Manager")
    
    # Check for Exam ID in URL Query Parameters
    query_params = st.query_params
    auto_exam_id = None
    default_role = "Teacher"
    
    if "exam_id" in query_params:
        auto_exam_id = query_params["exam_id"]
        default_role = "Student"
        st.toast("Exam loaded from link!", icon="🎓")
    
    # 1. Logic to show API Key ONLY for Teacher
    role_options = ["Teacher", "Student"]
    role_index = 1 if default_role == "Student" else 0
    role = st.sidebar.radio("I am a:", role_options, index=role_index)
    
    api_key = None
    if role == "Teacher":
        st.sidebar.markdown("---")
        api_key = st.sidebar.text_input("Google API Key", value=DEFAULT_API_KEY, type="password")
        st.sidebar.info("API Key is required for Teacher to generate exams.")
    
    st.sidebar.divider()
    
    if role == "Teacher":
        teacher_dashboard(api_key)
    else:
        # Student view does NOT need API Key
        student_view(auto_exam_id)

if __name__ == "__main__":
    main()
