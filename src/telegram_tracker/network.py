# -*- coding: utf-8 -*-
'''
Offline network builder: reads the collected chats of a collection
folder (through telegram_tracker.reader: <data>/collected_chats.csv or
the database) and writes Graph.gexf, network.png and network-build.json
(the source read).

	build_network('./output/data')

Each collected chat links from every channel in its `source` list to its
`username`, except itself. The exported graph is a directed, unweighted
nx.DiGraph: repeated observations (`counter`) and duplicate rows collapse
into one edge. Louvain communities, computed on the undirected graph,
color the preview image.

Inputs are checked before anything is written; outputs are replaced
through temporary files. Plotting libraries load only when rendering.
No Telethon, config, or session access.
'''

# import modules
import ast
import json
import math
import numbers
import os
import sys

from dataclasses import dataclass

# import local submodules
from telegram_tracker.reader import AUTO, SQLITE, open_collection
from telegram_tracker.reader.records import build_record
from telegram_tracker.storage.status import utc_now
from telegram_tracker.utils import DataInputError, discard, normalize_values, temp_path_for


COLLECTED_CHATS = 'collected_chats.csv'
GRAPH_FILE = 'Graph.gexf'
IMAGE_FILE = 'network.png'
BUILD_FILE = 'network-build.json'

REQUIRED_COLUMNS = ('username', 'counter', 'source')


@dataclass
class NetworkSummary:
	'''
	Outcome of one network build.
	'''
	gexf: str
	png: str
	nodes: int
	edges: int
	source: str = None
	build_record: str = None


'''

Inputs

'''

def _is_count(value):
	'''
	A non-negative whole number (a float column from pandas is fine).
	'''
	if isinstance(value, bool) or not isinstance(value, numbers.Real):
		return False
	return math.isfinite(value) and value >= 0 and float(value).is_integer()


def read_chats(chats):
	'''
	Rows of collected_chats.csv (a reader.records.ChatsInput) as
	(username, counter, sources) tuples.
	`source` must be a Python list literal of usernames and `counter` a
	non-negative whole number.
	'''
	import pandas as pd

	chats_path = chats.label
	try:
		chats_file = pd.read_csv(chats.source, encoding='utf-8')
	except (ValueError, UnicodeDecodeError) as exc:
		raise DataInputError(f'{chats_path} is not a readable CSV: {exc}') from None

	missing = [c for c in REQUIRED_COLUMNS if c not in chats_file.columns]
	if missing:
		raise DataInputError(
			f'{chats_path} is missing columns: {", ".join(missing)}'
		)

	rows = []
	for line, (user, counter, source) in enumerate(
			zip(chats_file['username'], chats_file['counter'], chats_file['source']),
			start=2):
		try:
			sources = ast.literal_eval(source)
		except (ValueError, SyntaxError):
			sources = None
		if not isinstance(sources, list):
			raise DataInputError(
				f'{chats_path} line {line}: source is not a list literal: {source!r}'
			)

		if not _is_count(counter):
			raise DataInputError(
				f'{chats_path} line {line}: counter is not a whole number: {counter!r}'
			)

		rows.append((user, int(counter), sources))

	return rows


'''

Graph

'''

def edge_list(rows):
	'''
	Directed (source, target) pairs. A username's
	first row supplies its sources and each target repeats `counter` times
	(the DiGraph collapses repeats). Sources keep first-occurrence order.
	'''
	first_sources = {}
	for user, _, sources in rows:
		first_sources.setdefault(user, sources)

	net = {}
	for user, counter, _ in rows:
		for i in dict.fromkeys(first_sources[user]):
			if user != i:
				net.setdefault(i, []).extend([user] * counter)

	return [(k, target) for k, targets in net.items() for target in targets]


def build_graphs(edges):
	'''
	The exported DiGraph and the undirected graph used for Louvain.
	'''
	import networkx as nx

	G = nx.DiGraph()
	G.add_edges_from(edges)
	G_louvain = nx.Graph()
	G_louvain.add_edges_from(edges)
	return G, G_louvain


'''

Outputs

'''

def render(G, G_louvain, image_path):
	'''
	Save the preview image: spring layout, node size by degree, color by
	Louvain community. An empty graph produces a blank image.
	'''
	import matplotlib
	import networkx as nx

	from community import community_louvain
	from matplotlib.figure import Figure

	# a standalone Figure: no pyplot state or GUI backend
	fig = Figure(figsize=(16, 10), frameon=False)
	ax = fig.add_subplot()

	if G.number_of_nodes():
		# Community louvain -> compute the best partition
		partition = community_louvain.best_partition(G_louvain)

		# Pos -> Graph
		pos = nx.spring_layout(G)

		# Color the nodes according to their partition
		cmap = matplotlib.colormaps['viridis'].resampled(max(partition.values()) + 1)

		nodes = list(G.nodes)
		nx.draw_networkx_edges(G, pos, ax=ax, alpha=0.3)
		nx.draw_networkx_nodes(
			G,
			pos,
			nodes,
			ax=ax,
			node_size=normalize_values(
				list(dict(G.degree(nodes)).items())
			),
			cmap=cmap,
			node_color=[partition[n] for n in nodes],
			alpha=0.9
		)
		nx.draw_networkx_labels(
			G,
			pos,
			ax=ax,
			font_size=9,
			font_family='georgia',
			bbox={'facecolor':'white', 'alpha':0.5, 'edgecolor':'#373737'}
		)
	else:
		ax.set_axis_off()

	fig.savefig(image_path, format='png')


def build_network(data_path, *, image=True, source=AUTO):
	'''
	Build Graph.gexf, (unless image=False) network.png and
	network-build.json from the collected chats of `data_path`, read from
	`source` (reader.select: auto, files or sqlite). Returns a
	NetworkSummary.

	Raises DataInputError, before writing anything, when the folder
	cannot be read from that source (reader.select), the last collection
	into `data_path` is running or was interrupted, or collected_chats is
	missing, lacks username/counter/source, or has a malformed row. A
	note about a partial collection goes to stderr.
	'''
	import networkx as nx

	gexf_path = f'{data_path}/{GRAPH_FILE}'
	png_path = f'{data_path}/{IMAGE_FILE}'
	build_path = f'{data_path}/{BUILD_FILE}'

	with open_collection(data_path, source, messages=False) as collection:
		for note in collection.notes:
			print(f'> Note: {note}.', file=sys.stderr)
		if collection.source == SQLITE:
			print(f'Reading the database of {data_path}')

		print('Creating network graph')
		rows = read_chats(collection.collected_chats())

	edges = edge_list(rows)
	G, G_louvain = build_graphs(edges)
	record = build_record('build-network', collection, utc_now(), outputs={
		GRAPH_FILE: {'nodes': G.number_of_nodes(), 'edges': G.number_of_edges()},
		COLLECTED_CHATS: {'rows': len(rows)}
	})

	tmp_gexf = temp_path_for(gexf_path)
	tmp_png = temp_path_for(png_path)
	tmp_build = temp_path_for(build_path)
	try:
		nx.write_gexf(G, tmp_gexf)
		if image:
			render(G, G_louvain, tmp_png)
		with open(tmp_build, mode='w', encoding='utf-8') as writer:
			json.dump(record, writer, ensure_ascii=False, indent=1)
			writer.write('\n')

		os.replace(tmp_gexf, gexf_path)
		print('Saved')
		if image:
			os.replace(tmp_png, png_path)
		os.replace(tmp_build, build_path)
	finally:
		discard(tmp_gexf)
		discard(tmp_png)
		discard(tmp_build)

	return NetworkSummary(
		gexf=gexf_path,
		png=png_path if image else None,
		nodes=G.number_of_nodes(),
		edges=G.number_of_edges(),
		source=collection.source,
		build_record=build_path
	)


