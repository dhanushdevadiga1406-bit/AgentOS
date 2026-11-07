import re
from typing import TypedDict

from groq import AsyncGroq
from langgraph.graph import END, START, StateGraph


class WorkflowState(TypedDict, total=False):
    request: str
    plan: str
    research: str
    execution: str
    verification: str
    response: str
    approval_required: bool


def build_workflow(client: AsyncGroq, model: str):
    async def complete(system: str, prompt: str) -> str:
        result = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt[:12000]},
            ],
            temperature=0.25,
            max_tokens=1200,
        )
        content = result.choices[0].message.content
        if not content:
            raise RuntimeError("The language model returned an empty response.")
        return content.strip()

    async def planner(state: WorkflowState) -> WorkflowState:
        plan = await complete(
            "You are the Planner Agent. Break the user's request into a small, "
            "ordered plan. Do not claim to perform actions. Keep the plan concise.",
            f"User request:\n{state['request']}",
        )
        return {"plan": plan}

    async def researcher(state: WorkflowState) -> WorkflowState:
        research = await complete(
            "You are the Researcher Agent. Analyze the request and plan using your "
            "existing knowledge only. You cannot browse the web or access private "
            "systems. State uncertainty and do not invent sources.",
            f"Request:\n{state['request']}\n\nPlan:\n{state['plan']}",
        )
        return {"research": research}

    async def risk_gate(state: WorkflowState) -> WorkflowState:
        return {"approval_required": _requires_approval(state["request"])}

    async def executor(state: WorkflowState) -> WorkflowState:
        execution = await complete(
            "You are the Executor Agent. No external tools, accounts, files, or "
            "services are connected. Never claim an external action was performed. "
            "Provide a useful answer or a safe, actionable draft instead.",
            f"Request:\n{state['request']}\n\nPlan:\n{state['plan']}"
            f"\n\nAnalysis:\n{state['research']}",
        )
        return {"execution": execution}

    async def verifier(state: WorkflowState) -> WorkflowState:
        material = state.get("execution") or (
            "No action was executed. Approval is required before this sensitive "
            "request can proceed. The app has no connected external action tools."
        )
        verification = await complete(
            "You are the Verifier Agent. Check the provided draft for unsupported "
            "claims, missing caveats, and whether it falsely implies an action "
            "happened. Return brief corrections or say it is ready.",
            f"Request:\n{state['request']}\n\nPlan:\n{state['plan']}"
            f"\n\nResearch:\n{state['research']}\n\nDraft:\n{material}",
        )
        return {"verification": verification}

    async def communicator(state: WorkflowState) -> WorkflowState:
        response = await complete(
            "You are the Communicator Agent. Give the user a direct, helpful final "
            "answer. Never claim external actions were performed. If approval is "
            "required, explain that no action was taken and approval only records "
            "authorization; no external action tools are connected.",
            f"Request:\n{state['request']}\n\nPlan:\n{state['plan']}"
            f"\n\nAnalysis:\n{state['research']}\n\n"
            f"Draft:\n{state.get('execution', 'No action executed; approval required.')}"
            f"\n\nVerification:\n{state['verification']}\n\n"
            f"Approval required: {state.get('approval_required', False)}",
        )
        return {"response": response}

    graph = StateGraph(WorkflowState)
    graph.add_node("planner", planner)
    graph.add_node("researcher", researcher)
    graph.add_node("risk_gate", risk_gate)
    graph.add_node("executor", executor)
    graph.add_node("verifier", verifier)
    graph.add_node("communicator", communicator)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("researcher", "risk_gate")
    graph.add_conditional_edges(
        "risk_gate",
        lambda state: "verifier" if state["approval_required"] else "executor",
        {"executor": "executor", "verifier": "verifier"},
    )
    graph.add_edge("executor", "verifier")
    graph.add_edge("verifier", "communicator")
    graph.add_edge("communicator", END)
    return graph.compile()


def _requires_approval(request: str) -> bool:
    return bool(
        re.search(
            r"\b(send|email|text|message|post|publish|buy|purchase|transfer|pay|"
            r"delete|book|order|execute|run|submit|cancel|schedule)\b",
            request,
            flags=re.IGNORECASE,
        )
    )
