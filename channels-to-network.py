# -*- coding: utf-8 -*-

# import modules
import ast
import time
import pandas as pd
import networkx as nx

# import submodules
from community import community_louvain

# Plot - Graph dependencies
import matplotlib.cm as cm
import matplotlib.pyplot as plt

# log results
text = f'''
Init program at {time.ctime()}

'''
print (text)

# Read collected chats data
print ('Creating network graph')
chats_file_path = './output/collected_chats.csv'
chats_file = pd.read_csv(
	chats_file_path,
	encoding='utf-8'
)

# Create network
net = {}
source = [
	j for i in chats_file['source'].tolist()
	for j in ast.literal_eval(i)
]

# Remove duplicates
source = list(set(source))
channels = chats_file['username'].tolist()
for user in channels:
	if user not in source:
		t = chats_file[
			chats_file['username'] == user
		]['source'].iloc[0]
		t = list(set(ast.literal_eval(t)))

		for i in t:
			if i in net.keys():
				net[i].append(user)
			else:
				net[i] = [user]

# Create network data
network_data = pd.concat(
	[
		pd.DataFrame(
			{
				'source': [k] * len(net[k]),
				'target': net[k]
			}
		) for k in net.keys()
	]
)

# Create graph
G = nx.from_pandas_edgelist(
	network_data,
	create_using=nx.DiGraph()
)

# Save network data
network_path = './output/Graph.gexf'
nx.write_gexf(G, network_path)
print ('Saved')

# Community louvain -> compute the best partition
G = nx.from_pandas_edgelist(
	network_data,
	create_using=nx.Graph()
)
partition = community_louvain.best_partition(G)

# Draw graph
pos = nx.spring_layout(G)

# Color the nodes according to their partition
cmap = cm.get_cmap('viridis', max(partition.values()) + 1)
nx.draw_networkx_nodes(
	G,
	pos,
	partition.keys(),
	node_size=40,
	cmap=cmap,
	node_color=list(partition.values())
)
nx.draw_networkx_edges(G, pos, alpha=0.5)

# Show
plt.show()
