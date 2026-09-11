"""
Flask Server for Mobile UI to PSD Converter
Advanced OCR with EasyOCR + Tesseract + Perfect Typography Matching
- Graphics untouched: original raster preserved as Background layer
- Text only: editable frames with fontSize, lineHeight, letterSpacing, alignment, weight, style
"""

from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
import sys
import warnings
import cv2
import numpy as np
import base64
import io
import os
import hashlib
import math
from PIL import Image, ImageFont, ImageDraw
from pathlib import Path
import json

# Handle PyInstaller frozen environment path
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env for GROQ_API_KEY
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BUNDLE_DIR, '.env'))
    load_dotenv(os.path.join(os.getcwd(), '.env'))
except ImportError:
    pass
# Groq matcher (optional)
try:
    from groq_matcher import correct_with_groq, correct_auto
    HAS_GROQ = True
except ImportError as e:
    HAS_GROQ = False
    print(f"groq_matcher not available: {e}")
    def correct_with_groq(elements, target_text, api_key=None, model=None, timeout=12):
        # fallback positional
        if isinstance(target_text, str):
            lines = [l.strip() for l in target_text.splitlines() if l.strip()]
        else:
            lines = [str(l).strip() for l in target_text if str(l).strip()]
        if not lines:
            return elements
        sorted_idx = sorted(list(enumerate(elements)), key=lambda kv: (kv[1]["y"], kv[1]["x"]))
        corr = [dict(e) for e in elements]
        for (oi,_), t in zip(sorted_idx, lines):
            corr[oi]["text"] = t
        return corr
    def correct_auto(elements, image_b64=None, api_key=None, model=None, timeout=14):
        return elements

# Try to import EasyOCR for better accuracy
try:
    import easyocr
    HAS_EASYOCR = True
except ImportError:
    HAS_EASYOCR = False
    print("EasyOCR not installed. Install with: pip install easyocr")

# Try to import Tesseract (much faster on CPU). Requires the Tesseract binary
# to also be installed (e.g. UB Mannheim installer on Windows).
try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False
    print("pytesseract not installed. Install with: pip install pytesseract")

# Try psd-tools for server-side PSD generation
try:
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer, TypeLayer
    HAS_PSDTOOLS = True
except ImportError:
    HAS_PSDTOOLS = False

def configure_tesseract():
    """Point pytesseract at the Windows binary if it isn't on PATH."""
    if not HAS_TESSERACT:
        return False
    try:
        # Already usable?
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        pass
    # Common Windows install locations
    for cand in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.exists(cand):
            pytesseract.pytesseract.tesseract_cmd = cand
            try:
                pytesseract.get_tesseract_version()
                return True
            except Exception:
                return False
    return False

TESSERACT_OK = configure_tesseract()
if HAS_TESSERACT and not TESSERACT_OK:
    print("Tesseract binary not found. Install Tesseract-OCR and add it to PATH.")

template_dir = os.path.join(BUNDLE_DIR, 'templates')
static_dir = os.path.join(BUNDLE_DIR, 'static')
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
CORS(app)

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    assets_dir = os.path.join(BUNDLE_DIR, 'assets')
    return send_from_directory(assets_dir, filename)

# Store OCR reader (initialize once)
ocr_reader = None

# Cache OCR results keyed by image hash to avoid re-processing identical uploads
_detect_cache = {}

# Worker pool for OCR. EasyOCR is NOT thread-safe for concurrent calls on the
# same reader, so we serialize detection through a single-worker pool. This
# keeps the Flask server responsive and prevents model collisions.
import concurrent.futures
_ocr_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# Maximum dimension we feed in. This only guards against absurdly large
# uploads; the detector's own `canvas_size` (below) is what really controls
# detection speed.
MAX_OCR_DIM = 1280

# Detector input cap. EasyOCR resizes the longer side toward ~canvas_size
# before running its detection CNN, so this is the dominant speed knob:
# 960 is ~1.3-1.5x faster than the default 1280 with negligible loss for
# UI/screenshot text. Lower it further (e.g. 800) for more speed if needed.
OCR_CANVAS_SIZE = 960

# Font detection: try to locate a sans-serif TTF for metrics
def _find_sans_font():
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
        r"C:\Windows\Fonts\ARIAL.TTF",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\Helvetica.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

SANS_FONT_PATH = _find_sans_font()
print(f"Sans font for metrics: {SANS_FONT_PATH}")

# 80-100% size-only fit: tracking disabled per user request
ENABLE_TRACKING = False
print(f"Tracking enabled: {ENABLE_TRACKING} (size-only 80-100% fit)")

# Auto font family candidates for perfect matching – only those present on disk are used
_RAW_CANDIDATE_FONTS = [
    ("ArialMT", r"C:\Windows\Fonts\arial.ttf"),
    ("Roboto", r"C:\Windows\Fonts\Roboto-Regular.ttf"),
    ("OpenSans", r"C:\Windows\Fonts\OpenSans-Regular.ttf"),
    ("Ubuntu", r"C:\Windows\Fonts\Ubuntu-Regular.ttf"),
    ("SegoeUI", r"C:\Windows\Fonts\segoeui.ttf"),
    ("Tahoma", r"C:\Windows\Fonts\tahoma.ttf"),
    ("Verdana", r"C:\Windows\Fonts\verdana.ttf"),
    ("Calibri", r"C:\Windows\Fonts\calibri.ttf"),
    ("Montserrat", r"C:\Windows\Fonts\Montserrat-Regular.ttf"),
    ("Helvetica", r"C:\Windows\Fonts\helvetica.ttf"),
    ("Inter", r"C:\Windows\Fonts\Inter-Regular.ttf"),
    ("Inter", os.path.join(BUNDLE_DIR, "assets", "fonts", "Inter-Regular.ttf")),
    ("Poppins", r"C:\Windows\Fonts\Poppins-Regular.ttf"),
    ("Poppins", os.path.join(BUNDLE_DIR, "assets", "fonts", "Poppins-Regular.ttf")),
    ("NotoSans", r"C:\Windows\Fonts\NotoSans-Regular.ttf"),
    ("NotoSans", os.path.join(BUNDLE_DIR, "assets", "fonts", "NotoSans-Regular.ttf")),
    ("Arial", r"C:\Windows\Fonts\arial.ttf"),
    ("Calibri", r"C:\Windows\Fonts\calibri.ttf"),
    ("TimesNewRoman", r"C:\Windows\Fonts\times.ttf"),
    ("TimesNewRoman", r"C:\Windows\Fonts\TimesNewRomanPSMT.otf"),
    ("Aptos", r"C:\Windows\Fonts\Aptos.ttf"),
    ("CourierNew", r"C:\Windows\Fonts\cour.ttf"),
    ("MyriadPro", r"C:\Windows\Fonts\MyriadPro-Regular.otf"),
    ("MyriadPro", r"C:\Windows\Fonts\MYRIADPRO-REGULAR.OTF"),
    ("Cambria", r"C:\Windows\Fonts\cambria.ttc"),
    ("Cambria", r"C:\Windows\Fonts\cambria.ttc"),
    ("Symbol", r"C:\Windows\Fonts\symbol.ttf"),
    ("Symbol", r"C:\Windows\Fonts\Symbol Regular.ttf"),
    ("CambriaMath", r"C:\Windows\Fonts\cambria.ttc"),
    ("SegoeUIHistoric", r"C:\Windows\Fonts\seguihis.ttf"),
    ("Windings", r"C:\Windows\Fonts\wingding.ttf"),
    ("Windings", r"C:\Windows\Fonts\WINGDNG2.TTF"),
    ("Windings", r"C:\Windows\Fonts\WINGDNG3.TTF"),
    ("Webdings", r"C:\Windows\Fonts\webdings.ttf"),
]
CANDIDATE_FONTS = [(name, path) for name, path in _RAW_CANDIDATE_FONTS if os.path.exists(path)]
# Ensure at least SANS_FONT_PATH is in list
if SANS_FONT_PATH and not any(p==SANS_FONT_PATH for _,p in CANDIDATE_FONTS):
    CANDIDATE_FONTS.insert(0, ("ArialMT", SANS_FONT_PATH))
print(f"Candidate fonts available: {[n for n,_ in CANDIDATE_FONTS]}")

# Groq AI config
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
if GROQ_API_KEY:
    print(f"Groq AI enabled: model={GROQ_MODEL} key={GROQ_API_KEY[:8]}...")
else:
    print("Groq AI disabled: set GROQ_API_KEY in .env to enable AI text fitting")

def match_font_family(text, bbox_w, bbox_h, heuristic_size=None):
    """
    Auto font matching via rendered width/height scoring across CANDIDATE_FONTS.
    Returns (best_name, best_path, confidence 0-1). Confidence 1 = perfect width match.
    Uses heuristic_size ~ h*0.93 if not provided, tries each font at that size and
    picks minimal 0.6*widthError + 0.4*heightError.
    """
    if not text or bbox_w < 2 or bbox_h < 2 or not CANDIDATE_FONTS:
        return "ArialMT", SANS_FONT_PATH, 0.0
    # Heuristic size if not provided
    if heuristic_size is None:
        has_desc = any(c in 'gjpqy' for c in text)
        has_asc = any(c in 'bdfhkltABCDEFGHIJKLMNOPQRSTUVWXYZ' for c in text)
        if has_desc and has_asc:
            heuristic_size = bbox_h * 0.90
        elif has_desc or has_asc:
            heuristic_size = bbox_h * 0.93
        else:
            heuristic_size = bbox_h * 1.02
        heuristic_size = max(6, heuristic_size)
    best_name, best_path, best_err = None, None, float('inf')
    best_score = 0
    for name, path in CANDIDATE_FONTS:
        try:
            sz = int(round(max(6, heuristic_size)))
            try:
                font = ImageFont.truetype(path, sz)
            except Exception:
                continue
            try:
                bbox = font.getbbox(text)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            except Exception:
                # fallback via draw
                img_tmp = Image.new('RGB', (1,1))
                draw = ImageDraw.Draw(img_tmp)
                bbox = draw.textbbox((0,0), text, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            if tw <= 0 or th <= 0:
                continue
            err_w = abs(tw - bbox_w) / max(1, bbox_w)
            err_h = abs(th - bbox_h) / max(1, bbox_h)
            err = err_w * 0.6 + err_h * 0.4
            # Penalize extreme aspect mismatch slightly
            if err < best_err:
                best_err = err
                best_name = name
                best_path = path
                best_score = max(0, 1 - err)
        except Exception:
            continue
    if best_name is None:
        return "ArialMT", SANS_FONT_PATH, 0.0
    # Confidence threshold: if best_err >0.35, uncertain → fallback to ArialMT with low conf
    if best_err > 0.35:
        # still return best but low confidence
        pass
    return best_name, best_path, float(max(0, min(1, best_score)))

def get_ocr_reader():
    global ocr_reader
    if ocr_reader is None and HAS_EASYOCR:
        print("Initializing EasyOCR reader (one-time, may download models)...")
        # EasyOCR's quantized LSTM triggers a PyTorch deprecation UserWarning
        # under torch >= 2.x (torch.quantize_per_tensor/per_channel). It is
        # harmless, so we silence it to keep startup output clean.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore',
                message='.*quantize_per_tensor.*|.*quantize_per_channel.*'
            )
            easyocr_model_dir = os.path.join(BUNDLE_DIR, 'easyocr_models')
            if not os.path.exists(easyocr_model_dir):
                easyocr_model_dir = None
            ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False, model_storage_directory=easyocr_model_dir)
        print("EasyOCR reader ready.")
    return ocr_reader

def run_ocr(processed):
    """Run detection+recognition off the request thread (EasyOCR)."""
    reader = get_ocr_reader()
    # Ensure 3-channel input for EasyOCR (it expects RGB/BGR)
    if len(processed.shape) == 2:
        processed_3ch = cv2.cvtColor(processed, cv2.COLOR_GRAY2BGR)
    else:
        processed_3ch = processed
    # canvas_size caps detector input -> big speed win. mag_ratio=1 keeps the
    # detector from upscaling small images.
    return reader.readtext(processed_3ch, decoder='greedy', rotation_info=[0],
                           paragraph=False, canvas_size=OCR_CANVAS_SIZE,
                           mag_ratio=1.0)

def run_tesseract(image):
    """
    Fast CPU detection via Tesseract. Returns results in the same
    (bbox, text, conf) format as EasyOCR so the rest of the pipeline is shared.
    Tesseract is typically 5-10x faster than EasyOCR on CPU.
    """
    # Tesseract works best on grayscale
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image

    data = pytesseract.image_to_data(
        gray, output_type=pytesseract.Output.DICT, config='--psm 11'
    )

    results = []
    for i, txt in enumerate(data['text']):
        txt = txt.strip()
        if not txt:
            continue
        conf = float(data['conf'][i])
        if conf < 0:  # Tesseract uses -1 for unreliable words
            continue
        x = int(data['left'][i])
        y = int(data['top'][i])
        w = int(data['width'][i])
        h = int(data['height'][i])
        if w <= 0 or h <= 0:
            continue
        bbox = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        results.append((bbox, txt, conf / 100.0))
    return results

def run_detection(image, processed):
    """
    Run text detection, preferring the fast Tesseract engine and falling back
    to EasyOCR if Tesseract is unavailable or fails.
    """
    if TESSERACT_OK:
        try:
            return run_tesseract(image), 'tesseract'
        except Exception as e:
            print("Tesseract failed, falling back to EasyOCR:", e)
    if HAS_EASYOCR:
        return run_ocr(processed), 'easyocr'
    raise RuntimeError('No OCR engine available')

def resize_for_ocr(image, max_dim=MAX_OCR_DIM):
    """
    Downscale large images so detection runs on far fewer pixels, upscale tiny images for small-text perfection.
    Returns the (possibly resized) image and the scale factor applied.
    NOTE: Original image is never mutated – we operate on a clone.
    """
    h, w = image.shape[:2]
    scale = min(1.0, max_dim / float(max(h, w)))
    if scale < 1.0:
        image = cv2.resize(image, (int(w * scale), int(h * scale)),
                           interpolation=cv2.INTER_AREA)
        return image, scale
    # Upscale small images for small-text super-resolution (95% target)
    if max(h, w) < 800:
        up_scale = min(2.0, 800 / float(max(h, w)))
        if up_scale > 1.01:
            image = cv2.resize(image, (int(w * up_scale), int(h * up_scale)),
                               interpolation=cv2.INTER_CUBIC)
            return image, up_scale
    return image, scale

def preprocess_for_ocr(image):
    """
    Light, fast preprocessing. EasyOCR performs its own internal
    preprocessing, so feeding it a heavy binary/inverted image only wastes
    time and hurts recognition. We just lift contrast with CLAHE and pass
    grayscale, which is both faster and more accurate for the detector.
    """
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    return enhanced

def extract_colors_from_text(image, bbox):
    """
    Extract accurate color from text region.
    New: masks to text pixels only via Otsu threshold so background
    doesn't pollute the dominant color. Falls back to quantized dominant
    if masking fails.
    """
    x0, y0, x1, y1 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    
    # Ensure coordinates are within bounds
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(image.shape[1], x1)
    y1 = min(image.shape[0], y1)
    
    # Extract region
    region = image[y0:y1, x0:x1]
    
    if region.size == 0:
        return "#666666"
    
    # Downsample huge regions only (keep text color fidelity for normal boxes)
    h, w = region.shape[:2]
    if max(h, w) > 400:
        # use area resize for better averaging, not slicing
        scale = 400 / max(h, w)
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        region = cv2.resize(region, (new_w, new_h), interpolation=cv2.INTER_AREA)
        h, w = region.shape[:2]
    
    # For grayscale, convert to BGR for color conversion
    if len(region.shape) == 2:
        region = cv2.cvtColor(region, cv2.COLOR_GRAY2BGR)
    
    # Try masked extraction: isolate interior core via distanceTransform (avoids anti-alias fringe)
    try:
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        count_zero = np.sum(binary == 0)
        count_white = np.sum(binary == 255)
        if min(count_zero, count_white) < (h * w * 0.03):
            raise ValueError("low contrast mask")
        mask = (binary == 0) if count_zero < count_white else (binary == 255)
        mask_u8 = mask.astype(np.uint8)
        # Interior core via distance transform – deeper core for perfect color (avoid fringe), keep >=5px for tiny text
        core_mask = None
        try:
            dist = cv2.distanceTransform(mask_u8, cv2.DIST_L2, 5)
            max_d = float(dist.max()) if dist.size else 0
            if max_d > 1.5:
                core_mask = dist > (max_d * 0.50)
                if np.sum(core_mask) < 5:
                    core_mask = dist > (max_d * 0.35)
                    if np.sum(core_mask) < 5:
                        core_mask = None
            else:
                kernel = np.ones((2,2), np.uint8)
                eroded = cv2.erode(mask_u8*255, kernel, iterations=1) > 0
                if np.sum(eroded) >= 5:
                    core_mask = eroded
                else:
                    core_mask = mask
        except Exception:
            core_mask = mask
        if core_mask is None:
            core_mask = mask
        text_pixels = region[core_mask]
        if text_pixels.size > 0:
            flat = text_pixels.reshape(-1,3).astype(np.float32)
            median_bgr = np.median(flat, axis=0).astype(np.int32)  # BGR
            bm, gm, rm = int(median_bgr[0]), int(median_bgr[1]), int(median_bgr[2])
            brightness_med = 0.299*rm + 0.587*gm + 0.114*bm
            # LAB k-means for saturated perceptual accuracy
            lab_b = lab_g = lab_r = None
            lab_brightness = None
            try:
                n = int(text_pixels.shape[0])
                if n >= 5:
                    lab = cv2.cvtColor(text_pixels.reshape(1,-1,3).astype(np.uint8), cv2.COLOR_BGR2LAB).reshape(-1,3).astype(np.float32)
                    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
                    k = 2 if n > 10 else 1
                    attempts = 3 if n > 20 else 2
                    _, labels, centers = cv2.kmeans(lab, k, None, criteria, attempts, cv2.KMEANS_PP_CENTERS)
                    if k == 1:
                        lab_dom = centers[0]
                    else:
                        cnt0 = int(np.sum(labels==0)); cnt1 = int(np.sum(labels==1))
                        lab_dom = centers[0] if cnt0 > cnt1 else centers[1]
                    bgr_lab = cv2.cvtColor(np.uint8([[lab_dom]]), cv2.COLOR_LAB2BGR)[0][0]
                    lab_b, lab_g, lab_r = int(bgr_lab[0]), int(bgr_lab[1]), int(bgr_lab[2])
                    lab_brightness = 0.299*lab_r + 0.587*lab_g + 0.114*lab_b
            except:
                pass
            qp = (text_pixels.astype(np.int32) // 10) * 10 + 5
            qp = np.clip(qp, 0, 255)
            unique, counts = np.unique(qp, axis=0, return_counts=True)
            dominant = unique[np.argmax(counts)]
            bd, gd, rd = int(dominant[0]), int(dominant[1]), int(dominant[2])
            brightness_dom = 0.299*rd + 0.587*gd + 0.114*bd
            # For core mask, trust median/LAB even for white (255) or black (0) – mask is already text-only
            # Prefer LAB if available and more saturated
            if lab_b is not None and lab_brightness is not None:
                # LAB is most perceptual; use if median is grayish or LAB more saturated
                if abs(lab_brightness-128) > abs(brightness_med-128) + 5:
                    bm, gm, rm = lab_b, lab_g, lab_r
                    brightness_med = lab_brightness
            # Return median (core) directly – allow white/black for text
            # No brightness filter for core, since mask is text-only
            return f"#{int(np.clip(rm,0,255)):02x}{int(np.clip(gm,0,255)):02x}{int(np.clip(bm,0,255)):02x}"
        # fallback to full region handled below
    except Exception:
        pass
    
    # Fallback: quantized dominant over whole region (small sample) – unified 8-245
    pixels = region.reshape(-1, 3).astype(np.int32)
    pixels = (pixels // 16) * 16 + 8
    pixels = np.clip(pixels, 0, 255)
    unique, counts = np.unique(pixels, axis=0, return_counts=True)
    idx_sorted = np.argsort(-counts)
    for idx in idx_sorted[:10]:
        b, g, r = unique[idx]
        brightness = 0.299*r + 0.587*g + 0.114*b
        if brightness < 245 and brightness > 8:
            return f"#{int(np.clip(r,0,255)):02x}{int(np.clip(g,0,255)):02x}{int(np.clip(b,0,255)):02x}"
    # last resort
    b, g, r = unique[np.argmax(counts)]
    return f"#{int(np.clip(r,0,255)):02x}{int(np.clip(g,0,255)):02x}{int(np.clip(b,0,255)):02x}"

# ---------- Typography Helpers ----------

def estimate_font_size(text, bbox_w, bbox_h, font_path=SANS_FONT_PATH):
    """
    Estimate fontSize (px) that makes rendered text width ≈ bbox_w and height ≈ bbox_h.
    Uses Pillow rendering binary search if font available, else heuristic.
    Returns float fontSize px.
    """
    text = text.strip()
    if not text:
        return float(bbox_h)
    # heuristic baseline
    has_descender = any(c in 'gjpqy\u00e7\u00f1' for c in text)
    has_ascender = any(c in 'bdfhkltABCDEFGHIJKLMNOPQRSTUVWXYZ' for c in text)
    if has_descender and has_ascender:
        heuristic = bbox_h * 0.90
    elif has_descender or has_ascender:
        heuristic = bbox_h * 0.93
    else:
        # caps-only or x-height only like "525" -> taller relative
        heuristic = bbox_h * 1.05

    # If no font file, return heuristic
    if font_path is None or not os.path.exists(font_path):
        return float(max(6, heuristic))

    try:
        # Size-only 80-100% fit: binary search for largest size where tw<=w and th<=h and fill 80-100%
        best = heuristic
        best_err = float('inf')
        best_fit = None
        best_fit_fill = -1
        lo, hi = 4.0, max(8.0, bbox_h * 2.2)
        for _ in range(20):
            mid = (lo + hi) / 2
            try:
                font = ImageFont.truetype(font_path, int(round(mid)))
            except Exception:
                break
            try:
                bbox = font.getbbox(text)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            except AttributeError:
                img_tmp = Image.new('RGB', (1,1))
                draw = ImageDraw.Draw(img_tmp)
                bbox = draw.textbbox((0,0), text, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            # Check fit – 95-99% perfection target (tight, no tracking)
            fits_w = tw <= bbox_w + 0.5
            fits_h = th <= bbox_h + 0.5
            if fits_w and fits_h:
                fill_w = tw / max(1, bbox_w)
                fill_h = th / max(1, bbox_h)
                # 95-99% fill is ideal; penalize below 95% heavily
                if fill_w < 0.95:
                    err = (0.95 - fill_w) * 3 + (1 - fill_h)*0.3
                else:
                    # prefer closest to 99% without overflow
                    err = abs(0.99 - fill_w)*0.7 + (1 - fill_h)*0.3
                if err < best_err:
                    best_err = err
                    best = mid
                # track best fitting with maximal fill (closest to 99% without overflow)
                fill = fill_w*0.7 + fill_h*0.3
                if fill > best_fit_fill and fill_w >= 0.90:
                    best_fit = mid
                    best_fit_fill = fill
                # try larger size
                lo = mid
                if err < 0.015:
                    break
            else:
                # overflow – too large
                hi = mid
                overflow = 0
                if tw > bbox_w:
                    overflow += (tw - bbox_w)/bbox_w
                if th > bbox_h:
                    overflow += (th - bbox_h)/bbox_h
                err = 10 + overflow
                if err < best_err and best_fit is None:
                    best_err = err
                    best = mid
        # Subpixel refinement for 95-99% perfection
        try:
            refine_best = best
            refine_err = best_err
            for delta in (-1.0, -0.75, -0.5, -0.25, 0.25, 0.5, 0.75, 1.0):
                cand = best + delta
                if cand < 4 or cand > bbox_h*1.8:
                    continue
                try:
                    rf = ImageFont.truetype(font_path, int(round(cand)))
                except:
                    continue
                try:
                    rb = rf.getbbox(text)
                    rtw = rb[2]-rb[0]
                    rth = rb[3]-rb[1]
                except:
                    continue
                if rtw <= bbox_w + 0.5 and rth <= bbox_h + 0.5:
                    rf_w = rtw / max(1, bbox_w)
                    if rf_w < 0.95:
                        err = (0.95 - rf_w)*3 + (1 - rth/max(1,bbox_h))*0.3
                    else:
                        err = abs(0.99 - rf_w)*0.7 + (1 - rth/max(1,bbox_h))*0.3
                    if err < refine_err:
                        refine_err = err
                        refine_best = cand
            best = refine_best
            best_err = refine_err
        except:
            pass
        # Prefer best fitting that respects 95-99% if found
        if best_fit is not None and best_fit_fill >= 0.90:
            # prefer refined best if it has higher fill
            if best_fit_fill > 0.94:
                best = best_fit
                best_err = 1 - best_fit_fill
            else:
                # keep refined if better
                pass
        # If still large error, blend with heuristic but prefer heuristic only if heuristic also fits – tighter for 95% target
        if best_err > 0.15:
            try:
                fh = ImageFont.truetype(font_path, int(round(heuristic)))
                bh = fh.getbbox(text)
                thw = bh[2]-bh[0]
                thh = bh[3]-bh[1]
                if thw <= bbox_w and thh <= bbox_h:
                    pass
                else:
                    pass
            except Exception:
                pass
            if best_err > 0.25:
                return float(max(6, min(heuristic, bbox_h*1.8)))
        best = max(5, min(best, bbox_h * 1.8))
        return float(best)
    except Exception:
        return float(heuristic)

def estimate_font_weight_and_style(image, bbox):
    """
    Estimate weight (400/500/600/700) and style (normal/italic) from image region.
    """
    x0, y0, x1, y1 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(image.shape[1], x1); y1 = min(image.shape[0], y1)
    region = image[y0:y1, x0:x1]
    if region.size == 0 or region.shape[0] < 4 or region.shape[1] < 4:
        return 400, 'normal'
    try:
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if len(region.shape)==3 else region
        # Downsample huge regions for speed
        if max(gray.shape) > 120:
            scale = 120 / max(gray.shape)
            gray = cv2.resize(gray, (int(gray.shape[1]*scale), int(gray.shape[0]*scale)), interpolation=cv2.INTER_AREA)
        # Threshold
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        count_zero = np.sum(binary==0)
        count_white = np.sum(binary==255)
        # Text mask is minority
        is_dark_text = count_zero < count_white
        mask = (binary==0) if is_dark_text else (binary==255)
        total = mask.size
        text_ratio = np.sum(mask) / total
        # Weight heuristic based on fill ratio
        if text_ratio > 0.42:
            weight = 700
        elif text_ratio > 0.33:
            weight = 600
        elif text_ratio > 0.24:
            weight = 500
        else:
            weight = 400
        # Style: detect skew via minAreaRect
        style = 'normal'
        try:
            # need contours
            bin_u8 = mask.astype(np.uint8)*255
            contours, _ = cv2.findContours(bin_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                # largest contour
                cnt = max(contours, key=cv2.contourArea)
                if cv2.contourArea(cnt) > 20:
                    rect = cv2.minAreaRect(cnt)
                    angle = rect[2]  # -90 to 0
                    # Normalize: angle = 0 means horizontal, -90 vertical
                    # For italic, rectangle is skewed ~ ±10-15deg
                    # Convert to deviation from horizontal
                    if angle < -45:
                        angle = 90 + angle
                    # angle now in -45..45, 0 is horizontal
                    if 4 < abs(angle) < 20:
                        # Verify elongated: width > height
                        (w,h) = rect[1]
                        if w > h*1.8 or h > w*1.8:
                            style = 'italic'
        except Exception:
            pass
        return weight, style
    except Exception:
        return 400, 'normal'

def heuristic_alignment(x, w, W):
    """Helper: deterministic left/center/right from geometry."""
    cx = x + w/2
    img_cx = W / 2
    left_dist = x
    right_dist = W - (x + w)
    center_tolerance = max(15, W * 0.03)
    if abs(cx - img_cx) < center_tolerance and abs(left_dist - right_dist) < center_tolerance*1.5:
        return 'center'
    elif right_dist < 20 and left_dist > 40:
        return 'right'
    else:
        return 'left'

def detect_alignment_and_spacing(elements, image_width, image_height):
    """
    For each element compute alignment (left/center/right) and letterSpacing.
    Also compute paragraph-level leading (line spacing) as median gap.
    Returns: updated elements with alignment, letterSpacing, lineHeight
    """
    if not elements:
        return elements
    # Sort by y then x
    elements = sorted(elements, key=lambda e: (e['y'], e['x']))
    # Determine alignment per element
    for e in elements:
        e['alignment'] = heuristic_alignment(e['x'], e['w'], image_width)
        # Justify detection: full-width lines with distributed gaps (rare in mobile)
        # keep as left for now
        if e['w'] > image_width * 0.75 and e['text'].count(' ') >= 3:
            # could be justify, but keep left to avoid PSD issues
            pass

    # Line spacing (leading) : compute gaps between successive lines
    gaps = []
    for i in range(len(elements)-1):
        curr = elements[i]
        nxt = elements[i+1]
        # Only consider lines that are roughly same x alignment (same column)
        # For mobile UI often single column -> consider all
        # Gap = next y - (curr y + curr h)
        gap = nxt['y'] - (curr['y'] + curr['h'])
        # Only sensible gaps: 0 to ~3*height
        if -curr['h']*0.2 < gap < curr['h']*3 and gap > -5:
            # avoid negative overlap
            if gap < 0:
                gap = 0
            gaps.append(gap)
    median_gap = float(np.median(gaps)) if gaps else 0
    # If no gaps, use 0
    if not gaps:
        median_gap = 0
    # Clamp median_gap reasonable
    median_gap = float(max(0, min(median_gap, 60)))
    # Assign lineHeight per element
    for idx, e in enumerate(elements):
        # Find gap to next line in same column (next element)
        gap_next = None
        if idx < len(elements)-1:
            nxt = elements[idx+1]
            gap_next = nxt['y'] - (e['y'] + e['h'])
            if gap_next is None or gap_next < 0 or gap_next > e['h']*3:
                gap_next = median_gap
            if gap_next < 0:
                gap_next = median_gap if median_gap>0 else e['h']*0.15
        else:
            gap_next = median_gap
        # lineHeight = fontSize + gap
        # fontSize already estimated; we can compute lineHeight = bbox h + gap
        # But for PSD leading: distance between baselines = h + gap
        # CSS lineHeight = baseline distance
        # Use bbox_h + gap, with minimum 1.0 * fontSize
        base_h = e.get('h', 12)
        font_sz = e.get('fontSize', base_h * 0.9)
        # lineHeight in px
        # Prefer bbox_h + gap for visual match
        proposed = base_h + (gap_next if gap_next is not None else 0)
        # Ensure lineHeight >= fontSize * 1.0 and <= fontSize*2.0
        proposed = max(font_sz * 1.02, min(proposed, font_sz * 2.2))
        # Fallback if gap very small: use 1.15 factor
        if gaps and median_gap < 1:
            # tight lines
            proposed = max(proposed, font_sz * 1.12)
        elif not gaps:
            proposed = font_sz * 1.15
        e['lineHeight'] = float(round(proposed, 2))
        e['paragraphSpacing'] = float(round(gap_next if gap_next else median_gap, 2))
        # Tracking disabled per user request – size-only 80-100% fit
        e['letterSpacing'] = 0.0

    return elements

def enrich_typography(image, elements, image_width, image_height):
    """
    Full typography enrichment pipeline – now font-aware perfect matching.
    Order: 1) auto font family, 2) font-aware size, 3) alignment/spacing/tracking, 4) weight/style, 5) color already done
    Adds fontSize, lineHeight, letterSpacing, fontWeight, fontStyle, fontFamily, alignment, color
    """
    if not elements:
        return elements
    # First, auto font family + font-aware size per element
    for e in elements:
        try:
            # 1) match family
            fam, path, conf = match_font_family(e['text'], e['w'], e['h'])
            e['fontFamily'] = fam
            e['fontFamilyConfidence'] = float(round(conf, 3))
            e['fontPath'] = path
            # 2) size calibrated to that font
            fs = estimate_font_size(e['text'], e['w'], e['h'], font_path=path)
            e['fontSize'] = float(round(fs, 2))
        except Exception as ex:
            # fallback
            try:
                fs = estimate_font_size(e['text'], e['w'], e['h'])
            except Exception:
                fs = e['h'] * 0.92
            e['fontSize'] = float(round(fs, 2))
            e['fontFamily'] = e.get('fontFamily', 'ArialMT')
            e['fontFamilyConfidence'] = 0.0
            e['fontPath'] = SANS_FONT_PATH
        # default weight/style
        e['fontWeight'] = 400
        e['fontStyle'] = 'normal'

    # 3) Alignment + spacing + tracking (needs fontSize + fontPath)
    elements = detect_alignment_and_spacing(elements, image_width, image_height)

    # 4) Weight/style per element via image analysis (refined)
    for e in elements:
        try:
            wgt, sty = estimate_font_weight_and_style(image, [e['x'], e['y'], e['x']+e['w'], e['y']+e['h']])
            e['fontWeight'] = int(wgt)
            e['fontStyle'] = sty
        except Exception:
            pass
        # Ensure required fields
        if 'color' not in e:
            e['color'] = '#666666'
        if 'alignment' not in e:
            e['alignment'] = 'left'
        if 'letterSpacing' not in e:
            e['letterSpacing'] = 0.0
        if 'lineHeight' not in e:
            e['lineHeight'] = float(round(e['fontSize']*1.15,2))
        # cleanup internal path before JSON (keep family, drop path)
        e.pop('fontPath', None)

    return elements

def group_text_elements(results):
    """
    Group detected text elements into lines intelligently
    """
    if not results:
        return []
    
    # Extract info from EasyOCR results
    elements = []
    for (bbox, text, confidence) in results:
        if confidence < 0.15:  # Very low threshold
            continue
        
        # bbox is [[x0,y0], [x1,y1], [x2,y2], [x3,y3]]
        pts = np.array(bbox, dtype=np.float32)
        x_coords = pts[:, 0]
        y_coords = pts[:, 1]
        
        x0, x1 = min(x_coords), max(x_coords)
        y0, y1 = min(y_coords), max(y_coords)
        
        elements.append({
            'text': text.strip(),
            'x': float(x0),
            'y': float(y0),
            'w': float(x1 - x0),
            'h': float(y1 - y0),
            'confidence': float(confidence),
            'bbox': bbox
        })
    
    # Group into lines (strict grouping - no aggressive merging)
    if not elements:
        return elements
    
    # Sort by Y position first
    elements_sorted = sorted(elements, key=lambda e: e['y'])
    
    lines = []
    for elem in elements_sorted:
        merged = False
        
        for line in lines:
            # Check if within same line (strict: 0.3x of average height)
            avg_height = (elem['h'] + line.get('avg_height', elem['h'])) / 2
            y_dist = abs(elem['y'] - line['y'])
            
            if y_dist < avg_height * 0.3:  # Very strict
                # Check horizontal adjacency – allow elem on left or right, with overlap handling
                left_edge = line['x']
                right_edge = line['x'] + line['w']
                elem_left = elem['x']
                elem_right = elem['x'] + elem['w']
                gap_right = elem_left - right_edge
                gap_left = left_edge - elem_right
                # overlapping horizontally
                overlap = not (elem_right < left_edge or elem_left > right_edge)
                is_adjacent = False
                if overlap:
                    is_adjacent = True
                else:
                    # gap to closest edge
                    min_gap = min(gap_right if gap_right>=0 else 1e9, gap_left if gap_left>=0 else 1e9)
                    if min_gap < elem['h'] * 0.8:  # allow a bit more horizontal gap
                        is_adjacent = True
                if is_adjacent:
                    # update bounds correctly (min/max)
                    new_x = min(line['x'], elem['x'])
                    new_y = min(line['y'], elem['y'])
                    new_xmax = max(line['x']+line['w'], elem['x']+elem['w'])
                    new_ymax = max(line['y']+line['h'], elem['y']+elem['h'])
                    # preserve left-to-right text order
                    if elem['x'] < line['x']:
                        line['text'] = elem['text'] + ' ' + line['text']
                    else:
                        line['text'] += ' ' + elem['text']
                    line['x'] = new_x
                    line['y'] = new_y
                    line['w'] = new_xmax - new_x
                    line['h'] = new_ymax - new_y
                    line['avg_height'] = avg_height
                    merged = True
                    break
        
        if not merged:
            elem['avg_height'] = elem['h']
            lines.append(elem)
    
    # Sort by position (top to bottom, left to right)
    return sorted(lines, key=lambda e: (e['y'], e['x']))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/detect', methods=['POST'])
def detect_text():
    """
    Detect text in uploaded image. Uses the fast Tesseract engine when
    available, falling back to EasyOCR for trickier images.
    Returns perfect typography: fontSize, lineHeight, letterSpacing, alignment, weight, style, color.
    Graphics note: original image bytes are never altered for Background layer.
    """
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No image provided'}), 400
        
        file = request.files['image']
        
        # Read image - preserve original bytes untouched
        image_data = file.read()
        
        # Return cached result for identical uploads (avoids re-running OCR)
        cache_key = hashlib.md5(image_data).hexdigest()
        if cache_key in _detect_cache:
            return jsonify(_detect_cache[cache_key])
        
        nparr = np.frombuffer(image_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if image is None:
            return jsonify({'error': 'Failed to read image'}), 400
        
        # Get image dimensions
        height, width = image.shape[:2]
        
        # Clone for processing – don't mutate original `image`
        resized, scale = resize_for_ocr(image.copy())
        processed = preprocess_for_ocr(resized)
        
        # Run detection (Tesseract preferred, EasyOCR fallback) via worker pool
        try:
            raw_results, engine = _ocr_executor.submit(
                run_detection, image, processed).result(timeout=300)
        except Exception as e:
            import traceback as tb
            err_detail = tb.format_exc()
            print(f"OCR failed: {e}\n{err_detail}")
            # also log to file for debugging
            try:
                os.makedirs("logs", exist_ok=True)
                with open("logs/detection_error.log", "a", encoding="utf-8") as lf:
                    lf.write(f"\n[{width}x{height} {len(image_data)} bytes] OCR failed: {e}\n{err_detail}\n")
            except: pass
            return jsonify({'error': f'OCR failed: {str(e)}', 'detail': err_detail[:2000]}), 500
        
        # Map EasyOCR coordinates (computed on the resized image) back to the
        # original image space. Tesseract already runs on the original image.
        if engine == 'easyocr' and scale != 1.0:
            raw_results = [([[p[0] / scale, p[1] / scale] for p in bbox], text, conf)
                           for bbox, text, conf in raw_results]
        
        # Extract and group elements
        elements = group_text_elements(raw_results)
        # Discard giant bboxes that are likely false detections (cover >40% of image) – prevents huge fontSize 1700 → invisible clipped text in PS 2026
        orig_n = len(elements)
        elements = [e for e in elements if e['w']*e['h'] < width*height*0.4 and e['w'] > 2 and e['h'] > 4]
        if len(elements) != orig_n:
            print(f"Filtered giant bboxes: {orig_n} -> {len(elements)}")
        
        # Extract colors from original image (precise masked)
        for elem in elements:
            try:
                color = extract_colors_from_text(image, [elem['x'], elem['y'], elem['x'] + elem['w'], elem['y'] + elem['h']])
                elem['color'] = color
            except Exception:
                elem['color'] = '#666666'
        
        # Enrich typography: fontSize, lineHeight, alignment, weight, style, tracking
        try:
            elements = enrich_typography(image, elements, width, height)
        except Exception as e:
            print("Typography enrichment failed:", e)
            # fallback minimal typography
            for elem in elements:
                if 'fontSize' not in elem:
                    elem['fontSize'] = float(round(elem['h'] * 0.92, 2))
                if 'lineHeight' not in elem:
                    elem['lineHeight'] = float(round(elem['fontSize']*1.15,2))
                if 'letterSpacing' not in elem:
                    elem['letterSpacing'] = 0.0
                if 'fontWeight' not in elem:
                    elem['fontWeight'] = 400
                if 'fontStyle' not in elem:
                    elem['fontStyle'] = 'normal'
                if 'alignment' not in elem:
                    elem['alignment'] = 'left'
        
        # Final sort and round values for JSON cleanliness
        for e in elements:
            e['x'] = float(round(e['x'],2))
            e['y'] = float(round(e['y'],2))
            e['w'] = float(round(e['w'],2))
            e['h'] = float(round(e['h'],2))
            # remove internal bbox coords if present to keep payload clean? keep bbox for debug
            e.pop('bbox', None)
            e.pop('avg_height', None)

        response = {
            'success': True,
            'engine': engine,
            'elements': elements,
            'count': len(elements),
            'image_width': width,
            'image_height': height
        }
        
        _detect_cache[cache_key] = response
        return jsonify(response)
    
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Detect error: {e}\n{tb}")
        try:
            os.makedirs("logs", exist_ok=True)
            with open("logs/detection_error.log", "a", encoding="utf-8") as lf:
                lf.write(f"\nDetect error: {e}\n{tb}\n")
        except: pass
        return jsonify({'error': str(e), 'detail': tb[:3000]}), 500

@app.route('/api/export-psd', methods=['POST'])
def export_psd():
    """
    Export detected elements to PSD format
    Now: returns enriched manifest with perfect typography.
    If psd_tools available and client sent base64 background, can generate binary server-side.
    Frontend ag-psd generates final PSD with Background (original raster untouched) + editable text frames.
    """
    try:
        data = request.get_json()
        elements = data.get('elements', [])
        image_data = data.get('image')  # base64 encoded (original, not scaled)
        font_family = data.get('font', 'ArialMT')
        
        if image_data and ',' in image_data:
            try:
                image_bytes = base64.b64decode(image_data.split(',')[1])
                image = Image.open(io.BytesIO(image_bytes))
                w, h = image.size
            except Exception:
                w = h = 0
        else:
            w = data.get('width', 0)
            h = data.get('height', 0)
        
        # Enrich fontFamily if overridden by UI
        for el in elements:
            if font_family and font_family != 'ArialMT':
                el['fontFamily'] = font_family
            else:
                el.setdefault('fontFamily', el.get('fontFamily', 'ArialMT'))
        
        # If psd_tools is available and we have image, try to build PSD binary on server
        # (optional – frontend ag-psd is primary path)
        psd_base64 = None
        if HAS_PSDTOOLS and image_data:
            try:
                # psd_tools path would go here; for now keep JSON manifest
                # as ag-psd in browser gives richer editable text support
                pass
            except Exception as e:
                print("psd_tools generation failed:", e)

        return jsonify({
            'success': True,
            'message': 'Elements prepared for export – background untouched, text frames with perfect typography',
            'count': len(elements),
            'width': w,
            'height': h,
            'elements': elements,
            'psdBase64': psd_base64
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/correct', methods=['POST'])
def correct_text():
    """
    Groq AI text fitting: maps target text to detected boxes.
    Body: {elements: [...], targetText: "line1\nline2...", model?: "..."}
    Returns corrected elements with same geometry/color/size but text replaced via Groq.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON provided'}), 400
        elements = data.get('elements', [])
        target_text = data.get('targetText', '') or data.get('target', '')
        image_b64 = data.get('image', '') or data.get('imageBase64', '')
        model = data.get('model') or GROQ_MODEL
        # api key from .env or from request (optional, not recommended to send from frontend)
        api_key = data.get('apiKey') or GROQ_API_KEY
        if not elements:
            return jsonify({'error': 'elements is required'}), 400
        # Auto mode: no targetText provided → Groq self-correct without manual paste
        if not target_text or not target_text.strip():
            if not api_key:
                return jsonify({'error': 'targetText is required when Groq key not set (or provide image for auto)'}), 400
            # Call auto correction (vision if image provided, else text-only)
            # image_b64 may be data URI or raw base64; pass through
            corrected = correct_auto(elements, image_b64=image_b64, api_key=api_key, model=model)
            # Re-fit size after text changed (size-only 80-100%) + expand box for PS to avoid clip
            try:
                for e in corrected:
                    new_text = e.get('text','')
                    orig = next((o for o in elements if abs(o['x']-e['x'])<1 and abs(o['y']-e['y'])<1), e)
                    w = orig.get('w', e['w']); h = orig.get('h', e['h'])
                    fam = e.get('fontFamily', 'ArialMT')
                    cand_path = next((p for n,p in CANDIDATE_FONTS if n==fam), SANS_FONT_PATH)
                    fs = estimate_font_size(new_text, w, h, font_path=cand_path)
                    e['fontSize'] = float(round(fs,2))
                    e['letterSpacing'] = 0.0
                    # Expand w/h to fit new text at new size (for PS 2026 paragraph bounds)
                    try:
                        ft = ImageFont.truetype(cand_path, int(round(fs)))
                        bbox = ft.getbbox(new_text)
                        tw = bbox[2]-bbox[0]
                        th = bbox[3]-bbox[1]
                        # Keep original x/y, grow right/bottom with padding to contain tw/th
                        e['w'] = float(max(w, tw + 6))
                        e['h'] = float(max(h, th + 8))
                    except:
                        pass
            except Exception as ex:
                print(f"Re-fit after Groq auto failed: {ex}")
            # Validate Groq alignment against heuristic – keep heuristic if Groq misclassifies center/right
            try:
                W_est = max(int(e['x']+e['w']) for e in corrected) if corrected else 800
                if image_b64 and "," in image_b64:
                    try:
                        import base64 as b64m
                        from PIL import Image as PILImage
                        import io as bio
                        b64data = image_b64.split(",",1)[1]
                        im = PILImage.open(bio.BytesIO(b64m.b64decode(b64data)))
                        W_est = im.size[0]
                    except: pass
                for e in corrected:
                    heu = heuristic_alignment(e['x'], e['w'], W_est)
                    if e.get('alignment') != heu:
                        if heu == 'center' and e.get('alignment') == 'left':
                            if abs((e['x']+e['w']/2) - W_est/2) < max(15, W_est*0.03):
                                e['alignment'] = heu
                        elif heu == 'right' and e.get('alignment') == 'left':
                            if W_est - (e['x']+e['w']) < 20 and e['x'] > 40:
                                e['alignment'] = heu
            except: pass
            return jsonify({
                'success': True,
                'elements': corrected,
                'count': len(corrected),
                'groqUsed': True,
                'auto': True,
                'model': model
            })
        if not api_key:
            # fallback positional without LLM
            corrected = correct_with_groq(elements, target_text, api_key=None)
            return jsonify({
                'success': True,
                'elements': corrected,
                'count': len(corrected),
                'groqUsed': False,
                'message': 'Groq key not set – used positional fallback. Set GROQ_API_KEY in .env for AI correction.'
            })
        # call Groq with target
        corrected = correct_with_groq(elements, target_text, api_key=api_key, model=model)
        # Re-fit size after Groq (text may be longer) + expand box + validate alignment
        try:
            for e in corrected:
                new_text = e.get('text','')
                orig = next((o for o in elements if o['x']==e['x'] and o['y']==e['y']), None)
                if orig:
                    w = orig['w']; h = orig['h']
                else:
                    w = e['w']; h = e['h']
                fam = e.get('fontFamily','ArialMT')
                cand_path = next((p for n,p in CANDIDATE_FONTS if n==fam), SANS_FONT_PATH)
                fs = estimate_font_size(new_text, w, h, font_path=cand_path)
                e['fontSize'] = float(round(fs,2))
                e['letterSpacing'] = 0.0
                try:
                    ft = ImageFont.truetype(cand_path, int(round(fs)))
                    bbox = ft.getbbox(new_text)
                    tw = bbox[2]-bbox[0]
                    th = bbox[3]-bbox[1]
                    e['w'] = float(max(w, tw + 6))
                    e['h'] = float(max(h, th + 8))
                except:
                    pass
            # Validate Groq alignment vs heuristic – keep heuristic if Groq misclassifies center/right
            try:
                W_est = max(int(e['x']+e['w']) for e in corrected) if corrected else 800
                for e in corrected:
                    heu = heuristic_alignment(e['x'], e['w'], W_est)
                    if e.get('alignment') != heu:
                        if heu == 'center' and e.get('alignment') == 'left':
                            if abs((e['x']+e['w']/2) - W_est/2) < max(15, W_est*0.03):
                                e['alignment'] = heu
                        elif heu == 'right' and e.get('alignment') == 'left':
                            if W_est - (e['x']+e['w']) < 20 and e['x'] > 40:
                                e['alignment'] = heu
            except: pass
        except Exception as ex:
            print(f"Re-fit after Groq failed: {ex}")
        return jsonify({
            'success': True,
            'elements': corrected,
            'count': len(corrected),
            'groqUsed': True,
            'model': model
        })
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Correct error: {e}\n{tb}")
        return jsonify({'error': str(e), 'detail': tb[:2000]}), 500

@app.route('/api/auto-correct', methods=['POST'])
def auto_correct():
    """
    Groq auto-detect without manual paste.
    Body: {elements: [...], image: "data:image/png;base64,..." (optional for vision), model?: "..."}
    Uses Groq to self-correct OCR typos, no targetText needed.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON provided'}), 400
        elements = data.get('elements', [])
        image_b64 = data.get('image', '') or data.get('imageBase64', '')
        model = data.get('model') or GROQ_MODEL
        api_key = data.get('apiKey') or GROQ_API_KEY
        if not elements:
            return jsonify({'error': 'elements is required'}), 400
        if not api_key:
            return jsonify({'error': 'GROQ_API_KEY not set in .env – cannot auto-correct without LLM. Add key or paste targetText for positional fallback.'}), 400
        # Call auto correction (vision if image provided)
        corrected = correct_auto(elements, image_b64=image_b64, api_key=api_key, model=model)
        # Re-fit size after correction + expand box for PS (same as /api/correct)
        try:
            for e in corrected:
                new_text = e.get('text','')
                orig = next((o for o in elements if abs(o['x']-e['x'])<1 and abs(o['y']-e['y'])<1), None)
                w = orig['w'] if orig else e['w']
                h = orig['h'] if orig else e['h']
                fam = e.get('fontFamily','ArialMT')
                cand_path = next((p for n,p in CANDIDATE_FONTS if n==fam), SANS_FONT_PATH)
                fs = estimate_font_size(new_text, w, h, font_path=cand_path)
                e['fontSize'] = float(round(fs,2))
                e['letterSpacing'] = 0.0
                try:
                    ft = ImageFont.truetype(cand_path, int(round(fs)))
                    bbox = ft.getbbox(new_text)
                    tw = bbox[2]-bbox[0]
                    th = bbox[3]-bbox[1]
                    e['w'] = float(max(w, tw + 6))
                    e['h'] = float(max(h, th + 8))
                except:
                    pass
        except Exception as ex:
            print(f"Re-fit after auto failed: {ex}")
        return jsonify({
            'success': True,
            'elements': corrected,
            'count': len(corrected),
            'groqUsed': True,
            'auto': True,
            'model': model
        })
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Auto-correct error: {e}\n{tb}")
        return jsonify({'error': str(e), 'detail': tb[:2000]}), 500

@app.route('/api/detect-with-groq', methods=['POST'])
def detect_with_groq():
    """
    Combined detect + Groq fit: uploads image + targetText in same multipart request.
    Form fields: image (file), targetText (string), useGroq (bool, optional)
    """
    # Reuse detect logic but also fit target if provided
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No image provided'}), 400
        target_text = request.form.get('targetText', '') or request.form.get('target', '')
        use_groq = request.form.get('useGroq', 'false').lower() in ('1','true','yes','on')
        # First run normal detect (reuse same code path via internal call)
        # Instead of duplicating, we call the same logic as /api/detect but inline
        file = request.files['image']
        image_data = file.read()
        nparr = np.frombuffer(image_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            return jsonify({'error': 'Failed to read image'}), 400
        height, width = image.shape[:2]
        resized, scale = resize_for_ocr(image.copy())
        processed = preprocess_for_ocr(resized)
        try:
            raw_results, engine = _ocr_executor.submit(run_detection, image, processed).result(timeout=300)
        except Exception as e:
            import traceback as tb
            return jsonify({'error': f'OCR failed: {e}', 'detail': tb.format_exc()[:2000]}), 500
        if engine == 'easyocr' and scale != 1.0:
            raw_results = [([[p[0]/scale, p[1]/scale] for p in bbox], text, conf) for bbox, text, conf in raw_results]
        elements = group_text_elements(raw_results)
        for elem in elements:
            try:
                elem['color'] = extract_colors_from_text(image, [elem['x'], elem['y'], elem['x']+elem['w'], elem['y']+elem['h']])
            except: elem['color'] = '#666666'
        try:
            elements = enrich_typography(image, elements, width, height)
        except Exception as e:
            print("Typography enrichment failed:", e)
            for elem in elements:
                elem.setdefault('fontSize', float(round(elem['h']*0.92,2)))
                elem.setdefault('lineHeight', float(round(elem['fontSize']*1.15,2)))
                elem.setdefault('letterSpacing', 0.0)
                elem.setdefault('fontWeight', 400)
                elem.setdefault('fontStyle', 'normal')
                elem.setdefault('alignment', 'left')
        for e in elements:
            e['x']=float(round(e['x'],2)); e['y']=float(round(e['y'],2)); e['w']=float(round(e['w'],2)); e['h']=float(round(e['h'],2))
            e.pop('bbox',None); e.pop('avg_height',None)
        # Groq handling: manual target or auto-detect (no paste) – re-fit w/h for PS
        groq_used = False
        if use_groq and GROQ_API_KEY:
            if target_text and target_text.strip():
                try:
                    elements = correct_with_groq(elements, target_text, api_key=GROQ_API_KEY)
                    groq_used = True
                    # Re-fit after manual target (text may be longer)
                    for ec in elements:
                        try:
                            fam = ec.get('fontFamily','ArialMT')
                            cand_path = next((p for n,p in CANDIDATE_FONTS if n==fam), SANS_FONT_PATH)
                            # use enriched w/h as base, expand if needed
                            w0, h0 = ec['w'], ec['h']
                            fs = estimate_font_size(ec.get('text',''), w0, h0, font_path=cand_path)
                            ec['fontSize'] = float(round(fs,2))
                            ec['letterSpacing'] = 0.0
                            try:
                                ft = ImageFont.truetype(cand_path, int(round(fs)))
                                bbox = ft.getbbox(ec.get('text',''))
                                tw = bbox[2]-bbox[0]
                                th = bbox[3]-bbox[1]
                                ec['w'] = float(max(w0, tw + 6))
                                ec['h'] = float(max(h0, th + 8))
                            except: pass
                        except: pass
                except Exception as e:
                    print(f"Groq fit failed, returning OCR only: {e}")
            else:
                # Auto mode – no manual paste, Groq self-corrects via vision+text
                try:
                    import base64 as b64mod
                    _, buf = cv2.imencode('.png', image)
                    b64 = b64mod.b64encode(buf).decode()
                    b64_data = f"data:image/png;base64,{b64}"
                    elements = correct_auto(elements, image_b64=b64_data, api_key=GROQ_API_KEY)
                    # Re-fit size after auto text changes + expand box
                    for ec in elements:
                        try:
                            fam = ec.get('fontFamily','ArialMT')
                            cand_path = next((p for n,p in CANDIDATE_FONTS if n==fam), SANS_FONT_PATH)
                            w0, h0 = ec['w'], ec['h']
                            fs = estimate_font_size(ec.get('text',''), w0, h0, font_path=cand_path)
                            ec['fontSize'] = float(round(fs,2))
                            ec['letterSpacing'] = 0.0
                            try:
                                ft = ImageFont.truetype(cand_path, int(round(fs)))
                                bbox = ft.getbbox(ec.get('text',''))
                                tw = bbox[2]-bbox[0]
                                th = bbox[3]-bbox[1]
                                ec['w'] = float(max(w0, tw + 6))
                                ec['h'] = float(max(h0, th + 8))
                            except: pass
                        except: pass
                    # Validate alignment for Groq auto (detect-with-groq)
                    try:
                        W_est2 = width
                        for ec in elements:
                            heu2 = heuristic_alignment(ec['x'], ec['w'], W_est2)
                            if ec.get('alignment') != heu2:
                                if heu2 == 'center' and ec.get('alignment') == 'left':
                                    if abs((ec['x']+ec['w']/2) - W_est2/2) < max(15, W_est2*0.03):
                                        ec['alignment'] = heu2
                                elif heu2 == 'right' and ec.get('alignment') == 'left':
                                    if W_est2 - (ec['x']+ec['w']) < 20 and ec['x'] > 40:
                                        ec['alignment'] = heu2
                    except: pass
                    groq_used = True
                except Exception as e:
                    print(f"Groq auto failed, returning OCR only: {e}")
                    import traceback
                    traceback.print_exc()
        elif target_text and target_text.strip():
            # positional fallback when Groq not enabled but target provided
            try:
                elements = correct_with_groq(elements, target_text, api_key=None)
            except Exception as e:
                print(f"Positional fit failed: {e}")
        response = {
            'success': True,
            'engine': engine,
            'elements': elements,
            'count': len(elements),
            'image_width': width,
            'image_height': height,
            'groqUsed': groq_used
        }
        # also cache without groq? cache original detect separately
        return jsonify(response)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Detect-with-groq error: {e}\n{tb}")
        return jsonify({'error': str(e), 'detail': tb[:3000]}), 500

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'tesseract': TESSERACT_OK,
        'easyocr': HAS_EASYOCR,
        'psd_tools': HAS_PSDTOOLS,
        'sansFont': SANS_FONT_PATH,
        'groq': bool(GROQ_API_KEY),
        'groq_model': GROQ_MODEL if GROQ_API_KEY else None,
        'candidate_fonts': [n for n,_ in CANDIDATE_FONTS]
    })

if __name__ == '__main__':
    # Ensure templates directory exists
    try:
        os.makedirs(template_dir, exist_ok=True)
    except Exception:
        pass

    # Warm up the OCR model at startup
    if HAS_EASYOCR:
        try:
            get_ocr_reader()
        except Exception as e:
            print("Warning: EasyOCR failed to initialize:", e)

    import argparse, sys, threading, time
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000, help='Port to run on')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Host to bind')
    parser.add_argument('--server-only', action='store_true', help='Run server only without GUI window')
    args, _ = parser.parse_known_args()

    port = int(os.environ.get('PORT', args.port))
    host = args.host

    def start_flask():
        try:
            app.run(debug=False, host=host, port=port, use_reloader=False, threaded=True)
        except OSError as e:
            if "Address already in use" in str(e) or "WinError 10048" in str(e):
                app.run(debug=False, host=host, port=port + 1, use_reloader=False, threaded=True)

    def launch_desktop_app_window(url):
        import subprocess, shutil, webbrowser
        
        # 1. Try pywebview
        try:
            import webview
            window = webview.create_window(
                title='Image to PSD Converter',
                url=url,
                width=1366,
                height=860,
                resizable=True,
                min_size=(900, 600)
            )
            webview.start()
            return
        except Exception as e:
            print("pywebview launch failed, trying Edge App mode:", e)

        # 2. Try MS Edge --app mode (Chromeless native window)
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            shutil.which("msedge")
        ]
        for ep in edge_paths:
            if ep and os.path.exists(ep):
                user_data = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'ImageToPSD_AppProfile')
                cmd = [ep, f'--app={url}', f'--user-data-dir={user_data}', '--window-size=1366,860']
                proc = subprocess.Popen(cmd)
                proc.wait()
                return

        # 3. Try Chrome --app mode
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            shutil.which("chrome")
        ]
        for cp in chrome_paths:
            if cp and os.path.exists(cp):
                user_data = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'ImageToPSD_AppProfile')
                cmd = [cp, f'--app={url}', f'--user-data-dir={user_data}', '--window-size=1366,860']
                proc = subprocess.Popen(cmd)
                proc.wait()
                return

        # 4. Standard Browser Fallback
        webbrowser.open(url)

    if args.server_only:
        print("Starting Image to PSD Server...")
        start_flask()
    else:
        # Start server in background thread
        server_thread = threading.Thread(target=start_flask, daemon=True)
        server_thread.start()
        time.sleep(0.6)

        # Launch desktop app window
        print("Launching Image to PSD Desktop Application Window...")
        launch_desktop_app_window(f'http://127.0.0.1:{port}')
        sys.exit(0)
