# -*- coding: utf-8 -*-
'''
Offline network builder (network.py, channels-to-network.py). Expected
nodes and directed edges come from fixtures/expected/network.json, written
from static inspection of the baseline channels-to-network.py. The image
is checked for existence and readability only (layout and community
labels are random).
'''

# import modules
import contextlib
import io
import os
import tempfile
import unittest

import offline_guard

from builder_data import collected_data, network_fixture, run_script
from fakes import load_expected

import network

from utils import EQUAL_DEGREE_NODE_SIZE, DataInputError, normalize_values


EXPECTED = load_expected('network.json')


def file_bytes(path):
	with open(path, 'rb') as fh:
		return fh.read()


class NetworkTestCase(offline_guard.OfflineTestCase):

	def setUp(self):
		super().setUp()
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = tmp.name
		self.data = os.path.join(tmp.name, 'data')
		self.gexf = os.path.join(self.data, 'Graph.gexf')
		self.png = os.path.join(self.data, 'network.png')

	def build(self, **kwargs):
		with contextlib.redirect_stdout(io.StringIO()):
			return network.build_network(self.data, **kwargs)

	def read_graph(self):
		import networkx as nx

		return nx.read_gexf(self.gexf)

	def assert_graph(self, spec):
		G = self.read_graph()
		self.assertTrue(G.is_directed())
		self.assertEqual(sorted(G.nodes), sorted(spec['nodes']))
		self.assertEqual(sorted(G.edges), sorted(tuple(e) for e in spec['edges']))
		for _, _, attrs in G.edges(data=True):
			self.assertNotIn('weight', attrs)

	def assert_readable_png(self):
		from PIL import Image

		with Image.open(self.png) as image:
			self.assertEqual(image.format, 'PNG')
			image.load()
			self.assertGreater(image.width * image.height, 0)


class BatchNetworkTests(NetworkTestCase):

	def setUp(self):
		super().setUp()
		collected_data(self.data)
		self.summary = self.build()

	def test_directed_unweighted_graph(self):
		self.assert_graph(EXPECTED['collection_batch'])
		self.assertEqual((self.summary.nodes, self.summary.edges), (6, 5))

	def test_self_sources_are_not_edges(self):
		G = self.read_graph()
		for name in EXPECTED['collection_batch']['self_sources_excluded']:
			self.assertFalse(G.has_edge(name, name))

	def test_image_is_readable(self):
		self.assert_readable_png()

	def test_outputs_and_no_temporary_files(self):
		self.assertEqual(
			sorted(f for f in os.listdir(self.data) if f in ('Graph.gexf', 'network.png') or '.tmp' in f),
			EXPECTED['collection_batch']['outputs']
		)

	def test_rebuild_is_stable(self):
		first = sorted(self.read_graph().edges)
		self.build()
		self.assertEqual(sorted(self.read_graph().edges), first)


class EdgeCaseTests(NetworkTestCase):

	def test_no_edges_writes_empty_outputs(self):
		# intended correction: the baseline crashed in pd.concat([])
		network_fixture(self.data, 'no_edges.csv')
		summary = self.build()
		self.assert_graph(EXPECTED['no_edges'])
		self.assertEqual((summary.nodes, summary.edges), (0, 0))
		self.assert_readable_png()

	def test_equal_degree_uses_constant_size(self):
		# intended correction: the baseline divided by zero
		network_fixture(self.data, 'equal_degree.csv')
		self.build()
		self.assert_graph(EXPECTED['equal_degree'])
		self.assert_readable_png()

	def test_repeated_observations_and_duplicate_rows_collapse(self):
		os.makedirs(self.data)
		with open(os.path.join(self.data, 'collected_chats.csv'), 'w', encoding='utf-8') as fh:
			fh.write(
				'id,username,counter,source\n'
				"1,b_fx,4,\"['a_fx', 'a_fx', 'c_fx']\"\n"
				"1,b_fx,2,\"['z_fx']\"\n"
			)
		self.build()
		# a username's first row supplies its sources (baseline rule)
		self.assertEqual(sorted(self.read_graph().edges), [('a_fx', 'b_fx'), ('c_fx', 'b_fx')])

	def test_graph_only(self):
		network_fixture(self.data, 'equal_degree.csv')
		summary = self.build(image=False)
		self.assertIsNone(summary.png)
		self.assertFalse(os.path.exists(self.png))
		self.assertTrue(os.path.isfile(self.gexf))


class NormalizeValuesTests(offline_guard.OfflineTestCase):

	def test_scales_between_50_and_500(self):
		self.assertEqual(normalize_values([('a', 1), ('b', 3), ('c', 2)]), [50, 500, 275])

	def test_equal_degrees_get_one_finite_size(self):
		self.assertEqual(
			normalize_values([('a', 2), ('b', 2)]),
			[EQUAL_DEGREE_NODE_SIZE, EQUAL_DEGREE_NODE_SIZE]
		)
		self.assertEqual(normalize_values([]), [])


class InvalidInputTests(NetworkTestCase):
	'''
	Malformed input fails before writing and leaves earlier outputs intact.
	'''

	def setUp(self):
		super().setUp()
		network_fixture(self.data, 'equal_degree.csv')
		self.build()
		self.before = {p: file_bytes(p) for p in (self.gexf, self.png)}

	def write_csv(self, text):
		with open(os.path.join(self.data, 'collected_chats.csv'), 'w', encoding='utf-8') as fh:
			fh.write(text)

	def assert_rejected(self, message):
		with self.assertRaises(DataInputError) as ctx:
			self.build()
		self.assertIn(message, str(ctx.exception))
		for path, content in self.before.items():
			self.assertEqual(file_bytes(path), content, path)
		self.assertEqual([f for f in os.listdir(self.data) if '.tmp' in f], [])

	def test_missing_csv(self):
		os.remove(os.path.join(self.data, 'collected_chats.csv'))
		self.assert_rejected('collected_chats.csv not found')

	def test_missing_columns(self):
		self.write_csv('id,username\n1,a_fx\n')
		self.assert_rejected('missing columns: counter, source')

	def test_malformed_source_literal(self):
		self.write_csv('id,username,counter,source\n1,a_fx,1,"[\'b_fx\'"\n')
		self.assert_rejected("line 2: source is not a list literal")

	def test_source_that_is_not_a_list(self):
		self.write_csv('id,username,counter,source\n1,a_fx,1,b_fx\n')
		self.assert_rejected("line 2: source is not a list literal")

	def test_missing_counter(self):
		# e.g. a row appended by an earlier run that this run's counter did not cover
		self.write_csv("id,username,counter,source\n1,a_fx,2,\"['b_fx']\"\n2,c_fx,,\"['b_fx']\"\n")
		self.assert_rejected('line 3: counter is not a whole number')


class LegacyScriptTests(NetworkTestCase):
	'''
	channels-to-network.py in a fresh interpreter: fully offline.
	'''

	def test_data_path_forms(self):
		collected_data(self.data)
		for argv in (['--data-path', self.data], ['-d', self.data + '/'], [f'--data-path={self.data}']):
			with self.subTest(argv=argv):
				for path in (self.gexf, self.png):
					if os.path.exists(path):
						os.remove(path)
				report, out, _ = run_script('channels-to-network.py', argv, self.tmp)
				self.assertEqual(report['code'], 0)
				self.assertEqual(report['violations'], [])
				self.assertEqual([m for m in report['modules'] if m != 'matplotlib'], [])
				self.assertIn('Saved', out)
				self.assert_graph(EXPECTED['collection_batch'])
				self.assert_readable_png()

	def test_default_data_path_is_relative_to_working_directory(self):
		cwd = os.path.join(self.tmp, 'cwd')
		network_fixture(os.path.join(cwd, 'output', 'data'), 'equal_degree.csv')
		report, _, _ = run_script('channels-to-network.py', [], cwd)
		self.assertEqual(report['code'], 0)
		self.assertTrue(os.path.isfile(os.path.join(cwd, 'output', 'data', 'Graph.gexf')))

	def test_malformed_input_exits_1_with_message(self):
		os.makedirs(self.data)
		with open(os.path.join(self.data, 'collected_chats.csv'), 'w', encoding='utf-8') as fh:
			fh.write('id,username\n1,a_fx\n')
		report, _, err = run_script('channels-to-network.py', ['-d', self.data], self.tmp)
		self.assertEqual(report['code'], 1)
		self.assertIn('> build-network:', err)
		self.assertNotIn('Traceback', err)

	def test_help_is_offline_and_skips_plotting(self):
		report, out, _ = run_script('channels-to-network.py', ['--help'], self.tmp)
		self.assertEqual(report['code'], 0)
		self.assertIn('--data-path', out)
		self.assertEqual(report['modules'], [])


if __name__ == '__main__':
	unittest.main()
