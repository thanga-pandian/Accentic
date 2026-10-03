# Obligation Extraction Prompt

_This file will be filled in Sprint 2._

The obligation extractor (`extract/obligations.py`) uses this prompt to ask the LLM to
identify obligations within a given `reg_section.raw_text`. The LLM returns structured
obligation data (type, entity, action, condition, deadline). Python code then verifies
every returned obligation's text exists verbatim in `raw_text` — the model never outputs
offsets.
