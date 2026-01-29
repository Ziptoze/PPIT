import gradio as gr
import torch
import os
import re
import tempfile
from PIL import Image
from docx import Document
from bs4 import BeautifulSoup
from threading import Thread

# --- Transformers Import ---
try:
    from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor, TextIteratorStreamer
except ImportError as e:
    raise ImportError("Transformers library not found. Please install git+https://github.com/huggingface/transformers.git") from e

# --- Global Model Loading ---
print("Loading AI Model (2.1B Parameters)... This may take a minute...")
try:
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    # Bfloat16 is better for CPU/GPU if supported, but float32 is safest for pure CPU
    dtype = torch.float32 
    if device == "cuda":
        # HARDWARE ACCELERATION SETTINGS
        torch.backends.cudnn.benchmark = True 
        torch.backends.cuda.matmul.allow_tf32 = True 
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    
    model_id = "lightonai/LightOnOCR-2-1B"
    processor = LightOnOcrProcessor.from_pretrained(model_id)
    
    model = LightOnOcrForConditionalGeneration.from_pretrained(
        model_id,
        torch_dtype=dtype,
        attn_implementation="sdpa",
        low_cpu_mem_usage=True
    ).to(device)
    model.eval()
    print("Model Loaded Successfully!")

except Exception as e:
    print(f"Failed to load model: {e}")
    model = None
    processor = None

# --- Helper Functions ---
def resize_for_ocr(image, max_dim=1580):
    """Resize image to be faster (max 1280px)."""
    if image is None: return None
    w, h = image.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        return image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    return image

def clean_latex_for_word(text):
    """Clean simple LaTeX commands for better readability in Word."""
    text = re.sub(r'\\begin\{array\}\{.*?\}', '', text)
    text = text.replace(r'\end{array}', '')
    text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\textbf\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\textit\{([^}]*)\}', r'\1', text)
    text = text.replace(r'\\', '\n')
    text = text.replace(r'\rightarrow', '→').replace(r'\leftarrow', '←')
    text = text.replace(r'\leftrightarrow', '↔').replace(r'\Rightarrow', '⇒')
    text = text.replace(r'\downarrow', '↓').replace(r'\uparrow', '↑')
    text = text.replace(r'\ldots', '...').replace(r'\cdots', '...')
    text = text.replace(r'\times', '×').replace(r'\approx', '≈')
    text = text.replace(r'\le', '≤').replace(r'\ge', '≥')
    return text

def format_latex_for_display(text):
    """
    Auto-detects lines containing LaTeX (math/chemical equations) and wraps them in $$ 
    so Gradio/Markdown renders them correctly.
    """
    lines = text.split('\n')
    formatted = []
    # Regex to detect lines that look like chemical equations (have arrows, subscripts, superscripts)
    # Checks for: \xrightarrow, \rightarrow, _{num}, ^{num}, \frac, etc.
    chem_pattern = re.compile(r"(\\xrightarrow|\\rightarrow|\\frac|\^\{|_\{|_[0-9]|[A-Z][a-z]?_\d)")
    
    for line in lines:
        # If line contains LaTeX indicators and isn't already wrapped in $$
        if chem_pattern.search(line) and "$$" not in line:
            # Avoid wrapping lines that look like plain text but just have one subscript
            # But for chemistry, usually even simple formulas look better in math mode
            formatted.append(f"$${line}$$")
        else:
            formatted.append(line)
            
    return "\n".join(formatted)

def process_markdown_segment(text, doc):
    """Process standard markdown text lines."""
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line: continue
        line = clean_latex_for_word(line)

        if line.startswith('#'):
            parts = line.split(' ', 1)
            if len(parts) > 1:
                hashes, content = parts
                if all(c == '#' for c in hashes):
                    doc.add_heading(content, level=min(len(hashes), 9))
                    continue
        
        if '$' in line:
            p = doc.add_paragraph()
            parts = line.split('$')
            for i, part in enumerate(parts):
                if i % 2 == 1:
                    run = p.add_run(part)
                    run.italic = True
                    run.font.name = 'Cambria Math'
                else:
                    p.add_run(part)
            continue

        if line.startswith('- ') or line.startswith('* '):
            doc.add_paragraph(line[2:].strip(), style='List Bullet')
        else:
            doc.add_paragraph(line)

def process_html_table(html_str, doc):
    """Parse HTML table and add to Docx."""
    try:
        soup = BeautifulSoup(html_str, 'html.parser')
        rows = soup.find_all('tr')
        if not rows: return
        max_cols = max([len(row.find_all(['td', 'th'])) for row in rows]) if rows else 0
        if max_cols == 0: return

        table = doc.add_table(rows=len(rows), cols=max_cols)
        table.style = 'Table Grid'
        
        for i, row in enumerate(rows):
            cols = row.find_all(['td', 'th'])
            for j, col in enumerate(cols):
                if j < max_cols:
                    table.cell(i, j).text = col.get_text(strip=True)
    except Exception as e:
        doc.add_paragraph(f"[Error parsing table]")

def markdown_to_docx(text):
    """Convert extracted text to Docx object."""
    doc = Document()
    table_pattern = re.compile(r'(<table.*?>.*?</table>)', re.IGNORECASE | re.DOTALL)
    parts = table_pattern.split(text)
    for part in parts:
        if not part.strip(): continue
        if part.strip().lower().startswith('<table'):
            process_html_table(part, doc)
        else:
            process_markdown_segment(part, doc)
    return doc

# --- Main Gradio Logic ---
def stream_ocr(image):
    if model is None:
        yield "Error: Model not loaded.", None
        return
    
    if image is None:
        yield "Please upload an image.", None
        return

    try:
        # Resize
        valid_image = resize_for_ocr(image)

        # Prepare Inputs
        conversation = [
            {
                "role": "user", 
                "content": [
                    {"type": "image", "image": valid_image},
                    {"type": "text", "text": "Transcribe this document exactly."}
                ]
            }
        ]
        
        inputs = processor.apply_chat_template(
            conversation,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt"
        )
        
        inputs = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in inputs.items()}
        if "pixel_values" in inputs:
            inputs["pixel_values"] = inputs["pixel_values"].to(dtype=dtype)

        # Setup Streaming
        streamer = TextIteratorStreamer(processor, skip_prompt=True, skip_special_tokens=True)
        generation_kwargs = dict(
            inputs,
            streamer=streamer,
            max_new_tokens=2048,
            repetition_penalty=1.2,
            no_repeat_ngram_size=0,
            do_sample=True,
            temperature=0.2,
            use_cache=True
        )

        # Start Thread
        thread = Thread(target=model.generate, kwargs=generation_kwargs)
        thread.start()
        
        generated_text = ""
        for new_text in streamer:
            generated_text += new_text
            # Yield partial text with LaTeX formatting applied
            formatted_text = format_latex_for_display(generated_text)
            yield formatted_text, None

        # Final Doc Generation
        doc = markdown_to_docx(generated_text)  # Use raw text for DOCX generation logic
        
        # Save to temp file
        temp_dir = tempfile.gettempdir()
        output_path = os.path.join(temp_dir, "ocr_result.docx")
        doc.save(output_path)
        
        # Yield final text (formatted) and file
        yield format_latex_for_display(generated_text), output_path

    except Exception as e:
        yield f"Error during processing: {str(e)}", None

# --- Gradio UI ---
# Prepare Examples
example_images = []
data_dir = os.path.join(os.path.dirname(__file__), 'data')
if os.path.exists(data_dir):
    valid_exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    # Gradio Examples expects a list of lists: [[path1], [path2], ...]
    example_images = [[os.path.join(data_dir, f)] for f in os.listdir(data_dir) 
                      if os.path.splitext(f)[1].lower() in valid_exts]

# Aesthetic Custom CSS & Theme
custom_css = """
/* Dark Purple Gradient Background */
body, .gradio-container {
    background: linear-gradient(-45deg, #0f0c29, #302b63, #24243e);
    background-size: 400% 400%;
    animation: gradient 15s ease infinite;
    color: #e0e7ff !important;
}

@keyframes gradient {
    0% { background-position: 0% 50%; }
    50% { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}

/* Enhanced Glassmorphism Classes */
.header-text { 
    text-align: center; 
    margin-bottom: 2rem; 
    padding: 3rem; 
    background: rgba(255, 255, 255, 0.05);
    border-radius: 20px;
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
}

.header-text h1 { 
    font-family: 'Inter', sans-serif; 
    font-weight: 800; 
    color: #ffffff; 
    text-shadow: 0 0 25px rgba(167, 139, 250, 0.6);
    margin-bottom: 0.8rem; 
    font-size: 3.5rem;
    letter-spacing: -1.5px;
}

.header-text p { 
    font-size: 1.1rem; 
    color: #c4b5fd; 
    font-weight: 400;
    letter-spacing: 2px;
    text-transform: uppercase;
}

/* Scrollable Markdown Area */
.scrollable-md {
    height: 400px;
    overflow-y: auto;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    padding: 10px;
    background: rgba(0, 0, 0, 0.2);
}
"""

theme = gr.themes.Glass(
    primary_hue="violet",
    secondary_hue="slate",
    neutral_hue="stone",
    font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "system-ui", "sans-serif"],
).set(
    body_background_fill="transparent",
    body_text_color="#e0e7ff",
    background_fill_primary="rgba(20, 20, 35, 0.2)",
    background_fill_secondary="rgba(20, 20, 35, 0.2)",
    border_color_primary="rgba(255, 255, 255, 0.1)",
    block_background_fill="rgba(30, 25, 45, 0.2)",
    block_border_width="1px",
    block_label_background_fill="rgba(50, 40, 70, 0.4)",
    input_background_fill="rgba(20, 20, 40, 0.3)",
    button_primary_background_fill="linear-gradient(90deg, #8b5cf6 0%, #6d28d9 100%)",
    button_primary_border_color="rgba(255, 255, 255, 0.3)",
    button_primary_text_color="#ffffff",
    button_primary_shadow="0 0 20px rgba(139, 92, 246, 0.6)",
    slider_color="#8b5cf6",
)

with gr.Blocks(title="Ultra OCR", theme=theme, css=custom_css) as demo:
    with gr.Column():
        gr.Markdown(
            """
            <div class="header-text">
                <h1>🤖 Ultra OCR</h1>
                <p>Crafted with ❤️ by The Best Team</p>
            </div>
            """
        )
        
        with gr.Row(equal_height=False, variant="panel"):
            with gr.Column(scale=4):
                input_img = gr.Image(
                    type="pil", 
                    label="📄 Document Source", 
                    height=500,
                    sources=['upload', 'clipboard'],
                    format="png"
                )
                run_btn = gr.Button("⚡ Start Transcription", variant="primary", size="lg")
            
            with gr.Column(scale=5):
                with gr.Tabs():
                    with gr.TabItem("📝 Live Text"):
                        output_text = gr.Markdown(
                            label="Real-time Extraction",
                            elem_classes=["scrollable-md"] 
                        )
                    with gr.TabItem("💾 Export"):
                        gr.Markdown("### Download Results")
                        output_file = gr.File(label="Download Word (.docx)", type="filepath")

        # Example Gallery
        if example_images:
            gr.HTML("<hr>")
            gr.Markdown("### 📂 Sample Documents")
            gr.Examples(
                examples=example_images,
                inputs=input_img,
                label="Click a sample to test",
                examples_per_page=5
            )
            
    # Interactions
    run_btn.click(
        fn=stream_ocr,
        inputs=[input_img],
        outputs=[output_text, output_file],
        concurrency_limit=5
    )

if __name__ == "__main__":
    demo.launch(allowed_paths=[os.path.dirname(__file__)])
