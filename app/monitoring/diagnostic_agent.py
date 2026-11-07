import json
from typing import Any, TypedDict

from groq import AsyncGroq
from langgraph.graph import END, START, StateGraph


class DiagnosticState(TypedDict, total=False):
    snapshot: dict[str, Any]
    analysis: str
    report: str


def build_diagnostic_workflow(client: AsyncGroq, model: str):
    async def analyze(state: DiagnosticState) -> DiagnosticState:
        result = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a System Diagnostic Agent. Diagnose AgentOS from the "
                        "provided real monitoring snapshot and recent error logs only. "
                        "Separate confirmed observations from possible causes. Explain "
                        "technical errors in plain language. Recommend safe, reversible "
                        "next steps. Never claim a fix was applied or verified, and never "
                        "recommend automatic system modifications. If evidence is "
                        "insufficient, say so explicitly."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(state["snapshot"], ensure_ascii=True)[:14000],
                },
            ],
            temperature=0.1,
            max_tokens=1200,
            timeout=15.0,
        )
        content = result.choices[0].message.content
        if not content:
            raise RuntimeError("The diagnostic model returned an empty report.")
        return {"analysis": content.strip()}

    async def format_report(state: DiagnosticState) -> DiagnosticState:
        report = (
            "Diagnostic report based on the monitoring snapshot collected at "
            f"{state['snapshot']['collected_at']}.\n\n{state['analysis']}\n\n"
            "Recommendations are advisory only. No corrective actions were executed."
        )
        return {"report": report}

    graph = StateGraph(DiagnosticState)
    graph.add_node("analyze", analyze)
    graph.add_node("format_report", format_report)
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "format_report")
    graph.add_edge("format_report", END)
    return graph.compile()
