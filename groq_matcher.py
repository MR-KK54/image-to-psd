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
- Given detected text boxes (sorted by y,x) with {id,x,y,w,h,text,confidence,alignment,color} and a list of TARGET lines (ground truth in visual order), map each target line to the best box.
- Correct OCR typos using target as ground truth, but keep numbers/codes exact (e.g., LA-1141/44).
- Also correct alignment per box: choose left/right/center/justify based on x position vs image width (center if cx ~ W/2, right if x+w near W, else left).
- Also verify/correct color per box: look at color hex (text ink), if sampled color is wrong (e.g., white sampled as gray), fix to original ink color matching image. Keep hex #rrggbb.
- Preserve geometry/size/font – only correct text, alignment and color.
- If target has fewer lines than boxes, leave extra boxes unchanged.
- If target has more lines than boxes, ignore extras.
- Return JSON: {"corrected": [{"id":0,"correctedText":"...","alignment":"left","color":"#rrggbb"}, ...]} with same length as detected, in original id order. Alignment must be one of left, right, center, justify. Color must be #rrggbb.
- Do not invent new lines, do not change x/y/w/h/size.
- Be concise, temperature low.
"""

SYSTEM_PROMPT_AUTO = """You are an OCR auto-correction assistant for Mobile UI to PSD.

Given detected text boxes (sorted y,x) with {id,x,y,w,h,text,confidence,alignment,color}, correct obvious OCR typos, preserve numbers/codes (e.g., LA-1141/44) and keep visual order. Also verify/correct alignment per box based on x vs image width (left/center/right) and color per box based on image ink. Do NOT invent new content, do NOT change geometry/size. Keep same count, same ids, same order.
Fix: spelling, missing spaces, confusions (0/O, 1/l/I, 5/S), punctuation, alignment and color.
If text looks already correct, keep it and keep alignment/color.
Return JSON: {"corrected": [{"id":0,"correctedText":"...","alignment":"left","color":"#rrggbb"}, ...]} with same length, original id order.
"""

SYSTEM_PROMPT_VISION = """You are a vision OCR correction assistant. You see the image and the detected boxes {id,x,y,w,h,text,confidence,alignment,color}. Look at the image, read each box's text correctly, fix OCR errors, preserve numbers/codes, and determine correct alignment (left/center/right/justify) from visual position and correct color (hex #rrggbb) from original ink. Do not change geometry/size. Return JSON {"corrected": [{"id":0,"correctedText":"...","alignment":"left","color":"#rrggbb"}, ...]} same length and order. Be precise.
"""

def _build_user_payload(elements, target_lines, image_size):
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
            "color": e.get("color","#000000")
        })
    payload = {
        "image": f"{image_size[0]}x{image_size[1]}",
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
            if orig_idx in corrected_map:
                new_text = corrected_map[orig_idx]
                new_align = align_map.get(orig_idx)
                new_color = color_map.get(orig_idx)
            else:
                pos = next((si for si, (oi,_) in enumerate(indexed_sorted) if oi==orig_idx), None)
                if pos is not None and pos in corrected_map:
                    new_text = corrected_map[pos]
                    new_align = align_map.get(pos)
                    new_color = color_map.get(pos)
            new_elem = dict(elem)
            if new_text:
                new_elem["text"] = new_text
            if new_align:
                new_elem["alignment"] = new_align
            if new_color:
                new_elem["color"] = new_color
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

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if image_b64:
        if model and "vision" not in model.lower() and "scout" not in model.lower() and "maverick" not in model.lower():
            chosen_model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        else:
            chosen_model = model or os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        if not image_b64.startswith("data:"):
            image_b64 = f"data:image/png;base64,{image_b64}"
        user_content = [
            {"type": "text", "text": _build_user_payload(detected_sorted, [], image_size) + "\n\nLook at the image and correct each detected text, alignment and color to match original."},
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
                {"role": "user", "content": _build_user_payload(detected_sorted, [], image_size)}
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
                    if col and not _is_valid_hex(col):
                        col = None
                    if txt:
                        corrected_map[cid] = txt
                    if ali:
                        align_map[cid] = ali
                    if col:
                        color_map[cid] = col.lower()
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
            if pos is not None and pos in corrected_map:
                new_text = corrected_map[pos]
                new_align = align_map.get(pos)
                new_color = color_map.get(pos)
            elif orig_idx in corrected_map:
                new_text = corrected_map[orig_idx]
                new_align = align_map.get(orig_idx)
                new_color = color_map.get(orig_idx)
            new_elem = dict(elem)
            if new_text:
                new_elem["text"] = new_text
            if new_align:
                new_elem["alignment"] = new_align
            if new_color:
                new_elem["color"] = new_color
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
