# graph.py — LangGraph assembly; routing reads state["next_agent"] only.
from langgraph.graph import END, StateGraph

from agents.coordinator import coordinator
from agents.critic import critic
from agents.output import output_agent
from agents.research import research
from agents.writer import writer
from state import AgentState


def route(state: dict):
    return state["next_agent"]


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("coordinator", coordinator)
    g.add_node("research", research)
    g.add_node("writer", writer)
    g.add_node("critic", critic)
    g.add_node("output", output_agent)

    g.set_entry_point("coordinator")

    for node in ["coordinator", "research", "writer", "critic"]:
        g.add_conditional_edges(
            node,
            route,
            {
                "coordinator": "coordinator",
                "research": "research",
                "writer": "writer",
                "critic": "critic",
                "output": "output",
            },
        )

    g.add_edge("output", END)
    return g.compile()


graph = build_graph()
