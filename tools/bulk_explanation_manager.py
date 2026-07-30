#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.

import os
import sys
import argparse
import json
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../hams_com/daemons")))
from hams_config import get_odoo_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bulk_explanation_manager")

def export_pending(client, output_file):
    logger.info("Exporting questions without explanations...")
    try:
        # Search for questions with missing explanations.
        # Ensure we only fetch questions that have an ncvec_code to avoid irrelevant survey questions
        questions = client.execute("survey.question", "search_read", 
            [["ncvec_code", "!=", False], "|", ["explanation", "=", False], ["explanation", "=", ""]],
            {"fields": ["id", "ncvec_code", "title", "suggested_answer_ids", "survey_id"], "limit": 10000}
        )
        
        logger.info(f"Found {len(questions)} questions needing explanations.")
        
        export_data = []
        for q in questions:
            # Fetch choices
            answers = client.execute("survey.question.answer", "search_read", 
                [["id", "in", q.get("suggested_answer_ids", [])]],
                {"fields": ["id", "value", "is_correct"]}
            )
            
            choices = {}
            correct = None
            for ans in answers:
                val = ans.get("value", "")
                if val and len(val) >= 2 and val[1] == ".":
                    letter = val[0]
                    text = val[2:].strip()
                else:
                    # Fallback if answer isn't formatted with "A. "
                    letter = str(ans.get("id"))
                    text = val
                
                choices[letter] = text
                if ans.get("is_correct"):
                    correct = letter

            q_dict = {
                "id": q["id"],
                "code": q.get("ncvec_code", ""),
                "text": q.get("title", ""),
                "correct": correct,
                "choices": choices
            }
            export_data.append(q_dict)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
            
        logger.info(f"Exported {len(export_data)} questions to {output_file}")
        
    except Exception as e:
        logger.exception(f"Failed to export: {e}")

def import_completed(client, input_file):
    logger.info(f"Importing explanations from {input_file}...")
    if not os.path.exists(input_file):
        logger.error(f"File not found: {input_file}")
        return
        
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            completed_data = json.load(f)
            
        updates = {}
        for item in completed_data:
            q_id = item.get("id")
            explanation = item.get("explanation", "").strip()
            
            if q_id and explanation:
                updates[str(q_id)] = {"explanation": explanation}
                
        if updates:
            # We can use the daemon_write_questions method or direct write
            # Since daemon_write_questions exists, let's use it or fallback to standard write
            # The custom method is daemon_write_questions on survey.question
            try:
                client.execute("survey.question", "daemon_write_questions", q_dict=updates)
                logger.info(f"Successfully updated {len(updates)} questions using bulk method.")
            except Exception as e:
                logger.warning(f"Bulk write failed, falling back to iterative write. Error: {e}")
                for str_id, vals in updates.items():
                    client.execute("survey.question", "write", [int(str_id)], vals)
                logger.info(f"Successfully updated {len(updates)} questions using iterative method.")
        else:
            logger.info("No valid explanations found to import.")
            
    except Exception as e:
        logger.exception(f"Failed to import: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk explanation manager for Odoo questions")
    parser.add_argument("--export", action="store_true", help="Export questions missing explanations")
    parser.add_argument("--import_file", type=str, help="Import JSON file containing completed explanations")
    parser.add_argument("--file", type=str, default="/home/bruce/workspace/tmp/pending_explanations.json", help="File path to use for export")
    
    args = parser.parse_args()
    
    client = get_odoo_client(logger)
    
    if args.export:
        export_pending(client, args.file)
    elif args.import_file:
        import_completed(client, args.import_file)
    else:
        parser.print_help()
