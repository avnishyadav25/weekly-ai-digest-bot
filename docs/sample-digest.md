# Sample digest (dry run)

A real `--dry-run` of `weekly-ai-digest` on 2026-11-10 against the five configured feeds. Nothing was sent or saved; without `ANTHROPIC_API_KEY` there are no summaries.

```text
Would notify email:  (not configured: set SMTP_PASSWORD)
  [INFO] Weekly AI digest: 42 new from 5 sources
  ## OpenAI News (28 new)
  - Closing the AI guidance gap in education
    https://openai.com/index/closing-ai-guidance-gap-education
  - ChatGPT Enterprise now available in AWS Marketplace
    https://openai.com/index/chatgpt-enterprise-aws-marketplace
  - Control Room: A central system to track and stop model workloads across our research infrastructure
    https://openai.com/index/control-room
  - CodeRabbit scales agentic software development and ships 20x more
    https://openai.com/index/coderabbit-codex-daybreak
  - Kuken Industries searches 70 years of drawings in minutes
    https://openai.com/index/kuken
  - Partnering with Hacktron to find vulnerabilities in OpenAI’s systems
    https://openai.com/index/partnering-with-hacktron
  - Measuring how AI agents handle everyday wet-lab science
    https://openai.com/index/measuring-how-ai-agents-handle-wet-lab-science
  - Cult.fit builds operational muscle with Codex
    https://openai.com/index/cultfit-builds-with-codex
  ... and 20 more
  
  ## Google DeepMind News (2 new)
  - Gemini Omni 1.2 Flash: Fast and cost-effective with longer clips
    https://deepmind.google/blog/gemini-omni-12-flash-fast-and-cost-effective-with-longer-clips/
  - Agentic cheminformatics with Google Antigravity
    https://deepmind.google/blog/agentic-cheminformatics-with-google-antigravity/
  
  ## Google AI blog (2 new)
  - Gemini Omni 1.2 Flash: Fast and cost-effective with longer clips
    https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemini-omni-1-2-flash/
  - The latest AI news we announced in October 2026
    https://blog.google/innovation-and-ai/technology/ai/google-ai-updates-october-2026/
  
  ## Hugging Face - Blog (9 new)
  - Nemotron-Personas-Finance-Japan: A Synthetic Dataset Capturing Japan's Demographics and Financial Context
    https://huggingface.co/blog/nvidia/nemotron-personas-finance-japan
  - Your Experts Have Partners. There's HOPE for Keeping Them Together
    https://huggingface.co/blog/amazon/higher-order-pruning-experts
  - ToyBlocks: A controlled world for robot learning and evaluation
    https://huggingface.co/blog/allenai/toyblocks
  - Egyptian and Nigerian English join the Open ASR Leaderboard
    https://huggingface.co/blog/open-asr-leaderboard-liva
  - A Closer Look at Phonon TTS. Launching High-Quality On-Device Speech That Tops the Benchmarks.
    https://huggingface.co/blog/phonon-tts
  - Dharma Field Extractor: Any Document In, Structured Fields in JSON Out
    https://huggingface.co/blog/Dharma-AI/dharma-field-extractor
  - Palette: teaching a small model to build decks in your design language
    https://huggingface.co/blog/ibm-research/palette-deck-building
  - CWE-bench v2: More coverage, quality, and transparency
    https://huggingface.co/blog/cwe-bench
  ... and 1 more
  
  ## Salesforce Engineering Blog (1 new)
  - Using AI to Build AI: How Agentforce Engineers Scale Automation Without Sacrificing Reliability
    https://engineering.salesforce.com/using-ai-to-build-ai-how-agentforce-engineers-scale-automation-without-sacrificing-reliability/
  -- weekly-ai-digest · run 917ec8790beb

Would notify slack:  (not configured: set SLACK_WEBHOOK_URL)
  [INFO] Weekly AI digest: 42 new from 5 sources
  • OpenAI News (28): Closing the AI guidance gap in education
  • Google DeepMind News (2): Gemini Omni 1.2 Flash: Fast and cost-effective with longer clips
  • Google AI blog (2): Gemini Omni 1.2 Flash: Fast and cost-effective with longer clips
  • Hugging Face - Blog (9): Nemotron-Personas-Finance-Japan: A Synthetic Dataset Capturing Japan's Demographics and Financial Context
  • Salesforce Engineering Blog (1): Using AI to Build AI: How Agentforce Engineers Scale Automation Without Sacrificing Reliability
  Full digest by email.
  -- weekly-ai-digest · run 917ec8790beb

Would notify email:  (not configured: set SMTP_PASSWORD)
  [INFO] weekly-ai-digest recovered
  The run succeeded again after failing since 2026-11-10 08:40:23 UTC.
  -- weekly-ai-digest · run 917ec8790beb

Would notify console:
  [INFO] weekly-ai-digest recovered
  The run succeeded again after failing since 2026-11-10 08:40:23 UTC.
  -- weekly-ai-digest · run 917ec8790beb

Would save state:
  mark 42 key(s) as seen: https://openai.com/news/rss.xml https://openai.com/index/closing-ai-guidance-gap-education, https://openai.com/news/rss.xml https://openai.com/index/chatgpt-enterprise-aws-marketplace, https://openai.com/news/rss.xml https://openai.com/index/control-room (+39 more)

weekly-ai-digest: success in 3.12s (run 917ec8790beb): 42 new items sent
```
