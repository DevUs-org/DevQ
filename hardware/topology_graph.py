'''
Tags: Main

Converts a backend coupling_map (topology) into an undirected,
unweighted NetworkX graph. Consumed by the graph-based allocators
for BFS connectivity search and feasibility checks.
'''

import networkx as nx

def build_graph(coupling_map, num_qubits):
    graph = nx.Graph()

    graph.add_nodes_from(range(num_qubits))
    graph.add_edges_from(coupling_map)

    return graph