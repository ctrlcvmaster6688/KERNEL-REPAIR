"""Fault-localization half of CoHiKer++.

Layout
------
``main.py``            single entry point (one command, single case or batch)
``settings.py``        resolves every path from the repository-level ``paths.py``
``llm.py``             OpenAI-compatible client shared with the repair half
``case_list.py``       case-list normalisation (``syzid`` -> ``syzid/0/0/0/0``)
``export.py``          fl_method result -> ``file_func_result_v32.txt`` / jsonl
``root_cause.py``      per-case root-cause hypotheses -> ``rc_by_rank.jsonl``
``contrastive_reasoning/``  mutant generation + mutant reproduction (needs QEMU)
``fl_file/``           file-level localization (LLM)
``fl_method/``         candidate-function filtering (RankNet) + function localization
``evaluation/``        file / function level metrics against the oracle files
``prompt/``            prompt templates used by the stages above
"""
