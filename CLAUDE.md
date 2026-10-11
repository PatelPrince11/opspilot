# OpsPilot: rules for this repo
- Teach while building: before each major component, explain the concept and why, in plain English. After it, list what I must be able to explain in an interview.
- Never invent metrics, datasets, benchmarks or citations. A feature works only once a test proves it.
- When something fails, explain the root cause before fixing it.
- Append decisions, errors and fixes to NOTES.md after each milestone.
- All LLM calls go through llm/client.py. Never call a provider SDK directly elsewhere. (Local embedding and reranking models are not LLM calls and are exempt.)
- Keep it simple: no Kubernetes, no multi-agent, no fine-tuning, no new services without asking.
- Run tests after every change. Do not mark a step done with failing tests.
- Never pipe test, lint or gate commands through tail/head/grep unless set -o pipefail is on; a hidden failure must not reach a commit.
- Do not use the superpowers plugin's brainstorming, plan-writing, plan-executing or subagent skills. These rules override any plugin skill.

The full plan is in docs/BUILD_GUIDE.md. Follow it phase by phase.
