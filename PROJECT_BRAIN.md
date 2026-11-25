# PROJECT BRAIN — Iranian Shorts AI Agent (Zero-Budget, Approval-First)

## 1. Objective & Core Rules
- Goal: Produce 3 English YouTube Shorts per day (30–40 seconds each, ~85–110 words) about Iranian proverbs, short stories/fables, and cultural/historical facts.
- Budget: $0 (Free tiers, open-source tools, local/free automation).
- Approval Workflow: Generate -> Send to user for review/approval -> Upload to YouTube ONLY after explicit approval.
- Leadership & Quality Policy: Zero ambiguity. Every output must pass strict quality control (natural English, no literal/clunky translation, accurate facts, clear pronunciation, varied hooks).

## 2. Team Roles
- Leader (Gemini): Architecture, prompt generation, strict quality gate (Approve/Reject/Edit), pipeline integration, assembly specs, handoff prompts.
- Claude: Natural, punchy English scripts, storytelling, idioms/proverbs adaptation without robotic translation.
- Grok: Fresh angles, contrarian/curious hooks, avoiding clichés and repetitive openings.
- DeepSeek: Bulk topic expansion, alternative drafts, helper scripts/utilities.
- ChatGPT: Structured formatting, strict editing/polishing, Python pipeline & FFmpeg assembly scripts.

## 3. Content Formats (Rotating)
1. Proverbs: Hook/literal quirk -> Real meaning -> Modern everyday application.
2. Short Stories / Fables: Drop straight into the scene/conflict -> Twist/lesson -> Closing thought.
3. Cultural / Historical Facts: Sharp contrast or intriguing question -> Clear core explanation -> Takeaway/comment prompt.

## 4. Pronunciation & Translation Safeguards
- Separate display text (subtitles) from TTS input text (phonetic/spelling adjustments where needed).
- Maintain a growing pronunciation map for Persian names/terms (e.g., Ferdowsi, Simorgh, Yakhchal, Hafez, Caravanserai).
- Never allow repetitive hooks like "Did you know..." across consecutive videos.

## 5. Current Phase
- Phase 2 (Voice & Subtitles Engine): Build and test 02_voice_and_subs.py using edge-tts on scripts_batch_1.json.