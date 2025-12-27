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
import zlib
import hashlib

# ==========================================
# CONFIGURATION & SETUP
# ==========================================

st.set_page_config(
    page_title="AI Exam Manager",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ------------------------------------------------------------------
#  CONSTANTS & SECRETS
# ------------------------------------------------------------------
# HARDCODED API KEY (As requested)
# WARNING: In a public GitHub repo, this key will be visible to everyone.
DEFAULT_API_KEY = "AIzaSyAxlBTz4UdrOXaHmTqLRjKjHhdg2D4J_lk" 

# Your deployed app URL (Auto-fill for sharing links)
# NOTE: If you redeploy your app, this URL will change. Update it in the Teacher Dashboard.
DEFAULT_APP_URL = "https://exam-platform-hpzbdqrrr5rx3qyg6nyhjp.streamlit.app"

# DB File for Users (Local JSON storage)
USER_DB_FILE = "users.json"

# ==========================================
# AUTHENTICATION & USER MANAGEMENT
# ==========================================

def load_users():
    """Loads users from the local JSON file."""
    if not os.path.exists(USER_DB_FILE):
        return {}
    try:
        with open(USER_DB_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_users(users):
    """Saves users to the local JSON file."""
    with open(USER_DB_FILE, 'w') as f:
        json.dump(users, f)

def hash_password(password):
    """Simple hash for storing passwords."""
    return hashlib.sha256(password.encode()).hexdigest()

def authenticate(email, password):
    """Checks credentials."""
    users = load_users()
    if email in users:
        if users[email]['password'] == hash_password(password):
            return users[email]
    return None

def register_user(email, password, role, name):
    """Registers a new user."""
    users = load_users()
    if email in users:
        return False, "Email already exists."
    
    users[email] = {
        "password": hash_password(password),
        "role": role,
        "name": name,
        "email": email
    }
    save_users(users)
    return True, "Registration successful! Please log in."

def reset_password(email, new_password):
    """Resets password for a given email."""
    users = load_users()
    if email in users:
        users[email]['password'] = hash_password(new_password)
        save_users(users)
        return True, "Password updated successfully."
    return False, "Email not found."

# ==========================================
# HELPER FUNCTIONS: ENCODING & COMPRESSION
# ==========================================

def compress_and_encode(json_data):
    """Compresses JSON data and returns a URL-safe base64 string."""
    json_str = json.dumps(json_data)
    compressed_data = zlib.compress(json_str.encode('utf-8'))
    b64_encoded = base64.urlsafe_b64encode(compressed_data).decode('utf-8')
    return b64_encoded

def decode_and_decompress(encoded_str):
    """Decodes and decompresses the string back to JSON."""
    try:
        decoded_data = base64.urlsafe_b64decode(encoded_str)
        decompressed_data = zlib.decompress(decoded_data)
        return json.loads(decompressed_data.decode('utf-8'))
    except Exception:
        # Fallback for old links
        try:
            decoded_bytes = base64.b64decode(encoded_str)
            return json.loads(decoded_bytes.decode('utf-8'))
        except Exception:
            return None

# ==========================================
# HELPER FUNCTIONS: FILE UPLOAD
# ==========================================

def upload_to_gemini(path, mime_type, api_key):
    """Uploads file to Gemini File API."""
    genai.configure(api_key=api_key)
    try:
        gemini_file = genai.upload_file(path, mime_type=mime_type)
        
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
    """Splits and uploads large PDFs."""
    uploaded_file.seek(0)
    try:
        pdf_reader = PyPDF2.PdfReader(uploaded_file)
        total_pages = len(pdf_reader.pages)
    except Exception as e:
        st.warning(f"Could not read PDF structure ({e}). Uploading as single file.")
        return []

    gemini_files = []
    
    with tempfile.TemporaryDirectory() as temp_dir:
        status_text = st.empty()
        progress_bar = st.progress(0)
        
        for i in range(0, total_pages, chunk_size):
            chunk_writer = PyPDF2.PdfWriter()
            end_page = min(i + chunk_size, total_pages)
            
            for page_num in range(i, end_page):
                chunk_writer.add_page(pdf_reader.pages[page_num])
            
            if len(chunk_writer.pages) == 0: continue

            chunk_filename = os.path.join(temp_dir, f"chunk_{i}_{end_page}.pdf")
            with open(chunk_filename, "wb") as f:
                chunk_writer.write(f)
            
            # Optimized: Just extract text for chunks to save API calls/Time if needed, 
            # OR use File API. Let's stick to File API as requested for accuracy.
            g_file = upload_to_gemini(chunk_filename, "application/pdf", api_key)
            if g_file: gemini_files.append(g_file)

            progress_bar.progress(min((i + chunk_size) / total_pages, 1.0))
            
        status_text.empty()
        progress_bar.empty()
        
    return gemini_files

def prepare_content_for_gemini(uploaded_files, api_key):
    content_parts = []
    for file in uploaded_files:
        file_type = file.type
        if "pdf" in file_type:
            try:
                with st.spinner(f"Analyzing structure of {file.name}..."):
                    gemini_files = split_and_upload_pdf(file, api_key)
                    if gemini_files: content_parts.extend(gemini_files)
                    else:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                            tmp.write(file.getvalue())
                            tmp_path = tmp.name
                        g_file = upload_to_gemini(tmp_path, "application/pdf", api_key)
                        if g_file: content_parts.append(g_file)
                        os.remove(tmp_path)
            except Exception as e:
                st.error(f"Error preparing PDF {file.name}: {e}")
        elif file_type in ["image/png", "image/jpeg", "image/webp", "image/heic"]:
            suffix = "." + file.name.split('.')[-1] if '.' in file.name else ".jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(file.getvalue())
                tmp_path = tmp.name
            with st.spinner(f"Uploading image {file.name}..."):
                g_file = upload_to_gemini(tmp_path, file_type, api_key)
                if g_file: content_parts.append(g_file)
            os.remove(tmp_path)
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
    if not api_key: return None
    genai.configure(api_key=api_key)
    
    system_instruction = f"""
    You are an expert teacher. Create an exam paper based ONLY on the provided documents.
    Requirements:
    1. Create {num_mcq} Multiple Choice Questions (MCQs). Each carries 1 mark. Provide 4 options and the correct answer.
    2. Create {num_short} Short Answer Questions. Each carries 2 marks.
    3. Create {num_long} Long Answer Questions. Each carries 3 marks.
    Output Format: strictly valid JSON.
    {{ "mcqs": [ {{"question": "...", "options": ["A", "B", "C", "D"], "correct": "A", "marks": 1}} ], "short": [ {{"question": "...", "marks": 2}} ], "long": [ {{"question": "...", "marks": 3}} ] }}
    """
    
    model = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025', system_instruction=system_instruction) 
    try:
        response = model.generate_content(content_parts, generation_config={"response_mime_type": "application/json"}, request_options={"timeout": 600})
        return json.loads(response.text)
    except Exception as e:
        st.error(f"AI Generation failed: {e}")
        return None

def add_more_questions(current_exam, content_parts, api_key, q_type):
    genai.configure(api_key=api_key)
    system_instruction = f"Generate 1 NEW {q_type} question different from existing ones."
    model = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025', system_instruction=system_instruction)
    prompt = f"Return JSON: {{ 'question': '...', 'marks': 1, 'options': [], 'correct': '' }}"
    try:
        response = model.generate_content(content_parts + [prompt], generation_config={"response_mime_type": "application/json"})
        new_q = json.loads(response.text)
        if q_type == "MCQ": current_exam['mcqs'].append(new_q)
        elif q_type == "Short": current_exam['short'].append(new_q)
        elif q_type == "Long": current_exam['long'].append(new_q)
        return current_exam
    except Exception as e:
        st.error(f"Failed to add question: {e}")
        return current_exam

# ==========================================
# UI COMPONENTS: DASHBOARDS
# ==========================================

def teacher_dashboard(api_key):
    st.title(f"👨‍🏫 Teacher Dashboard - Welcome {st.session_state['user_info']['name']}")
    
    # Logout Button in Header
    if st.button("Logout", key="t_logout"):
        st.session_state['logged_in'] = False
        st.session_state['user_info'] = None
        st.rerun()

    uploaded_files = st.file_uploader("Upload Study Materials", accept_multiple_files=True, type=['pdf', 'docx', 'png', 'jpg', 'jpeg', 'webp'])

    if uploaded_files:
        if st.button("🚀 Generate Exam Paper"):
            content_parts = prepare_content_for_gemini(uploaded_files, api_key)
            st.session_state.uploaded_content_parts = content_parts
            if content_parts:
                with st.spinner("Generating Questions..."):
                    exam_json = generate_exam_paper(content_parts, api_key)
                    if exam_json:
                        st.session_state.exam_data = exam_json
                        st.success("Exam Generated!")

    if st.session_state.exam_data:
        st.divider()
        st.subheader("📝 Edit Exam Paper")
        exam = st.session_state.exam_data
        
        st.markdown("### MCQs")
        for i, q in enumerate(exam['mcqs']):
            with st.expander(f"Q{i+1}: {q.get('question', '')[:50]}..."):
                q['question'] = st.text_area("Question", value=q['question'], key=f"mcq_q_{i}")
                while len(q['options']) < 4: q['options'].append("")
                c1, c2 = st.columns(2)
                with c1: q['options'][0] = st.text_input("A", q['options'][0], key=f"m_{i}_0")
                with c2: q['options'][1] = st.text_input("B", q['options'][1], key=f"m_{i}_1")
                c3, c4 = st.columns(2)
                with c3: q['options'][2] = st.text_input("C", q['options'][2], key=f"m_{i}_2")
                with c4: q['options'][3] = st.text_input("D", q['options'][3], key=f"m_{i}_3")
                curr = q.get('correct', q['options'][0])
                if curr not in q['options']: curr = q['options'][0]
                q['correct'] = st.selectbox("Correct", q['options'], index=q['options'].index(curr), key=f"c_{i}")

        st.divider()
        c1, c2, c3 = st.columns(3)
        if c1.button("➕ Add MCQ"):
            with st.spinner("Adding..."):
                st.session_state.exam_data = add_more_questions(exam, st.session_state.uploaded_content_parts, api_key, "MCQ")
                st.rerun()
        if c2.button("💾 Publish"):
            st.session_state.exam_link = compress_and_encode(st.session_state.exam_data)
            st.rerun()

        if st.session_state.exam_link:
            st.divider()
            st.success("✅ Exam Published!")
            st.subheader("🔗 Share Exam")
            
            # --- UPDATED: Uses your specific App URL ---
            base_url = st.text_input("App URL (Copy from browser address bar):", value=DEFAULT_APP_URL)
            if base_url != DEFAULT_APP_URL:
                 st.caption(f"Using custom URL: {base_url}")
            
            encoded_id = urllib.parse.quote(st.session_state.exam_link)
            full_link = f"{base_url}/?exam_id={encoded_id}"
            
            st.write("**Send this Link to Students:**")
            st.code(full_link, language="text")
            
            st.info("When students click this link, they will be asked to log in, and then the exam will open.")

def student_view(auto_exam_id=None):
    st.title(f"🎓 Student Portal - Welcome {st.session_state['user_info']['name']}")
    
    if st.button("Logout", key="s_logout"):
        st.session_state['logged_in'] = False
        st.session_state['user_info'] = None
        st.rerun()

    # If ID came from URL (pending) or manual input
    if auto_exam_id:
        exam_id = auto_exam_id
    else:
        exam_id = st.text_input("Enter Exam ID (if not auto-filled):", value="")
    
    if exam_id:
        if "%" in exam_id: exam_id = urllib.parse.unquote(exam_id)
        exam_data = decode_and_decompress(exam_id)
        
        if exam_data:
            st.divider()
            with st.form("exam_form"):
                st.subheader("Exam Questions")
                
                answers = {}
                score = 0
                total_mcq = 0
                
                # MCQs
                st.markdown("### Section A: Multiple Choice")
                for i, q in enumerate(exam_data['mcqs']):
                    st.write(f"**Q{i+1}. {q['question']}** ({q['marks']} marks)")
                    answers[f"m_{i}"] = st.radio("Select Answer", q['options'], key=f"s_{i}", label_visibility="collapsed")
                    total_mcq += q['marks']

                # Short & Long
                st.markdown("### Section B: Written Answers")
                for t in ['short', 'long']:
                    if t in exam_data:
                        for i, q in enumerate(exam_data[t]):
                            st.write(f"**Q. {q['question']}** ({q['marks']} marks)")
                            st.text_area("Your Answer", key=f"s_{t}_{i}")

                st.divider()
                teacher_email = st.text_input("Teacher's Email (to send results):")
                
                submitted = st.form_submit_button("Submit Exam")
                
                if submitted:
                    # Calculate MCQ score
                    for i, q in enumerate(exam_data['mcqs']):
                        if answers.get(f"m_{i}") == q.get('correct'): score += q['marks']
                    
                    st.balloons()
                    st.success(f"Exam Submitted by {st.session_state['user_info']['name']}!")
                    st.info(f"**MCQ Score:** {score} / {total_mcq}")
                    st.write(f"Results simulated sent to {teacher_email}")
        else:
            st.error("Invalid Exam Link. Please ask your teacher for the correct link.")

# ==========================================
# AUTHENTICATION PAGES
# ==========================================

def login_page():
    st.title("🔐 Exam Platform Login")
    
    # Check if a student clicked a link and was redirected here
    if 'pending_exam_id' in st.session_state and st.session_state['pending_exam_id']:
        st.info("Please Log In or Sign Up to access your Exam.")

    tab1, tab2 = st.tabs(["Log In", "Sign Up"])

    with tab1:
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_pass")
        
        if st.button("Log In"):
            user = authenticate(email, password)
            if user:
                st.session_state['logged_in'] = True
                st.session_state['user_info'] = user
                st.success(f"Welcome back, {user['name']}!")
                st.rerun()
            else:
                st.error("Invalid email or password")
        
        # Forgot Password Section
        with st.expander("Forgot Password?"):
            st.write("Create a new password.")
            fp_email = st.text_input("Enter your Email", key="fp_email")
            fp_new_pass = st.text_input("New Password", type="password", key="fp_pass")
            if st.button("Reset Password"):
                success, msg = reset_password(fp_email, fp_new_pass)
                if success:
                    st.success(msg)
                else:
                    st.error(msg)

    with tab2:
        st.write("Create a new account")
        new_name = st.text_input("Full Name", key="su_name")
        new_email = st.text_input("Email", key="su_email")
        new_role = st.selectbox("I am a...", ["Student", "Teacher"], key="su_role")
        new_pass = st.text_input("Password", type="password", key="su_pass")
        new_pass_conf = st.text_input("Confirm Password", type="password", key="su_pass_conf")
        
        if st.button("Sign Up"):
            if new_pass != new_pass_conf:
                st.error("Passwords do not match.")
            elif not new_email or not new_pass or not new_name:
                st.error("Please fill all fields.")
            else:
                success, msg = register_user(new_email, new_pass, new_role, new_name)
                if success:
                    st.success(msg)
                else:
                    st.error(msg)

# ==========================================
# MAIN APP LOGIC
# ==========================================

# Initialize Session State
if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False
if 'user_info' not in st.session_state: st.session_state['user_info'] = None
if 'exam_data' not in st.session_state: st.session_state.exam_data = None
if 'exam_link' not in st.session_state: st.session_state.exam_link = None
if 'uploaded_content_parts' not in st.session_state: st.session_state.uploaded_content_parts = []
if 'pending_exam_id' not in st.session_state: st.session_state['pending_exam_id'] = None

def main():
    # 1. Capture Exam ID from URL (if any)
    query_params = st.query_params
    if "exam_id" in query_params:
        st.session_state['pending_exam_id'] = query_params["exam_id"]
    
    # 2. Authentication Check
    if not st.session_state['logged_in']:
        login_page()
    else:
        # 3. Role-Based Routing
        user_role = st.session_state['user_info']['role']
        
        if user_role == "Teacher":
            # Teachers see Dashboard
            # Note: We pass the hardcoded KEY directly
            teacher_dashboard(DEFAULT_API_KEY)
        
        elif user_role == "Student":
            # Students see Exam View
            # If there was a pending link, use it, then clear it
            exam_id_to_load = st.session_state['pending_exam_id']
            student_view(exam_id_to_load)

if __name__ == "__main__":
    main()