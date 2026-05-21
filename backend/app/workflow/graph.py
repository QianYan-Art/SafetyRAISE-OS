from langgraph.graph import END, START, StateGraph

from app.workflow.nodes import WorkflowNodes
from app.workflow.state import WorkflowState


def build_graph(nodes: WorkflowNodes):
    graph = StateGraph(WorkflowState)
    graph.add_node("load_input", nodes.load_input_node)
    graph.add_node("validate_input", nodes.validate_input_node)
    graph.add_node("generate_guidance", nodes.generate_guidance_node)
    graph.add_node("retrieve_knowledge", nodes.retrieve_knowledge_node)
    graph.add_node("generate_report", nodes.generate_report_node)
    graph.add_node("postprocess", nodes.postprocess_node)

    graph.add_edge(START, "load_input")
    graph.add_edge("load_input", "validate_input")
    graph.add_edge("validate_input", "generate_guidance")
    graph.add_edge("generate_guidance", "retrieve_knowledge")
    graph.add_edge("retrieve_knowledge", "generate_report")
    graph.add_edge("generate_report", "postprocess")
    graph.add_edge("postprocess", END)
    return graph.compile()
