'''
Tags: Main

Converts backend coupling_map (topology) to an undirected unweighted graph for schedulers and alocators
'''

import networkx as nx

def build_graph(coupling_map, num_qubits):
    graph = nx.Graph()

    graph.add_nodes_from(range(num_qubits))
    graph.add_edges_from(coupling_map)

    return graph