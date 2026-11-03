# -*- coding: utf-8 -*-
'''
The release version. pyproject.toml is its only definition; the program
reads it back from the installed package metadata and records it in the
collection database and the media ledger, which never compare it. The
changelog's newest entry names it. LICENSE is the Apache License 2.0
text that the package metadata declares.

A sqlite or both folder, and its media ledger, written by 0.1.0 are
updated, checked and built by this version without a refusal or a
migration. Through cli.main with fakes (no network, no credentials,
synthetic data).
'''

# import modules
import hashlib
import os
import re
import tomllib
import unittest

from unittest import mock

import source_path

from builder_parity_support import build
from media_collect_support import ALPHA, KIB, MediaCase, photo, standard_world
from storage_modes_support import listing

from telegram_tracker.storage.sqlite import connection


OLD = '0.1.0'
BATCH = ('alpha_fx', 'beta_fx')

# SHA-256 of LICENSE with LF line endings (as git stores it): the
# Apache License 2.0 text of Esteban's OsintGPT, byte for byte.
LICENSE_SHA256 = 'bb498baaa8abad499e3e45d3ba6e70f6947ecce508bb59b54f6981eb65933edc'


def pyproject():
	with open(os.path.join(source_path.ROOT, 'pyproject.toml'), 'rb') as fh:
		return tomllib.load(fh)


def project_version():
	return pyproject()['project']['version']


def read_text(name):
	with open(os.path.join(source_path.ROOT, name), encoding='utf-8') as fh:
		return fh.read()


def as_version(version):
	'''
	app_version() as `version` would record it; other packages keep
	their installed version.
	'''
	real = connection.package_version

	def package_version(name):
		return version if name == 'telegram-tracker' else real(name)

	return mock.patch.object(connection, 'package_version', package_version)


class VersionTests(unittest.TestCase):

	def test_the_changelog_starts_with_the_project_version(self):
		version = project_version()
		entries = re.findall(r'^## \[([^\]]+)\]', read_text('CHANGELOG.md'), re.M)
		self.assertTrue(entries)
		self.assertEqual(entries[0], version)

	def test_the_readme_links_the_changelog(self):
		self.assertTrue('](CHANGELOG.md)' in read_text('README.md'), 'README.md does not link CHANGELOG.md')

	def test_the_sdist_includes_the_changelog(self):
		self.assertRegex(read_text('MANIFEST.in'), r'(?m)^include CHANGELOG\.md$')

	def test_the_recorded_version_is_the_installed_metadata(self):
		with mock.patch.object(connection.metadata, 'version', lambda name: f'{name}-v'):
			self.assertEqual(connection.app_version(), 'telegram-tracker-v')

	def test_a_source_run_without_installed_metadata_records_unknown(self):
		def missing(name):
			raise connection.metadata.PackageNotFoundError(name)

		with mock.patch.object(connection.metadata, 'version', missing):
			self.assertEqual(connection.app_version(), 'unknown')


class LicenseTests(unittest.TestCase):

	def test_the_license_file_is_the_apache_2_0_text(self):
		with open(os.path.join(source_path.ROOT, 'LICENSE'), 'rb') as fh:
			text = fh.read().replace(b'\r\n', b'\n')
		head = [line.strip() for line in text.decode('ascii').splitlines()[:3]]
		self.assertEqual(head, ['', 'Apache License', 'Version 2.0, January 2004'])
		self.assertEqual(hashlib.sha256(text).hexdigest(), LICENSE_SHA256)

	def test_the_project_declares_the_license_file(self):
		config = pyproject()
		project = config['project']
		self.assertEqual(project['license'], 'Apache-2.0')
		self.assertEqual(project['license-files'], ['LICENSE'])
		# an SPDX expression and a License classifier can't be combined
		self.assertEqual([c for c in project.get('classifiers', []) if c.startswith('License ::')], [])
		# the SPDX form needs setuptools 77 (PEP 639)
		minimums = [
			int(m.group(1)) for m in map(re.compile(r'setuptools>=(\d+)').fullmatch, config['build-system']['requires'])
			if m]
		self.assertEqual(len(minimums), 1, config['build-system']['requires'])
		self.assertGreaterEqual(minimums[0], 77)

	def test_the_readme_and_changelog_name_the_license(self):
		readme = read_text('README.md')
		self.assertIn('\n## License\n\nLicensed under the [Apache License 2.0](LICENSE).', readme)
		newest = re.split(r'(?m)^## \[', read_text('CHANGELOG.md'))[1]
		self.assertIn('Licensed under Apache-2.0', newest)


class EarlierFolderTests(MediaCase):
	'''
	A folder collected with media by 0.1.0 (the same code recording
	0.1.0), then used by this version.
	'''

	def setUp(self):
		super().setUp()
		self.new = project_version()
		self.assertNotEqual(self.new, OLD)

	def add_posts(self, client):
		files = client.files
		files.post(7, photo(files, 9002, 4 * KIB, 7), ALPHA)

	def versions(self, out):
		'''
		What records a version or would change on a migration.
		'''
		return {
			'meta': self.rows(
				'SELECT schema_version, dataset_uuid, created_at, app_version FROM schema_meta', out=out),
			'migrations': self.rows('SELECT * FROM migrations', out=out),
			'user_version': self.rows('PRAGMA user_version', out=out),
			'ledger_meta': self.ledger_rows('SELECT * FROM ledger_meta', out=out),
			'ledger_user_version': self.ledger_rows('PRAGMA user_version', out=out),
		}

	def test_an_earlier_folder_is_updated_checked_and_built_without_a_migration(self):
		for storage in ('sqlite', 'both'):
			with self.subTest(storage=storage):
				out = os.path.join(self.tmp, storage)
				client = standard_world()
				with as_version(OLD):
					code, _, err = self.media(targets=BATCH, out=out, storage=storage, client=client)
				self.assertEqual(code, 0, err)
				before = self.versions(out)
				# the folder is an 0.1.0 one
				self.assertEqual(before['meta'][0][3], OLD)
				self.assertEqual(before['ledger_meta'][0]['app_version'], OLD)
				self.assertEqual(self.rows('SELECT app_version FROM collection_runs', out=out), [(OLD,)])
				self.assertEqual([r['app_version'] for r in self.runs(out)], [OLD])
				files_before = listing(out)

				self.add_posts(client)
				with as_version(self.new):
					code, _, err = self.media('--update', targets=BATCH, out=out, client=client)
					self.assertEqual(code, 0, err)
					code, stdout, err = self.download('--check', out=out)
					self.assertEqual(code, 0, err)
					self.assertIn('> No problem found.', stdout)
					dataset, network, _, err = build(out)

				self.assertTrue(dataset, err)
				self.assertTrue(network, err)
				# nothing migrated: the identity and creation records keep 0.1.0
				self.assertEqual(self.versions(out), before)
				# the new runs record this version
				self.assertEqual(
					self.rows('SELECT app_version FROM collection_runs ORDER BY run_id', out=out),
					[(OLD,), (self.new,)])
				self.assertEqual([r['app_version'] for r in self.runs(out)], [OLD, self.new])
				self.assertEqual(self.states(out)[(ALPHA, 7)], ('complete', None))
				# no backup or other new file beside the database
				added = set(listing(out)) - set(files_before)
				self.assertEqual([f for f in added if 'sqlite3' in f or 'backup' in f], [])
				self.assertNoLocks(out)


if __name__ == '__main__':
	unittest.main()
