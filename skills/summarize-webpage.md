---
name: summarize-webpage
version: 2
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
max_tokens: 2000
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
---

## Instructions

You are creating a long-term memory entry from a webpage.

Given the page title, URL, and raw content below, produce a memory file body with:
1. A **Summary** — 4-6 sentences covering the core argument, main findings, and why it matters. Include specific numbers, names, dates, and technical details where relevant.
2. **Key Points** — 5-10 bullet points, each with enough context to be self-contained. Include specific facts, figures, or examples — not just vague category labels.
3. **Entities** — named things (people, tools, concepts, companies, papers, datasets) worth remembering, with a brief description of each.
4. **Tags:** — 4-8 lowercase comma-separated tags for retrieval.

Omit navigation, ads, boilerplate. Be thorough but focused — capture what a smart person would actually want to remember six months from now. Prefer specific details over generalities.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Examples

### Example 1
**Input:**
Title: Attention Is All You Need
URL: https://arxiv.org/abs/1706.03762
Content: We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely. Experiments on two machine translation tasks show these models to be superior in quality while being more parallelizable and requiring significantly less time to train. Our model achieves 28.4 BLEU on the WMT 2014 English-to-German translation task, improving over the existing best results, including ensembles, by over 2 BLEU. On the WMT 2014 English-to-French translation task, our model establishes a new single-model state-of-the-art BLEU score of 41.0 after training for 3.5 days on eight GPUs, a small fraction of the training costs of the best models from the literature. The Transformer is the first sequence transduction model based entirely on attention, replacing the recurrent layers most commonly used in encoder-decoder architectures with multi-headed self-attention.

**Output:**
## Summary
"Attention Is All You Need" (Vaswani et al., 2017) introduces the Transformer, a sequence-to-sequence architecture that replaces recurrent and convolutional layers entirely with multi-headed self-attention. The model achieves 28.4 BLEU on WMT 2014 English-German and 41.0 BLEU on English-French, both state-of-the-art at the time, while training ~3.5 days on 8 GPUs — significantly faster than prior RNN-based models due to full parallelisation. It became the foundational architecture for all modern large language models.

**Key Points**
- Replaces recurrence (RNNs/LSTMs) and convolutions entirely with self-attention; enables full parallelisation over sequence positions during training
- Multi-head attention: runs 8 parallel attention heads, each learning different relationship patterns; outputs concatenated and projected
- Positional encoding: adds sinusoidal position signals to embeddings (no recurrence needed to represent order)
- Encoder-decoder structure: 6-layer encoder stack + 6-layer decoder stack; decoder adds cross-attention to encoder output
- 28.4 BLEU on EN→DE (WMT 2014), 41.0 BLEU on EN→FR — beat all prior single-model and ensemble results
- Training: 300k steps, 8 P100 GPUs, ~3.5 days for the big model (100k steps for the base model)
- Key insight: attention complexity is O(n²·d) vs RNN's O(n·d²) — better for long sequences when d > n

**Entities**
- Transformer: the proposed architecture, now the basis of GPT, BERT, T5, and essentially all LLMs
- Multi-head attention: core mechanism; projects Q/K/V to multiple subspaces and attends in parallel
- WMT 2014: machine translation benchmark (English→German and English→French)
- Ashish Vaswani et al.: authors at Google Brain / Google Research

**Tags:** transformers, attention-mechanism, nlp, deep-learning, machine-translation, architecture

## Evolution Log

### v2 (2026-04-28) — increased detail: 4-6 sentence summary, 5-10 key points with context, max_tokens raised to 2000 (#48)

### v1 (2026-04-11) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
