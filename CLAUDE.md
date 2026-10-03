# OpsPilot: rules for this repo
- Teach while building: before each major component, explain the concept and why, in plain English. After it, list what I must be able to explain in an interview.
- Never invent metrics, datasets, benchmarks or citations. A feature works only once a test proves it.
- When something fails, explain the root cause before fixing it.
- Append decisions, errors and fixes to NOTES.md after each milestone.
- All LLM calls go through llm/client.py. Never call a provider SDK directly elsewhere.
- Keep it simple: no Kubernetes, no multi-agent, no fine-tuning, no new services without asking.
- Run tests after every change. Do not mark a step done with failing tests.

The full plan is in docs/BUILD_GUIDE.md. Follow it phase by phase.
