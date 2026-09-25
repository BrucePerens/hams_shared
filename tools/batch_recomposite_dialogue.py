"""
Batch Re-composite Speech Bubble Dialogue
Replaces stiff/fact-announcing speech bubble text in chapter illustrations with natural,
action-grounded dialogue using Windows Media OCR and Pillow.
Preserves original graphic novel artwork, character staging, and zero-pointing postures.
"""

import os
import sys
import re
import json
import argparse
import logging
from PIL import Image, ImageDraw, ImageFont

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger("batch_recomposite")

# OCR integration via Windows.Media.Ocr in PowerShell
SCRATCH_DIR = r"C:\Users\bruce\.gemini\antigravity\brain\80b1316e-857f-4c8d-93d7-c7ffe5119f20\scratch"
if SCRATCH_DIR not in sys.path:
    sys.path.append(SCRATCH_DIR)

from ocr_utils import extract_ocr_lines
from test_clean_box import cluster_lines

REPO_ROOT = r"c:\Users\ai\workspace\hams_com"
PROPOSALS_MD = os.path.join(REPO_ROOT, "docs", "speech_bubble_dialogue_proposals.md")
TMP_INGEST = r"c:\Users\ai\workspace\tmp\ham_ingestion"

def load_proposals():
    """Load original and proposed speech bubbles from speech_bubble_dialogue_proposals.md."""
    if not os.path.exists(PROPOSALS_MD):
        _logger.error(f"Proposals file not found: {PROPOSALS_MD}")
        return {}

    with open(PROPOSALS_MD, "r", encoding="utf-8") as f:
        text = f.read()

    pattern = r'### Chapter (\d+) \(Batch (\d+)\)(.*?)(?=### Chapter|\Z)'
    matches = list(re.finditer(pattern, text, re.DOTALL))
    parsed = {}
    gene_pos = text.find('## 2. General (course_HAM_GENE) Dialogue Proposals')

    for m in matches:
        batch_id = int(m.group(2))
        content = m.group(3)
        pos = m.start()
        course = 'TECH'
        if gene_pos != -1 and pos > gene_pos:
            course = 'GENE'
            
        cur_match = re.search(r'\*\*Current Speech Bubbles[^*]*\*\*:(.*?)(?=\*\*Diagnosis|\*\*Proposed|\Z)', content, re.DOTALL)
        prop_match = re.search(r'\*\*Proposed Human Dialogue\*\*:(.*?)(?=---|\Z)', content, re.DOTALL)
        
        cur_b, prop_b = [], []
        if cur_match:
            for cl in cur_match.group(1).strip().split('\n'):
                bm = re.match(r'-\s*\*\*([^*]+)\*\*:\s*["“]([^"”]+)["”]', cl.strip())
                if bm:
                    cur_b.append({'speaker': bm.group(1).strip(), 'text': bm.group(2).strip()})
        if prop_match:
            prop_lines = prop_match.group(1).strip().split('\n')
            for pl in prop_lines:
                bm = re.match(r'-\s*\*\*([^*]+)\*\*\s*\(([^)]+)\):\s*["“]([^"”]+)["”]', pl.strip())
                if bm:
                    prop_b.append({
                        'speaker': bm.group(1).strip(),
                        'type': bm.group(2).strip(),
                        'text': bm.group(3).strip()
                    })
        parsed[(course, batch_id)] = {'current': cur_b, 'proposed': prop_b}
        
    return parsed

def get_original_bubbles(course: str, batch_id: int, proposals: dict):
    """Retrieve original speech bubbles for a given chapter."""
    if course == 'GENE':
        # Check tmp/ham_ingestion/ham_pipeline_{batch_id}_visual.json first
        vis_file = os.path.join(TMP_INGEST, f"ham_pipeline_{batch_id}_visual.json")
        if os.path.exists(vis_file):
            try:
                with open(vis_file, "r", encoding="utf-8") as f:
                    vd = json.load(f)
                    if vd.get("bubbles"):
                        return vd["bubbles"]
            except Exception as e:
                _logger.warning(f"Error reading {vis_file}: {e}")
                
    item = proposals.get((course, batch_id), {})
    return item.get('current', [])

def get_target_bubbles(course: str, batch_id: int, proposals: dict):
    """Retrieve target remediated bubbles from chapter JSON or proposals."""
    ch_dir = os.path.join(REPO_ROOT, "ham_training", "data", f"course_HAM_{course}", "chapters")
    ch_file = os.path.join(ch_dir, f"{batch_id:04d}.json")
    if os.path.exists(ch_file):
        try:
            with open(ch_file, "r", encoding="utf-8") as f:
                cd = json.load(f)
                b = cd.get("visuals", {}).get("bubbles")
                if b:
                    return b
        except Exception as e:
            _logger.warning(f"Error reading {ch_file}: {e}")
            
    item = proposals.get((course, batch_id), {})
    return item.get('proposed', [])

def recomposite_chapter_image(img_path: str, orig_bubbles: list, target_bubbles: list, out_path: str = None):
    """Process a single image: detect bubbles via OCR, whiten old text, draw new text."""
    if not os.path.exists(img_path):
        return False, "Image file not found"
        
    if not target_bubbles:
        return False, "No target bubbles defined"
        
    if out_path is None:
        out_path = img_path

    # Extract OCR lines
    lines = extract_ocr_lines(img_path)
    if not lines:
        # Fallback: crop top 65% where speech bubbles live to avoid complex lower-half textures (gravel, grass, tools)
        try:
            img_temp = Image.open(img_path)
            tw, th = img_temp.size
            top_crop = img_temp.crop((0, 0, tw, int(th * 0.65)))
            tmp_crop_path = img_path + ".top_crop.jpg"
            top_crop.save(tmp_crop_path)
            lines = extract_ocr_lines(tmp_crop_path)
            if os.path.exists(tmp_crop_path):
                os.remove(tmp_crop_path)
        except Exception as e:
            _logger.warning(f"Error during top-crop fallback for {img_path}: {e}")
            
    if not lines:
        return False, "No text detected by OCR"
        
    clusters = cluster_lines(lines)
    if not clusters:
        return False, "No clusters formed from OCR lines"

    img = Image.open(img_path).convert('RGB')
    w, h = img.size
    draw = ImageDraw.Draw(img)

    font_path = "C:/Windows/Fonts/arialbd.ttf"
    if not os.path.exists(font_path):
        font_path = "C:/Windows/Fonts/arial.ttf"

    matched_count = 0

    for c in clusters:
        c_words = set(re.findall(r'\w+', c['text'].lower()))
        best_idx = -1
        best_score = 0
        
        # Match against original bubbles
        for idx, ob in enumerate(orig_bubbles):
            ob_words = set(re.findall(r'\w+', ob.get('text', '').lower()))
            score = len(c_words.intersection(ob_words))
            if score > best_score:
                best_score = score
                best_idx = idx

        # Also match against target bubbles if original text was already partially updated
        if best_score < 3:
            for idx, tb in enumerate(target_bubbles):
                tb_words = set(re.findall(r'\w+', tb.get('text', '').lower()))
                score = len(c_words.intersection(tb_words))
                if score > best_score:
                    best_score = score
                    best_idx = idx

        # If a cluster clearly belongs to a bubble
        if best_idx >= 0 and best_score >= 3 and best_idx < len(target_bubbles):
            target = target_bubbles[best_idx]
            matched_count += 1
            
            # Whiten each line in cluster + padding
            for line in c['lines']:
                lx0 = max(0, line['MinX'] - 10)
                lx1 = min(w - 1, line['MaxX'] + 10)
                ly0 = max(0, line['MinY'] - 6)
                ly1 = min(h - 1, line['MaxY'] + 6)
                draw.rectangle([lx0, ly0, lx1, ly1], fill=(255, 255, 255))
                
            # Whiten bounding box between lines
            bx0 = max(0, c['min_x'] - 12)
            bx1 = min(w - 1, c['max_x'] + 12)
            by0 = max(0, c['min_y'] - 8)
            by1 = min(h - 1, c['max_y'] + 8)
            draw.rectangle([bx0, by0, bx1, by1], fill=(255, 255, 255))
            
            avail_w = c['max_x'] - c['min_x']
            avail_h = (c['max_y'] - c['min_y']) + 16
            
            text = target.get('text', '')
            words = text.split()
            
            best_font = None
            best_lines = []
            for fsize in range(24, 11, -1):
                font = ImageFont.truetype(font_path, fsize)
                lines_wrap = []
                cur_line = []
                for word in words:
                    test_str = ' '.join(cur_line + [word])
                    bbox = font.getbbox(test_str)
                    lw = bbox[2] - bbox[0]
                    if lw <= avail_w:
                        cur_line.append(word)
                    else:
                        if cur_line:
                            lines_wrap.append(' '.join(cur_line))
                            cur_line = [word]
                        else:
                            lines_wrap.append(word)
                            cur_line = []
                if cur_line:
                    lines_wrap.append(' '.join(cur_line))
                    
                line_height = font.getbbox("Ay")[3] - font.getbbox("Ay")[1] + 4
                tot_h = len(lines_wrap) * line_height
                if tot_h <= avail_h:
                    best_font = font
                    best_lines = lines_wrap
                    break
                    
            if not best_font:
                best_font = ImageFont.truetype(font_path, 12)
                best_lines = [text]
                
            line_height = best_font.getbbox("Ay")[3] - best_font.getbbox("Ay")[1] + 4
            tot_h = len(best_lines) * line_height
            start_y = c['center_y'] - tot_h // 2
            
            for l_idx, line_txt in enumerate(best_lines):
                bbox = best_font.getbbox(line_txt)
                lw = bbox[2] - bbox[0]
                line_x = c['center_x'] - lw // 2
                line_y = start_y + l_idx * line_height
                draw.text((line_x, line_y), line_txt, fill=(0, 0, 0), font=best_font)

    if matched_count > 0:
        # Save atomically
        tmp_save = out_path + ".tmp.jpg"
        img.save(tmp_save, quality=95)
        os.replace(tmp_save, out_path)
        return True, f"Recomposited {matched_count} bubbles"
    else:
        return False, "No clusters matched bubbles with sufficient confidence"

def run_batch_recomposite(course: str, start_batch: int = 0, end_batch: int = 130):
    proposals = load_proposals()
    img_dir = os.path.join(REPO_ROOT, "ham_training", "data", f"course_HAM_{course}", "images")
    
    _logger.info(f"Starting batch recomposition for course HAM_{course} (Batches {start_batch} to {end_batch})...")
    successes, skipped, failures = 0, 0, 0

    for batch_id in range(start_batch, end_batch + 1):
        img_name = f"{batch_id:04d}.jpg"
        img_path = os.path.join(img_dir, img_name)
        if not os.path.exists(img_path):
            continue
            
        orig_b = get_original_bubbles(course, batch_id, proposals)
        target_b = get_target_bubbles(course, batch_id, proposals)
        
        if not target_b:
            skipped += 1
            continue
            
        ok, msg = recomposite_chapter_image(img_path, orig_b, target_b, img_path)
        if ok:
            successes += 1
            _logger.info(f"[{course} {img_name}] SUCCESS: {msg}")
        else:
            if "No target bubbles" in msg or "No clusters" in msg:
                skipped += 1
                _logger.info(f"[{course} {img_name}] SKIPPED: {msg}")
            else:
                failures += 1
                _logger.warning(f"[{course} {img_name}] FAILED: {msg}")
                
    _logger.info(f"Finished {course}: {successes} updated, {skipped} skipped, {failures} failed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-composite speech bubble dialogue into images")
    parser.add_argument("--course", choices=["TECH", "GENE", "ALL"], default="TECH", help="Course to process")
    parser.add_argument("--start", type=int, default=0, help="Start batch ID")
    parser.add_argument("--end", type=int, default=130, help="End batch ID")
    args = parser.parse_args()

    courses = ["TECH", "GENE"] if args.course == "ALL" else [args.course]
    for c in courses:
        run_batch_recomposite(c, args.start, args.end)
