'''
ID: Main

Converts backend coupling_map (topology) to an undirected unweighted graph for schedulers and alocators
'''

from collections import defaultdict

def build_graph(coupling_map):
    graph = defaultdict(list)

    for qubit1, qubit2 in coupling_map:
        if qubit1 not in graph[qubit2]:
            graph[qubit2].append(qubit1)
        
        if qubit2 not in graph[qubit1]:
            graph[qubit1].append(qubit2)

    return graph