"""
Groq LLM Text + Alignment + Color Corrector for perfect matching.
Maps OCR detected boxes to target text and corrects typos + alignment + color.
Uses Groq OpenAI-compatible API via requests.
"""
import os
import json
import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """You are an OCR post-correction assistant for a Mobile UI to PSD converter.

Goals:
- Given detected text boxes (sorted by y,x) with {id,x,y,w,h,text,confidence,alignment,color,fontFamily,fontWeight,fontStyle} and a list of TARGET lines (ground truth in visual order), map each target line to the best box.
- Correct OCR typos using target as ground truth, but keep numbers/codes exact (e.g., LA-1141/44).
- Also correct alignment per box STRICTLY by geometry: image width W, box center cx = x + w/2, left_dist = x, right_dist = W - (x+w). If abs(cx - W/2) < max(15, W*0.03) and abs(left_dist - right_dist) < max(15,W*0.03)*1.5 → center; else if right_dist < 20 and left_dist > 40 → right; else left. Use justify only if w > W*0.75 and text has ≥3 spaces.
- Also verify/correct color per box: look at color hex (text ink), if sampled color is wrong (e.g., white sampled as gray), fix to original ink color matching image. Keep hex #rrggbb.
- Also correct font style per box: choose fontFamily from [ArialMT,TimesNewRoman,Aptos,CourierNew,MyriadPro,Cambria,CambriaMath,SegoeUIHistoric,Symbol,Windings,Roboto,OpenSans,Ubuntu,SegoeUI,Tahoma,Verdana,Calibri,Montserrat,Poppins,Helvetica,Inter,NotoSans], fontWeight 100-900, fontStyle normal/italic based on visual style (bold vs regular, italic slant).
- Preserve geometry/size – only correct text, alignment, color and font style.
- If target has fewer lines than boxes, leave extra boxes unchanged.
- If target has more lines than boxes, ignore extras.
- Return JSON: {"corrected": [{"id":0,"correctedText":"...","alignment":"left","color":"#rrggbb","fontFamily":"ArialMT","fontWeight":400,"fontStyle":"normal"}, ...]} with same length as detected, in original id order. Alignment must be one of left, right, center, justify. Color must be #rrggbb. fontWeight 100-900, fontStyle normal/italic.
- Do not invent new lines, do not change x/y/w/h/size.
- Be concise, temperature low.
"""

SYSTEM_PROMPT_AUTO = """You are an OCR auto-correction assistant for Mobile UI to PSD.

Given detected text boxes (sorted y,x) with {id,x,y,w,h,text,confidence,alignment,color,fontFamily,fontWeight,fontStyle}, correct obvious OCR typos, preserve numbers/codes (e.g., LA-1141/44) and keep visual order. Also verify/correct alignment per box based on x vs image width (left/center/right), color per box based on image ink, and font style (family from [ArialMT,TimesNewRoman,Aptos,CourierNew,MyriadPro,Cambria,CambriaMath,SegoeUIHistoric,Symbol,Windings,Roboto,OpenSans,Ubuntu,SegoeUI,Tahoma,Verdana,Calibri,Montserrat,Poppins,Helvetica,Inter,NotoSans], weight, italic) based on visual style. Do NOT invent new content, do NOT change geometry/size. Keep same count, same ids, same order.
Fix: spelling, missing spaces, confusions (0/O, 1/l/I, 5/S), punctuation, alignment, color, font.
If text looks already correct, keep it and keep alignment/color/font.
Return JSON: {"corrected": [{"id":0,"correctedText":"...","alignment":"left","color":"#rrggbb","fontFamily":"ArialMT","fontWeight":400,"fontStyle":"normal"}, ...]} with same length, original id order.
"""

SYSTEM_PROMPT_VISION = """You are a vision OCR correction assistant. You see the image and the detected boxes {id,x,y,w,h,text,confidence,alignment,color,fontFamily,fontWeight,fontStyle}. Look at the image, read each box's text correctly, fix OCR errors, preserve numbers/codes, and determine correct alignment (left/center/right/justify) from visual position, correct color (hex #rrggbb) from original ink, and font style (family from [ArialMT,TimesNewRoman,Aptos,CourierNew,MyriadPro,Cambria,CambriaMath,SegoeUIHistoric,Symbol,Windings,Roboto,OpenSans,Ubuntu,SegoeUI,Tahoma,Verdana,Calibri,Montserrat,Poppins,Helvetica,Inter,NotoSans], weight 100-900, italic) from visual style. Do not change geometry/size. Return JSON {"corrected": [{"id":0,"correctedText":"...","alignment":"left","color":"#rrggbb","fontFamily":"ArialMT","fontWeight":400,"fontStyle":"normal"}, ...]} same length and order. Be precise.
"""

def _build_user_payload(elements, target_lines, image_size, actual_image_size=None):
    detected = []
    for idx, e in enumerate(elements):
        detected.append({
            "id": idx,
            "x": round(e["x"],1),
            "y": round(e["y"],1),
            "w": round(e["w"],1),
            "h": round(e["h"],1),
            "text": e["text"],
            "confidence": round(e.get("confidence",0),3),
            "alignment": e.get("alignment","left"),
            "color": e.get("color","#000000"),
            "fontFamily": e.get("fontFamily","ArialMT"),
            "fontWeight": e.get("fontWeight",400),
            "fontStyle": e.get("fontStyle","normal"),
            "fontSize": round(e.get("fontSize",12),1)
        })
    # Use actual image dimensions for alignment context if provided, else max extents
    W, H = image_size
    if actual_image_size:
        W, H = actual_image_size
    payload = {
        "image": f"{W}x{H}",
        "detected": detected,
        "target": target_lines
    }
    return json.dumps(payload, ensure_ascii=False)

def correct_with_groq(elements, target_text, api_key=None, model=DEFAULT_MODEL, timeout=12):
    if not elements:
        return elements
    if isinstance(target_text, str):
        target_lines = [l.strip() for l in target_text.splitlines() if l.strip() != ""]
    else:
        target_lines = [str(l).strip() for l in target_text if str(l).strip() != ""]
    if not target_lines:
        return elements

    indexed = list(enumerate(elements))
    indexed_sorted = sorted(indexed, key=lambda kv: (kv[1]["y"], kv[1]["x"]))

    api_key = api_key or os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY")
    if not api_key:
        return _fallback_positional(elements, target_lines)

    image_size = (max(int(e["x"]+e["w"]) for e in elements) if elements else 0,
                  max(int(e["y"]+e["h"]) for e in elements) if elements else 0)
    # Try to get actual image dimensions from elements' image_width/height if available via global, else use max extents
    # For now use max extents, but correct_auto will use actual image size when provided
    user_content = _build_user_payload([e for _,e in indexed_sorted], target_lines, image_size)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    }
    body["response_format"] = {"type": "json_object"}

    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=body, timeout=timeout)
        if resp.status_code != 200:
            if resp.status_code in (400, 422) and "response_format" in resp.text:
                body.pop("response_format", None)
                resp = requests.post(GROQ_API_URL, headers=headers, json=body, timeout=timeout)
            if resp.status_code != 200:
                print(f"Groq API error {resp.status_code}: {resp.text[:500]}")
                return _fallback_positional(elements, target_lines)
        data = resp.json()
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            content = data.get("choices", [{}])[0].get("text", "")
        if not content:
            print("Groq empty content", data)
            return _fallback_positional(elements, target_lines)
        try:
            parsed = json.loads(content)
        except Exception:
            import re
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
            else:
                print("Groq JSON parse failed", content[:500])
                return _fallback_positional(elements, target_lines)

        corrected_map = {}
        align_map = {}
        color_map = {}
        if isinstance(parsed, dict) and "corrected" in parsed:
            for item in parsed["corrected"]:
                try:
                    cid = int(item.get("id"))
                    txt = str(item.get("correctedText", "")).strip()
                    ali = str(item.get("alignment", "")).strip().lower()
                    col = str(item.get("color", "")).strip()
                    if ali not in ("left","right","center","justify"):
                        ali = None
                    if col and not col.startswith("#"):
                        col = "#" + col
                    # validate hex
                    if col and not _is_valid_hex(col):
                        col = None
                    if txt:
                        corrected_map[cid] = txt
                    if ali:
                        align_map[cid] = ali
                    if col:
                        color_map[cid] = col.lower()
                except: continue
        elif isinstance(parsed, list):
            for i, txt in enumerate(parsed):
                if i < len(indexed_sorted):
                    orig_idx = indexed_sorted[i][0]
                    corrected_map[orig_idx] = str(txt).strip()
        else:
            return _fallback_positional(elements, target_lines)

        corrected_elements = []
        for orig_idx, elem in enumerate(elements):
            new_text = None
            new_align = None
            new_color = None
            new_fam = None
            new_wgt = None
            new_sty = None
            if orig_idx in corrected_map:
                new_text = corrected_map[orig_idx]
                new_align = align_map.get(orig_idx)
                new_color = color_map.get(orig_idx)
                new_fam = font_map.get(orig_idx)
                new_wgt = weight_map.get(orig_idx)
                new_sty = style_map.get(orig_idx)
            else:
                pos = next((si for si, (oi,_) in enumerate(indexed_sorted) if oi==orig_idx), None)
                if pos is not None and pos in corrected_map:
                    new_text = corrected_map[pos]
                    new_align = align_map.get(pos)
                    new_color = color_map.get(pos)
                    new_fam = font_map.get(pos)
                    new_wgt = weight_map.get(pos)
                    new_sty = style_map.get(pos)
            new_elem = dict(elem)
            if new_text:
                new_elem["text"] = new_text
            # Preserve numeric alignment – Groq often flips numeric left→center incorrectly, keep original for pure numeric/codes
            if new_align and _is_numeric_text(elem.get("text","")):
                orig_align = elem.get("alignment", "left")
                if new_align != orig_align:
                    # Keep original for numeric to avoid PS reflow shift after Update
                    new_align = None
            if new_align:
                new_elem["alignment"] = new_align
            if new_color:
                new_elem["color"] = new_color
            if new_fam:
                new_elem["fontFamily"] = new_fam
            if new_wgt:
                new_elem["fontWeight"] = new_wgt
            if new_sty:
                new_elem["fontStyle"] = new_sty
            corrected_elements.append(new_elem)
        return corrected_elements

    except requests.exceptions.Timeout:
        print("Groq timeout, fallback positional")
        return _fallback_positional(elements, target_lines)
    except Exception as e:
        print(f"Groq exception {e}, fallback")
        import traceback
        traceback.print_exc()
        return _fallback_positional(elements, target_lines)

def _is_valid_hex(s):
    if not isinstance(s, str): return False
    s=s.strip()
    if not s.startswith("#"): return False
    hexpart=s[1:]
    if len(hexpart)!=6: return False
    try:
        int(hexpart,16)
        return True
    except: return False

def _is_numeric_text(text):
    """Check if text is mostly numeric/code like 525, LA-1141/44, 12:30, 100%"""
    if not text: return False
    t = text.strip()
    # Remove common separators and check if remaining is digits/letters code
    cleaned = t.replace(" ", "").replace(":", "").replace("-", "").replace("/", "").replace(".", "").replace(",", "").replace("%", "")
    # If cleaned is digits and original contains digits and length >=2, consider numeric
    if len(cleaned) >= 2 and cleaned.isdigit():
        return True
    # Also handle codes like LA-1141/44 (letters + digits + hyphen/slash) – treat as numeric/code
    if len(t) >= 3 and any(c.isdigit() for c in t) and any(c in "-/:." for c in t):
        # If contains digits and hyphen/slash/colon and mostly digits
        digit_count = sum(c.isdigit() for c in t)
        if digit_count >= 2:
            return True
    return False

def _fallback_positional(elements, target_lines):
    """Simple Y-order mapping without LLM: replace text in order, keep alignment/color."""
    indexed_sorted = sorted(list(enumerate(elements)), key=lambda kv: (kv[1]["y"], kv[1]["x"]))
    corrected = [dict(e) for e in elements]
    for (orig_idx, _), target in zip(indexed_sorted, target_lines):
        corrected[orig_idx]["text"] = target
    return corrected

def correct_auto(elements, image_b64=None, api_key=None, model=None, timeout=14):
    """Auto-detect correction without manual targetText – fixes text + alignment + color."""
    if not elements:
        return elements
    api_key = api_key or os.getenv("GROQ_API_KEY") or os.getenv("GROQ_KEY")
    if not api_key:
        print("Groq auto: no API key, returning original")
        return elements

    indexed = list(enumerate(elements))
    indexed_sorted = sorted(indexed, key=lambda kv: (kv[1]["y"], kv[1]["x"]))
    detected_sorted = [e for _, e in indexed_sorted]
    image_size = (max(int(e["x"]+e["w"]) for e in elements) if elements else 0,
                  max(int(e["y"]+e["h"]) for e in elements) if elements else 0)
    actual_size = None

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if image_b64:
        if model and "vision" not in model.lower() and "scout" not in model.lower() and "maverick" not in model.lower():
            chosen_model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        else:
            chosen_model = model or os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        if not image_b64.startswith("data:"):
            image_b64 = f"data:image/png;base64,{image_b64}"
        user_content = [
            {"type": "text", "text": _build_user_payload(detected_sorted, [], image_size, actual_size) + "\n\nLook at the image and correct each detected text, alignment and color to match original."},
            {"type": "image_url", "image_url": {"url": image_b64}}
        ]
        system_prompt = SYSTEM_PROMPT_VISION
        body = {
            "model": chosen_model,
            "temperature": 0.1,
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "response_format": {"type": "json_object"}
        }
    else:
        chosen_model = model or DEFAULT_MODEL
        body = {
            "model": chosen_model,
            "temperature": 0.1,
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_AUTO},
                {"role": "user", "content": _build_user_payload(detected_sorted, [], image_size, actual_size)}
            ],
            "response_format": {"type": "json_object"}
        }

    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=body, timeout=timeout)
        if resp.status_code != 200:
            if resp.status_code in (400, 422) and "response_format" in resp.text:
                body.pop("response_format", None)
                resp = requests.post(GROQ_API_URL, headers=headers, json=body, timeout=timeout)
            if resp.status_code != 200:
                print(f"Groq auto error {resp.status_code}: {resp.text[:500]}")
                return elements
        data = resp.json()
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            content = data.get("choices", [{}])[0].get("text", "")
        if not content:
            print("Groq auto empty", data)
            return elements
        try:
            parsed = json.loads(content)
        except Exception:
            import re
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
            else:
                print("Groq auto JSON parse failed", content[:500])
                return elements
        corrected_map = {}
        align_map = {}
        color_map = {}
        font_map = {}
        weight_map = {}
        style_map = {}
        if isinstance(parsed, dict) and "corrected" in parsed:
            for item in parsed["corrected"]:
                try:
                    cid = int(item.get("id"))
                    txt = str(item.get("correctedText", "")).strip()
                    ali = str(item.get("alignment", "")).strip().lower()
                    col = str(item.get("color", "")).strip()
                    fam = str(item.get("fontFamily", "")).strip()
                    wgt = item.get("fontWeight")
                    sty = str(item.get("fontStyle", "")).strip().lower()
                    if ali not in ("left","right","center","justify"):
                        ali = None
                    if col and not col.startswith("#"):
                        col = "#" + col
                    if col and not _is_valid_hex(col):
                        col = None
                    if fam and len(fam) > 30:
                        fam = None
                    if not fam:
                        fam = None
                    try:
                        wgt = int(wgt) if wgt is not None else None
                        if wgt not in (100,200,300,400,500,600,700,800,900):
                            wgt = min([100,200,300,400,500,600,700,800,900], key=lambda x: abs(x-wgt)) if wgt else None
                    except:
                        wgt = None
                    if sty not in ("normal","italic"):
                        sty = None
                    if txt:
                        corrected_map[cid] = txt
                    if ali:
                        align_map[cid] = ali
                    if col:
                        color_map[cid] = col.lower()
                    if fam:
                        font_map[cid] = fam
                    if wgt:
                        weight_map[cid] = wgt
                    if sty:
                        style_map[cid] = sty
                except: continue
        else:
            print("Groq auto unexpected shape", parsed)
            return elements

        corrected_elements = []
        for orig_idx, elem in enumerate(elements):
            pos = next((si for si, (oi, _) in enumerate(indexed_sorted) if oi == orig_idx), None)
            new_text = None
            new_align = None
            new_color = None
            new_fam = None
            new_wgt = None
            new_sty = None
            if pos is not None and pos in corrected_map:
                new_text = corrected_map[pos]
                new_align = align_map.get(pos)
                new_color = color_map.get(pos)
                new_fam = font_map.get(pos)
                new_wgt = weight_map.get(pos)
                new_sty = style_map.get(pos)
            elif orig_idx in corrected_map:
                new_text = corrected_map[orig_idx]
                new_align = align_map.get(orig_idx)
                new_color = color_map.get(orig_idx)
                new_fam = font_map.get(orig_idx)
                new_wgt = weight_map.get(orig_idx)
                new_sty = style_map.get(orig_idx)
            new_elem = dict(elem)
            if new_text:
                new_elem["text"] = new_text
            # Preserve numeric alignment – Groq often misclassifies numeric center/left
            if new_align and _is_numeric_text(elem.get("text","")):
                orig_align = elem.get("alignment", "left")
                if new_align != orig_align:
                    new_align = None
            if new_align:
                new_elem["alignment"] = new_align
            if new_color:
                new_elem["color"] = new_color
            if new_fam:
                new_elem["fontFamily"] = new_fam
            if new_wgt:
                new_elem["fontWeight"] = new_wgt
            if new_sty:
                new_elem["fontStyle"] = new_sty
            corrected_elements.append(new_elem)
        return corrected_elements
    except requests.exceptions.Timeout:
        print("Groq auto timeout")
        return elements
    except Exception as e:
        print(f"Groq auto exception {e}")
        import traceback
        traceback.print_exc()
        return elements
