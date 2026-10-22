# -*- coding: utf-8 -*-
'''
Compare the three entry points of an installed telegram-tracker: the
console script `tgtracker` and `python -m telegram_tracker` from a virtual
environment holding the built wheel, and `python main.py` from the source
checkout (run by the same interpreter, so it imports the checkout's src).

Every case runs from an empty working directory with an application home
that is never created and no credential variables, so none reads
credentials or reaches a client: help, argument errors, missing
credentials, offline auth reports and builder refusals. Exit codes and
output must match once the program name, times and whitespace are
normalized. Prints one line per case; exits 1 on any difference, when
the module does not import from the environment, or when a case wrote a
file.

	python entry_smoke.py VENV CHECKOUT WORK
'''

# import modules
import os
import re
import subprocess
import sys


VENV, CHECKOUT, WORK = (os.path.abspath(path) for path in sys.argv[1:4])
BIN, EXE = ('Scripts', '.exe') if os.name == 'nt' else ('bin', '')
PYTHON = os.path.join(VENV, BIN, 'python' + EXE)
FORMS = {
	'source': [PYTHON, os.path.join(CHECKOUT, 'main.py')],
	'module': [PYTHON, '-m', 'telegram_tracker'],
	'console': [os.path.join(VENV, BIN, 'tgtracker' + EXE)],
}
# longest first: 'python -m telegram_tracker' contains no other program name
PROGRAMS = ('python -m telegram_tracker', 'tgtracker', 'main.py')
TIMES = re.compile(r'(Init|End) program at .*')

CASES = [
	[], ['--help'], ['collect', '--help'], ['auth', '--help'], ['build-dataset', '--help'],
	['build-network', '--help'], ['build-urls', '--help'],
	['bogus'], ['collect'], ['collect', '--telegram-channel', '../x'],
	['collect', '--telegram-channel', 'a', '--concurrency', '11'],
	['--telegram-channel', 'a', '--update'],
	['collect', '--telegram-channel', 'a'],
	['auth', 'status'], ['auth', 'profiles'],
	['build-dataset', '-d', 'nowhere'], ['build-network', '-d', 'nowhere'], ['build-urls', '-d', 'nowhere'],
]


def environment():
	env = {
		key: value for key, value in os.environ.items()
		if key not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP')
		and not key.startswith('TELEGRAM_TRACKER_')
	}
	env['PYTHONNOUSERSITE'] = '1'
	env['TELEGRAM_TRACKER_HOME'] = os.path.join(WORK, 'apphome')
	return env


def normalized(text):
	text = TIMES.sub('', text)
	for program in PROGRAMS:
		text = text.replace(program, 'PROG')
	return ' '.join(text.split())


def run(command, argv, env):
	completed = subprocess.run(
		command + argv, cwd=WORK, env=env, capture_output=True, text=True,
		encoding='utf-8', errors='replace', timeout=300
	)
	return completed.returncode, normalized(completed.stdout), normalized(completed.stderr)


def main():
	os.makedirs(WORK, exist_ok=True)
	if os.listdir(WORK):
		sys.exit(f'{WORK} must be empty')
	env = environment()
	failures = 0

	location = subprocess.run(
		[PYTHON, '-c', 'import telegram_tracker; print(telegram_tracker.__file__)'],
		cwd=WORK, env=env, capture_output=True, text=True, timeout=300
	).stdout.strip()
	installed = os.path.normcase(location).startswith(os.path.normcase(VENV) + os.sep)
	print(f'module location: {location} ({"installed" if installed else "NOT the environment"})')
	failures += not installed

	for argv in CASES:
		results = {form: run(command, argv, env) for form, command in FORMS.items()}
		same = len(set(results.values())) == 1
		codes = ', '.join(f'{form} {result[0]}' for form, result in results.items())
		print(f'{"same" if same else "DIFFERENT"}: {argv} -> {codes}')
		if not same:
			failures += 1
			for form, (code, out, err) in results.items():
				print(f'  {form}: exit {code}\n    stdout: {out[:800]}\n    stderr: {err[:800]}')

	written = os.listdir(WORK)
	if written:
		print(f'files written: {written}')
		failures += 1
	print(f'{len(CASES)} cases, {failures} failure(s)')
	return 1 if failures else 0


if __name__ == '__main__':
	sys.exit(main())
